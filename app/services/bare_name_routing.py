"""Bare-name routing（Controller Agent 之後、context 綁定之前；deterministic，不呼叫 LLM）。

在沿用前一題已確認產品之前，先看「這一句原文」是不是獨立的成分 / 營養素詢問。判斷只依原文與參考資料，
不依賴 Controller 預測的 intent / 產品欄位（Controller 可能因為前文實體而把裸名稱解析成前一題產品）。

* 整句原文就是唯一的 canonical ingredient entity 名稱（Ingredient Bridge 正式名稱 / 正式 alias），
  且沒有劑型、產品字樣 → STANDALONE_ENTITY：這一題不綁定前文產品（不改動 context），改為獨立成分詢問。
* 原文含「有什麼功能」等問句，但 Controller 放進 product_candidates 的名稱全是 canonical entity、
  且沒有任何產品訊號（品牌 / 公司 / 字號 / 劑型 / 產品字樣 / 代名詞）→ 同樣是獨立成分詢問。
* 名稱同時是 canonical entity 又完全等於某項官方產品名稱（或官方別名）→ identity collision，請使用者選擇。
  只有基本品名相同（尾端括號規格）不算 collision，不能蓋過 canonical entity exact。
* 代名詞 follow-up（「它有含 X 嗎？」）原文不是裸名稱，不受影響，照舊沿用前一題產品。
* 其他情形、查詢失敗 → 不改動。
一般化規則：不含任何成分、品牌或產品名稱。
"""

import logging
from typing import Any

from app.normalization import normalize_name
from app.risk.candidate_relevance import DISTINCTIVE, FORM_TERMS, NUTRIENT, char_classes
from app.schemas.enums import InputType, IngredientQueryResolution, Intent, ProductReferenceStatus
from app.schemas.evidence import OFFICIAL_ALIAS_MATCHED_BY
from app.schemas.user_request import NormalizedUserRequest
from app.tools.ingredient_tools import IngredientReference
from app.tools.product_tools import identity_generic_terms, lookup_product

logger = logging.getLogger("nutri_safe_ai.bare_name_routing")

STANDALONE_INGREDIENT_NOTE = "名稱是 canonical 成分 / 營養素、沒有產品訊號，視為獨立的成分詢問（不沿用前一題的產品）"
COLLISION_NOTE = "名稱同時是成分名稱與官方產品名稱，請使用者選擇"
# 產品字樣（一般性語言規則）：出現就不是「只有成分名稱」
_PRODUCT_WORDS = ("產品", "這款", "牌", "product")
_PRODUCT_LEANING_INTENTS = (Intent.UNCLEAR, Intent.PRODUCT_VERIFICATION, Intent.GENERAL_QUESTION)  # 重分類後改為成分詢問
_REROUTABLE_INTENTS = (*_PRODUCT_LEANING_INTENTS, Intent.INGREDIENT_INQUIRY, Intent.EFFICACY_INQUIRY, Intent.SAFETY_INQUIRY)
_EDGE_PUNCT = " \t\r\n。.!！?？~～、，,；;：:「」『』\"'()（）"


def _has_product_wording(text: str) -> bool:
    lowered = (text or "").lower()
    return any(t.lower() in lowered for t in (*FORM_TERMS, *_PRODUCT_WORDS))


def _bare_text(request: NormalizedUserRequest) -> str | None:
    """整句原文（去掉頭尾標點）就是一個名稱：只看原文，不看 Controller 的 intent / 產品欄位。"""
    if request.input_type not in (InputType.TEXT, InputType.AUDIO) or request.url_sources or request.image_identification:
        return None
    text = (request.original_text or "").strip(_EDGE_PUNCT)
    if not normalize_name(text) or _has_product_wording(text) or len(text) > 40:
        return None
    return text


def _controller_named_ingredients(request: NormalizedUserRequest) -> list[str]:
    """原文是問句時：Controller 放在 product_candidates、且確實出現在原文中的名稱（沒有任何產品訊號才採用）。"""
    if (request.intent not in _REROUTABLE_INTENTS or request.claims or request.url_sources or request.image_identification
            or request.brand_candidates or request.company_candidates or request.permit_number_candidates
            or request.refers_to_unspecified_product or request.context_entities_used or request.certification_marks
            or request.input_type not in (InputType.TEXT, InputType.AUDIO) or not request.product_candidates):
        return []
    text = normalize_name(request.original_text)
    if not text or any(normalize_name(p) not in text for p in request.product_candidates):
        return []  # 名稱不在使用者輸入中（例如前文帶回）：不是這則訊息的名稱
    if _has_product_wording(request.original_text):
        return []
    return list(request.product_candidates)


def _is_generic_nutrient_wording(text: str, repo: Any) -> bool:
    """整句全是一般成分 / 營養素用語（Candidate Relevance Gate 的同一套字元分類），沒有任何有辨識力的字樣。
    這種裸名稱只因為產品名稱含有相同字樣，不能被當成產品查詢（品牌 / 品名特有字樣的錯字仍走產品候選確認）。"""
    classes = char_classes(normalize_name(text), identity_generic_terms(repo))
    return bool(classes) and DISTINCTIVE not in classes and NUTRIENT in classes


def _is_exact_official_product(repo: Any, name: str) -> bool:
    """完全等於官方產品名稱或官方別名（基本品名相同不算）。"""
    key = normalize_name(name)
    for m in lookup_product(repo, [name]).candidates:
        if key == normalize_name(m.product.product_name_zh) or (
                m.matched_by == OFFICIAL_ALIAS_MATCHED_BY and normalize_name(m.query) == key):
            return True
    return False


def route_bare_name(request: NormalizedUserRequest, repo: Any) -> NormalizedUserRequest:
    bare = _bare_text(request)
    attempts = [[bare]] if bare else []
    if controller_names := _controller_named_ingredients(request):
        attempts.append(controller_names)
    if not attempts:
        return request
    try:
        ref = IngredientReference(repo.get_ingredient_bridge_reference())
        names = next((n for n in attempts
                      if all(ref.resolve_query(x)[0] == IngredientQueryResolution.RESOLVED for x in n)), None)
        if names is None and bare and _is_generic_nutrient_wording(bare, repo):
            names = [bare]  # 不在 identity corpus，但整句只是一般成分 / 營養素用語（沒有任何有辨識力的品牌 / 品名字樣）
        if names is None:
            return request  # 沒有一組名稱是 canonical entity 或一般成分用語：維持 Controller 的結果
        collision = any(_is_exact_official_product(repo, n) for n in names)
    except Exception as exc:  # noqa: BLE001 - 判斷失敗就維持 Controller 的結果，不阻斷查證
        logger.warning("bare_name_routing_failed", extra={"request_id": request.request_id,
                                                          "error_code": type(exc).__name__})
        return request
    if collision:
        logger.info("bare_name_collision", extra={"request_id": request.request_id, "stage": "identity_collision"})
        return request.model_copy(update={
            "identity_collision": True, "product_candidates": list(names), "brand_candidates": [], "company_candidates": [],
            "refers_to_unspecified_product": False, "context_entities_used": [], "user_confirmed_target": None,
            "needs_product_lookup": True, "product_reference_status": ProductReferenceStatus.EXPLICIT,
            "adapter_notes": request.adapter_notes + [COLLISION_NOTE]})
    logger.info("standalone_entity_override", extra={"request_id": request.request_id, "stage": "standalone_entity",
                                                     "controller_intent": request.intent.value})
    return request.model_copy(update={
        "intent": Intent.INGREDIENT_INQUIRY if request.intent in _PRODUCT_LEANING_INTENTS else request.intent,
        "product_candidates": [], "corrected_product_candidates": [], "product_mentions": [],
        "brand_candidates": [], "company_candidates": [], "permit_number_candidates": [],
        "ingredient_candidates": list(dict.fromkeys(names)),
        "refers_to_unspecified_product": False, "context_entities_used": [], "user_confirmed_target": None,
        "needs_product_lookup": False, "needs_graph_lookup": True,
        "needs_rag": request.needs_rag if request.intent in (Intent.EFFICACY_INQUIRY, Intent.SAFETY_INQUIRY) else False,
        "product_reference_status": ProductReferenceStatus.NOT_NEEDED,
        "standalone_entity": True,
        "adapter_notes": request.adapter_notes + [STANDALONE_INGREDIENT_NOTE],
    })
