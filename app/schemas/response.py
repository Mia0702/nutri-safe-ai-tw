"""Agent C 的輸出契約（與 UI 無關）。

* ResponsePayload：每個 request 唯一的 primary response 內容。
* DetailPayload：使用者按下 on-demand 按鈕後，依同一個 request_id 產生的詳細內容。

Agent C 只輸出這些結構；轉成 LINE Flex Message 由 AdaptiveLineRenderer 負責。
Payload 可以比 UI 顯示的更完整（例如 key_points 超過卡片上限），UI template 不限制 Agent C 的表達內容。
"""

from pydantic import BaseModel, Field, model_validator

from app.schemas.enums import ResponseAction, ResponseType, RiskLevel
from app.schemas.evidence import SourceRef

# 風險識別只用 emoji，不用大面積色塊；只有 response_type=RISK 會使用
RISK_EMOJI = {RiskLevel.LOW: "🟢", RiskLevel.MEDIUM: "🟠", RiskLevel.HIGH: "🔴"}
# 說法查證（CLAIM_RESULT）的宣稱風險標示：針對「這句話若作為產品功效宣稱」，不是產品本身的風險
CLAIM_RISK_LABEL = {
    RiskLevel.LOW: "🟢 官方核准範圍內／符合官方資料",
    RiskLevel.MEDIUM: "🟠 需要更多證據",
    RiskLevel.HIGH: "🔴 高風險宣稱",
}


class ProductChoice(BaseModel):
    """產品確認卡上的一個候選（只來自該 request 的 EvidenceBundle 候選，不由 UI 自行產生）。"""

    product_id: str  # canonical product id（postback candidate_id）
    label: str  # 能區分品項的短名稱 / 規格；品名完全相同時為許可證字號
    full_name: str
    permit_number: str | None = None
    is_active: bool = True
    status_text: str | None = None  # 非有效許可證時的狀態說明


class ResponsePayload(BaseModel):
    request_id: str
    response_type: ResponseType
    risk_level: RiskLevel | None = None  # 只有 RISK 才有；其他型態不硬加風險等級
    # 只有 CLAIM_RESULT 才有：所有命題中最高的 claim_risk（取自 ClaimAssessment，不由 UI 重新推論）
    claim_risk_level: RiskLevel | None = None
    is_conclusive: bool = True
    title: str
    summary: str  # 一句主要結論
    key_points: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)  # 必要警語 / 免責聲明
    available_actions: list[ResponseAction] = Field(default_factory=list)
    source_count: int = 0
    sources: list[SourceRef] = Field(default_factory=list)
    # 產品確認卡：可直接點選的候選（Flex action，屬於這一題）；choice_note 是候選下方的補充說明
    product_choices: list[ProductChoice] = Field(default_factory=list)
    choice_note: str | None = None
    # 圖片辨識到的少量識別資訊（產品名 / 品牌 / 字號 / 公司）：產品尚未確認時讓使用者知道系統看到了什麼
    recognized_content: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _risk_level_only_for_risk(self) -> "ResponsePayload":
        if (self.response_type == ResponseType.RISK) != (self.risk_level is not None):
            raise ValueError("risk_level 必須且只能出現在 response_type=RISK")
        if self.claim_risk_level is not None and self.response_type != ResponseType.CLAIM_RESULT:
            raise ValueError("claim_risk_level 只用於 CLAIM_RESULT")
        if len(set(self.available_actions)) != len(self.available_actions):
            raise ValueError("available_actions 不可重複")
        if self.product_choices and self.response_type != ResponseType.MISSING_INFORMATION:
            raise ValueError("product_choices 只用於產品確認（MISSING_INFORMATION）")
        return self

    def plain_text(self) -> str:
        """純文字版本：只給 debug API / log 使用，不會送到 LINE。"""
        title = f"{RISK_EMOJI[self.risk_level]} {self.title}" if self.risk_level else self.title
        parts = [title, self.summary]
        parts += [f"・{p}" for p in self.key_points]
        parts += self.warnings
        return "\n".join(p for p in parts if p)


class DetailPayload(BaseModel):
    request_id: str
    action: ResponseAction
    title: str
    query_text: str = ""  # 這份內容屬於哪一題（使用者可能已問了下一題才按舊按鈕）
    body: list[str] = Field(default_factory=list)  # 段落；Renderer 決定排版
    available_actions: list[ResponseAction] = Field(default_factory=list)  # 下一層 on-demand 按鈕（同一 request）
    used_fallback_template: bool = False
