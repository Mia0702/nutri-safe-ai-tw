"""Risk Engine 的輸出契約。"""

from pydantic import BaseModel, Field

from app.schemas.enums import (
    ClaimAssessmentStatus,
    EvidenceStrength,
    ProductVerificationStatus,
    RiskLevel,
)


class RiskRuleHit(BaseModel):
    rule_id: str
    level: RiskLevel
    reason: str  # 白話繁中，Reporter 可以直接參考


class RiskAssessment(BaseModel):
    risk_level: RiskLevel
    product_verification: ProductVerificationStatus
    claim_statuses: list[ClaimAssessmentStatus] = Field(default_factory=list)
    evidence_strength: EvidenceStrength
    triggered_rules: list[RiskRuleHit] = Field(default_factory=list)
    required_disclaimers: list[str] = Field(default_factory=list)
    is_conclusive: bool = Field(
        description="False 代表資訊不足，風險等級只是保守預設，Reporter 必須說明不確定性"
    )
    data_incomplete: bool = Field(
        default=False, description="有資料來源查詢失敗，結果可能不完整"
    )
