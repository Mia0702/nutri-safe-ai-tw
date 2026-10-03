"""Claim policy：被說出來的宣稱（包裝 / 廣告 / 轉述）與官方證據的 deterministic 比對規則。

判定順序（每一步都有 rule_id，可測試、可追蹤）：
  1. 明確醫療效能（治療 / 治癒 / 診斷 / 取代醫療）→ MEDICAL_TREATMENT_CLAIM
     依據：健康食品管理法 §2（保健功效「非屬治療、矯正人類疾病之醫療效能」）、§14 II（不得涉及醫療效能）。
     核准功效依法不可能是治療效能，因此這一步在核准功效比對之前。
  2. 該產品官方核准功效比對（product-specific approved claim，優先於任何一般字面規則）
     文字相同（正規化後）→ APPROVED_MATCH；比對步驟判定語意等價 → APPROVED_MATCH；
     範圍內較保守 → APPROVED_SCOPE_CONSISTENT；更廣 / 更強 / 加上官方未支持的效果 → EXCEEDS_APPROVED_SCOPE
     （§14 I：宣稱之保健效能不得超過許可範圍）。
  3. 沒有對應核准功效時：疾病預防 / 改善類 → MEDICAL_TREATMENT_CLAIM；其餘 → UNSUPPORTED_CLAIM。
  4. 判定不了（產品未確認、比對步驟失敗、資料來源失敗）→ INSUFFICIENT_EVIDENCE，不猜。

結構化 claim-policy corpus（StructuredPolicyContext，選填；沒有提供時以上規則完全不變）：
  * Evidence priority：該產品核准功效 → 結構化規則 / 官方例示 → RAG 法規文件。
    官方例示只在核准功效比對沒有結果之後才使用，不能覆蓋產品自己的核准功效。
  * 宣稱文字與官方例示詞句正規化後完全相同、例示與所屬規則都是 DECISION_ELIGIBLE 才能下結論：
    醫療效能例示（規則 PROHIBITED）→ MEDICAL_TREATMENT_CLAIM；
    通常可使用例示（規則 PERMITTED，且沒有產品核准功效可比對）→ PERMITTED_GENERAL_WORDING。
  * CONTEXT_ONLY / CORROBORATION_REQUIRED / 有條件或一般功能例示只提供脈絡：
    不單獨產生合法 / 違法結論，原本會是「找不到官方支持」時改為 INSUFFICIENT_EVIDENCE。
  * DECISION_ELIGIBLE 的醫療效能禁止規則作為醫療效能判定的法規來源（取代需要 RAG 才有的來源）。

不使用任何「看到某個字就判高風險」的關鍵字規則：醫療語意來自 Agent A 的結構化抽取（MedicalAssertionType），
是否在核准範圍內來自 Agent B 的比對步驟（ClaimEvidenceRelation），最終狀態由這裡的規則決定。
"""

from dataclasses import dataclass, field

from app.normalization import normalize_name
from app.schemas.enums import (
    EXPLICIT_MEDICAL_ASSERTIONS,
    ClaimAssessmentStatus,
    ClaimEvidenceRelation,
    ClaimPolicyResult,
    ClaimPolicyType,
    ClaimSemanticCategory,
    EvidenceScope,
    MedicalAssertionType,
)
from app.schemas.evidence import (
    ApprovedClaimEvidence,
    ClaimEvidenceLink,
    ClaimPolicyDecision,
    ClaimPolicyExampleEvidence,
    ClaimPolicyRuleEvidence,
    SourceRef,
)
from app.schemas.user_request import ExtractedClaim

# policy result → Claim Assessment status（Risk Engine 的輸入；Risk Engine 規則本身不變）
POLICY_STATUS = {
    ClaimPolicyResult.APPROVED_MATCH: ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL,
    ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT: ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL,
    ClaimPolicyResult.EXCEEDS_APPROVED_SCOPE: ClaimAssessmentStatus.POTENTIALLY_EXAGGERATED,
    ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM: ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM,
    ClaimPolicyResult.UNSUPPORTED_CLAIM: ClaimAssessmentStatus.UNSUPPORTED,
    ClaimPolicyResult.INSUFFICIENT_EVIDENCE: ClaimAssessmentStatus.UNKNOWN,
    ClaimPolicyResult.PERMITTED_GENERAL_WORDING: ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL,
}

# 固定說明模板（Reporter 直接使用，不交給 LLM 自由發揮法律結論）
REASON_APPROVED_MATCH = "這項文字與該產品官方核准的保健功效一致。"
REASON_APPROVED_SCOPE = "這項文字在該產品官方核准的保健功效範圍內，沒有擴大效果。"
REASON_EXCEEDS = "官方核准的保健功效範圍比這項宣稱小，宣稱擴大了效果或加上官方沒有核准的效果。"
REASON_EXCEEDS_ABSOLUTE = "這項宣稱使用保證、根治等絕對化或誇大用語，超出官方核准的保健功效範圍。"
REASON_MEDICAL_EXPLICIT = "這項宣稱涉及疾病治療、診斷或取代醫療，超出保健食品可宣稱的範圍。"
REASON_MEDICAL_DISEASE = "這項宣稱涉及預防或改善特定疾病，且不在該產品官方核准的保健功效中，超出保健食品可宣稱的範圍。"
REASON_MEDICAL_DISEASE_NO_PRODUCT = "這項宣稱涉及預防或改善特定疾病，保健食品不能這樣宣稱。"
REASON_UNSUPPORTED_APPROVED = "這項宣稱不在該產品官方核准的保健功效中，找不到官方資料支持。"
REASON_UNSUPPORTED = "目前沒有找到支持這項宣稱的官方資料。"
REASON_EXAGGERATED_NO_APPROVED = "這項宣稱使用絕對化或誇大用語，目前沒有官方資料支持。"
REASON_PRODUCT_UNRESOLVED = "產品身份還沒有確認，無法和官方核准功效比對，需要先確認是哪一項產品。"
REASON_COMPARISON_INCOMPLETE = "和官方核准功效的比對沒有完成，目前無法判斷這項宣稱。"
REASON_NO_SOURCE = "資料來源暫時無法查詢，無法判斷這項宣稱。"
NOTE_CLAIM_LEVEL_ONLY = "產品身份尚未完全確認；這個判定只針對宣稱內容，不代表產品真偽。"
REASON_OFFICIAL_MEDICAL_EXAMPLE = "這項宣稱和官方列舉的醫療效能詞句相同，保健食品不能這樣宣稱。"
REASON_OFFICIAL_PERMITTED_EXAMPLE = "這項文字和官方列為通常可使用的詞句相同。"
REASON_POLICY_CONTEXT_ONLY = "官方資料有相關說明，但要看使用條件或仍待官方資料複核，目前不能單獨據此判定合法或違法。"

# 例示類型 × 所屬規則效果：只有這兩種組合可以由「文字相同」直接下結論（其餘只提供脈絡）
MEDICAL_EXAMPLE_TYPE = "MEDICAL_EFFICACY_EXAMPLE"
PERMITTED_EXAMPLE_TYPE = "PERMITTED_EXAMPLE"
EFFECT_PROHIBITED = "PROHIBITED"
EFFECT_PERMITTED = "PERMITTED"


@dataclass
class StructuredPolicyContext:
    """單一宣稱可用的結構化 claim-policy 證據（Agent B 由 Evidence Tools 取得，evidence_use 原樣保留）。"""

    example_matches: list[ClaimPolicyExampleEvidence] = field(default_factory=list)  # 與宣稱文字相同的官方例示
    rules: dict[str, ClaimPolicyRuleEvidence] = field(default_factory=dict)  # rule_id → 規則（含例示所屬規則）
    medical_rules: list[ClaimPolicyRuleEvidence] = field(default_factory=list)  # 醫療效能禁止規則

    def decisive_example(self) -> tuple[ClaimPolicyExampleEvidence, ClaimPolicyRuleEvidence, ClaimPolicyResult] | None:
        """可直接下結論的例示：例示與規則都 DECISION_ELIGIBLE，且例示類型與規則效果一致。醫療效能優先。"""
        found = []
        for ex in self.example_matches:
            rule = self.rules.get(ex.rule_id)
            if rule is None or not (ex.decision_eligible and rule.decision_eligible):
                continue
            if ex.example_type == MEDICAL_EXAMPLE_TYPE and rule.policy_effect == EFFECT_PROHIBITED:
                found.append((0, ex.example_id, ex, rule, ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM))
            elif ex.example_type == PERMITTED_EXAMPLE_TYPE and rule.policy_effect == EFFECT_PERMITTED:
                found.append((1, ex.example_id, ex, rule, ClaimPolicyResult.PERMITTED_GENERAL_WORDING))
        if not found:
            return None
        _, _, ex, rule, result = min(found, key=lambda item: item[:2])
        return ex, rule, result

    def context_evidence_ids(self) -> list[str]:
        """有對應但不能單獨下結論的官方資料（待複核 / 需佐證 / 有條件例示）。"""
        decisive = self.decisive_example()
        decisive_id = decisive[0].evidence_id if decisive else None
        return [ex.evidence_id for ex in self.example_matches if ex.evidence_id != decisive_id]

    def decision_medical_rules(self) -> list[ClaimPolicyRuleEvidence]:
        return [r for r in self.medical_rules if r.decision_eligible]


def is_explicit_medical_claim(claim: ExtractedClaim) -> bool:
    """明確的治療 / 治癒 / 診斷 / 取代醫療宣稱（依法不可能是核准保健功效）。"""
    return claim.medical_assertion in EXPLICIT_MEDICAL_ASSERTIONS


def is_disease_claim(claim: ExtractedClaim) -> bool:
    """涉及特定疾病的宣稱（明確治療類，或疾病預防 / 改善類）。

    Agent A 沒有提供 medical_assertion 時，才退回 semantic_category（相容舊資料）。
    """
    if claim.medical_assertion is None:
        return claim.semantic_category == ClaimSemanticCategory.MEDICAL_EFFICACY
    return claim.medical_assertion != MedicalAssertionType.NONE


def is_asserted_disease_claim(claim: ExtractedClaim) -> bool:
    """被說出來（包裝 / 廣告 / 轉述）的疾病宣稱：圖片自動查證、官方網站是否需要查詢都用這個條件。"""
    return claim.is_asserted and is_disease_claim(claim)


def exact_approved_match(claim_text: str, approved: list[ApprovedClaimEvidence]) -> ApprovedClaimEvidence | None:
    """正規化後與某條官方核准功效敘述完全相同（只比字面，不推測語意）。"""
    norm = normalize_name(claim_text)
    if not norm:
        return None
    for e in approved:
        if normalize_name(e.claim_text) == norm:
            return e
    return None


def _approved_source(e: ApprovedClaimEvidence) -> SourceRef:
    return e.source.model_copy(update={"source_record_id": e.source.source_record_id or e.approved_claim_id})


def classify_claim_policy(
    claim: ExtractedClaim,
    links: list[ClaimEvidenceLink],
    *,
    approved_claims: list[ApprovedClaimEvidence],
    approved_claim_evidence_ids: set[str],
    regulation_sources: dict[str, SourceRef],
    product_unresolved: bool,
    comparison_completed: bool,
    any_source_succeeded: bool,
    structured: StructuredPolicyContext | None = None,
) -> ClaimPolicyDecision:
    """被說出來的宣稱（非 USER_QUESTION）的 claim-policy 判定。

    links：已過濾為「已通過 Relevance Gate 的官方證據」的比對結果。
    approved_claims / approved_claim_evidence_ids：只有已確認產品才會有（Agent B 只附上已確認產品的核准功效）。
    regulation_sources：本次已通過 Relevance Gate 的官方法規文件（evidence_id → SourceRef），作為法規類判定的來源。
    structured：結構化 claim-policy 證據（選填）；只在核准功效比對之後使用。
    """
    by_id = {e.evidence_id: e for e in approved_claims}
    approved_ids = set(approved_claim_evidence_ids) | set(by_id)
    approved_links = [l for l in links if l.evidence_id in approved_ids]
    reg_ids = list(regulation_sources)
    reg_refs = list(regulation_sources.values())
    sp = structured or StructuredPolicyContext()
    decisive = sp.decisive_example()
    context_ids = sp.context_evidence_ids()

    def decide(result, policy_type, rule_id, reason, *, approved: ApprovedClaimEvidence | None = None,
               approved_id: str | None = None, method=None, difference=None, statutory=False, example=None):
        sources: list[SourceRef] = []
        ids: list[str] = []
        policy_rules: list[ClaimPolicyRuleEvidence] = []
        if approved is not None:
            sources.append(_approved_source(approved))
            ids.append(approved.evidence_id)
        elif approved_id:
            ids.append(approved_id)
        if example is not None:
            ids.append(example[0].evidence_id)
            sources.append(example[0].source)
            policy_rules.append(example[1])
        if result == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM:
            policy_rules += [r for r in sp.decision_medical_rules() if r not in policy_rules]
        ids += [r.evidence_id for r in policy_rules] + (reg_ids if statutory else [])
        seen = {(x.source_name, x.source_url) for x in sources}
        for ref in [r.source for r in policy_rules] + (reg_refs if statutory else []):
            if (ref.source_name, ref.source_url) not in seen:  # 同一份法規文件的多個段落 / 規則只列一次來源
                seen.add((ref.source_name, ref.source_url))
                sources.append(ref)
        has_statutory = bool(policy_rules) or (statutory and bool(reg_refs))
        scope = (EvidenceScope.PRODUCT_SPECIFIC if (approved is not None or approved_id)
                 else EvidenceScope.CLAIM_REGULATION if has_statutory else None)
        return ClaimPolicyDecision(
            policy_result=result, policy_type=policy_type, rule_id=rule_id, reason=reason, evidence_scope=scope,
            matched_approved_claim_id=approved.approved_claim_id if approved else None,
            matched_approved_evidence_id=approved.evidence_id if approved else approved_id,
            matched_function_name=approved.function_name if approved else None,
            matched_approved_claim_text=approved.claim_text if approved else None,
            match_method=method, difference=difference, sources=sources,
            source_evidence_ids=list(dict.fromkeys(ids)), product_identity_unresolved=product_unresolved,
            policy_rule_ids=list(dict.fromkeys(r.rule_id for r in policy_rules)),
            matched_example_id=example[0].example_id if example else None,
            matched_example_text=example[0].example_text if example else None,
            context_evidence_ids=context_ids,
        )

    def decide_by_example():
        ex, ex_rule, result = decisive
        if result == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM:
            return decide(result, ClaimPolicyType.OFFICIAL_MEDICAL_EFFICACY_EXAMPLE, "POLICY_OFFICIAL_MEDICAL_EXAMPLE",
                          REASON_OFFICIAL_MEDICAL_EXAMPLE, method="official_example_exact_text", statutory=True,
                          example=(ex, ex_rule))
        return decide(result, ClaimPolicyType.OFFICIAL_PERMITTED_EXAMPLE, "POLICY_OFFICIAL_PERMITTED_EXAMPLE",
                      REASON_OFFICIAL_PERMITTED_EXAMPLE, method="official_example_exact_text", example=(ex, ex_rule))

    def first_link(relation):
        return next((l for l in approved_links if l.relation == relation), None)

    # 1. 明確醫療效能：依法不可能是核准功效
    if is_explicit_medical_claim(claim):
        return decide(ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM,
                      ClaimPolicyType.STATUTORY_MEDICAL_EFFICACY_PROHIBITION,
                      "POLICY_EXPLICIT_MEDICAL_EFFICACY", REASON_MEDICAL_EXPLICIT, statutory=True)

    # 2. 該產品官方核准功效比對（優先於一般字面規則）
    if approved_ids:
        exact = exact_approved_match(claim.claim_text, approved_claims)
        if exact is not None and not claim.uses_absolute_language:
            return decide(ClaimPolicyResult.APPROVED_MATCH, ClaimPolicyType.PRODUCT_SPECIFIC_APPROVED_CLAIM,
                          "POLICY_APPROVED_EXACT_TEXT", REASON_APPROVED_MATCH, approved=exact, method="exact_text")
        exceeds = first_link(ClaimEvidenceRelation.EXCEEDS)
        if exceeds is not None:
            return decide(ClaimPolicyResult.EXCEEDS_APPROVED_SCOPE, ClaimPolicyType.STATUTORY_APPROVED_SCOPE_LIMIT,
                          "POLICY_EXCEEDS_APPROVED_SCOPE", REASON_EXCEEDS, approved=by_id.get(exceeds.evidence_id),
                          approved_id=exceeds.evidence_id, method="semantic_exceeds",
                          difference=exceeds.rationale or None, statutory=True)
        within = first_link(ClaimEvidenceRelation.EQUIVALENT) or first_link(ClaimEvidenceRelation.SUPPORTED)
        if claim.uses_absolute_language or claim.semantic_category == ClaimSemanticCategory.EXAGGERATED_EFFICACY:
            # 主題在核准範圍內，但用保證 / 根治等絕對化用語：仍是超出許可範圍的誇張宣稱
            return decide(ClaimPolicyResult.EXCEEDS_APPROVED_SCOPE, ClaimPolicyType.STATUTORY_APPROVED_SCOPE_LIMIT,
                          "POLICY_ABSOLUTE_WORDING_EXCEEDS_SCOPE", REASON_EXCEEDS_ABSOLUTE,
                          approved=by_id.get(within.evidence_id) if within else None,
                          approved_id=within.evidence_id if within else None,
                          method="semantic_exceeds" if within else None, statutory=True)
        if within is not None:
            equivalent = within.relation == ClaimEvidenceRelation.EQUIVALENT
            return decide(
                ClaimPolicyResult.APPROVED_MATCH if equivalent else ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT,
                ClaimPolicyType.PRODUCT_SPECIFIC_APPROVED_CLAIM,
                "POLICY_APPROVED_SEMANTIC_MATCH" if equivalent else "POLICY_APPROVED_SCOPE_CONSISTENT",
                REASON_APPROVED_MATCH if equivalent else REASON_APPROVED_SCOPE,
                approved=by_id.get(within.evidence_id), approved_id=within.evidence_id,
                method="semantic_equivalent" if equivalent else "semantic_supported",
            )
        # 核准功效沒有對應：官方醫療效能例示（文字相同）才使用；通常可使用例示不覆蓋產品自己的核准範圍
        if decisive is not None and decisive[2] == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM:
            return decide_by_example()
        if not comparison_completed:
            return decide(ClaimPolicyResult.INSUFFICIENT_EVIDENCE, ClaimPolicyType.PRODUCT_SPECIFIC_APPROVED_CLAIM,
                          "POLICY_APPROVED_COMPARISON_INCOMPLETE", REASON_COMPARISON_INCOMPLETE)
        if is_disease_claim(claim):
            return decide(ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM,
                          ClaimPolicyType.STATUTORY_MEDICAL_EFFICACY_PROHIBITION,
                          "POLICY_DISEASE_CLAIM_NOT_APPROVED", REASON_MEDICAL_DISEASE, statutory=True)
        return decide(ClaimPolicyResult.UNSUPPORTED_CLAIM, ClaimPolicyType.STATUTORY_APPROVED_SCOPE_LIMIT,
                      "POLICY_NOT_IN_APPROVED_CLAIMS", REASON_UNSUPPORTED_APPROVED, statutory=True)

    # 3. 沒有已確認產品的核准功效可比對
    if decisive is not None and decisive[2] == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM:
        return decide_by_example()  # 只針對宣稱本身（產品未確認時另外標示）
    if is_disease_claim(claim):
        # 宣稱本身就是疾病預防 / 改善：可以對「宣稱」判定（產品身份未確認時另外標示）
        return decide(ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM,
                      ClaimPolicyType.STATUTORY_MEDICAL_EFFICACY_PROHIBITION,
                      "POLICY_DISEASE_CLAIM_NO_APPROVED_EVIDENCE", REASON_MEDICAL_DISEASE_NO_PRODUCT, statutory=True)
    if product_unresolved:
        return decide(ClaimPolicyResult.INSUFFICIENT_EVIDENCE, ClaimPolicyType.NO_POLICY_EVIDENCE,
                      "POLICY_PRODUCT_UNRESOLVED", REASON_PRODUCT_UNRESOLVED)
    if not any_source_succeeded:
        return decide(ClaimPolicyResult.INSUFFICIENT_EVIDENCE, ClaimPolicyType.NO_POLICY_EVIDENCE,
                      "POLICY_NO_SOURCE_AVAILABLE", REASON_NO_SOURCE)
    if decisive is not None:  # 與官方通常可使用例示相同（沒有產品核准功效可比對）
        return decide_by_example()
    if claim.uses_absolute_language or claim.semantic_category == ClaimSemanticCategory.EXAGGERATED_EFFICACY:
        return decide(ClaimPolicyResult.UNSUPPORTED_CLAIM, ClaimPolicyType.NO_POLICY_EVIDENCE,
                      "POLICY_EXAGGERATED_NO_APPROVED", REASON_EXAGGERATED_NO_APPROVED, statutory=True)
    if context_ids:  # 只有脈絡 / 有條件 / 待複核的官方資料：不單獨下合法或違法結論
        return decide(ClaimPolicyResult.INSUFFICIENT_EVIDENCE, ClaimPolicyType.POLICY_CONTEXT_NOT_DECISIVE,
                      "POLICY_CONTEXT_ONLY_NOT_DECISIVE", REASON_POLICY_CONTEXT_ONLY)
    return decide(ClaimPolicyResult.UNSUPPORTED_CLAIM, ClaimPolicyType.NO_POLICY_EVIDENCE,
                  "POLICY_NO_OFFICIAL_SUPPORT", REASON_UNSUPPORTED)


def approved_support_decision(
    links: list[ClaimEvidenceLink], approved_claims: list[ApprovedClaimEvidence], claim_text: str,
) -> ClaimPolicyDecision | None:
    """使用者提問（USER_QUESTION）：只標註是否對應到官方核准功效（提問不是宣稱，不做違規判定）。"""
    by_id = {e.evidence_id: e for e in approved_claims}
    exact = exact_approved_match(claim_text, approved_claims)
    link = next((l for l in links if l.evidence_id in by_id
                 and l.relation in (ClaimEvidenceRelation.EQUIVALENT, ClaimEvidenceRelation.SUPPORTED)), None)
    approved = exact or (by_id[link.evidence_id] if link else None)
    if approved is None:
        return None
    equivalent = exact is not None or link.relation == ClaimEvidenceRelation.EQUIVALENT
    return ClaimPolicyDecision(
        policy_result=ClaimPolicyResult.APPROVED_MATCH if equivalent else ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT,
        policy_type=ClaimPolicyType.PRODUCT_SPECIFIC_APPROVED_CLAIM,
        rule_id="POLICY_QUESTION_MATCHES_APPROVED",
        reason=REASON_APPROVED_MATCH if equivalent else REASON_APPROVED_SCOPE,
        evidence_scope=EvidenceScope.PRODUCT_SPECIFIC,
        matched_approved_claim_id=approved.approved_claim_id, matched_approved_evidence_id=approved.evidence_id,
        matched_function_name=approved.function_name, matched_approved_claim_text=approved.claim_text,
        match_method="exact_text" if exact is not None else
        ("semantic_equivalent" if equivalent else "semantic_supported"),
        sources=[_approved_source(approved)], source_evidence_ids=[approved.evidence_id],
    )
