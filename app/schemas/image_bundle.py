"""Multi-image bundle（同一產品的 2–3 張照片）的契約。

* Vision 一次看完所有圖片，回傳「每張圖片各自的結構化觀察」（`ImageBundleObservation`），不做產品結論。
* Fusion（deterministic，`app/services/image_fusion.py`）把各圖觀察融合成 `FusedImageBundle`：
  每個欄位值都保留來源圖片編號（`observed_in`，1-based）、readable / confidence；
  不一致的欄位以 `ImageFieldConflict` 保留（欄位、各值、各值的來源圖片），不由 LLM 挑一個。
"""

from enum import StrEnum

from pydantic import BaseModel, Field

MAX_BUNDLE_IMAGES = 3
MIN_BUNDLE_IMAGES = 2


class ImageKind(StrEnum):
    FRONT_PACKAGE = "front_package"
    BACK_LABEL = "back_label"
    PERMIT_AREA = "permit_area"
    ADVERTISEMENT = "advertisement"
    OTHER = "other"


class ObservedText(BaseModel):
    """一張圖片中讀到的一段文字（逐字照抄；看不清楚時 readable=false、value 空字串）。"""

    value: str = Field(description="逐字照抄的文字；看不清楚時給空字串，不可猜測")
    readable: bool = Field(description="這段文字是否清楚可讀")
    confidence: float = Field(description="讀對的把握 0~1")


class ObservedProductName(ObservedText):
    is_complete: bool = Field(description="是否為完整的產品名稱（不是被遮住或只拍到一部分的片段）")


class ImageObservation(BaseModel):
    """單張圖片的結構化觀察。image_index 是 1-based，必須對應輸入順序的圖片編號。"""

    image_index: int = Field(description="圖片編號（Image 1 → 1）")
    image_kind: ImageKind
    product_names: list[ObservedProductName] = Field(default_factory=list)
    brands: list[ObservedText] = Field(default_factory=list)
    companies: list[ObservedText] = Field(default_factory=list)
    permit_numbers: list[ObservedText] = Field(default_factory=list)
    ingredients: list[ObservedText] = Field(default_factory=list, description="成分表上的每一個成分名稱")
    claims: list[ObservedText] = Field(default_factory=list, description="包裝上的功效 / 健康宣稱原句（不是品名、規格）")
    warnings: list[ObservedText] = Field(default_factory=list)
    certification_marks: list[ObservedText] = Field(default_factory=list)
    other_visible_text: list[ObservedText] = Field(default_factory=list)


class ImageBundleObservation(BaseModel):
    """Vision 一次讀完整個 bundle 的結果（只有觀察，沒有產品結論）。"""

    images: list[ImageObservation]


# ------------------------------------------------------------------ fusion 結果


class FusedValue(BaseModel):
    """融合後的一個欄位值：保留出處圖片與可讀性。"""

    value: str
    observed_in: list[int]  # 來源圖片編號（1-based，已排序）
    readable: bool = True
    confidence: float = 0.0
    is_complete: bool | None = None  # 只有產品名稱使用


class ImageConflictCode(StrEnum):
    MULTI_IMAGE_IDENTITY_CONFLICT = "MULTI_IMAGE_IDENTITY_CONFLICT"  # 不同的許可證字號 → 停止
    SAME_PRODUCT_UNCERTAIN = "SAME_PRODUCT_UNCERTAIN"  # 明確不同的完整品名且沒有共同佐證 → 請使用者確認
    OCR_UNCERTAIN = "OCR_UNCERTAIN"  # 非關鍵的辨識差異（錯字層級）→ 依既有證據優先序繼續
    FIELD_VALUE_CONFLICT = "FIELD_VALUE_CONFLICT"  # 其他欄位值不一致（品牌 / 公司 / 有共同佐證的品名差異）


class ImageFieldConflict(BaseModel):
    field: str  # product_name / permit_number / brand / company
    code: ImageConflictCode
    values: list[FusedValue]  # 各個衝突的值與它們的來源圖片
    identity_critical: bool = False


class ImageBundleStatus(StrEnum):
    FUSED = "FUSED"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"  # 許可證字號衝突：停止產品查證
    SAME_PRODUCT_UNCERTAIN = "SAME_PRODUCT_UNCERTAIN"  # 可能不是同一個產品：先請使用者確認
    NO_READABLE_CONTENT = "NO_READABLE_CONTENT"


STOP_STATUSES = frozenset({ImageBundleStatus.IDENTITY_CONFLICT, ImageBundleStatus.SAME_PRODUCT_UNCERTAIN})


class ImageBundleMetrics(BaseModel):
    """延遲紀錄（毫秒）：multi-image 只增加 perception 成本。"""

    image_count: int = 0
    vision_bundle_ms: int | None = None
    semantic_ms: int | None = None
    total_ms: int | None = None  # Agent A 從開始到結束（pipeline 整體見 analysis_done.duration_ms）


class FusedImageBundle(BaseModel):
    """融合後的 bundle 觀察。identity 欄位是「觀察」，是否確認產品仍由 Agent B 的 resolver 決定。"""

    status: ImageBundleStatus = ImageBundleStatus.FUSED
    image_count: int
    image_kinds: dict[int, ImageKind] = Field(default_factory=dict)
    image_source_ids: dict[int, str] = Field(default_factory=dict)  # 圖片編號 → 來源識別碼（如 LINE message id）
    unreadable_images: list[int] = Field(default_factory=list)
    product_names: list[FusedValue] = Field(default_factory=list)
    brands: list[FusedValue] = Field(default_factory=list)
    companies: list[FusedValue] = Field(default_factory=list)
    permit_numbers: list[FusedValue] = Field(default_factory=list)
    ingredients: list[FusedValue] = Field(default_factory=list)
    claims: list[FusedValue] = Field(default_factory=list)
    warnings: list[FusedValue] = Field(default_factory=list)
    certification_marks: list[FusedValue] = Field(default_factory=list)
    other_visible_text: list[FusedValue] = Field(default_factory=list)
    conflicts: list[ImageFieldConflict] = Field(default_factory=list)
    metrics: ImageBundleMetrics = Field(default_factory=ImageBundleMetrics)

    @property
    def blocks_verification(self) -> bool:
        return self.status in STOP_STATUSES

    @property
    def ocr_uncertain(self) -> bool:
        return any(c.code == ImageConflictCode.OCR_UNCERTAIN for c in self.conflicts)

    def sources_of(self, values: list[FusedValue], text: str) -> list[int]:
        """某段文字（例如 Agent A 抽出的宣稱）對應的來源圖片：正規化後互相包含即視為同一段。"""
        from app.normalization import normalize_name

        key = normalize_name(text)
        if not key:
            return []
        out: set[int] = set()
        for v in values:
            vk = normalize_name(v.value)
            if vk and (vk in key or key in vk):
                out.update(v.observed_in)
        return sorted(out)
