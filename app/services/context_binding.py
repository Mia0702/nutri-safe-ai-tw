"""Confirmed-product context binding（Agent A 之後、Agent B 之前；deterministic，不呼叫 LLM）。

task context 中已確認的 canonical 產品（使用者選定或 resolver VERIFIED）是後續指稱的 authoritative referent：
下一題沒有明確提出新的產品名稱 / 許可證字號、且是產品相關的 follow-up 時，
把該產品的官方品名與許可證字號放進 NormalizedUserRequest，Agent B 既有的 resolver 會以 PERMIT_NUMBER 確認，
不再用模糊的基本品名重新比對。

一般化規則（不針對任何產品或問句）：
* 「明確提出」= 產品名稱 / 許可證字號真的出現在本次訊息內容（original_text）中。
  Agent A 從前文帶回的名稱（包括前面問句中的基本品名）不算明確提出。
* 明確提出新的產品 / 字號 → 不綁定，新的 entity 優先。
  例外：本次提到的名稱全都包含在已確認產品的官方品名中（例如只寫基本品名），代表仍是同一項產品，照樣綁定；
  任何其他名稱或字號都視為新的產品。
* 綁定條件：有尚未過期的 confirmed product，且
  (a) 本次訊息用代名詞指稱產品（refers_to_unspecified_product / MISSING_CONTEXT / RESOLVED_FROM_CONTEXT），或
  (b) 產品相關 intent（產品 / 成分 / 功效 / 安全性 / 說法查證）且本次訊息沒有任何實體（避免把一般成分問題綁到產品上）。
"""

import logging

from app.normalization import extract_permit_code, normalize_name
from app.schemas.enums import EntityType, Intent, ProductConfirmationSource, ProductReferenceStatus
from app.schemas.user_request import ConfirmedProduct, ContextEntity, NormalizedUserRequest, UserConfirmedTarget

logger = logging.getLogger("nutri_safe_ai.context_binding")

PRODUCT_FOLLOW_UP_INTENTS = frozenset({
    Intent.PRODUCT_VERIFICATION, Intent.INGREDIENT_INQUIRY, Intent.EFFICACY_INQUIRY,
    Intent.SAFETY_INQUIRY, Intent.CLAIM_VERIFICATION,
})
_REFERENCE_STATUSES = frozenset({ProductReferenceStatus.MISSING_CONTEXT, ProductReferenceStatus.RESOLVED_FROM_CONTEXT})
BOUND_NOTE = "產品指稱由 task context 中已確認的產品解析"


def _mentioned(names: list[str], text_norm: str) -> list[str]:
    return [n for n in names if normalize_name(n) and normalize_name(n) in text_norm]


def explicit_product_identifiers(request: NormalizedUserRequest) -> tuple[list[str], list[str]]:
    """本次訊息內容中真的出現的產品名稱與許可證字號（Agent A 從前文帶回的不算）。"""
    text_norm = normalize_name(request.original_text)
    raw_text = request.original_text or ""
    permits = [p for p in request.permit_number_candidates
               if (code := extract_permit_code(p)) and (code in raw_text or normalize_name(code) in text_norm)]
    return _mentioned(request.product_candidates, text_norm), permits


def bind_confirmed_product(request: NormalizedUserRequest, confirmed: ConfirmedProduct | None) -> NormalizedUserRequest:
    if confirmed is None or request.intent in (Intent.OUT_OF_SCOPE, Intent.UNCLEAR) or request.identity_collision or request.standalone_entity:
        return request
    products, permits = explicit_product_identifiers(request)
    official_norm = normalize_name(confirmed.official_product_name)
    same_product = bool(products) and not permits and all(normalize_name(p) in official_norm for p in products)
    if (products or permits) and not same_product:
        return request  # 明確提出的新產品 / 字號優先於 context
    text_norm = normalize_name(request.original_text)
    pronoun = request.refers_to_unspecified_product or request.product_reference_status in _REFERENCE_STATUSES
    no_entities = not (_mentioned(request.ingredient_candidates + request.brand_candidates
                                  + request.company_candidates, text_norm))
    implicit = request.intent in PRODUCT_FOLLOW_UP_INTENTS and no_entities
    if not (pronoun or implicit or same_product):
        return request
    entity = ContextEntity(entity_type=EntityType.PRODUCT, name=confirmed.official_product_name,
                           entity_id=confirmed.product_id)
    bound = request.model_copy(deep=True, update={
        "product_candidates": [confirmed.official_product_name],
        "corrected_product_candidates": [],
        "permit_number_candidates": [confirmed.permit_number] if confirmed.permit_number else [],
        # 只保留本次訊息真的提到的品牌 / 公司（前文帶回的模糊名稱不再參與比對）
        "brand_candidates": _mentioned(request.brand_candidates, text_norm),
        "company_candidates": _mentioned(request.company_candidates, text_norm),
        "product_reference_status": ProductReferenceStatus.RESOLVED_FROM_CONTEXT,
        "context_entities_used": [entity],
        "needs_product_lookup": True,
        "adapter_notes": request.adapter_notes + [BOUND_NOTE],
        # 由使用者確認的目標：後續 follow-up 仍帶著未解決的差異（不因沿用而變成官方確認）
        "user_confirmed_target": UserConfirmedTarget(
            product_id=confirmed.product_id, official_product_name=confirmed.official_product_name,
            permit_number=confirmed.permit_number, discrepancy=confirmed.discrepancy)
        if confirmed.confirmation_source == ProductConfirmationSource.USER_CONFIRMED_TARGET else None,
    })
    logger.info("context_bound", extra={
        "request_id": request.request_id, "stage": "confirmed_product_bound",
        "selected_product_id": confirmed.product_id, "match_type": confirmed.confirmation_source.value})
    return bound


def is_bound_from_context(request: NormalizedUserRequest) -> bool:
    return BOUND_NOTE in request.adapter_notes
