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
    ProductMentionProvenance,
    ProductNotFoundReason,
    ProductVerificationStatus,
)
from typing import Iterable

from app.normalization import base_product_name, extract_permit_code, name_match_score, normalize_name
from app.risk.candidate_relevance import core_text, missed_core_terms, supports_product_core
from app.risk.claim_policy import (
    StructuredPolicyContext,
    approved_function_decision,
    approved_support_decision,
    claim_risk_level,
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
# 純名稱相似（NAME_SIMILARITY）自動確認的安全條件（2026-10-04 resolver safety）：
# 第二名分數 >= SIMILARITY_RUNNER_UP_CEILING 時，第一名必須領先至少 SIMILARITY_SAFE_GAP，否則請使用者確認
SIMILARITY_RUNNER_UP_CEILING = 0.80
SIMILARITY_SAFE_GAP = 0.10


def _is_ordered_subsequence(query: str, name: str) -> bool:
    """正規化後的使用者文字可由官方品名「刪字 / 省略」得到（不允許替換字）。"""
    it = iter(name)
    return bool(query) and all(ch in it for ch in query)


def similarity_confirm_blockers(
    raw_queries: list[str], top: ProductCandidateMatch, others: list[tuple[ProductCandidateMatch, float]], top_score: float,
) -> tuple[bool, list[ProductCandidateMatch]]:
    """純名稱相似要自動確認，三條必須全部成立（wrong auto-confirm 比多一次確認更危險）：

    A. 使用者文字（正規化）是第一名官方品名的有序子序列：只有省略、沒有替換字。
    B. 沒有其他候選的官方品名也完整包含同一段使用者文字（一般品名 / 同名 / 跨類別同名）。
    C. 第二名 < SIMILARITY_RUNNER_UP_CEILING，或第一名領先 >= SIMILARITY_SAFE_GAP。
    回傳 (A 是否成立, 造成 B / C 不成立的對手候選)。只用正規化（全半形 / 空白 / 標點 / 大小寫）後的字元比較。
    """
    top_name = normalize_name(top.product.product_name_zh)
    queries = [q for q in (normalize_name(r) for r in raw_queries) if q]
    covering = [q for q in queries if _is_ordered_subsequence(q, top_name)]
    rivals = []
    for m, score in others:
        name = normalize_name(m.product.product_name_zh)
        contains_same_text = any(_is_ordered_subsequence(q, name) for q in covering)
        too_close = score >= SIMILARITY_RUNNER_UP_CEILING and top_score - score < SIMILARITY_SAFE_GAP
        if contains_same_text or too_close:
            rivals.append(m)
    return bool(covering), rivals


def scope_foreign_signal(request: NormalizedUserRequest, match: ProductMatch) -> NormalizedUserRequest:
    """外國 / 非台灣範圍只指「沒有台灣官方紀錄」的產品：resolver 已 VERIFIED 到本資料庫（台灣 TFDA 登記，
    含 imported_tablet_capsule）的產品，不因產品名稱或廠牌帶有國名而被當成外國產品。
    只依 resolver 結果判斷，不看名稱字樣；沒有確認到台灣紀錄時維持原本的外國產品處理。"""
    best = match.best_match
    if (not request.foreign_country_codes or match.status != ProductVerificationStatus.VERIFIED or best is None
            or best.product.country_code != request.country_code):
        return request
    return request.model_copy(update={"mentioned_country_codes": [
        c for c in request.mentioned_country_codes if c.upper() == request.country_code]})


def user_written_products(request: NormalizedUserRequest) -> list[str]:
    """product_candidates 中真的是使用者寫法者：Agent A 在輸入中找不到對應文字的名稱（NOT_IN_USER_TEXT）不算。
    （改了字的名稱在 Agent A 已換成使用者原文片段，修正寫法另存在 corrected_product_candidates。）"""
    inferred = {normalize_name(m.extracted_text) for m in request.product_mentions
                if m.provenance == ProductMentionProvenance.NOT_IN_USER_TEXT}
    return [n for n in request.product_candidates if normalize_name(n) not in inferred]


def user_product_queries(request: NormalizedUserRequest) -> list[str]:
    """使用者實際提供的產品名稱寫法（含「品牌＋品名」組合），與 lookup_product 的查詢方式一致。"""
    products = user_written_products(request)
    queries = list(products)
    for name in products:
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
    generic_terms: Iterable[str] = (),
) -> ProductMatch:
    """Deterministic product resolver＋Candidate Relevance Gate（見 apply_candidate_relevance_gate）。"""
    match = _resolve_product_match(request, name_matches, permit_matches, company_names_found, queried_terms,
                                   lookup_completed, inferred_links)
    return apply_candidate_relevance_gate(request, match, company_names_found, inferred_links, generic_terms)


GATED_MATCH_TYPES = (ProductMatchType.NAME_SIMILARITY, ProductMatchType.NAME_COMPANY_CORROBORATED)


def _identity_queries(request: NormalizedUserRequest, generic_terms: Iterable[str]) -> list[str]:
    """要檢查的使用者寫法：最完整的那些（品牌＋品名優先於只有品名；只給品牌 / 公司時用它們）。"""
    queries = user_product_queries(request) or list(request.brand_candidates + request.company_candidates)
    cores = {q: core_text(q, generic_terms) for q in queries}
    return [q for q in queries if not any(cores[q] != cores[o] and cores[q] in cores[o] for o in queries)]


def apply_candidate_relevance_gate(
    request: NormalizedUserRequest, match: ProductMatch, company_names_found: list[str],
    inferred_links: list[InferredEntityLink] | None, generic_terms: Iterable[str] = (),
) -> ProductMatch:
    """Product-Core Relevance Gate：「名稱相似 / 名稱＋品牌佐證」但未確認的候選，必須支持使用者寫法的產品核心。

    品牌 / 公司相同只代表可能同品牌；使用者寫了產品核心描述時，候選必須也有該核心（見 app.risk.candidate_relevance）。
    VERIFIED、字號、完全相同、官方別名、基本品名、修正寫法佐證都不經過這裡（判定與門檻完全不變）；
    這裡只會「移除」候選，不會因此確認任何產品：
    * 全部被移除 → 有公司 / 推測品牌對應時同既有 COMPANY_ONLY，否則 NOT_FOUND（NO_PRODUCT_IDENTITY_SUPPORT）
    * 多候選只剩一項 → 改為單一候選請使用者確認（LOW_MATCH_CONFIDENCE），不自動確認
    """
    if match.status != ProductVerificationStatus.PARTIAL_MATCH or match.match_type not in GATED_MATCH_TYPES:
        return match
    queries = _identity_queries(request, generic_terms)
    if not queries:
        return match
    corrected = {normalize_name(n) for n in request.corrected_product_candidates} - {""}

    def keep(m: ProductCandidateMatch) -> bool:
        name = m.product.product_name_zh
        if normalize_name(name) in corrected or normalize_name(base_product_name(name)) in corrected:
            return True  # Agent A 修正寫法與官方品名相同（既有 3b 規則的候選）
        return supports_product_core(queries, name, m.product.company_name, generic_terms)

    kept = [m for m in match.candidates if keep(m)]
    removed = len(match.candidates) - len(kept)
    if not removed:
        return match
    update: dict = {"filtered_candidate_count": removed, "candidates": kept, "candidate_count": len(kept)}
    if not kept:
        if company_names_found or inferred_links:
            notes = [f"官方紀錄中有申請商「{name}」，但無法確認是哪一項產品" for name in company_names_found[:3]]
            notes += [inferred_link_note(link) for link in (inferred_links or [])[:3]]
            return match.model_copy(update={**update, "match_type": ProductMatchType.COMPANY_ONLY, "best_match": None,
                                            "match_confidence": None, "ambiguity_reason": None, "notes": notes})
        top = match.candidates[0].product
        missing = missed_core_terms(queries[0], top.product_name_zh, top.company_name, generic_terms)[1]
        return match.model_copy(update={
            **update, "status": ProductVerificationStatus.NOT_FOUND, "best_match": None, "match_type": None,
            "match_confidence": None, "ambiguity_reason": None,
            "not_found_reason": ProductNotFoundReason.NO_PRODUCT_IDENTITY_SUPPORT,
            "unsupported_identity_terms": [t for t in missing if len(t) >= 2] or missing})
    best = match.best_match if match.best_match in kept else None
    if len(kept) == 1 or match.ambiguity_reason == ProductAmbiguityReason.LOW_MATCH_CONFIDENCE:
        update.update(best_match=best or kept[0], ambiguity_reason=ProductAmbiguityReason.LOW_MATCH_CONFIDENCE)
    else:
        update.update(best_match=best)
    return match.model_copy(update=update)


def _resolve_product_match(
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
       且「使用者原始寫法」的合理候選中它是第一名、沒有其他候選在 AMBIGUITY_MARGIN 內 → 單一候選請使用者確認
       （2026-10-04 起不自動確認：修正寫法是 Agent A 的推測，不是使用者 exact 輸入）
    exact（2 / 3）只比對使用者寫法：product_candidates 中 Agent A 改過字的名稱已換回使用者原文片段，
    輸入中找不到的推測名稱（NOT_IN_USER_TEXT）不算使用者寫法（見 user_written_products）。
    4. 名稱相似度（一律以使用者原始寫法計分，LLM / planner 提出的名稱不能抬高分數）：
       分數接近最高分的候選有多個：其中只有一項對應到本次看到的公司 / 品牌 → 以它為準
       （分數 >= VERIFIED_MIN_SCORE → VERIFIED，否則請使用者確認這一項）；否則 ambiguous；
       唯一且 >= VERIFIED_MIN_SCORE，且通過 similarity_confirm_blockers（只有省略字、沒有其他品名也包含同一段文字、
       第二名 < 0.80 或領先 >= 0.10）→ VERIFIED；有對手 → 候選確認；只有替換字 → 單一候選確認；
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

    # 1. 使用者提供的許可證字號：唯一對應一筆官方紀錄才可直接確認；對應多筆（例如不同資料類別）→ 請使用者選，不猜
    if len({m.product.product_id for m in user_permits}) > 1:
        return result(ProductVerificationStatus.PARTIAL_MATCH, candidates=user_permits,
                      match_type=ProductMatchType.PERMIT_NUMBER, confidence=1.0,
                      ambiguity=ProductAmbiguityReason.MULTIPLE_REGISTRATION_RECORDS)
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
            # 只有公司 / 品牌時沿用工具分數；產品名稱全是 Agent A 推測（不在輸入中）時，名稱相似度不能當證據
            return 0.0 if request.product_candidates else m.match_score
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
            # 修正寫法只是 Agent A 的推測：與使用者寫法一致也只列為單一候選請使用者確認（2026-10-04 經使用者同意，
            # 不再自動確認），不把改字後的名稱當成使用者 exact 輸入
            return result(ProductVerificationStatus.PARTIAL_MATCH, best=target, candidates=[target],
                          match_type=ProductMatchType.CORRECTED_NAME_CORROBORATED,
                          confidence=name_match_score(target.query, target.product.product_name_zh),
                          ambiguity=ProductAmbiguityReason.LOW_MATCH_CONFIDENCE, candidate_count=1)

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
            # 分數高不代表身份安全：替換字、一般品名被多個品名包含、第二名太接近 → 一律請使用者確認
            omission_only, rivals = similarity_confirm_blockers(raw_queries, plausible[0], by_raw[1:], top_score)
            if rivals:
                choices = [plausible[0]] + rivals
                return result(ProductVerificationStatus.PARTIAL_MATCH, candidates=choices,
                              match_type=ProductMatchType.NAME_SIMILARITY, confidence=top_score,
                              ambiguity=ProductAmbiguityReason.MULTIPLE_SIMILAR_NAMES,
                              candidate_count=len(choices))
            if omission_only:
                return result(ProductVerificationStatus.VERIFIED, best=plausible[0], candidates=plausible,
                              match_type=ProductMatchType.NAME_SIMILARITY, confidence=top_score)
            # 只有一個候選但使用者文字有替換字：最接近的一項，請使用者確認是不是它
            return result(ProductVerificationStatus.PARTIAL_MATCH, best=plausible[0], candidates=plausible,
                          match_type=ProductMatchType.NAME_SIMILARITY, confidence=top_score,
                          ambiguity=ProductAmbiguityReason.LOW_MATCH_CONFIDENCE, candidate_count=len(plausible))
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
            claim_risk=claim_risk_level(status, product_side=claim.is_product_side_assertion),
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
    # 提問不是宣稱（claim_role 保持 USER_QUESTION，Risk Engine 不因提問升級產品風險），
    # 但問題中待查證的命題仍依同一套 claim policy 評估：疾病命題若作為產品功效宣稱屬醫療效能 → claim_risk HIGH。
    if medical:
        proposition = classify_claim_policy(
            claim, official_links, approved_claims=approved_claims,
            approved_claim_evidence_ids=approved_claim_evidence_ids,
            regulation_sources=regulation_sources or {}, product_unresolved=product_unresolved,
            comparison_completed=comparison_completed, any_source_succeeded=any_source_succeeded,
            structured=structured_policy,
        )
        if proposition.policy_result == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM:
            return build(ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM, "QUESTION_PROPOSITION_MEDICAL_CLAIM",
                         QUESTION_MEDICAL_PROPOSITION, proposition)
        if proposition.policy_result in (ClaimPolicyResult.APPROVED_MATCH, ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT):
            return build(ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL, "QUESTION_SUPPORTED_BY_APPROVED",
                         "與該產品官方核准的保健功效相符。", proposition)
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
    if not medical and not exceeds and not claim.uses_absolute_language and approved_claims:
        # 產品特定提問：所問的功效就是該產品核准的保健功效類別（官方功效名稱相同）；只有部分重疊 → 無法確認
        function_policy = approved_function_decision(
            claim, approved_claims, structured_policy.question_function if structured_policy else None)
        if function_policy is not None:
            if function_policy.policy_result == ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT:
                return build(ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL, "QUESTION_WITHIN_APPROVED_FUNCTION",
                             function_policy.reason, function_policy)
            return build(ClaimAssessmentStatus.UNKNOWN, "QUESTION_FUNCTION_MATCH_AMBIGUOUS",
                         function_policy.reason, function_policy)
    if not any_source_succeeded:
        return build(ClaimAssessmentStatus.UNKNOWN, "CLAIM_NO_SOURCE_AVAILABLE", "資料來源暫時無法查詢，無法判斷這個說法。")
    if medical:
        return build(ClaimAssessmentStatus.UNSUPPORTED, "QUESTION_MEDICAL_NOT_SUPPORTED",
                     "使用者詢問的是疾病相關效果；沒有官方資料支持保健食品具有這種效果。")
    return build(ClaimAssessmentStatus.UNSUPPORTED, "QUESTION_NO_OFFICIAL_SUPPORT", "目前沒有找到支持這個說法的官方資料。")


QUESTION_MEDICAL_PROPOSITION = "若把這句話當成產品功效宣稱，屬於涉及疾病治療或醫療效能的高風險說法；保健食品不能這樣宣稱。"


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
