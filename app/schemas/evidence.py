"""Agent B 的輸出契約：EvidenceBundle 與其中的證據型別。

每一筆證據都要帶 SourceRef，Reporter 只能引用這裡出現的來源。
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimEvidenceRelation,
    ClaimOrigin,
    ClaimPolicyResult,
    ClaimPolicyType,
    ErrorCode,
    EvidenceScope,
    PolicyEvidenceUse,
    EvidenceStrength,
    ProductAmbiguityReason,
    ProductIdentityFlag,
    ProductMatchType,
    ProductStatus,
    ProductVerificationStatus,
    RiskLevel,
    SourceType,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SourceRef(BaseModel):
    source_name: str
    source_type: SourceType
    source_url: str | None = None
    source_record_id: str | None = None
    retrieved_at: datetime | None = None
    is_official: bool


class ToolError(BaseModel):
    code: ErrorCode
    message: str
    source: str


class ToolCallRecord(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    planned_by: str = "agent"  # llm_planner | guardrail | deterministic_fallback | agent
    ok: bool
    duration_ms: int
    result_count: int = 0
    error: ToolError | None = None


class EvidenceRelevance(BaseModel):
    """Evidence Relevance Gate 的判定結果。

    來源是官方網站不代表就能成為證據；必須與本次產品 / 宣稱 / 問題相關。
    """

    relevance_score: float = Field(ge=0.0, le=1.0)
    supports_claim: bool | None = None  # True 支持、False 反駁、None 與宣稱無直接關係
    evidence_scope: EvidenceScope
    rationale: str = ""
    assessed_by: str = "deterministic"  # deterministic | llm | source_role（使用者提供的官方網域頁面）


PRODUCT_RECORD_RELEVANCE = EvidenceRelevance(
    relevance_score=1.0,
    evidence_scope=EvidenceScope.PRODUCT_SPECIFIC,
    rationale="以 canonical ID 直接取得的該產品官方紀錄",
)


# ---------- 結構化官方資料 ----------


class ProductRecord(BaseModel):
    product_id: str
    country_code: str = "TW"
    product_name_zh: str
    product_name_en: str | None = None
    company_id: str | None = None
    company_name: str | None = None
    permit_id: str | None = None
    permit_number: str | None = None
    permit_type: str | None = None
    approval_date: str | None = None
    status: ProductStatus = ProductStatus.UNKNOWN
    source: SourceRef


# lookup_product 以官方 / 人工審核的產品別名完全命中時的 matched_by（推測別名不會使用此值）
OFFICIAL_ALIAS_MATCHED_BY = "official_alias"


class ProductCandidateMatch(BaseModel):
    product: ProductRecord
    match_score: float = Field(ge=0.0, le=1.0)
    matched_by: str  # permit_number | product_name | official_alias | company_name
    query: str


class ProductMatch(BaseModel):
    status: ProductVerificationStatus
    best_match: ProductCandidateMatch | None = None
    candidates: list[ProductCandidateMatch] = Field(default_factory=list)
    queried_terms: list[str] = Field(default_factory=list)
    lookup_completed: bool = Field(
        default=True, description="False 代表查詢失敗；此時 NOT_FOUND 不能解讀為「查無紀錄」"
    )
    notes: list[str] = Field(default_factory=list)
    # Deterministic product resolver 的判定依據（debug / 追蹤用，不含使用者原文）
    match_type: ProductMatchType | None = None
    match_confidence: float | None = None
    ambiguity_reason: ProductAmbiguityReason | None = None
    candidate_count: int = 0  # 真正合理的候選數（不是 lookup 回傳的列數）
    # 產品已由較強證據確認時，較弱的圖片文字證據與官方紀錄的差異（不改變 status）
    identity_flags: list[ProductIdentityFlag] = Field(default_factory=list)

    @property
    def is_ambiguous(self) -> bool:
        """兩個以上真正合理的候選：不可猜，應請使用者確認。

        LOW_MATCH_CONFIDENCE 只是相似度不足（可能是錯字 / 語音誤聽），交給完整查證流程，不算 ambiguous。
        """
        return (
            self.ambiguity_reason is not None
            and self.ambiguity_reason != ProductAmbiguityReason.LOW_MATCH_CONFIDENCE
            and self.candidate_count >= 2
        )

    @property
    def needs_confirmation(self) -> bool:
        """只有名稱相似、分數未達確認門檻的候選：不確認也不猜，請使用者確認是不是這一項。"""
        return (
            self.status == ProductVerificationStatus.PARTIAL_MATCH
            and self.ambiguity_reason == ProductAmbiguityReason.LOW_MATCH_CONFIDENCE
            and bool(self.candidates)
        )

    def resolver_debug(self) -> dict[str, Any]:
        return {
            "candidate_count": self.candidate_count,
            "selected_product_id": self.best_match.product.product_id
            if self.best_match and self.status == ProductVerificationStatus.VERIFIED else None,
            "match_type": self.match_type.value if self.match_type else None,
            "match_confidence": self.match_confidence,
            "ambiguity_reason": self.ambiguity_reason.value if self.ambiguity_reason else None,
        }


# approved_claims.claim_kind：官方紀錄只有宣稱原文、未列功效類別
CLAIM_KIND_UNCLASSIFIED = "HEALTH_FOOD_CLAIM_UNCLASSIFIED"


class ApprovedClaimEvidence(BaseModel):
    evidence_id: str
    relevance: EvidenceRelevance = PRODUCT_RECORD_RELEVANCE
    product_id: str
    approved_claim_id: str
    function_name: str  # 官方保健功效類別，例如「調節血脂」
    claim_text: str  # 官方核准的功效敘述原文
    # False：官方紀錄只有宣稱原文、未列功效類別（宣稱仍是官方核准證據，只是沒有類別名稱）
    function_classified: bool = True
    source: SourceRef


class WarningEvidence(BaseModel):
    evidence_id: str
    relevance: EvidenceRelevance = PRODUCT_RECORD_RELEVANCE
    product_id: str
    warning_id: str
    warning_type: str  # WARNING | PRECAUTION
    text: str
    source: SourceRef


class IngredientRecord(BaseModel):
    ingredient_id: str
    name_zh: str
    name_en: str | None = None
    source: SourceRef


class IllegalAdEvidence(BaseModel):
    evidence_id: str
    relevance: EvidenceRelevance | None = None  # 尚未通過 Relevance Gate 時為 None
    case_id: str
    product_name: str | None = None
    company_name: str | None = None
    violation_content: str
    violation_type: str | None = None
    penalty_date: str | None = None
    source: SourceRef


# ---------- 知識圖譜 / RAG / 官方網站 ----------


class GraphEvidence(BaseModel):
    evidence_id: str
    relevance: EvidenceRelevance | None = None  # 尚未通過 Relevance Gate 時為 None
    query_type: str
    subject_id: str
    subject_name: str
    relation: str
    object_id: str
    object_name: str
    properties: dict[str, Any] = Field(default_factory=dict)
    source: SourceRef


class RagPolicyMetadata(BaseModel):
    """claim-policy corpus chunk 的 provenance 與 quarantine（取自 rag_chunks.metadata，不由 LLM 產生）。"""

    source_id: str  # corpus 原始來源 ID（SRC_xxx）
    status: str  # CURRENT | SUPERSEDED | NEEDS_REVIEW
    review_status: str | None = None
    evidence_use: PolicyEvidenceUse
    article_or_section: str | None = None
    effective_date: str | None = None
    source_url: str | None = None
    extraction_method: str | None = None
    corpus_version: str | None = None

    @property
    def decision_eligible(self) -> bool:
        return self.evidence_use == PolicyEvidenceUse.DECISION_ELIGIBLE


class RagEvidence(BaseModel):
    evidence_id: str
    relevance: EvidenceRelevance | None = None  # 尚未通過 Relevance Gate 時為 None
    document_id: str
    chunk_id: str
    title: str
    content: str
    similarity: float
    document_type: str | None = None
    source: SourceRef
    policy_metadata: RagPolicyMetadata | None = None  # 只有 claim-policy corpus chunk 才有

    @property
    def decision_eligible(self) -> bool:
        """可否作為 final legal evidence：非 corpus 文件沿用原規則；corpus chunk 只有 DECISION_ELIGIBLE 可以。"""
        return self.policy_metadata is None or self.policy_metadata.decision_eligible


# ---------- 結構化 claim-policy（TFDA 宣稱法規 corpus，migration 0003） ----------


class ClaimPolicyRuleEvidence(BaseModel):
    """一條官方條文 / 附件項目的適用判斷（claim_policy_rules）。"""

    evidence_id: str  # policy_rule:<rule_id>
    rule_id: str
    source_id: str  # corpus 原始來源 ID
    rule_category: str
    policy_effect: str  # PERMITTED | RESTRICTED | PROHIBITED | REQUIRES_EVIDENCE | PRODUCT_SPECIFIC_ONLY | NEEDS_REVIEW
    claim_category: str
    rule_summary_zh: str | None = None
    official_text: str
    conditions: str | None = None
    legal_basis: str | None = None
    article_or_section: str
    effective_date: str | None = None
    status: str
    review_status: str
    evidence_use: PolicyEvidenceUse
    quarantine_reasons: list[str] = Field(default_factory=list)
    source: SourceRef

    @property
    def decision_eligible(self) -> bool:
        return self.evidence_use == PolicyEvidenceUse.DECISION_ELIGIBLE


class ClaimPolicyExampleEvidence(BaseModel):
    """官方例示詞句（claim_policy_examples）以及它與哪一則宣稱文字相同。"""

    evidence_id: str  # policy_example:<example_id>
    example_id: str
    rule_id: str
    source_id: str
    example_text: str
    example_type: str  # PERMITTED_EXAMPLE | GENERAL_FUNCTION_EXAMPLE | CONDITIONAL_EXAMPLE | MEDICAL_EFFICACY_EXAMPLE | PROHIBITED_EXAMPLE
    claim_category: str
    conditions: str | None = None
    article_or_section: str | None = None
    evidence_use: PolicyEvidenceUse
    quarantine_reasons: list[str] = Field(default_factory=list)
    matched_claim_texts: list[str] = Field(default_factory=list)
    match_method: str = "exact_text"  # 正規化後文字完全相同（不推測語意）
    source: SourceRef

    @property
    def decision_eligible(self) -> bool:
        return self.evidence_use == PolicyEvidenceUse.DECISION_ELIGIBLE


class HealthFunctionEvidence(BaseModel):
    """健康食品保健功效目錄的一個功效項目（health_function_catalog）。"""

    evidence_id: str  # health_function:<function_id>
    function_id: str
    official_function_name: str
    official_definition: str | None = None  # 官方未提供時為 None，不自行補寫
    evaluation_method_title: str | None = None
    legal_basis: str | None = None
    effective_date: str | None = None
    status: str
    review_status: str
    evidence_use: PolicyEvidenceUse
    source: SourceRef


class OfficialWebEvidence(BaseModel):
    evidence_id: str
    relevance: EvidenceRelevance | None = None  # 尚未通過 Relevance Gate 時為 None
    title: str
    url: str
    domain: str
    snippet: str
    source: SourceRef


# ---------- 宣稱評估 ----------


class ClaimEvidenceLink(BaseModel):
    evidence_id: str
    relation: ClaimEvidenceRelation
    rationale: str


class InferredEntityLink(BaseModel):
    """系統推測的實體關聯（例如由產品名稱推導「品牌 → 申請商」）。

    這是 inferred evidence，不是官方確認事實：Reporter 只能以「推測」描述，不得說成官方確認。
    """

    query: str  # 使用者提到的名稱
    entity_type: str  # BRAND | COMPANY …
    alias: str
    resolved_company_id: str | None = None
    resolved_company_name: str | None = None
    confidence: float
    inference_basis: dict[str, Any] = Field(default_factory=dict)
    is_official_confirmation: bool = False


class RejectedEvidence(BaseModel):
    """未通過 Relevance Gate 的證據：保留供 debug / 稽核，Reporter 不可引用。"""

    evidence_id: str
    evidence_kind: str
    title: str
    source: SourceRef
    relevance: EvidenceRelevance | None = None
    reason: str


class PlannerStep(BaseModel):
    round: int
    planned_calls: list[str] = Field(default_factory=list)
    rejected_calls: list[str] = Field(default_factory=list)
    note: str = ""


class ClaimPolicyDecision(BaseModel):
    """單一宣稱的 claim-policy 判定與可解釋依據（Reporter 只能引用這裡的內容，不自行創造法律結論）。"""

    policy_result: ClaimPolicyResult
    policy_type: ClaimPolicyType
    rule_id: str
    reason: str  # 固定模板文字
    evidence_scope: EvidenceScope | None = None
    # 與該產品官方核准功效比對的結果（若有）
    matched_approved_claim_id: str | None = None
    matched_approved_evidence_id: str | None = None
    matched_function_name: str | None = None
    matched_approved_claim_text: str | None = None
    match_method: str | None = None  # exact_text | semantic_equivalent | semantic_supported | semantic_exceeds
    difference: str | None = None  # 超出範圍的說明（來自比對步驟的 rationale）
    # 支持這個判定的官方來源（核准功效紀錄 / 已通過 Relevance Gate 的法規文件）
    sources: list[SourceRef] = Field(default_factory=list)
    source_evidence_ids: list[str] = Field(default_factory=list)
    product_identity_unresolved: bool = False  # 判定只針對宣稱本身，產品身份尚未確認
    # 結構化 claim-policy corpus 的依據（Reporter 的「法規依據」明細使用；primary card 只用 reason）
    policy_rule_ids: list[str] = Field(default_factory=list)
    matched_example_id: str | None = None
    matched_example_text: str | None = None
    # 有對應但不能單獨下結論的官方資料（CONTEXT_ONLY / CORROBORATION_REQUIRED / 有條件例示）
    context_evidence_ids: list[str] = Field(default_factory=list)


class ClaimAssessment(BaseModel):
    claim_index: int
    claim_text: str
    origin: ClaimOrigin = ClaimOrigin.USER_QUESTION
    is_product_side_assertion: bool = False
    involves_medical_efficacy: bool = False
    status: ClaimAssessmentStatus
    evidence_links: list[ClaimEvidenceLink] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    explanation: str = ""
    policy: ClaimPolicyDecision | None = None
    # 這個命題「若作為產品功效宣稱」的風險（deterministic，由 status / role 決定）；
    # Primary card、為什麼、完整分析都只讀這個欄位，不各自重新推論。不等於產品本身的風險。
    claim_risk: RiskLevel | None = None


class EvidenceBundle(BaseModel):
    request_id: str
    product_match: ProductMatch
    approved_claims: list[ApprovedClaimEvidence] = Field(default_factory=list)
    warnings: list[WarningEvidence] = Field(default_factory=list)
    ingredients: list[IngredientRecord] = Field(default_factory=list)
    illegal_ad_evidence: list[IllegalAdEvidence] = Field(default_factory=list)
    graph_evidence: list[GraphEvidence] = Field(default_factory=list)
    rag_evidence: list[RagEvidence] = Field(default_factory=list)
    official_web_evidence: list[OfficialWebEvidence] = Field(default_factory=list)
    # 結構化 claim-policy（evidence_use 一路保留；EXCLUDED_SUPERSEDED 不會出現在 current query）
    claim_policy_rules: list[ClaimPolicyRuleEvidence] = Field(default_factory=list)
    claim_policy_examples: list[ClaimPolicyExampleEvidence] = Field(default_factory=list)
    health_functions: list[HealthFunctionEvidence] = Field(default_factory=list)
    claim_assessment: list[ClaimAssessment] = Field(default_factory=list)
    evidence_strength: EvidenceStrength = EvidenceStrength.NONE
    conflicts: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    rejected_evidence: list[RejectedEvidence] = Field(default_factory=list)
    inferred_links: list[InferredEntityLink] = Field(default_factory=list)  # 推測，不可描述為官方確認
    errors: list[ToolError] = Field(default_factory=list)
    tools_called: list[ToolCallRecord] = Field(default_factory=list)
    planner_mode: str = "llm_planner"  # llm_planner | deterministic_fallback | structured_fast_path
    planner_trace: list[PlannerStep] = Field(default_factory=list)
    official_web_search_attempted: bool = False
    fast_path_eligible: bool = False
    fast_path_reason: str | None = None  # 走 / 不走 structured fast path 的原因代碼（debug 用）

    def all_sources(self) -> list[SourceRef]:
        """依證據出現順序列出不重複的來源，給 Reporter 附來源使用。"""
        items: list[SourceRef] = []
        if self.product_match.best_match:
            items.append(self.product_match.best_match.product.source)
        for group in (
            self.approved_claims,
            self.warnings,
            self.illegal_ad_evidence,
            self.graph_evidence,
            self.rag_evidence,
            self.official_web_evidence,
            self.claim_policy_rules,
            self.claim_policy_examples,
            self.health_functions,
        ):
            items.extend(item.source for item in group)
        seen: set[tuple[str, str | None]] = set()
        unique: list[SourceRef] = []
        for source in items:
            key = (source.source_name, source.source_url)
            if key not in seen:
                seen.add(key)
                unique.append(source)
        return unique

    def evidence_ids(self) -> set[str]:
        ids: set[str] = set()
        for group in (
            self.approved_claims,
            self.warnings,
            self.illegal_ad_evidence,
            self.graph_evidence,
            self.rag_evidence,
            self.official_web_evidence,
            self.claim_policy_rules,
            self.claim_policy_examples,
            self.health_functions,
        ):
            ids.update(item.evidence_id for item in group)
        return ids

    def evidence_count(self) -> int:
        return len(self.evidence_ids()) + (1 if self.product_match.best_match else 0)
