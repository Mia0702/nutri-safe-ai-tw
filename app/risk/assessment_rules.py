"""Claim Assessment 與 Evidence Strength 的 deterministic 規則。

LLM 只提供：
- 宣稱的語意分類（Agent A）
- 宣稱與官方資料的語意關係（Agent B 的比對步驟）

狀態判定一律由這裡的規則決定，每條規則都有 rule_id 方便追蹤與測試。
"""

from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimEvidenceRelation,
    ClaimPolicyResult,
    EvidenceScope,
    EvidenceStrength,
    InputType,
    ProductAmbiguityReason,
    ProductIdentityFlag,
    ProductMatchType,
    ProductVerificationStatus,
)
from app.normalization import base_product_name, extract_permit_code, name_match_score, normalize_name
from app.risk.claim_policy import (
    StructuredPolicyContext,
    approved_support_decision,
    classify_claim_policy,
    is_disease_claim,
)
from app.schemas.evidence import (
    OFFICIAL_ALIAS_MATCHED_BY,
    ApprovedClaimEvidence,
    ClaimAssessment,
    ClaimPolicyDecision,
    ClaimEvidenceLink,
    EvidenceBundle,
    EvidenceRelevance,
    InferredEntityLink,
    ProductCandidateMatch,
    ProductMatch,
    SourceRef,
)
from app.schemas.user_request import ExtractedClaim, NormalizedUserRequest


def inferred_link_note(link: InferredEntityLink) -> str:
    """推測關聯的固定說明文字：一定標明「推測、非官方確認」與信心值。"""
    return (
        f"系統依產品名稱推測（非官方確認）：「{link.query}」可能與申請商「{link.resolved_company_name}」有關"
        f"（信心 {link.confidence:.2f}）"
    )


VERIFIED_MIN_SCORE = 0.9
PARTIAL_MIN_SCORE = 0.5
MAX_CANDIDATES = 5
# 第二名與第一名分數差距在此範圍內：兩者都是合理候選（ambiguous），不可只挑第一名
AMBIGUITY_MARGIN = 0.05


def user_product_queries(request: NormalizedUserRequest) -> list[str]:
    """使用者實際提供的產品名稱寫法（含「品牌＋品名」組合），與 lookup_product 的查詢方式一致。"""
    queries = list(request.product_candidates)
    for name in request.product_candidates:
        for brand in request.brand_candidates:
            if normalize_name(brand) and normalize_name(brand) not in normalize_name(name):
                queries.append(f"{brand}{name}")
    return queries


def names_from_image(request: NormalizedUserRequest) -> bool:
    """產品名稱來自圖片讀取（Vision 文字有辨識誤差，是比許可證字號弱的證據）。"""
    return (request.input_type == InputType.IMAGE or request.image_identification
            or any(r.tool_name == "analyze_image" for r in request.input_tools_called))


def _matches_company_or_brand(request: NormalizedUserRequest, m: ProductCandidateMatch) -> bool:
    """候選產品的申請商 / 品名是否對應到本次看到的公司或品牌（正規化後相同或互相包含）。"""
    company = normalize_name(m.product.company_name or "")
    name = normalize_name(m.product.product_name_zh)
    for c in (normalize_name(x) for x in request.company_candidates):
        if len(c) >= 2 and company and (c in company or company in c):
            return True
    return any(len(b) >= 2 and b in name for b in (normalize_name(x) for x in request.brand_candidates))


def _image_name_flags(request: NormalizedUserRequest, official_name: str, name_score: float) -> list[ProductIdentityFlag]:
    """字號已確認產品時，圖片讀到的品名與官方品名的差異（品名只是被讀到一部分不算差異）。"""
    official = normalize_name(official_name)
    differing = [n for n in (normalize_name(x) for x in request.product_candidates)
                 if n and n not in official and official not in n]
    if not differing or name_score >= VERIFIED_MIN_SCORE:
        return []
    if name_score < PARTIAL_MIN_SCORE:
        return [ProductIdentityFlag.IMAGE_TEXT_CONFLICT, ProductIdentityFlag.OCR_UNCERTAIN]
    return [ProductIdentityFlag.OCR_UNCERTAIN]


def _unique_products(matches: list[ProductCandidateMatch]) -> list[ProductCandidateMatch]:
    best: dict[str, ProductCandidateMatch] = {}
    for m in matches:
        cur = best.get(m.product.product_id)
        if cur is None or m.match_score > cur.match_score:
            best[m.product.product_id] = m
    return sorted(best.values(), key=lambda m: m.match_score, reverse=True)


def _user_permit_matches(
    request: NormalizedUserRequest, permit_matches: list[ProductCandidateMatch]
) -> list[ProductCandidateMatch]:
    """只有「使用者提供的」許可證字號可以直接確認產品。

    planner 從工具結果抄出的字號再查一次，不是使用者給的識別資訊：若採用，
    同名的多個品項會被任意挑一個（等於猜），因此不列入。
    """
    codes = {c for c in (extract_permit_code(p) for p in request.permit_number_candidates) if c}
    return [m for m in permit_matches if extract_permit_code(m.product.permit_number or "") in codes]


def resolve_product_match(
    request: NormalizedUserRequest,
    name_matches: list[ProductCandidateMatch],
    permit_matches: list[ProductCandidateMatch],
    company_names_found: list[str],
    queried_terms: list[str],
    lookup_completed: bool,
    inferred_links: list[InferredEntityLink] | None = None,
) -> ProductMatch:
    """Deterministic product resolver：依固定順序判定 Product Verification。

    1. 使用者提供的許可證字號命中 → VERIFIED（名稱明顯不同 → CONFLICT）
       圖片讀到的品名（證據比字號弱）：只有「清楚完整的官方品名指向另一項產品」才是 CONFLICT；
       其他差異（辨識誤差）仍 VERIFIED，以 identity_flags（IMAGE_TEXT_CONFLICT / OCR_UNCERTAIN）保留差異
    2. 正規化後與官方品名 / 官方別名完全相同：唯一 → VERIFIED；多筆 → ambiguous
    3. 與官方品名去掉尾端括號規格後相同：唯一 → VERIFIED；多個品項 → ambiguous
    3b. Agent A 的錯字 / 同音修正寫法（查資料庫之前、只根據使用者輸入產生）與官方品名 / 基本品名相同的唯一產品，
       且「使用者原始寫法」的合理候選中它是第一名、沒有其他候選在 AMBIGUITY_MARGIN 內 → VERIFIED
       （兩個獨立訊號一致才確認；只有修正寫法命中、原始寫法不支持 → 不確認）
    4. 名稱相似度（一律以使用者原始寫法計分，LLM / planner 提出的名稱不能抬高分數）：
       分數接近最高分的候選有多個：其中只有一項對應到本次看到的公司 / 品牌 → 以它為準
       （分數 >= VERIFIED_MIN_SCORE → VERIFIED，否則請使用者確認這一項）；否則 ambiguous；
       唯一且 >= VERIFIED_MIN_SCORE → VERIFIED；
       其餘 >= PARTIAL_MIN_SCORE → PARTIAL_MATCH
    5. 只對應到公司 / 推測品牌 → PARTIAL_MATCH（推測不會升級為 VERIFIED）
    6. 有查詢但沒有合理候選 → NOT_FOUND；沒有產品識別資訊 → NOT_APPLICABLE

    lookup 回傳的列數不等於候選數；candidate_count 只計真正合理的候選。
    ambiguous 一律是 PARTIAL_MATCH 且不設 best_match（不猜是哪一個）。
    LOW_MATCH_CONFIDENCE（只有名稱相似、未達門檻）也不確認；資訊類問題由 Agent B 直接請使用者確認。
    """
    if not request.has_product_identifiers:
        notes = ["使用者以代名詞指稱產品，但本次輸入與對話前文都無法確認是哪一項產品"] if request.missing_product_context else []
        return ProductMatch(status=ProductVerificationStatus.NOT_APPLICABLE, notes=notes)

    def result(status, *, best=None, candidates=(), match_type=None, confidence=None, ambiguity=None,
               candidate_count=None, notes=()):
        candidates = list(candidates)
        return ProductMatch(
            status=status, best_match=best, candidates=candidates[:MAX_CANDIDATES], queried_terms=queried_terms,
            lookup_completed=lookup_completed, notes=list(notes), match_type=match_type,
            match_confidence=confidence, ambiguity_reason=ambiguity,
            candidate_count=candidate_count if candidate_count is not None else (1 if best else len(candidates)),
        )

    ranked = _unique_products(name_matches)
    user_permits = _user_permit_matches(request, permit_matches)

    # 1. 使用者提供的許可證字號
    if user_permits:
        best = user_permits[0]
        candidates = user_permits + [m for m in ranked if m.match_score >= PARTIAL_MIN_SCORE]
        if request.product_candidates and names_from_image(request) and len(
                {m.product.product_id for m in user_permits}) == 1:
            name_score = max(
                name_match_score(name, best.product.product_name_zh) for name in request.product_candidates
            )
            user_norms = {normalize_name(q) for q in user_product_queries(request)} - {""}
            rival = next((m for m in ranked if m.product.product_id != best.product.product_id and (
                normalize_name(m.product.product_name_zh) in user_norms
                or (m.matched_by == OFFICIAL_ALIAS_MATCHED_BY and normalize_name(m.query) in user_norms))), None)
            if rival is not None:  # 兩個強證據互相矛盾：字號指向 A，清楚完整的官方品名指向 B
                return result(
                    ProductVerificationStatus.CONFLICT, best=best, candidates=candidates,
                    match_type=ProductMatchType.PERMIT_NUMBER, confidence=1.0,
                    notes=[f"許可證字號對應「{best.product.product_name_zh}」，但圖片上的完整品名對應另一項官方產品"
                           f"「{rival.product.product_name_zh}」"],
                )
            flags = _image_name_flags(request, best.product.product_name_zh, name_score)
            match = result(ProductVerificationStatus.VERIFIED, best=best, candidates=candidates,
                           match_type=ProductMatchType.PERMIT_NUMBER, confidence=1.0,
                           notes=["許可證字號唯一對應官方產品；圖片文字辨識的品名與官方名稱有差異"] if flags else [])
            match.identity_flags = flags
            return match
        if request.product_candidates:
            name_score = max(
                name_match_score(name, best.product.product_name_zh) for name in request.product_candidates
            )
            if name_score < PARTIAL_MIN_SCORE:
                return result(
                    ProductVerificationStatus.CONFLICT, best=best, candidates=candidates,
                    match_type=ProductMatchType.PERMIT_NUMBER, confidence=1.0,
                    notes=["許可證字號對應的官方產品名稱，與提供的產品名稱不一致"],
                )
        return result(ProductVerificationStatus.VERIFIED, best=best, candidates=candidates,
                      match_type=ProductMatchType.PERMIT_NUMBER, confidence=1.0)

    raw_queries = user_product_queries(request)
    user_norms = {normalize_name(q) for q in raw_queries} - {""}

    # 2. 官方品名 / 官方別名完全相同（別名也必須是由使用者原始寫法命中）
    exact = [m for m in ranked
             if normalize_name(m.product.product_name_zh) in user_norms
             or (m.matched_by == OFFICIAL_ALIAS_MATCHED_BY and normalize_name(m.query) in user_norms)]
    if exact:
        match_type = (ProductMatchType.CANONICAL_EXACT
                      if normalize_name(exact[0].product.product_name_zh) in user_norms
                      else ProductMatchType.OFFICIAL_ALIAS_EXACT)
        if len(exact) == 1:
            return result(ProductVerificationStatus.VERIFIED, best=exact[0], candidates=exact,
                          match_type=match_type, confidence=1.0)
        return result(ProductVerificationStatus.PARTIAL_MATCH, candidates=exact, match_type=match_type,
                      confidence=1.0, ambiguity=ProductAmbiguityReason.MULTIPLE_EXACT_NAMES)

    # 3. 基本品名相同（官方品名尾端帶規格 / 口味括號）
    variants = [m for m in ranked if normalize_name(base_product_name(m.product.product_name_zh)) in user_norms]
    if len(variants) == 1:
        return result(ProductVerificationStatus.VERIFIED, best=variants[0], candidates=variants,
                      match_type=ProductMatchType.BASE_NAME_EXACT,
                      confidence=max(variants[0].match_score, VERIFIED_MIN_SCORE))
    if len(variants) > 1:
        return result(ProductVerificationStatus.PARTIAL_MATCH, candidates=variants,
                      match_type=ProductMatchType.BASE_NAME_EXACT, confidence=variants[0].match_score,
                      ambiguity=ProductAmbiguityReason.MULTIPLE_VARIANTS)

    # 使用者原始寫法的分數（沒有產品品名、只有公司 / 品牌時沿用工具分數）
    def raw_score(m: ProductCandidateMatch) -> float:
        if not raw_queries:
            return m.match_score
        return max(name_match_score(q, m.product.product_name_zh) for q in raw_queries)

    by_raw = sorted(((m, raw_score(m)) for m in ranked), key=lambda x: x[1], reverse=True)
    plausible = [m for m, score in by_raw if score >= PARTIAL_MIN_SCORE]

    # 3b. Agent A 修正寫法 + 使用者原始寫法佐證。
    # planner 的查詢名稱不算：planner 是看過查詢結果才寫出完整品名，不是獨立訊號（會變成拿第一名佐證自己）
    llm_norms = {normalize_name(n) for n in request.corrected_product_candidates} - user_norms - {""}
    proposed = [m for m in ranked
                if normalize_name(m.product.product_name_zh) in llm_norms
                or normalize_name(base_product_name(m.product.product_name_zh)) in llm_norms]
    if raw_queries and len(proposed) == 1 and plausible:
        target = proposed[0]
        top, top_score = by_raw[0]
        competitors = [m for m, score in by_raw[1:] if score >= max(PARTIAL_MIN_SCORE, top_score - AMBIGUITY_MARGIN)]
        if top.product.product_id == target.product.product_id and not competitors:
            return result(ProductVerificationStatus.VERIFIED, best=target, candidates=[target],
                          match_type=ProductMatchType.CORRECTED_NAME_CORROBORATED,
                          confidence=name_match_score(target.query, target.product.product_name_zh))

    # 4. 名稱相似度（使用者原始寫法）
    if plausible:
        top_score = by_raw[0][1]
        close = [m for m, score in by_raw if score >= max(PARTIAL_MIN_SCORE, top_score - AMBIGUITY_MARGIN)]
        corroborated = [m for m in close if _matches_company_or_brand(request, m)] if len(close) > 1 else []
        if len(corroborated) == 1:  # 名稱＋公司 / 品牌兩個訊號一致的唯一候選
            target, score = corroborated[0], raw_score(corroborated[0])
            if score >= VERIFIED_MIN_SCORE:
                return result(ProductVerificationStatus.VERIFIED, best=target, candidates=[target],
                              match_type=ProductMatchType.NAME_COMPANY_CORROBORATED, confidence=score)
            return result(ProductVerificationStatus.PARTIAL_MATCH, best=target, candidates=[target],
                          match_type=ProductMatchType.NAME_COMPANY_CORROBORATED, confidence=score,
                          ambiguity=ProductAmbiguityReason.LOW_MATCH_CONFIDENCE, candidate_count=1)
        if len(close) > 1:
            return result(ProductVerificationStatus.PARTIAL_MATCH, candidates=close,
                          match_type=ProductMatchType.NAME_SIMILARITY, confidence=top_score,
                          ambiguity=ProductAmbiguityReason.MULTIPLE_SIMILAR_NAMES)
        if top_score >= VERIFIED_MIN_SCORE:
            return result(ProductVerificationStatus.VERIFIED, best=plausible[0], candidates=plausible,
                          match_type=ProductMatchType.NAME_SIMILARITY, confidence=top_score)
        return result(ProductVerificationStatus.PARTIAL_MATCH, best=plausible[0], candidates=plausible,
                      match_type=ProductMatchType.NAME_SIMILARITY, confidence=top_score,
                      ambiguity=ProductAmbiguityReason.LOW_MATCH_CONFIDENCE, candidate_count=len(plausible))
    if proposed:  # 只有修正寫法命中、使用者原始寫法不支持：列為候選但不確認
        return result(ProductVerificationStatus.PARTIAL_MATCH, best=proposed[0], candidates=proposed,
                      match_type=ProductMatchType.NAME_SIMILARITY, confidence=by_raw[0][1] if by_raw else None,
                      ambiguity=ProductAmbiguityReason.LOW_MATCH_CONFIDENCE, candidate_count=len(proposed))

    # 5. 只對應到公司 / 推測品牌
    if company_names_found or inferred_links:
        notes = [f"官方紀錄中有申請商「{name}」，但無法確認是哪一項產品" for name in company_names_found[:3]]
        notes += [inferred_link_note(link) for link in (inferred_links or [])[:3]]
        return result(ProductVerificationStatus.PARTIAL_MATCH, match_type=ProductMatchType.COMPANY_ONLY, notes=notes)

    # 6. 查無
    return result(
        ProductVerificationStatus.NOT_FOUND,
        notes=[] if lookup_completed else ["產品資料查詢失敗，無法確認是否有官方紀錄"],
    )


def assess_claim(
    claim_index: int,
    claim: ExtractedClaim,
    links: list[ClaimEvidenceLink],
    approved_claim_evidence_ids: set[str],
    official_reference_ids: set[str],
    any_source_succeeded: bool,
    *,
    approved_claims: list[ApprovedClaimEvidence] | None = None,
    regulation_sources: dict[str, SourceRef] | None = None,
    product_unresolved: bool = False,
    comparison_completed: bool = True,
    structured_policy: StructuredPolicyContext | None = None,
) -> ClaimAssessment:
    """依 statement role 與固定優先順序判定單一命題的狀態。

    * 被「說出來」的宣稱（廣告、包裝、轉傳、使用者斷言）：先由 claim policy（app/risk/claim_policy.py）判定——
      明確醫療效能 → 該產品官方核准功效比對（優先於一般字面規則）→ 疾病宣稱 / 無官方支持 / 證據不足。
    * 使用者提問（USER_QUESTION）：只是待查證的命題，依官方證據回答「是否有支持」，
      醫療語意本身不會讓它變成「已被做出的醫療宣稱」。

    approved_claim_evidence_ids：已確認產品的官方核准功效證據 id（approved_claims 為其完整內容，供文字比對與說明）。
    official_reference_ids：所有已通過 Relevance Gate 的官方證據 id。
    regulation_sources：已通過 Relevance Gate 的官方法規文件，作為法規類判定的來源。
    product_unresolved：產品有多個 / 低信心候選，尚未確認。
    comparison_completed：與官方資料的語意比對步驟是否成功完成（失敗時不能把「沒有比對結果」當成「不在核准範圍」）。
    structured_policy：結構化 claim-policy 證據（官方規則 / 例示，evidence_use 原樣保留；只用於被說出來的宣稱）。
    """
    official_links = [link for link in links if link.evidence_id in official_reference_ids]
    relations = {link.relation for link in official_links}
    medical = is_disease_claim(claim)
    question = not claim.is_asserted
    approved_claims = list(approved_claims or [])

    def build(status: ClaimAssessmentStatus, rule_id: str, explanation: str,
              policy: ClaimPolicyDecision | None = None) -> ClaimAssessment:
        return ClaimAssessment(
            claim_index=claim_index,
            claim_text=claim.claim_text,
            origin=claim.origin,
            is_product_side_assertion=claim.is_product_side_assertion,
            involves_medical_efficacy=medical,
            status=status,
            evidence_links=official_links,
            rule_ids=[rule_id] + ([policy.rule_id] if policy and policy.rule_id != rule_id else []),
            explanation=explanation,
            policy=policy,
        )

    if not question:
        policy = classify_claim_policy(
            claim, official_links, approved_claims=approved_claims,
            approved_claim_evidence_ids=approved_claim_evidence_ids,
            regulation_sources=regulation_sources or {}, product_unresolved=product_unresolved,
            comparison_completed=comparison_completed, any_source_succeeded=any_source_succeeded,
            structured=structured_policy,
        )
        result = policy.policy_result
        if result in (ClaimPolicyResult.APPROVED_MATCH, ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT):
            return build(ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL, "CLAIM_MATCHES_APPROVED", policy.reason, policy)
        if result == ClaimPolicyResult.PERMITTED_GENERAL_WORDING:
            return build(ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL, "CLAIM_MATCHES_PERMITTED_WORDING",
                         policy.reason, policy)
        if result == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM:
            return build(ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM, "CLAIM_MEDICAL_EFFICACY_ASSERTED",
                         policy.reason, policy)
        if ClaimEvidenceRelation.CONTRADICTS in relations:
            return build(ClaimAssessmentStatus.CONFLICT_WITH_OFFICIAL, "CLAIM_CONTRADICTS_OFFICIAL",
                         "官方資料與這個說法相反。", policy)
        if result == ClaimPolicyResult.EXCEEDS_APPROVED_SCOPE or (
            result == ClaimPolicyResult.UNSUPPORTED_CLAIM
            and (policy.rule_id == "POLICY_EXAGGERATED_NO_APPROVED" or ClaimEvidenceRelation.EXCEEDS in relations)
        ):
            return build(ClaimAssessmentStatus.POTENTIALLY_EXAGGERATED, "CLAIM_EXAGGERATED", policy.reason, policy)
        if result == ClaimPolicyResult.INSUFFICIENT_EVIDENCE:
            rule = "CLAIM_NO_SOURCE_AVAILABLE" if policy.rule_id == "POLICY_NO_SOURCE_AVAILABLE" else "CLAIM_INSUFFICIENT_EVIDENCE"
            return build(ClaimAssessmentStatus.UNKNOWN, rule, policy.reason, policy)
        return build(ClaimAssessmentStatus.UNSUPPORTED, "CLAIM_NO_OFFICIAL_SUPPORT", policy.reason, policy)

    # ---- USER_QUESTION ----
    approved_policy = approved_support_decision(
        [l for l in official_links if l.evidence_id in approved_claim_evidence_ids], approved_claims, claim.claim_text)
    if ClaimEvidenceRelation.CONTRADICTS in relations:
        return build(ClaimAssessmentStatus.CONFLICT_WITH_OFFICIAL, "QUESTION_CONTRADICTED_BY_OFFICIAL",
                     "官方資料與這個說法相反。")
    exceeds = ClaimEvidenceRelation.EXCEEDS in relations
    supported_by_approved = approved_policy is not None or any(
        link.relation in (ClaimEvidenceRelation.SUPPORTED, ClaimEvidenceRelation.EQUIVALENT)
        and link.evidence_id in approved_claim_evidence_ids
        for link in official_links
    )
    if supported_by_approved and not medical and not exceeds:
        return build(ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL, "QUESTION_SUPPORTED_BY_APPROVED",
                     "與該產品官方核准的保健功效相符。", approved_policy)
    if not any_source_succeeded:
        return build(ClaimAssessmentStatus.UNKNOWN, "CLAIM_NO_SOURCE_AVAILABLE", "資料來源暫時無法查詢，無法判斷這個說法。")
    if medical:
        return build(ClaimAssessmentStatus.UNSUPPORTED, "QUESTION_MEDICAL_NOT_SUPPORTED",
                     "使用者詢問的是疾病相關效果；沒有官方資料支持保健食品具有這種效果。")
    return build(ClaimAssessmentStatus.UNSUPPORTED, "QUESTION_NO_OFFICIAL_SUPPORT", "目前沒有找到支持這個說法的官方資料。")


# ---------------------------------------------------------------------------
# Evidence Relevance Gate
# ---------------------------------------------------------------------------

RELEVANCE_MIN_SCORE = 0.6
GENERAL_BACKGROUND_MIN_SCORE = 0.8


def relevance_gate(relevance: EvidenceRelevance | None) -> tuple[bool, str]:
    """官方來源 ≠ 可引用證據。只有與本次產品 / 宣稱 / 問題足夠相關的才能進入 EvidenceBundle。"""
    if relevance is None:
        return False, "未完成相關性評估，不可作為證據"
    if relevance.evidence_scope == EvidenceScope.UNRELATED:
        return False, "與本次問題無關"
    if relevance.relevance_score < RELEVANCE_MIN_SCORE:
        return False, f"相關性分數 {relevance.relevance_score:.2f} 低於門檻 {RELEVANCE_MIN_SCORE}"
    if (
        relevance.evidence_scope == EvidenceScope.GENERAL_BACKGROUND
        and relevance.relevance_score < GENERAL_BACKGROUND_MIN_SCORE
    ):
        return False, "只有一般背景關聯，相關性不足"
    return True, "通過相關性門檻"


def compute_evidence_strength(bundle: EvidenceBundle) -> EvidenceStrength:
    """依證據種類與覆蓋程度計算證據強度。"""
    verified = bundle.product_match.status == ProductVerificationStatus.VERIFIED
    official_refs = (
        [e for e in bundle.approved_claims if e.source.is_official]
        + [e for e in bundle.rag_evidence if e.source.is_official]
        + [e for e in bundle.official_web_evidence if e.source.is_official]
    )
    claims = bundle.claim_assessment
    claims_covered = all(c.evidence_links for c in claims) if claims else True

    has_other_official = bool(official_refs or bundle.illegal_ad_evidence)

    # STRONG：產品已確認，且每個宣稱都有官方資料可對照
    if verified and claims_covered:
        return EvidenceStrength.STRONG
    # MEDIUM：產品已確認但宣稱缺乏對照；或產品未確認，但每個宣稱都有官方資料可對照
    if verified or (has_other_official and claims and claims_covered):
        return EvidenceStrength.MEDIUM
    # WEAK：只有片段官方資料、圖譜關聯或部分比對
    if (
        has_other_official
        or bundle.graph_evidence
        or bundle.product_match.status == ProductVerificationStatus.PARTIAL_MATCH
    ):
        return EvidenceStrength.WEAK
    return EvidenceStrength.NONE
