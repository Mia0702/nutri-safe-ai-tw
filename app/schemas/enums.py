"""全系統共用 enum。

這些 enum 是 Agent、Tool、Risk Engine 之間的契約，修改時需同步更新測試。
"""

from enum import StrEnum


class InputType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    URL = "url"
    MIXED = "mixed"


class Intent(StrEnum):
    PRODUCT_VERIFICATION = "product_verification"  # 這個產品有沒有官方登記
    CLAIM_VERIFICATION = "claim_verification"  # 某個功效說法是真的嗎
    EFFICACY_INQUIRY = "efficacy_inquiry"  # 這個產品 / 成分可以補什麼
    INGREDIENT_INQUIRY = "ingredient_inquiry"  # 成分相關問題
    SAFETY_INQUIRY = "safety_inquiry"  # 警語、注意事項、能不能吃
    GENERAL_QUESTION = "general_question"  # 其他保健食品一般問題
    OUT_OF_SCOPE = "out_of_scope"  # 與保健食品無關
    UNCLEAR = "unclear"


class ClaimOrigin(StrEnum):
    """Statement role：這句話是誰、以什麼角色說的。

    只有 PRODUCT_LABEL_CLAIM / ADVERTISEMENT_CLAIM 代表「產品方確實做出的宣稱」；
    USER_QUESTION 只是需要查證的命題，不代表產品或廣告真的這樣說。
    """

    USER_QUESTION = "USER_QUESTION"  # 使用者在問「可以…嗎？」
    USER_ASSERTION = "USER_ASSERTION"  # 使用者自己的陳述 / 認知
    PRODUCT_LABEL_CLAIM = "PRODUCT_LABEL_CLAIM"  # 包裝、標示上的文字
    ADVERTISEMENT_CLAIM = "ADVERTISEMENT_CLAIM"  # 廣告、商品頁、業配、銷售話術
    THIRD_PARTY_CLAIM = "THIRD_PARTY_CLAIM"  # 親友轉述、轉傳訊息、非產品方的貼文
    OFFICIAL_CLAIM = "OFFICIAL_CLAIM"  # 引述官方資料的內容


PRODUCT_SIDE_ORIGINS = frozenset({ClaimOrigin.PRODUCT_LABEL_CLAIM, ClaimOrigin.ADVERTISEMENT_CLAIM})
ASSERTED_ORIGINS = frozenset({
    ClaimOrigin.USER_ASSERTION,
    ClaimOrigin.PRODUCT_LABEL_CLAIM,
    ClaimOrigin.ADVERTISEMENT_CLAIM,
    ClaimOrigin.THIRD_PARTY_CLAIM,
    ClaimOrigin.OFFICIAL_CLAIM,
})


class UrlSourceRole(StrEnum):
    """使用者提供的網址屬於什麼來源角色（只看網域 allowlist，不看頁面怎麼自稱）。"""

    OFFICIAL_REGULATORY_SOURCE = "OFFICIAL_REGULATORY_SOURCE"  # 台灣官方監管網域：頁面內容可作為官方證據
    NON_OFFICIAL_EXTERNAL_SOURCE = "NON_OFFICIAL_EXTERNAL_SOURCE"  # 商城、廣告、新聞、社群：內容是待查證的外部宣稱
    UNKNOWN_EXTERNAL_SOURCE = "UNKNOWN_EXTERNAL_SOURCE"  # 無法可靠判斷（IP、非 allowlist 的政府網域等）：保守，不當官方


class ProductConfirmationSource(StrEnum):
    """task context 中「目前產品」是怎麼被確認的。"""

    USER_SELECTED = "USER_SELECTED"  # 使用者在確認卡上選定（文字 / 圖片 / 字號候選皆同）
    RESOLVER_VERIFIED = "RESOLVER_VERIFIED"  # 該題 deterministic resolver 判定 VERIFIED
    # 使用者回覆「是 / 對」確認系統詢問的候選就是他要查的產品：只代表目標已由使用者確認，
    # 不代表名稱 / 證據衝突已被官方查證解決
    USER_CONFIRMED_TARGET = "USER_CONFIRMED_TARGET"


class PendingClarificationType(StrEnum):
    """上一題系統正在等使用者回答的產品確認問題。"""

    PRODUCT_CANDIDATE_CONFIRMATION = "PRODUCT_CANDIDATE_CONFIRMATION"  # 只有一個低信心候選：是不是這一項
    PRODUCT_IDENTITY_CONFLICT = "PRODUCT_IDENTITY_CONFLICT"  # 字號對應的官方品名與看到 / 提供的品名不一致


class DialogueAct(StrEnum):
    """有 pending clarification 時，使用者下一句話的對話行為（deterministic 判定）。"""

    CONFIRM = "CONFIRM"
    REJECT = "REJECT"
    NEW_ENTITY = "NEW_ENTITY"
    OTHER = "OTHER"


class ClaimContentSource(StrEnum):
    """命題出自本次輸入的哪一段內容（Agent A 標記）。"""

    USER_TEXT = "user_text"  # 使用者自己打字 / 說的話
    IMAGE_CONTENT = "image_content"  # 圖片讀出的文字
    URL_CONTENT = "url_content"  # 使用者提供網址的頁面內容


class ProductReferenceStatus(StrEnum):
    """使用者指的是哪一項產品。"""

    EXPLICIT = "EXPLICIT"  # 輸入中有產品名稱 / 品牌 / 字號
    RESOLVED_FROM_CONTEXT = "RESOLVED_FROM_CONTEXT"  # 「它」「這個」由對話前文解析
    MISSING_CONTEXT = "MISSING_CONTEXT"  # 有指稱產品，但無法解析是哪一項
    NOT_NEEDED = "NOT_NEEDED"  # 問題本身不涉及特定產品


class ClaimSemanticCategory(StrEnum):
    """Agent A 對宣稱做的語意分類（不是風險結論）。"""

    HEALTH_FUNCTION = "health_function"  # 保健功效，例如「有助於調節血脂」
    NUTRIENT_SUPPLEMENT = "nutrient_supplement"  # 補充營養素
    MEDICAL_EFFICACY = "medical_efficacy"  # 治療、治癒、預防、改善、減輕疾病或症狀
    EXAGGERATED_EFFICACY = "exaggerated_efficacy"  # 保證、立即、百分之百等誇大
    GENERAL_WELLNESS = "general_wellness"  # 模糊的「對身體好」
    OTHER = "other"


class MedicalAssertionType(StrEnum):
    """Agent A 對一則宣稱「醫療語意」的細分（語意抽取，不是風險結論）。

    依健康食品管理法第 2 條：保健功效包含「減少疾病危害風險」，但不包含「治療、矯正人類疾病之醫療效能」；
    第 4 條第 1 款另允許營養素相關疾病的預防 / 改善表達。因此「明確治療類」與「疾病預防 / 改善類」要分開：
    前者不可能是核准功效；後者必須先與該產品官方核准功效比對，不能只因字面直接判定。
    """

    NONE = "none"  # 沒有疾病 / 醫療語意（生理機能保健、營養補充、一般好處）
    TREATS_OR_CURES_DISEASE = "treats_or_cures_disease"  # 治療、治癒、根治、矯正疾病
    DIAGNOSES_DISEASE = "diagnoses_disease"  # 診斷 / 檢測疾病
    REPLACES_MEDICAL_CARE = "replaces_medical_care"  # 取代藥物 / 醫療、可以停藥
    PREVENTS_OR_IMPROVES_DISEASE = "prevents_or_improves_disease"  # 預防、改善、減輕「特定疾病或症狀」


EXPLICIT_MEDICAL_ASSERTIONS = frozenset({
    MedicalAssertionType.TREATS_OR_CURES_DISEASE,
    MedicalAssertionType.DIAGNOSES_DISEASE,
    MedicalAssertionType.REPLACES_MEDICAL_CARE,
})


class ClaimPolicyResult(StrEnum):
    """單一宣稱的 deterministic claim-policy 判定（由規則決定，不由 LLM 決定）。"""

    APPROVED_MATCH = "APPROVED_MATCH"  # 與該產品官方核准功效文字相同或語意等價
    APPROVED_SCOPE_CONSISTENT = "APPROVED_SCOPE_CONSISTENT"  # 核准功效範圍內、較保守的表達
    EXCEEDS_APPROVED_SCOPE = "EXCEEDS_APPROVED_SCOPE"  # 比核准功效更廣 / 更強，或加上官方未支持的效果
    MEDICAL_TREATMENT_CLAIM = "MEDICAL_TREATMENT_CLAIM"  # 疾病治療 / 預防 / 診斷 / 取代醫療等醫療效能
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"  # 有可對照的官方資料，但沒有任何官方資料支持
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # 無法判定（產品未確認、比對未完成、來源失敗），不猜


class ClaimPolicyType(StrEnum):
    """claim-policy 判定所依據的政策 / 證據類型。"""

    PRODUCT_SPECIFIC_APPROVED_CLAIM = "PRODUCT_SPECIFIC_APPROVED_CLAIM"  # 該產品官方核准功效
    STATUTORY_APPROVED_SCOPE_LIMIT = "STATUTORY_APPROVED_SCOPE_LIMIT"  # 宣稱不得超過許可範圍（健康食品管理法 §14 I）
    STATUTORY_MEDICAL_EFFICACY_PROHIBITION = "STATUTORY_MEDICAL_EFFICACY_PROHIBITION"  # 不得涉及醫療效能（§2、§14 II）
    NO_POLICY_EVIDENCE = "NO_POLICY_EVIDENCE"  # 沒有可用的官方政策 / 核准資料


class ProductVerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ProductMatchType(StrEnum):
    """Deterministic product resolver 採用的比對依據（由強到弱）。"""

    PERMIT_NUMBER = "PERMIT_NUMBER"  # 使用者提供的許可證字號
    CANONICAL_EXACT = "CANONICAL_EXACT"  # 正規化後與官方品名完全相同
    OFFICIAL_ALIAS_EXACT = "OFFICIAL_ALIAS_EXACT"  # 與官方 / 人工審核的產品別名完全相同
    BASE_NAME_EXACT = "BASE_NAME_EXACT"  # 與官方品名去掉尾端括號規格（口味、包裝）後相同
    # Agent A 的錯字 / 同音修正寫法與官方品名相同，且使用者原始寫法也唯一指向同一產品
    CORRECTED_NAME_CORROBORATED = "CORRECTED_NAME_CORROBORATED"
    # 名稱相似的候選中，只有一項同時對應到本次看到的申請商 / 品牌（兩個獨立訊號一致）
    NAME_COMPANY_CORROBORATED = "NAME_COMPANY_CORROBORATED"
    NAME_SIMILARITY = "NAME_SIMILARITY"  # 只有名稱相似度
    COMPANY_ONLY = "COMPANY_ONLY"  # 只對應到申請商 / 推測品牌


class ProductIdentityFlag(StrEnum):
    """產品已由較強證據確認，但較弱的圖片文字證據與官方紀錄有差異（保留差異，不單獨改變產品判定）。"""

    IMAGE_TEXT_CONFLICT = "IMAGE_TEXT_CONFLICT"  # 圖片讀到的品名與字號對應的官方品名明顯不同
    OCR_UNCERTAIN = "OCR_UNCERTAIN"  # 圖片讀到的品名與官方品名有差異，可能是辨識誤差


class CertificationMarkType(StrEnum):
    """圖片上看到的標章種類（視覺證據；只能輔助，不能單獨確認產品）。"""

    HEALTH_FOOD_MARK = "health_food_mark"  # 健康食品標章
    OTHER_CERTIFICATION = "other_certification"  # 其他認證 / 驗證標章
    UNKNOWN = "unknown"  # 看得出是標章，但無法判斷種類


class ProductAmbiguityReason(StrEnum):
    """產品無法唯一確定的原因：有兩個以上真正合理的候選時不猜。"""

    MULTIPLE_EXACT_NAMES = "MULTIPLE_EXACT_NAMES"  # 多筆官方紀錄品名完全相同
    MULTIPLE_VARIANTS = "MULTIPLE_VARIANTS"  # 同一基本品名有多個規格 / 口味品項
    MULTIPLE_SIMILAR_NAMES = "MULTIPLE_SIMILAR_NAMES"  # 多個候選分數接近
    LOW_MATCH_CONFIDENCE = "LOW_MATCH_CONFIDENCE"  # 只有相似名稱，分數不足以確認


class ClaimAssessmentStatus(StrEnum):
    CONSISTENT_WITH_OFFICIAL = "CONSISTENT_WITH_OFFICIAL"
    UNSUPPORTED = "UNSUPPORTED"
    POTENTIALLY_EXAGGERATED = "POTENTIALLY_EXAGGERATED"
    MEDICAL_TREATMENT_CLAIM = "MEDICAL_TREATMENT_CLAIM"
    CONFLICT_WITH_OFFICIAL = "CONFLICT_WITH_OFFICIAL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ClaimEvidenceRelation(StrEnum):
    """宣稱與某一筆官方參考資料之間的語意關係（由 LLM 比對、由規則使用）。"""

    EQUIVALENT = "equivalent"  # 與官方資料文字相同或語意等價（同一效果、同一範圍）
    SUPPORTED = "supported"  # 官方資料涵蓋此宣稱（範圍內、沒有說得更強）
    EXCEEDS = "exceeds"  # 宣稱超出官方核准範圍 / 說得更誇張
    CONTRADICTS = "contradicts"  # 與官方資料矛盾
    UNRELATED = "unrelated"


class EvidenceScope(StrEnum):
    """一筆證據能說明到什麼範圍。"""

    PRODUCT_SPECIFIC = "PRODUCT_SPECIFIC"  # 針對這項產品
    INGREDIENT_LEVEL = "INGREDIENT_LEVEL"  # 針對成分
    CLAIM_REGULATION = "CLAIM_REGULATION"  # 與此類宣稱相關的法規 / 指引
    GENERAL_BACKGROUND = "GENERAL_BACKGROUND"  # 一般背景，與問題只有間接關係
    UNRELATED = "UNRELATED"


class EntityType(StrEnum):
    PRODUCT = "PRODUCT"
    BRAND = "BRAND"
    COMPANY = "COMPANY"
    INGREDIENT = "INGREDIENT"


class AliasType(StrEnum):
    OFFICIAL_NAME = "OFFICIAL_NAME"
    ENGLISH_NAME = "ENGLISH_NAME"
    SHORT_NAME = "SHORT_NAME"  # 去除公司組織型態等
    BRAND_NAME = "BRAND_NAME"
    DERIVED_BRAND_PREFIX = "DERIVED_BRAND_PREFIX"  # 由同公司產品名稱共同前綴推導
    COMMON_NAME = "COMMON_NAME"
    FORMER_NAME = "FORMER_NAME"


class EvidenceStatus(StrEnum):
    """一筆品牌 / 別名 / 關聯的確認程度。"""

    OFFICIAL_RECORD = "OFFICIAL_RECORD"  # 官方資料原文直接記載
    INFERRED = "INFERRED"  # 系統依規則推測，不是官方確認
    MANUAL_REVIEWED = "MANUAL_REVIEWED"  # 經人工審核


class BrandCompanyRelation(StrEnum):
    """申請商與品牌之間的關係語意（= brands.company_relation = Neo4j 關係類型）。"""

    OWNS_BRAND = "OWNS_BRAND"  # 擁有品牌：只限官方紀錄 / 人工審核，且有 ownership evidence
    ASSOCIATED_WITH_BRAND = "ASSOCIATED_WITH_BRAND"  # 僅知有關聯（例如由產品名稱推導），不代表擁有


class EvidenceStrength(StrEnum):
    STRONG = "STRONG"
    MEDIUM = "MEDIUM"
    WEAK = "WEAK"
    NONE = "NONE"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SourceType(StrEnum):
    STRUCTURED_DB = "structured_db"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    RAG_DOCUMENT = "rag_document"
    OFFICIAL_WEB = "official_web"
    USER_PROVIDED = "user_provided"
    DERIVED_INFERENCE = "derived_inference"  # 系統依規則推測（例如由產品名稱推導的品牌），非官方確認


class ProductStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    MERGED = "MERGED"
    CANCELLED = "CANCELLED"
    REVOKED = "REVOKED"
    UNKNOWN = "UNKNOWN"


class ErrorCode(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    SCHEMA_NOT_INITIALIZED = "SCHEMA_NOT_INITIALIZED"
    INVALID_INPUT = "INVALID_INPUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    UNSUPPORTED_INPUT_TYPE = "UNSUPPORTED_INPUT_TYPE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ResponseType(StrEnum):
    """Agent C 的回覆型態（決定 Renderer 使用的 template）；只有 RISK 會顯示風險等級。"""

    RISK = "RISK"
    PRODUCT_INFO = "PRODUCT_INFO"
    INGREDIENT_LIST = "INGREDIENT_LIST"
    CLAIM_RESULT = "CLAIM_RESULT"
    SOURCE_SUMMARY = "SOURCE_SUMMARY"
    MISSING_INFORMATION = "MISSING_INFORMATION"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    ERROR = "ERROR"


class ResponseAction(StrEnum):
    """Primary response 上的 on-demand 按鈕；postback 一律帶 request_id（見 app/response/actions.py）。"""

    DETAIL = "detail"  # 查看完整分析
    SOURCES = "sources"  # 查看資料來源
    INGREDIENTS = "ingredients"  # 查看完整成分
    OFFICIAL_INFO = "official_info"  # 查看官方資訊
    WHY = "why"  # 為什麼？
    RETRY = "retry"  # 重新查詢
    SELECT_PRODUCT = "select_product"  # 從產品確認卡選擇候選（postback 另帶 candidate_id）
    FULL_EFFICACY = "efficacy"  # 官方資訊卡 → 完整核准功效原文
    WARNINGS = "warnings"  # 官方資訊卡 → 警語與注意事項
    CHECK_PACKAGE_CLAIMS = "check_claims"  # 查驗圖片中觀察到的包裝宣稱（使用原 request 保存的內容）
