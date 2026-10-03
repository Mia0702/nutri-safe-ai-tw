"""Deterministic Risk Engine。

先分別取得 Product Verification、Claim Assessment、Evidence Strength，
再依下列透明規則合成 LOW / MEDIUM / HIGH。LLM 不參與這一步。

宣稱依 statement role 分三群：
  產品方宣稱  PRODUCT_LABEL_CLAIM / ADVERTISEMENT_CLAIM（有包裝或廣告證據）
  流傳說法    USER_ASSERTION / THIRD_PARTY_CLAIM / OFFICIAL_CLAIM（有人這樣說，但不能證明是產品方說的）
  使用者提問  USER_QUESTION（待查證的命題，不是宣稱）

規則（取所有命中規則中的最高等級）：

HIGH（必須有產品方或官方證據）
  RISK_HIGH_PRODUCT_SIDE_MEDICAL_CLAIM  包裝 / 廣告宣稱醫療效能
  RISK_HIGH_PRODUCT_SIDE_CONFLICT       包裝 / 廣告宣稱與官方資料矛盾
  RISK_HIGH_PRODUCT_CONFLICT            產品資訊與官方紀錄有重大矛盾
  RISK_HIGH_ILLEGAL_AD_RECORD           官方違規廣告紀錄與本產品 / 公司相符

MEDIUM
  RISK_MED_PRODUCT_NOT_FOUND            查不到官方產品紀錄（不等於假貨）
  RISK_MED_PRODUCT_LOOKUP_FAILED        產品查詢失敗，無法確認（不可說成查無紀錄）
  RISK_MED_PRODUCT_PARTIAL              只找到相似產品，無法確認是同一項
  RISK_MED_PERMIT_INACTIVE              官方紀錄存在，但許可證已非有效狀態
  RISK_MED_PRODUCT_SIDE_CLAIM_ISSUE     包裝 / 廣告宣稱誇大、無官方支持或無法判斷
  RISK_MED_CIRCULATING_CLAIM_ISSUE      流傳說法涉及醫療效能、與官方矛盾、誇大或無官方支持
  RISK_MED_EVIDENCE_INSUFFICIENT        證據強度為 WEAK / NONE
  RISK_MED_PRODUCT_CONTEXT_MISSING      使用者指稱「這個 / 它」，但無法確認是哪一項產品

LOW
  RISK_LOW_VERIFIED_CONSISTENT          產品已確認、被說出的宣稱都與官方相符、證據 STRONG/MEDIUM
  RISK_LOW_GENERAL_INFO                 不涉及特定產品、沒有問題宣稱、證據 STRONG/MEDIUM

使用者提問（USER_QUESTION）的查證結果只決定回答內容，不會單獨讓風險升級。
"""

from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimOrigin,
    EvidenceStrength,
    ProductStatus,
    ProductVerificationStatus,
    RiskLevel,
)
from app.schemas.evidence import ClaimAssessment, EvidenceBundle
from app.schemas.risk import RiskAssessment, RiskRuleHit
from app.schemas.user_request import NormalizedUserRequest

DISCLAIMER_NOT_FOUND = "目前未找到官方紀錄，不代表產品一定是假貨或違法。"
DISCLAIMER_MEDICAL = "保健食品不能用來治療疾病，身體有狀況請諮詢醫師或藥師。"
DISCLAIMER_DATA_INCOMPLETE = "目前部分資料來源暫時無法查詢，因此這次結果可能不完整。"
DISCLAIMER_TW_SCOPE = (
    "目前第一階段主要以台灣官方資料進行查證，因此這項產品目前無法完成完整的官方資料比對。"
)
DISCLAIMER_PRODUCT_CONTEXT = "目前不知道你指的是哪一項產品，所以無法判斷這項產品本身的風險。"

_LEVEL_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
_ACCEPTABLE_EVIDENCE = {EvidenceStrength.STRONG, EvidenceStrength.MEDIUM}
_OK_CLAIM_STATUSES = {ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL, ClaimAssessmentStatus.NOT_APPLICABLE}
_CIRCULATING_ORIGINS = {ClaimOrigin.USER_ASSERTION, ClaimOrigin.THIRD_PARTY_CLAIM, ClaimOrigin.OFFICIAL_CLAIM}


def _statuses(claims: list[ClaimAssessment]) -> set[ClaimAssessmentStatus]:
    return {c.status for c in claims}


class RiskEngine:
    def assess(self, request: NormalizedUserRequest, bundle: EvidenceBundle) -> RiskAssessment:
        product_status = bundle.product_match.status
        strength = bundle.evidence_strength
        claims = bundle.claim_assessment
        product_side = [c for c in claims if c.is_product_side_assertion]
        circulating = [c for c in claims if c.origin in _CIRCULATING_ORIGINS]
        asserted = product_side + circulating
        hits: list[RiskRuleHit] = []

        def hit(rule_id: str, level: RiskLevel, reason: str) -> None:
            hits.append(RiskRuleHit(rule_id=rule_id, level=level, reason=reason))

        # ---- HIGH ----
        ps = _statuses(product_side)
        if ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM in ps:
            hit("RISK_HIGH_PRODUCT_SIDE_MEDICAL_CLAIM", RiskLevel.HIGH, "包裝或廣告宣稱可以治療、預防或改善疾病，保健食品不能這樣宣稱。")
        if ClaimAssessmentStatus.CONFLICT_WITH_OFFICIAL in ps:
            hit("RISK_HIGH_PRODUCT_SIDE_CONFLICT", RiskLevel.HIGH, "包裝或廣告的說法與官方資料互相矛盾。")
        if product_status == ProductVerificationStatus.CONFLICT:
            hit("RISK_HIGH_PRODUCT_CONFLICT", RiskLevel.HIGH, "提供的產品資訊與官方紀錄不一致。")
        if bundle.illegal_ad_evidence:
            hit("RISK_HIGH_ILLEGAL_AD_RECORD", RiskLevel.HIGH, "官方曾公布相關的違規廣告紀錄。")

        # ---- MEDIUM：產品 ----
        lookup_completed = bundle.product_match.lookup_completed
        not_found = product_status == ProductVerificationStatus.NOT_FOUND and lookup_completed
        if not_found:
            hit("RISK_MED_PRODUCT_NOT_FOUND", RiskLevel.MEDIUM, "目前資料庫中查不到這項產品的官方紀錄。")
        if product_status == ProductVerificationStatus.NOT_FOUND and not lookup_completed:
            hit("RISK_MED_PRODUCT_LOOKUP_FAILED", RiskLevel.MEDIUM, "產品資料暫時查不到，無法確認是否有官方紀錄。")
        if product_status == ProductVerificationStatus.PARTIAL_MATCH:
            hit("RISK_MED_PRODUCT_PARTIAL", RiskLevel.MEDIUM, "只找到名稱相似的產品，無法確定是同一項。")
        best = bundle.product_match.best_match
        if (
            product_status == ProductVerificationStatus.VERIFIED
            and best is not None
            and best.product.status != ProductStatus.ACTIVE
        ):
            hit("RISK_MED_PERMIT_INACTIVE", RiskLevel.MEDIUM, "這項產品的官方許可目前不是有效狀態。")

        # ---- MEDIUM：宣稱 ----
        if ps & {
            ClaimAssessmentStatus.POTENTIALLY_EXAGGERATED,
            ClaimAssessmentStatus.UNSUPPORTED,
            ClaimAssessmentStatus.UNKNOWN,
        }:
            hit("RISK_MED_PRODUCT_SIDE_CLAIM_ISSUE", RiskLevel.MEDIUM, "包裝或廣告的說法可能誇大，或找不到官方資料支持。")
        if _statuses(circulating) - _OK_CLAIM_STATUSES:
            hit(
                "RISK_MED_CIRCULATING_CLAIM_ISSUE",
                RiskLevel.MEDIUM,
                "流傳的說法沒有官方支持或不正確，但目前沒有證據顯示是產品本身這樣宣稱。",
            )

        # ---- MEDIUM：證據 / 指稱 ----
        if strength in (EvidenceStrength.WEAK, EvidenceStrength.NONE):
            hit("RISK_MED_EVIDENCE_INSUFFICIENT", RiskLevel.MEDIUM, "目前可用的官方證據不足。")
        if request.missing_product_context:
            hit("RISK_MED_PRODUCT_CONTEXT_MISSING", RiskLevel.MEDIUM, "還不知道你指的是哪一項產品。")

        # ---- LOW ----
        asserted_ok = all(c.status in _OK_CLAIM_STATUSES for c in asserted)
        if not hits and asserted_ok and strength in _ACCEPTABLE_EVIDENCE:
            if product_status == ProductVerificationStatus.VERIFIED:
                hit("RISK_LOW_VERIFIED_CONSISTENT", RiskLevel.LOW, "有找到官方產品紀錄，且沒有發現不當宣稱。")
            elif product_status == ProductVerificationStatus.NOT_APPLICABLE:
                hit("RISK_LOW_GENERAL_INFO", RiskLevel.LOW, "這是一般資訊查詢，官方資料可以支持。")

        if not hits:
            hit("RISK_MED_DEFAULT_UNCERTAIN", RiskLevel.MEDIUM, "目前資訊不足以判定為低風險。")

        level = max((h.level for h in hits), key=_LEVEL_ORDER.__getitem__)
        data_incomplete = bool(bundle.errors)
        all_statuses = [c.status for c in claims]
        is_conclusive = level == RiskLevel.HIGH or (
            strength in _ACCEPTABLE_EVIDENCE
            and not request.missing_product_context
            and lookup_completed
            and ClaimAssessmentStatus.UNKNOWN not in all_statuses
        )

        disclaimers: list[str] = []
        if not_found:
            disclaimers.append(DISCLAIMER_NOT_FOUND)
        if request.missing_product_context:
            disclaimers.append(DISCLAIMER_PRODUCT_CONTEXT)
        if any(c.involves_medical_efficacy for c in claims):
            disclaimers.append(DISCLAIMER_MEDICAL)
        if request.foreign_country_codes:
            disclaimers.append(DISCLAIMER_TW_SCOPE)
        if data_incomplete:
            disclaimers.append(DISCLAIMER_DATA_INCOMPLETE)

        return RiskAssessment(
            risk_level=level,
            product_verification=product_status,
            claim_statuses=all_statuses,
            evidence_strength=strength,
            triggered_rules=sorted(hits, key=lambda h: -_LEVEL_ORDER[h.level]),
            required_disclaimers=disclaimers,
            is_conclusive=is_conclusive,
            data_incomplete=data_incomplete,
        )
