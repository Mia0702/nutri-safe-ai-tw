"""使用者輸入與 Agent A 的輸出契約。"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from app.schemas.enums import (
    ASSERTED_ORIGINS,
    PRODUCT_SIDE_ORIGINS,
    CertificationMarkType,
    ClaimContentSource,
    ClaimOrigin,
    ClaimSemanticCategory,
    EntityType,
    EvidenceScope,
    InputType,
    Intent,
    MedicalAssertionType,
    PendingClarificationType,
    ProductConfirmationSource,
    ProductMentionProvenance,
    ProductReferenceStatus,
    UrlSourceRole,
    ContextReference,
)
from app.schemas.evidence import ToolCallRecord
from app.schemas.image_bundle import FusedImageBundle
from app.schemas.interaction import (
    InteractionMode,
    InteractionQuestion,
    InteractionSubject,
    InteractionSubjectExtraction,
)


class ContextEntity(BaseModel):
    """前文中已辨識或已查證的實體（由系統紀錄，不是 LLM 自行產生）。"""

    entity_type: EntityType
    name: str
    entity_id: str | None = None  # 已查證的 canonical ID


class ConversationTurn(BaseModel):
    user_text: str
    entities: list[ContextEntity] = Field(default_factory=list)


class ConfirmedProduct(BaseModel):
    """task context 中已確認的 canonical 產品：之後「它 / 這個」等指稱的 authoritative referent。"""

    product_id: str
    permit_id: str | None = None
    permit_number: str | None = None
    official_product_name: str
    confirmation_source: ProductConfirmationSource
    confirmed_at: datetime
    confirmed_request_id: str
    # USER_CONFIRMED_TARGET 時保留未解決的差異（例如名稱與官方紀錄不一致）；確認不會消除差異
    discrepancy: str | None = None


class PendingClarification(BaseModel):
    """上一題系統在問「是不是這一項官方紀錄」，等使用者以「是 / 不是」回答（task context，短期）。"""

    clarification_type: PendingClarificationType
    candidate_product_id: str
    candidate_product_name: str
    permit_number: str | None = None
    previous_request_id: str
    unresolved_reason: str  # 例如 CONFLICT / LOW_MATCH_CONFIDENCE
    discrepancy: str | None = None  # 給使用者看的差異說明（名稱不一致等）；沒有差異時為 None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))  # 寫入 task context 時以 store 時鐘覆寫


class UserConfirmedTarget(BaseModel):
    """使用者確認的查詢目標：只代表使用者說「就是這一項」，不是官方查證結論。"""

    product_id: str
    official_product_name: str
    permit_number: str | None = None
    discrepancy: str | None = None


class ConversationContext(BaseModel):
    conversation_id: str | None = None
    turns: list[ConversationTurn] = Field(default_factory=list)
    confirmed_product: ConfirmedProduct | None = None  # 只有 task context（LINE）會提供
    # 上一個已完成的圖片 request（只是 id，不含圖片內容；結構化觀察在 request store）
    last_visual_request_id: str | None = None

    def known_entities(self, entity_type: EntityType | None = None) -> list[ContextEntity]:
        """由新到舊列出前文實體（同名只保留最新一次）。"""
        seen: set[tuple[str, str]] = set()
        out: list[ContextEntity] = []
        for turn in reversed(self.turns):
            for entity in turn.entities:
                key = (entity.entity_type.value, entity.name)
                if (entity_type is None or entity.entity_type == entity_type) and key not in seen:
                    seen.add(key)
                    out.append(entity)
        return out


class MediaInput(BaseModel):
    """使用者傳來的圖片或語音原始內容（由 Agent A 的 Input Tool 轉成文字）。"""

    kind: Literal["image", "audio"]
    content: bytes = Field(repr=False, exclude=True)
    mime_type: str
    filename: str | None = None
    source_id: str | None = None  # 來源識別碼（例如 LINE message id）；不含內容


class RawUserInput(BaseModel):
    """Channel（LINE / debug API）交給 Agent A 的原始輸入。

    text 是使用者打的文字；media / urls 是尚未理解的圖片、語音、網址，
    由 Agent A 呼叫 Input Tools（analyze_image / transcribe_audio / fetch_url_content）轉成文字，
    再與 text 一起走同一段語意理解。可同時有多種（mixed input）。
    """

    input_type: InputType
    text: str = ""
    media: list[MediaInput] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    source_url: str | None = None
    adapter_notes: list[str] = Field(default_factory=list)
    context: ConversationContext = Field(default_factory=ConversationContext)


class ExtractedClaim(BaseModel):
    """從輸入中抽出的一個功效 / 健康命題，以及它的 statement role。"""

    claim_text: str = Field(description="命題的完整敘述，例如「某產品可以降低血糖」")
    origin: ClaimOrigin = Field(description="這個命題是誰、以什麼角色說的")
    semantic_category: ClaimSemanticCategory
    target_condition: str | None = Field(
        description="命題涉及的疾病或症狀名稱；沒有則為 null"
    )
    target_function: str | None = Field(
        description="命題涉及的生理功能；沒有則為 null"
    )
    uses_absolute_language: bool = Field(
        description="是否使用保證、根治、百分之百、立即見效等絕對化用語"
    )
    medical_assertion: MedicalAssertionType | None = Field(
        default=None,
        description="醫療語意細分：none（生理機能保健 / 營養補充 / 一般好處，沒有疾病）、treats_or_cures_disease（治療、治癒、根治疾病）、"
                    "diagnoses_disease（診斷疾病）、replaces_medical_care（取代藥物或醫療、可停藥）、"
                    "prevents_or_improves_disease（預防、改善、減輕某個具體疾病或症狀）",
    )
    confidence: float | None = Field(
        default=None, description="這段文字確實是一則功效 / 健康宣稱（而不是品名、規格、標語）的把握程度 0~1"
    )
    content_source: ClaimContentSource | None = Field(
        default=None,
        description="命題出自哪一段內容：user_text（【使用者文字】）、image_content（【圖片內容】）、"
                    "url_content（【官方網頁內容】或【網頁內容】）",
    )
    # 以下由系統依 URL source role 填寫（不在 LLM schema 中）：命題來自網頁時的來源角色與網址
    source_role: SkipJsonSchema[UrlSourceRole | None] = None
    source_url: SkipJsonSchema[str | None] = None
    # multi-image：這句宣稱出現在哪幾張圖片（系統依融合觀察填寫，1-based；不在 LLM schema 中）
    source_images: SkipJsonSchema[list[int]] = Field(default_factory=list)

    @property
    def claim_role(self) -> ClaimOrigin:
        """statement role（= origin）。"""
        return self.origin

    @property
    def claim_target(self) -> str | None:
        """宣稱指向的疾病 / 症狀或生理功能。"""
        return self.target_condition or self.target_function

    @property
    def is_product_side_assertion(self) -> bool:
        return self.origin in PRODUCT_SIDE_ORIGINS

    @property
    def is_asserted(self) -> bool:
        return self.origin in ASSERTED_ORIGINS


class UrlSource(BaseModel):
    """使用者提供的一個網址：來源角色與 provenance（由系統依網域判斷，不由 LLM 決定）。"""

    requested_url: str
    source_url: str  # 重新導向後實際讀取的網址（讀取失敗時同 requested_url）
    source_domain: str | None = None
    source_role: UrlSourceRole
    title: str | None = None
    fetched: bool = False
    retrieved_at: datetime | None = None
    excerpt: str = Field(default="", repr=False)  # 頁面正文摘錄（官方頁才會作為證據使用）

    @property
    def is_official(self) -> bool:
        return self.source_role == UrlSourceRole.OFFICIAL_REGULATORY_SOURCE


class OfficialSourceStatement(BaseModel):
    """官方監管網頁上的內容：是官方來源的陳述（OFFICIAL_CLAIM / official evidence），不是待查證的外部宣稱。"""

    claim_text: str
    origin: ClaimOrigin = ClaimOrigin.OFFICIAL_CLAIM
    source_role: UrlSourceRole = UrlSourceRole.OFFICIAL_REGULATORY_SOURCE
    source_url: str
    source_domain: str | None = None
    title: str | None = None
    # 官方網域只代表來源角色；頁面這句話能說明的範圍由 Agent B 依頁面能否對應到已確認產品決定
    evidence_scope: EvidenceScope = EvidenceScope.GENERAL_BACKGROUND


class CertificationMarkObservation(BaseModel):
    """圖片上的認證標章（視覺證據）：標章的弧形 / 小字不必讀對；只能輔助，不能單獨確認產品。"""

    mark_type: CertificationMarkType
    visually_detected: bool = Field(description="是否從圖形上看得出這是一個標章")
    confidence: float = Field(description="對「這是此種標章」的把握，0 到 1")
    text_readable: bool = Field(description="標章上的文字是否清楚可讀；弧形、過小、模糊時為 false")
    text: str = Field(description="標章上清楚可讀的文字；text_readable 為 false 時給空字串，不可猜測")


class IntakeExtraction(BaseModel):
    """OpenAI Structured Output 直接產生的欄位（不含系統自行填寫的欄位）。"""

    normalized_text: str = Field(description="修正錯字、去除贅字後的繁體中文版本")
    language: str = Field(description="輸入的主要語言，BCP-47，例如 zh-TW")
    intent: Intent
    product_candidates: list[str]
    corrected_product_candidates: list[str] = Field(
        description="product_candidates 中有明顯錯字或語音同音錯誤時，修正後的寫法（與 normalized_text 中的寫法一致）；"
                    "沒有修正就給空陣列"
    )
    brand_candidates: list[str]
    company_candidates: list[str]
    ingredient_candidates: list[str]
    permit_number_candidates: list[str]
    certification_marks: list[str] = Field(
        default_factory=list,
        description="圖片 / 網頁上的認證標章或官方識別文字（例如「健康食品」標章字樣、認證名稱），不是功效宣稱；沒有則空陣列",
    )
    certification_mark_observations: list[CertificationMarkObservation] = Field(
        default_factory=list,
        description="圖片上看到的認證標章（依圖形判斷種類；標章文字看不清楚時 text_readable=false、text 空字串）；沒有則空陣列",
    )
    claims: list[ExtractedClaim]
    other_visible_text: list[str] = Field(
        default_factory=list,
        description="圖片 / 網頁上看得到、但既不是產品識別也不是功效宣稱的文字（容量、規格、口味、價格、促銷、"
                    "與健康無關的標語）；沒有則空陣列",
    )
    question: str = Field(description="使用者真正想問的問題，一句話")
    refers_to_unspecified_product: bool = Field(
        description="使用者用「這個」「它」「這罐」等指稱產品，但本次輸入沒有給出可辨識的產品名稱或許可證字號"
    )
    context_resolved_names: list[str] = Field(
        description="若指稱可以由 known_context_entities 解析，填入對應的實體名稱（必須逐字取自該清單）；否則空陣列"
    )
    mentioned_country_codes: list[str] = Field(
        description="輸入中明確提到的產品來源國家 ISO 3166-1 alpha-2 代碼，沒有則為空陣列"
    )
    needs_product_lookup: bool
    needs_claim_check: bool
    needs_graph_lookup: bool
    needs_rag: bool
    needs_official_web_search: bool
    # intent = interaction_inquiry 時：被比較 / 被問到的對象（名稱逐字取自使用者原文）與問題型態；其他 intent 給空陣列 / none
    interaction_subjects: list[InteractionSubjectExtraction] = Field(
        default_factory=list,
        description="intent = interaction_inquiry 時，被問「能不能一起吃 / 有沒有衝突 / 是不是重複」的每一個對象；"
                    "名稱必須逐字取自使用者原文，不可翻譯或改寫；其他 intent 給空陣列",
    )
    interaction_mode: InteractionMode | None = Field(
        default=None,
        description="pairwise：問兩個以上對象能不能一起吃 / 有沒有衝突；duplicate_comparison：問兩個產品是否都含有同一成分 / 是否重複；"
                    "不是 interaction 問題時給 null",
    )
    context_reference: ContextReference = Field(
        default=ContextReference.NONE,
        description="這句話指的是前文的哪一種對象：CURRENT_PRODUCT＝前文已確認的產品（問它的官方資料，例如成分、登記、功效）；"
                    "PREVIOUS_IMAGE＝上一張圖片 / 包裝本身（問那張圖上印了什麼、標示了什麼，只有 last_visual_request_available 為 true 才可用）；"
                    "NONE＝都不是（獨立的新問題，或自己已給出明確的產品 / 成分名稱）。依語意判斷，不要只看有沒有「這個」等字眼",
    )
    interaction_partner_unspecified: bool = Field(
        default=False,
        description="intent = interaction_inquiry 時：使用者泛指「它跟其他產品 / 別的東西有沒有交互作用」，沒有指定第二個具體的"
                    "成分 / 藥品 / 產品名稱時為 true（此時 interaction_subjects 不要列出代名詞或「其他產品」這類泛稱）；"
                    "有明確指名的對象時為 false",
    )
    interaction_question: InteractionQuestion | None = Field(
        default=None,
        description="interaction_inquiry 且 pairwise 時，使用者實際在問什麼：together_use＝能不能一起吃 / 一起使用；"
                    "interaction_existence＝有沒有交互作用 / 衝突；explanation＝為什麼會影響 / 怎麼影響；"
                    "不是 interaction 問題或是重複成分問題時給 null",
    )


class DetectedProduct(BaseModel):
    """輸入中辨識到的產品身份資訊（品名 / 品牌 / 公司 / 字號 / 標章），不含任何宣稱。"""

    product_names: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    permit_numbers: list[str] = Field(default_factory=list)
    certification_marks: list[str] = Field(default_factory=list)


class ProductMention(BaseModel):
    """一個產品名稱的 provenance：使用者原文 vs Agent A 的修正（只給 resolver 使用，不顯示給使用者）。"""

    extracted_text: str  # Agent A 抽出的寫法
    raw_text: str | None = None  # 使用者輸入中對應的原文片段（找不到時為 None）
    normalized_mention: str = ""  # raw_text 的純格式正規化
    corrected_name: str | None = None  # Agent A 改字後的寫法（只是查詢線索）
    correction_applied: bool = False
    provenance: ProductMentionProvenance
    edit_count: int = 0  # 修正寫法與原文片段的編輯距離（純格式差異為 0）


class NormalizedUserRequest(BaseModel):
    """Agent A 的正式輸出。後續 Agent 只讀這份結果，不再重新解析原始輸入。"""

    request_id: str
    input_type: InputType
    original_text: str
    normalized_text: str
    language: str = "zh-TW"
    country_code: str = "TW"
    intent: Intent
    product_candidates: list[str] = Field(default_factory=list)
    # Agent A 的錯字 / 同音修正：只是查詢線索，必須由使用者原始寫法佐證才能確認產品（見 resolve_product_match）
    corrected_product_candidates: list[str] = Field(default_factory=list)
    # product_candidates 中每個名稱與使用者原文的關係（只有 USER_TEXT 能走 exact；修正寫法只是線索）
    product_mentions: list[ProductMention] = Field(default_factory=list)
    # 整則訊息的名稱同時是 canonical 成分與官方產品名稱：不替使用者決定，請使用者選擇（見 bare_name_routing）
    identity_collision: bool = False
    # 目前這句是獨立的成分 / 營養素詢問（bare-name routing）：不綁定前一題的已確認產品（不改動 context，只是這一題不用）
    standalone_entity: bool = False
    brand_candidates: list[str] = Field(default_factory=list)
    company_candidates: list[str] = Field(default_factory=list)
    ingredient_candidates: list[str] = Field(default_factory=list)
    permit_number_candidates: list[str] = Field(default_factory=list)
    certification_marks: list[str] = Field(default_factory=list)  # 標章上清楚可讀的文字（不是產品識別）
    certification_mark_observations: list[CertificationMarkObservation] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)
    other_visible_text: list[str] = Field(default_factory=list)  # 非產品識別、非宣稱的可見文字（容量 / 規格 / 標語）
    question: str = ""
    refers_to_unspecified_product: bool = False
    product_reference_status: ProductReferenceStatus = ProductReferenceStatus.NOT_NEEDED
    context_entities_used: list[ContextEntity] = Field(default_factory=list)
    mentioned_country_codes: list[str] = Field(default_factory=list)
    needs_product_lookup: bool = False
    needs_claim_check: bool = False
    needs_graph_lookup: bool = False
    needs_rag: bool = False
    needs_official_web_search: bool = False
    source_url: str | None = None
    adapter_notes: list[str] = Field(default_factory=list)
    input_tools_called: list[ToolCallRecord] = Field(default_factory=list)  # Agent A 的 Input Tool 呼叫紀錄
    # URL 輸入：每個網址的來源角色 / provenance；官方頁面上的陳述另存，不進 claims（不是待查證宣稱）
    url_sources: list[UrlSource] = Field(default_factory=list)
    official_source_statements: list[OfficialSourceStatement] = Field(default_factory=list)
    # 圖片輸入：包裝上看到的宣稱先保存為 observed content（不自動查證，可按「查驗包裝宣稱」再查）
    observed_package_claims: list[ExtractedClaim] = Field(default_factory=list)
    image_identification: bool = False  # 只傳圖片：預設目標是辨識產品 / 產品資訊
    package_claim_check: bool = False  # 由「查驗包裝宣稱」postback 建立（已確認產品 + 原圖保存的 observed claims）
    claims_from_package: bool = False  # claims 來自包裝圖片（明確要求查驗、高風險醫療宣稱、或按鈕觸發）
    # 查詢目標由使用者回覆確認（pending clarification → CONFIRM，或之後沿用該產品的 follow-up）
    user_confirmed_target: UserConfirmedTarget | None = None
    # Interaction inquiry：對象（產品名稱只放這裡，不放 product_candidates；identity 由 Agent B 逐一確認）
    interaction_subjects: list[InteractionSubject] = Field(default_factory=list)
    interaction_mode: InteractionMode | None = None
    interaction_question: InteractionQuestion | None = None
    # 「目前產品」的泛指交互作用總覽（沒有第二個對象）；對象由 pipeline 以 task context 中已確認的產品綁定
    interaction_overview: bool = False
    # 文字追問指向前文的哪一種對象（見 ContextReference）；PREVIOUS_IMAGE 時 visual_request_id 是被指的圖片 request
    context_reference: ContextReference = ContextReference.NONE
    visual_request_id: str | None = None
    # 語音輸入：轉錄原文與字形正規化（簡體 → 繁體）後的逐字稿；Controller 與使用者介面使用 normalized_transcript
    raw_transcript: str | None = None
    normalized_transcript: str | None = None
    # Multi-image（同一產品的 2–3 張照片）：融合後的觀察（含每個值的來源圖片）；單張圖片 / 其他輸入為 None
    image_bundle: FusedImageBundle | None = None

    @property
    def image_bundle_blocked(self) -> bool:
        """多張圖片的身份衝突 / 可能不是同一產品：停止產品查證，先請使用者確認。"""
        return self.image_bundle is not None and self.image_bundle.blocks_verification

    @property
    def has_product_identifiers(self) -> bool:
        return bool(
            self.product_candidates
            or self.permit_number_candidates
            or self.brand_candidates
            or self.company_candidates
        )

    @property
    def detected_product(self) -> "DetectedProduct":
        """產品識別欄位（與 claims / other_visible_text 分開）。"""
        return DetectedProduct(
            product_names=list(self.product_candidates), brands=list(self.brand_candidates),
            companies=list(self.company_candidates), permit_numbers=list(self.permit_number_candidates),
            certification_marks=list(self.certification_marks),
        )

    @property
    def url_source_roles(self) -> set[UrlSourceRole]:
        return {u.source_role for u in self.url_sources}

    @property
    def missing_product_context(self) -> bool:
        return self.product_reference_status == ProductReferenceStatus.MISSING_CONTEXT

    @property
    def foreign_country_codes(self) -> list[str]:
        return [code for code in self.mentioned_country_codes if code.upper() != self.country_code]
