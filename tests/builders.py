"""TEST_ONLY — 測試用 schema 建構 helper。"""

from app.schemas.enums import ClaimOrigin, ClaimSemanticCategory, InputType, Intent, ProductReferenceStatus
from app.schemas.user_request import ExtractedClaim, IntakeExtraction, NormalizedUserRequest


def claim(
    text: str = "TEST_ONLY 宣稱",
    category: ClaimSemanticCategory = ClaimSemanticCategory.HEALTH_FUNCTION,
    origin: ClaimOrigin = ClaimOrigin.USER_QUESTION,
    absolute: bool = False,
    condition: str | None = None,
    function: str | None = None,
) -> ExtractedClaim:
    return ExtractedClaim(
        claim_text=text, origin=origin, semantic_category=category,
        target_condition=condition, target_function=function, uses_absolute_language=absolute,
    )


def request(**overrides) -> NormalizedUserRequest:
    base = dict(
        request_id="req-test", input_type=InputType.TEXT, original_text="TEST_ONLY",
        normalized_text="TEST_ONLY", intent=Intent.PRODUCT_VERIFICATION,
    )
    base.update(overrides)
    req = NormalizedUserRequest(**base)
    # 與 Agent A 的一致化規則相同：有實體就需要查
    if req.has_product_identifiers:
        req.needs_product_lookup = True
    if req.claims:
        req.needs_claim_check = True
    if "product_reference_status" not in overrides:
        if req.has_product_identifiers:
            req.product_reference_status = ProductReferenceStatus.EXPLICIT
        elif req.refers_to_unspecified_product:
            req.product_reference_status = ProductReferenceStatus.MISSING_CONTEXT
    return req


def extraction(**overrides) -> IntakeExtraction:
    base = dict(
        normalized_text="TEST_ONLY", language="zh-TW", intent=Intent.PRODUCT_VERIFICATION,
        product_candidates=[], corrected_product_candidates=[], brand_candidates=[], company_candidates=[], ingredient_candidates=[],
        permit_number_candidates=[], claims=[], question="TEST_ONLY 問題", refers_to_unspecified_product=False,
        context_resolved_names=[],
        mentioned_country_codes=[], needs_product_lookup=False, needs_claim_check=False,
        needs_graph_lookup=False, needs_rag=False, needs_official_web_search=False,
    )
    base.update(overrides)
    return IntakeExtraction(**base)


def user_text(ex: IntakeExtraction, base: str = "TEST_ONLY 問題") -> str:
    """Fake Agent A 抽出的產品名稱必須真的出現在使用者輸入中（與真實 Agent A 相同）：把名稱放進測試輸入文字。"""
    names = [n for n in ex.product_candidates + ex.permit_number_candidates if n]
    return f"{base} {' '.join(names)}".strip() if names else base
