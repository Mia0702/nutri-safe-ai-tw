"""整條 pipeline 的輸出（debug endpoint 與 LINE 共用）。"""

from pydantic import BaseModel, Field

from app.schemas.evidence import ClaimAssessment, EvidenceBundle, ProductMatch, ToolCallRecord, ToolError
from app.schemas.response import ResponsePayload
from app.schemas.risk import RiskAssessment
from app.schemas.user_request import NormalizedUserRequest


class ReporterOutput(BaseModel):
    """完整分析正文（on-demand「查看完整分析」）；primary response 見 ResponsePayload。"""

    text: str
    used_fallback_template: bool = False
    cited_source_count: int = 0


class AnalysisResult(BaseModel):
    request_id: str
    input_type: str | None = None  # 原始輸入型別；Agent A 失敗時仍可記錄
    normalized_request: NormalizedUserRequest | None = None
    tools_called: list[ToolCallRecord] = Field(default_factory=list)
    product_verification: ProductMatch | None = None
    evidence: EvidenceBundle | None = None
    claim_assessment: list[ClaimAssessment] = Field(default_factory=list)
    risk: RiskAssessment | None = None
    response: ResponsePayload  # Agent C 的 primary response（唯一）；Renderer 由此產生 LINE Flex
    final_answer: str  # response 的純文字版本，只給 debug / log
    errors: list[ToolError] = Field(default_factory=list)
    duration_ms: int = 0
