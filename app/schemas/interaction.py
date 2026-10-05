"""Interaction Runtime 的結構化契約（Agent A 抽取、Agent B 研究結果）。

只放 enum 與結構化欄位，不放任何最終使用者句子；Agent C（Phase 3）依這些欄位表達。

安全語意（由 service 以 deterministic 規則填寫，Agent C 不可改寫）：
* `InteractionConclusionStatus.CONCLUSION_ELIGIBLE` 才可以肯定陳述「有這個交互作用」，且一律附 applicability 條件。
  其他狀態（衝突 / 待審 / 證據不足 / 只有 lineage 對應）只能表達為證據與不確定性。
* `PairStatus.NO_CORPUS_RECORD` = 本資料庫沒有這組的紀錄，不等於「沒有交互作用」或「可以一起吃」。
* DuplicateFinding 只代表「重複成分存在」；一律 dose_evaluation = NOT_EVALUATED，並列出禁止推論的事項。
* evidence 的 `supports_direction` 逐筆保留；NO_SIGNIFICANT_EFFECT / MIXED 不算支持交互作用（`counts_as_support = False`）。
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class InteractionMode(StrEnum):
    """Agent A 判斷的問題型態（Interaction inquiry 的子類型）。"""

    PAIRWISE = "pairwise"                          # 「A 跟 B 可以一起吃嗎？」「這個跟 warfarin 有衝突嗎？」
    DUPLICATE_COMPARISON = "duplicate_comparison"  # 「這兩個都有鋅嗎？」（重複成分比較）


class InteractionQuestion(StrEnum):
    """Controller 判斷的「使用者實際在問什麼」（pairwise 的子類型；Reporter 依此決定第一句直接回答）。"""

    TOGETHER_USE = "together_use"                  # 「A 跟 B 可以一起吃嗎？」
    INTERACTION_EXISTENCE = "interaction_existence"  # 「A 跟 B 有沒有交互作用 / 衝突？」
    EXPLANATION = "explanation"                    # 「為什麼 A 會影響 B？」


class InteractionSubjectKind(StrEnum):
    """Agent A 對一個被提到對象的分類（只是線索；identity 一律由 Agent B 的 deterministic resolver 決定）。"""

    INGREDIENT = "ingredient"
    DRUG = "drug"
    PRODUCT = "product"


class SubjectResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"                                            # 成分 / 藥品 / 產品已確認 identity
    UNRESOLVED = "UNRESOLVED"                                        # 名稱不在 corpus identity（不翻譯、不猜）
    AMBIGUOUS = "AMBIGUOUS"                                          # 同一寫法對到多個 identity（成分 vs 藥品等）
    DRUG_ACTIVE_SUBSTANCE_UNMAPPED = "DRUG_ACTIVE_SUBSTANCE_UNMAPPED"  # 沒有可靠的藥品 → 活性成分對應（例如只有商品名）
    PRODUCT_NEEDS_CONFIRMATION = "PRODUCT_NEEDS_CONFIRMATION"        # 產品未 VERIFIED（多候選 / 低信心 / 查無）
    PRODUCT_NO_CONFIRMED_INGREDIENTS = "PRODUCT_NO_CONFIRMED_INGREDIENTS"  # 產品已確認但沒有任何已確認的成分 identity
    LOOKUP_FAILED = "LOOKUP_FAILED"                                  # 查詢失敗（不是「查無」）


class InteractionConclusionStatus(StrEnum):
    CONCLUSION_ELIGIBLE = "CONCLUSION_ELIGIBLE"          # 可 deterministic 陳述（仍須附 applicability 條件）
    LINEAGE_NOT_INHERITED = "LINEAGE_NOT_INHERITED"      # 只經 form → nutrient group 對應；Bridge 規定不自動繼承 interaction
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"              # 證據互相矛盾：只能說明衝突
    NEEDS_REVIEW = "NEEDS_REVIEW"                        # corpus 尚待審核
    INSUFFICIENT_FOR_CONCLUSION = "INSUFFICIENT_FOR_CONCLUSION"  # 只有背景 / 輔助證據（CONTEXT_ONLY / SUPPORTING）


class PairStatus(StrEnum):
    RECORDS_FOUND = "RECORDS_FOUND"
    NO_CORPUS_RECORD = "NO_CORPUS_RECORD"                # 本資料庫無此組合紀錄（不是「沒有交互作用」）


class EvidenceDirectionRole(StrEnum):
    """`supports_direction` 的 deterministic 分類（原值另外逐筆保留）。"""

    SUPPORTS = "SUPPORTS"
    SUPPORTS_QUALIFIED = "SUPPORTS_QUALIFIED"            # 支持，但有限定（例如只支持吸收、不代表需要改治療）
    INDIRECT = "INDIRECT"                                # 類別層級關聯 / 特定產品警語：不是這組成分的直接證據
    NOT_SUPPORTING = "NOT_SUPPORTING"                    # NO_SIGNIFICANT_EFFECT 等：不支持存在交互作用
    MIXED = "MIXED"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    UNCLASSIFIED = "UNCLASSIFIED"                        # 未知的新值：保守視為不支持


SUPPORTING_ROLES = frozenset({EvidenceDirectionRole.SUPPORTS, EvidenceDirectionRole.SUPPORTS_QUALIFIED})

# supports_direction 原值 → role。未列的新值一律 UNCLASSIFIED（不當成支持）。
DIRECTION_ROLES: dict[str, EvidenceDirectionRole] = {
    "SUPPORTS": EvidenceDirectionRole.SUPPORTS,
    "SUPPORTS_ABSORPTION_NOT_TREATMENT_NECESSITY": EvidenceDirectionRole.SUPPORTS_QUALIFIED,
    "CLASS_LEVEL_ASSOCIATION": EvidenceDirectionRole.INDIRECT,
    "PRODUCT_SPECIFIC_CAUTION": EvidenceDirectionRole.INDIRECT,
    "NO_SIGNIFICANT_EFFECT": EvidenceDirectionRole.NOT_SUPPORTING,
    "NO_ADDED_CLINICAL_BENEFIT_IN_STUDIED_CONTEXT": EvidenceDirectionRole.NOT_SUPPORTING,
    "MIXED": EvidenceDirectionRole.MIXED,
    "MIXED_ENDPOINTS": EvidenceDirectionRole.MIXED,
    "MIXED_POPULATION_CONTEXT": EvidenceDirectionRole.MIXED,
    "CONTEXT_NOT_PAIR_SPECIFIC": EvidenceDirectionRole.CONTEXT_ONLY,
}


def direction_role(supports_direction: str | None) -> EvidenceDirectionRole:
    return DIRECTION_ROLES.get(supports_direction or "", EvidenceDirectionRole.UNCLASSIFIED)


class InteractionResearchStatus(StrEnum):
    COMPLETE = "COMPLETE"                                # 所有對象都已確認 identity，已查 corpus
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"          # 有對象 identity 未確認：先請使用者確認，不做 interaction research
    INSUFFICIENT_SUBJECTS = "INSUFFICIENT_SUBJECTS"      # 少於兩個可比較的對象
    CORPUS_UNAVAILABLE = "CORPUS_UNAVAILABLE"            # Interaction corpus 尚未可用（未匯入 / 查詢失敗）


class DuplicateFindingType(StrEnum):
    DUPLICATE_INGREDIENT_DETECTED = "DUPLICATE_INGREDIENT_DETECTED"


class DoseEvaluation(StrEnum):
    NOT_EVALUATED = "NOT_EVALUATED"


class ProhibitedInference(StrEnum):
    """重複成分偵測「不得」推論的事項（Agent C 不可表達成已發生）。"""

    OVERDOSE = "OVERDOSE"
    EXCESSIVE_INTAKE = "EXCESSIVE_INTAKE"
    EXCEEDS_UPPER_LIMIT = "EXCEEDS_UPPER_LIMIT"
    TOXICITY = "TOXICITY"
    UNSAFE_COMBINATION = "UNSAFE_COMBINATION"
    CLINICAL_HARM = "CLINICAL_HARM"
    SHOULD_NOT_TAKE_TOGETHER = "SHOULD_NOT_TAKE_TOGETHER"


DUPLICATE_PROHIBITED_INFERENCES = tuple(ProhibitedInference)


# --------------------------------------------------------------------------- Agent A 輸出


class InteractionSubjectExtraction(BaseModel):
    """OpenAI Structured Output：使用者問題中被比較 / 被問到的一個對象。"""

    text: str = Field(description="對象的名稱，必須逐字取自使用者原文（不可翻譯、不可改寫成英文或其他寫法）")
    kind: InteractionSubjectKind = Field(
        description="ingredient：成分 / 營養素；drug：藥品（用藥 / 藥物名稱）；product：保健食品或其他產品的名稱")


class InteractionSubject(BaseModel):
    """NormalizedUserRequest 中的對象（含候選確認後的固定結果）。"""

    text: str
    kind: InteractionSubjectKind
    # 使用者從確認卡選定產品後，以該官方紀錄的字號固定（不重跑 Agent A）；只有 product 會有
    pinned_permit_number: str | None = None
    pinned_product_name: str | None = None
    selection_order: int | None = None


# --------------------------------------------------------------------------- Agent B 輸出


class EntityRefOut(BaseModel):
    entity_id: str                  # canonical ID（N_* / F_* / D_*）；內部使用，不給使用者看
    kind: str                       # NutrientGroup | IngredientForm | DrugSubstance
    canonical_name_en: str
    display_name_zh: str | None = None
    identity_level: str | None = None


class IngredientMappingOut(BaseModel):
    """產品的一個「已確認」官方成分對應（供 Reporter 的「成分對應」說明；只含正式 mapping，不含候選 / 未解析）。"""

    raw_texts: list[str] = Field(default_factory=list)   # 官方原文
    entity: EntityRefOut                                  # 已確認 entity（成分形式或營養素類別）
    parent: EntityRefOut | None = None                    # 成分形式 → 營養素類別（CONTRIBUTES_TO_NUTRIENT，不是 SAME_AS）
    via: EntityRefOut | None = None                       # entity 是由哪個成分形式推導的（FORM_TO_GROUP）


class SubjectResolution(BaseModel):
    text: str
    kind: InteractionSubjectKind
    status: SubjectResolutionStatus
    entities: list[EntityRefOut] = Field(default_factory=list)       # RESOLVED 的 canonical identity
    product_id: str | None = None
    product_name: str | None = None
    product_confirmed: bool = False
    # 產品對象的 entity 來源：direct = 官方原文直接對應；via_form = 只經 form → group 對應（Bridge lineage）
    via_forms: dict[str, list[str]] = Field(default_factory=dict)     # group entity_id → 產生它的 form entity_id
    unresolved_ingredient_count: int = 0                              # 產品仍有幾個官方成分沒有已確認 identity
    ingredient_mappings: list[IngredientMappingOut] = Field(default_factory=list)  # 產品已確認的成分對應（Reporter 用）
    official_ingredient_count: int = 0
    note: str | None = None


class InteractionParticipant(BaseModel):
    role: str                       # AFFECTING | AFFECTED | SELF
    entity: EntityRefOut


class InteractionEvidenceItem(BaseModel):
    evidence_id: str
    source_id: str
    source_title: str | None = None
    authority: str | None = None
    source_type: str
    is_official: bool
    study_type: str | None = None
    supports_direction: str         # 原值
    direction_role: EvidenceDirectionRole
    counts_as_support: bool         # SUPPORTS / SUPPORTS_QUALIFIED 才為 True
    evidence_use: str
    evidence_level: str
    certainty: str
    retrieved_at: str | None = None
    source_retrieval_status: str
    source_url: str | None = None
    article_or_section: str | None = None
    population: str | None = None
    applicability_scope: str | None = None
    result_summary_zh: str | None = None
    limitations_zh: str | None = None


class Applicability(BaseModel):
    """結論適用條件（corpus 原文）。條件是否成立尚未評估（沒有劑量 / 用法 / 族群資料）。"""

    conditions: str
    dose_context: str | None = None
    population_context: str | None = None
    timing_advice: str | None = None
    evaluated: bool = False
    identity_match: str             # DIRECT：對象 identity 就是 interaction 宣告的 identity；VIA_FORM_TO_GROUP：只經 lineage


class SubjectMatch(BaseModel):
    """interaction 的一個參與者對應到哪一個使用者對象（text）與其匹配依據。"""

    subject_text: str
    entity_id: str
    match_basis: str                # DIRECT | VIA_FORM_TO_GROUP
    via_entity_id: str | None = None


class InteractionFinding(BaseModel):
    interaction_id: str
    record_kind: str                # PAIRWISE_INTERACTION
    interaction_type: str
    directionality: str
    participants: list[InteractionParticipant]
    subject_matches: list[SubjectMatch] = Field(default_factory=list)
    severity: str
    certainty: str
    evidence_level: str
    evidence_use: str
    review_status: str
    decision_mode: str
    deterministic_conclusion_allowed: bool          # corpus 資料欄位（0006 generated column）
    conclusion_status: InteractionConclusionStatus  # 加上 identity 對應方式後，runtime 實際可用的結論資格
    applicability: Applicability
    clinical_effect: str | None = None
    mechanism_summary: str | None = None
    recommendation: str | None = None
    evidence: list[InteractionEvidenceItem] = Field(default_factory=list)
    supporting_evidence_count: int = 0
    non_supporting_evidence_count: int = 0
    corpus_version: str | None = None


class PairCoverage(BaseModel):
    subject_a: str
    subject_b: str
    status: PairStatus
    interaction_ids: list[str] = Field(default_factory=list)


class DuplicateProductRef(BaseModel):
    product_id: str
    product_name: str | None = None
    via_entities: list[EntityRefOut]                # 本產品經由哪些 form / group 對應到該營養素群


class DuplicateFinding(BaseModel):
    finding: DuplicateFindingType = DuplicateFindingType.DUPLICATE_INGREDIENT_DETECTED
    nutrient: EntityRefOut
    products: list[DuplicateProductRef]
    same_identity_level_across_products: bool       # 兩邊都是同一個 form / group；不同 form → False（不可比較重量）
    shared_entity_ids: list[str] = Field(default_factory=list)
    dose_evaluation: DoseEvaluation = DoseEvaluation.NOT_EVALUATED
    severity: str = "DOSE_DEPENDENT_NOT_ASSIGNED"
    prohibited_inferences: list[ProhibitedInference] = Field(default_factory=lambda: list(DUPLICATE_PROHIBITED_INFERENCES))
    missing_information: list[str] = Field(
        default_factory=lambda: ["AMOUNT_PER_SERVING", "SERVING_SIZE", "DAILY_SERVINGS", "AMOUNT_BASIS"])
    duplicate_rule_id: str | None = None            # corpus DUP_* 規則（獨立語意；不是有害交互作用）
    duplicate_rule_evidence: list[InteractionEvidenceItem] = Field(default_factory=list)
    deterministic_conclusion_allowed: bool = False  # 只對「重複成分存在」；不代表任何劑量 / 風險結論


class DuplicateCoverage(BaseModel):
    """每個產品有多少官方成分尚未確認 identity：沒有偵測到重複 ≠ 沒有重複。"""

    product_id: str
    official_count: int
    confirmed_count: int
    unresolved_count: int


class InteractionResearch(BaseModel):
    """Agent B 的 interaction 研究結果（bundle.interaction_research）。"""

    mode: InteractionMode
    status: InteractionResearchStatus
    subjects: list[SubjectResolution] = Field(default_factory=list)
    findings: list[InteractionFinding] = Field(default_factory=list)
    pairs: list[PairCoverage] = Field(default_factory=list)
    duplicates: list[DuplicateFinding] = Field(default_factory=list)
    duplicate_coverage: list[DuplicateCoverage] = Field(default_factory=list)
    corpus_version: str | None = None
    # 只有「identity 全部確認、corpus 已查」才有；資料不足（沒有任何 corpus 紀錄）才會依既有 Researcher policy 補 RAG / 官方網站
    corpus_sufficient: bool = False
    fallback_used: bool = False
    clarification_reason: str | None = None

    @property
    def conclusion_eligible(self) -> list[InteractionFinding]:
        return [f for f in self.findings if f.conclusion_status == InteractionConclusionStatus.CONCLUSION_ELIGIBLE]
