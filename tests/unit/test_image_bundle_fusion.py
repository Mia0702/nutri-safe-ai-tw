"""Multi-image Fusion Runtime Phase 1（Perception + Fusion Core）回歸測試。

* 2–3 張「同一產品」的照片：一次 Vision（analyze_image_bundle）→ 逐張結構化觀察（保留來源圖片）→
  deterministic fusion → 既有 Controller / Researcher pipeline（只進一次）。
* 許可證字號衝突 → STOP；明確不同的完整品名且無共同佐證 → 請使用者確認；單張圖片流程完全不變。

全部使用虛構的圖片觀察與 TEST_ONLY 產品，不使用任何真實照片。檢查呼叫路徑與次數，不檢查秒數。
"""

import json
import re

import pytest

from app.agents.intake_agent import IntakeAgent, merge_bundle_into_extraction
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import (
    ASSESSMENT_STEP,
    CLARIFICATION_MODE,
    FAST_PATH_MODE,
    PACKAGE_CLAIM_MODE,
    VerificationAgent,
)
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimContentSource,
    ClaimOrigin,
    ClaimSemanticCategory,
    ErrorCode,
    InputType,
    Intent,
    ProductMatchType,
    ProductVerificationStatus,
    ResponseAction,
    ResponseType,
    RiskLevel,
)
from app.schemas.image_bundle import (
    FusedImageBundle,
    ImageBundleObservation,
    ImageBundleStatus,
    ImageConflictCode,
    ImageKind,
    ImageObservation,
    ObservedProductName,
    ObservedText,
)
from app.schemas.user_request import ConfirmedProduct, IntakeExtraction, MediaInput, RawUserInput
from app.services.context_binding import bind_confirmed_product
from app.services.errors import ServiceError
from app.services.image_fusion import candidate_product_names, fuse_image_bundle, render_fused_content
from app.tools.input_tools import BUNDLE_INPUT_TOOL_NAMES, INPUT_TOOL_NAMES, analyze_image_bundle
from tests.builders import claim, extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore

PRODUCT = "虛構牌晶亮測試膠囊"  # synthetic p-1：有核准功效與成分「虛構素甲」
PERMIT = "測試健食字第Z00001號"  # p-1 的字號
OTHER_PERMIT = "測試健食字第Z00002號"  # p-2 的字號
OTHER_PRODUCT = "另一虛構保健測試錠"  # synthetic p-3
COMPANY = "虛構測試生技股份有限公司"
SLOW_TOOLS = {"plan_tool_calls", "search_rag", ASSESSMENT_STEP, "search_tw_official_web"}
IMG = ClaimContentSource.IMAGE_CONTENT


# ------------------------------------------------------------------ 虛構的 Vision 觀察


def t(value, conf=0.9, readable=True):
    return ObservedText(value=value if readable else "", readable=readable, confidence=conf)


def n(value, conf=0.9, complete=True):
    return ObservedProductName(value=value, readable=True, confidence=conf, is_complete=complete)


def obs(index=1, kind=ImageKind.FRONT_PACKAGE, **fields):
    mapping = {"names": "product_names", "brands": "brands", "companies": "companies", "permits": "permit_numbers",
               "ingredients": "ingredients", "claims": "claims", "warnings": "warnings", "marks": "certification_marks",
               "other": "other_visible_text"}
    return ImageObservation(image_index=index, image_kind=kind, **{mapping[k]: v for k, v in fields.items()})


def bundle_of(*observations):
    return ImageBundleObservation(images=list(observations))


def fuse(*observations, count=None):
    return fuse_image_bundle(bundle_of(*observations), count or len(observations))


def label_claim(text, category=ClaimSemanticCategory.HEALTH_FUNCTION, condition=None, absolute=False):
    c = claim(text, category, ClaimOrigin.PRODUCT_LABEL_CLAIM, absolute=absolute, condition=condition)
    c.content_source = IMG
    return c


GENERAL_CLAIM = "TEST_ONLY 有助於維持測試機能甲"
MEDICAL_CLAIM = "TEST_ONLY 可以治療虛構病"


# ------------------------------------------------------------------ pipeline 組裝（bytes → 該張圖片的虛構觀察）


def fake_llm(by_image, intake=None):
    """by_image: 圖片 bytes → ImageObservation（image_index 依該圖片在輸入中的位置填入，與真實 Vision 相同）。"""
    llm = FakeLLM()
    llm.describe_image = lambda **kw: llm.calls.append(("describe_image", kw["mime_type"])) or "TEST_ONLY 圖片文字：單張"

    def vision(images):
        return ImageBundleObservation(images=[
            by_image[content].model_copy(update={"image_index": i}) for i, (content, _) in enumerate(images, start=1)])

    llm.structured[ImageBundleObservation] = vision
    llm.structured[IntakeExtraction] = intake or extraction(intent=Intent.PRODUCT_VERIFICATION,
                                                            product_candidates=[PRODUCT])
    return llm


def pipeline(llm):
    return AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )


def media(*keys):
    return [MediaInput(kind="image", content=k, mime_type="image/jpeg") for k in keys]


def run(by_image, order=None, intake=None):
    keys = order or list(by_image)
    llm = fake_llm(by_image, intake)
    raw = RawUserInput(input_type=InputType.IMAGE, media=media(*keys))
    return pipeline(llm).run(raw), llm


def tools(result):
    return [x.tool_name for x in result.evidence.tools_called]


FRONT = obs(names=[n(PRODUCT)], claims=[t(GENERAL_CLAIM)], marks=[t("健康食品標章")])
BACK = obs(kind=ImageKind.BACK_LABEL, ingredients=[t("虛構素甲"), t("測試乳酸菌")], warnings=[t("TEST_ONLY 請依標示食用")])
PERMIT_IMG = obs(kind=ImageKind.PERMIT_AREA, permits=[t(PERMIT)], companies=[t(COMPANY)])


# ============================================================ 1. front + ingredient back → 同一產品融合


def test_front_and_ingredient_back_are_fused_into_one_product():
    result, llm = run({b"front": FRONT, b"back": BACK})
    req, bundle = result.normalized_request, result.evidence
    fused = req.image_bundle
    assert fused.status == ImageBundleStatus.FUSED and fused.image_count == 2
    assert [(v.value, v.observed_in) for v in fused.product_names] == [(PRODUCT, [1])]
    assert [v.observed_in for v in fused.ingredients] == [[2], [2]]
    assert bundle.product_match.status == ProductVerificationStatus.VERIFIED
    assert bundle.product_match.best_match.product.product_name_zh == PRODUCT
    # 一次 Vision、沒有逐張 analyze_image；整題只進一次 Controller / Researcher
    assert [c for c in llm.calls if c[0] == "describe_images_structured"] == [("describe_images_structured", ["image/jpeg"] * 2)]
    assert not llm.called("describe_image")
    assert sum(1 for c in llm.calls if c[0] == "parse_structured" and c[1] == "IntakeExtraction") == 1
    assert [r.tool_name for r in req.input_tools_called] == ["analyze_image_bundle"]


# ============================================================ 2. front + permit image → 許可證確認 identity


def test_permit_image_confirms_identity_by_permit_number():
    result, _ = run({b"front": obs(names=[n("虛構牌晶亮")]), b"permit": PERMIT_IMG})
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.VERIFIED and match.match_type == ProductMatchType.PERMIT_NUMBER
    assert match.best_match.product.product_name_zh == PRODUCT
    fused = result.normalized_request.image_bundle
    assert [(v.value, v.observed_in) for v in fused.permit_numbers] == [(PERMIT, [2])]
    assert [(v.value, v.observed_in) for v in fused.companies] == [(COMPANY, [2])]


# ============================================================ 3. 某張缺值、另一張有清楚值


def test_missing_or_unreadable_field_in_one_image_uses_readable_value_from_another():
    fused = fuse(obs(1, names=[n(PRODUCT)], permits=[t("", readable=False)]),
                 obs(2, kind=ImageKind.PERMIT_AREA, permits=[t(PERMIT)], companies=[t(COMPANY)]),
                 obs(3, kind=ImageKind.BACK_LABEL, names=[n("", conf=0.0)]))
    assert [(v.value, v.observed_in) for v in fused.permit_numbers] == [(PERMIT, [2])]
    assert [(v.value, v.observed_in) for v in fused.companies] == [(COMPANY, [2])]
    assert fused.status == ImageBundleStatus.FUSED and fused.conflicts == []
    assert fused.unreadable_images == [3]


# ============================================================ 4. 同欄位一致 → merge


def test_same_field_consistent_across_images_merges_with_all_sources():
    fused = fuse(obs(1, names=[n(PRODUCT)], permits=[t(PERMIT)]),
                 obs(2, names=[n("虛構牌 晶亮測試膠囊", conf=0.7)], permits=[t("測試健食字第 Z00001 號", conf=0.8)]),
                 obs(3, kind=ImageKind.PERMIT_AREA, permits=[t(PERMIT)]))
    assert [(v.value, v.observed_in) for v in fused.product_names] == [(PRODUCT, [1, 2])]
    assert [(v.value, v.observed_in) for v in fused.permit_numbers] == [(PERMIT, [1, 2, 3])]
    assert fused.conflicts == [] and fused.status == ImageBundleStatus.FUSED


def test_partial_name_is_kept_as_observation_without_conflict():
    fused = fuse(obs(1, names=[n(PRODUCT)]), obs(2, names=[n("晶亮測試", complete=False)]))
    assert {v.value for v in fused.product_names} == {PRODUCT, "晶亮測試"}  # 保留各 observation
    assert fused.conflicts == [] and fused.status == ImageBundleStatus.FUSED
    assert candidate_product_names(fused) == [PRODUCT]  # 不完整片段沒有額外資訊，不送 resolver


# ============================================================ 5. 許可證字號衝突 → STOP


def test_conflicting_permit_numbers_stop_product_verification():
    result, llm = run({b"a": obs(names=[n(PRODUCT)], permits=[t(PERMIT)]),
                       b"b": obs(kind=ImageKind.PERMIT_AREA, permits=[t(OTHER_PERMIT)])})
    req, bundle, payload = result.normalized_request, result.evidence, result.response
    fused = req.image_bundle
    assert fused.status == ImageBundleStatus.IDENTITY_CONFLICT and req.image_bundle_blocked
    [conflict] = fused.conflicts
    assert conflict.code == ImageConflictCode.MULTI_IMAGE_IDENTITY_CONFLICT and conflict.identity_critical
    assert {(v.value, tuple(v.observed_in)) for v in conflict.values} == {(PERMIT, (1,)), (OTHER_PERMIT, (2,))}
    # 不任選其中一個：沒有產品查證、沒有任何證據查詢、也不跑語意理解
    assert bundle.product_match.status == ProductVerificationStatus.NOT_APPLICABLE and bundle.product_match.best_match is None
    assert bundle.planner_mode == CLARIFICATION_MODE
    assert not (set(tools(result)) & (SLOW_TOOLS | {"lookup_product", "lookup_permit"}))
    assert not llm.called("parse_structured") and not llm.called("plan_step")
    assert payload.response_type == ResponseType.MISSING_INFORMATION and payload.risk_level is None
    text = payload.plain_text()
    assert "不同的許可證字號" in text and PERMIT in text and OTHER_PERMIT in text and "分開傳送" in text
    assert not re.search(r"圖\s*[1-9]", text)  # 來源圖片編號只在結構化資料，不顯示給使用者


def test_low_confidence_different_permit_is_only_ocr_uncertain():
    fused = fuse(obs(1, permits=[t(PERMIT)]), obs(2, permits=[t(OTHER_PERMIT, conf=0.3)]))
    assert fused.status == ImageBundleStatus.FUSED
    assert [v.value for v in fused.permit_numbers] == [PERMIT]  # 低信心值不採用
    assert [c.code for c in fused.conflicts] == [ImageConflictCode.OCR_UNCERTAIN]


# ============================================================ 6. 明確不同的產品 → clarification


def test_clearly_different_products_without_shared_evidence_ask_for_confirmation():
    result, llm = run({b"a": obs(names=[n(PRODUCT)], companies=[t(COMPANY)]),
                       b"b": obs(names=[n(OTHER_PRODUCT)], companies=[t("範例測試食品有限公司")])})
    fused = result.normalized_request.image_bundle
    assert fused.status == ImageBundleStatus.SAME_PRODUCT_UNCERTAIN
    assert {(c.field, c.code) for c in fused.conflicts} == {
        ("product_name", ImageConflictCode.SAME_PRODUCT_UNCERTAIN), ("company", ImageConflictCode.FIELD_VALUE_CONFLICT)}
    assert result.evidence.product_match.status == ProductVerificationStatus.NOT_APPLICABLE
    assert not (set(tools(result)) & (SLOW_TOOLS | {"lookup_product"})) and not llm.called("parse_structured")
    payload = result.response
    assert payload.response_type == ResponseType.MISSING_INFORMATION
    assert "明顯不同" in payload.plain_text() and PRODUCT in payload.plain_text() and OTHER_PRODUCT in payload.plain_text()


def test_different_names_with_shared_company_evidence_are_not_stopped():
    fused = fuse(obs(1, names=[n(PRODUCT)], companies=[t(COMPANY)]),
                 obs(2, kind=ImageKind.BACK_LABEL, names=[n(OTHER_PRODUCT)], companies=[t(COMPANY)]))
    assert fused.status == ImageBundleStatus.FUSED
    assert [c.code for c in fused.conflicts] == [ImageConflictCode.FIELD_VALUE_CONFLICT]
    assert {PRODUCT, OTHER_PRODUCT} <= set(candidate_product_names(fused))  # 兩個都交給 resolver 判斷


# ============================================================ 7. OCR 錯字 + exact 字號 → 既有優先序


def test_ocr_typo_name_with_exact_permit_in_another_image_keeps_permit_priority():
    result, _ = run({b"front": obs(names=[n("虛構牌晶亮測試膠嚢", conf=0.5)]), b"permit": PERMIT_IMG},
                    intake=extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=["虛構牌晶亮測試膠嚢"]))
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.VERIFIED and match.match_type == ProductMatchType.PERMIT_NUMBER
    assert match.best_match.product.product_name_zh == PRODUCT
    assert result.risk.risk_level != RiskLevel.HIGH  # 辨識誤差不單獨造成 HIGH


def test_typo_level_name_difference_is_recorded_as_ocr_uncertain_not_stopped():
    fused = fuse(obs(1, names=[n(PRODUCT)]), obs(2, names=[n("虛構牌晶亮測試膠嚢", conf=0.8)]))
    assert fused.status == ImageBundleStatus.FUSED
    [conflict] = fused.conflicts
    assert conflict.code == ImageConflictCode.OCR_UNCERTAIN and not conflict.identity_critical
    assert {tuple(v.observed_in) for v in conflict.values} == {(1,), (2,)}
    assert set(candidate_product_names(fused)) == {PRODUCT, "虛構牌晶亮測試膠嚢"}


# ============================================================ 8. 成分不會變成產品身份


def test_ingredients_from_back_image_never_become_product_candidates():
    llm_extraction = extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT, "虛構素甲", "測試乳酸菌"],
                                ingredient_candidates=["虛構素甲", "測試乳酸菌"])
    result, _ = run({b"front": FRONT, b"back": BACK}, intake=llm_extraction)
    req = result.normalized_request
    assert req.product_candidates == [PRODUCT]
    assert set(req.ingredient_candidates) == {"虛構素甲", "測試乳酸菌"}
    assert result.evidence.product_match.best_match.product.product_name_zh == PRODUCT
    fused = fuse_image_bundle(bundle_of(obs(1, names=[n(PRODUCT)]), BACK.model_copy(update={"image_index": 2})), 2)
    assert candidate_product_names(fused) == [PRODUCT]
    # LLM 漏抽品名 / 字號時，融合觀察補上；成分只留在成分
    merged = merge_bundle_into_extraction(extraction(intent=Intent.PRODUCT_VERIFICATION), fuse(FRONT, PERMIT_IMG.model_copy(update={"image_index": 2})))
    assert merged.product_candidates == [PRODUCT] and merged.permit_number_candidates == [PERMIT]


# ============================================================ 9. claims 保留來源圖片


def test_claims_and_warnings_keep_source_image():
    result, _ = run({b"front": FRONT, b"back": BACK, b"permit": PERMIT_IMG},
                    intake=extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT],
                                      claims=[label_claim(GENERAL_CLAIM)]))
    req = result.normalized_request
    assert [(v.value, v.observed_in) for v in req.image_bundle.claims] == [(GENERAL_CLAIM, [1])]
    assert [(v.value, v.observed_in) for v in req.image_bundle.warnings] == [("TEST_ONLY 請依標示食用", [2])]
    [observed] = req.observed_package_claims
    assert observed.source_images == [1]
    assert req.image_bundle.image_kinds == {1: ImageKind.FRONT_PACKAGE, 2: ImageKind.BACK_LABEL, 3: ImageKind.PERMIT_AREA}
    assert "圖1" in render_fused_content(req.image_bundle)


# ============================================================ 10 / 11. 包裝宣稱：沿用既有 image claim contract


GENERAL = [label_claim(GENERAL_CLAIM), label_claim("TEST_ONLY 補充每日所需營養", ClaimSemanticCategory.NUTRIENT_SUPPLEMENT),
           label_claim("TEST_ONLY 效果看得見", ClaimSemanticCategory.EXAGGERATED_EFFICACY, absolute=True)]


def test_ordinary_package_claims_do_not_trigger_planner_or_web():
    result, llm = run({b"front": FRONT, b"back": BACK},
                      intake=extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT], claims=GENERAL))
    req = result.normalized_request
    assert req.claims == [] and len(req.observed_package_claims) == 3 and req.image_identification
    assert result.evidence.planner_mode == FAST_PATH_MODE
    assert not (set(tools(result)) & SLOW_TOOLS) and not llm.called("plan_step") and not llm.called("web_search_structured")
    assert result.response.response_type == ResponseType.PRODUCT_INFO
    assert result.response.available_actions[0] == ResponseAction.CHECK_PACKAGE_CLAIMS


def test_medical_package_claim_keeps_existing_safety_behavior():
    medical = label_claim(MEDICAL_CLAIM, ClaimSemanticCategory.MEDICAL_EFFICACY, condition="虛構病")
    front = obs(names=[n(PRODUCT)], claims=[t(GENERAL_CLAIM), t(MEDICAL_CLAIM)])
    result, llm = run({b"front": front, b"back": BACK},
                      intake=extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT],
                                        claims=[label_claim(GENERAL_CLAIM), medical]))
    req = result.normalized_request
    assert [c.semantic_category for c in req.claims] == [ClaimSemanticCategory.MEDICAL_EFFICACY] and req.claims_from_package
    assert req.claims[0].source_images == [1]
    assert result.risk.risk_level == RiskLevel.HIGH and result.response.response_type == ResponseType.RISK
    assert result.evidence.planner_mode == PACKAGE_CLAIM_MODE and not llm.called("plan_step")
    assert not llm.called("web_search_structured")


# ============================================================ 12. 單張圖片流程不變


def test_single_image_path_is_unchanged():
    llm = fake_llm({})
    llm.describe_image = lambda **kw: llm.calls.append(("describe_image", kw["mime_type"])) or f"TEST_ONLY 圖片文字：{PRODUCT}"
    raw = RawUserInput(input_type=InputType.IMAGE, media=media(b"only"))
    result = pipeline(llm).run(raw)
    assert [c for c in llm.calls if c[0] == "describe_image"] == [("describe_image", "image/jpeg")]
    assert not llm.called("describe_images_structured")
    req = result.normalized_request
    assert req.image_bundle is None and [r.tool_name for r in req.input_tools_called] == ["analyze_image"]
    assert "【圖片內容】" in req.original_text and "圖1" not in req.original_text
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED


def test_bundle_tool_is_separate_from_existing_input_tool_names():
    assert set(INPUT_TOOL_NAMES) == {"analyze_image", "transcribe_audio", "fetch_url_content", "reinspect_image_identity"}
    assert BUNDLE_INPUT_TOOL_NAMES == ("analyze_image_bundle",) and not set(BUNDLE_INPUT_TOOL_NAMES) & set(INPUT_TOOL_NAMES)


# ============================================================ 13. 圖片順序不影響融合後的 identity


@pytest.mark.parametrize("order", [[b"front", b"back", b"permit"], [b"permit", b"back", b"front"], [b"back", b"permit", b"front"]])
def test_image_order_does_not_change_fused_identity(order):
    by_image = {b"front": FRONT, b"back": BACK, b"permit": PERMIT_IMG}
    result, _ = run(by_image, order=order)
    fused = result.normalized_request.image_bundle
    assert [v.value for v in fused.product_names] == [PRODUCT] and [v.value for v in fused.permit_numbers] == [PERMIT]
    assert [v.value for v in fused.companies] == [COMPANY]
    # 來源編號隨輸入順序改變，但指向的是同一張圖片
    assert fused.product_names[0].observed_in == [order.index(b"front") + 1]
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert result.evidence.product_match.best_match.product.product_name_zh == PRODUCT


def test_conflict_detection_is_order_independent():
    a, b = obs(1, permits=[t(PERMIT)]), obs(2, permits=[t(OTHER_PERMIT)])
    forward = fuse(a, b)
    backward = fuse_image_bundle(bundle_of(b.model_copy(update={"image_index": 1}), a.model_copy(update={"image_index": 2})), 2)
    assert forward.status == backward.status == ImageBundleStatus.IDENTITY_CONFLICT
    assert {v.value for v in forward.permit_numbers} == {v.value for v in backward.permit_numbers}


# ============================================================ 14. 圖片數量限制


def test_max_image_guard_rejects_before_any_vision_call():
    keys = [b"a", b"b", b"c", b"d"]
    llm = fake_llm({k: FRONT for k in keys})
    with pytest.raises(ServiceError) as exc:
        IntakeAgent(llm).understand("req-1", RawUserInput(input_type=InputType.IMAGE, media=media(*keys)))
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert not llm.called("describe_images_structured") and not llm.called("describe_image")


def test_bundle_tool_rejects_wrong_image_counts_and_bad_images():
    llm = fake_llm({})
    for images in ([(b"a", "image/jpeg")], [(b"a", "image/jpeg")] * 4, [(b"a", "image/jpeg"), (b"", "image/jpeg")],
                   [(b"a", "image/jpeg"), (b"b", "text/plain")]):
        with pytest.raises(ServiceError):
            analyze_image_bundle(llm, images)
    assert not llm.called("describe_images_structured")


# ============================================================ 補充：Vision 請求形狀 / 延遲紀錄 / 其他邊界


def test_vision_request_labels_each_image_in_one_request():
    from app.services.openai_service import OpenAIService

    captured = []

    class Responses:
        def parse(self, **kwargs):
            captured.append(kwargs)
            return type("R", (), {"output_parsed": ImageBundleObservation(images=[])})()

    service = OpenAIService.__new__(OpenAIService)
    service._client = type("C", (), {"responses": Responses()})()
    service._settings = type("S", (), {"openai_model": "test-model"})()
    service.describe_images_structured(instructions="x", images=[(b"a", "image/png"), (b"b", "image/jpeg")],
                                       output_model=ImageBundleObservation)
    [call] = captured  # 一次 request
    content = call["input"][0]["content"]
    assert [c["text"] for c in content if c["type"] == "input_text"] == ["Image 1", "Image 2"]
    assert [c["type"] for c in content] == ["input_text", "input_image", "input_text", "input_image"]
    assert call["text_format"] is ImageBundleObservation


def test_latency_metrics_are_recorded_and_no_per_image_pipeline_runs():
    result, llm = run({b"front": FRONT, b"back": BACK, b"permit": PERMIT_IMG})
    metrics = result.normalized_request.image_bundle.metrics
    assert metrics.image_count == 3 and metrics.vision_bundle_ms is not None
    assert metrics.semantic_ms is not None and metrics.total_ms == result.duration_ms
    assert sum(1 for c in llm.calls if c[0] == "describe_images_structured") == 1
    # Agent B 只跑一次：同一個產品只查一次
    assert tools(result).count("lookup_product") <= 1


def test_blocked_bundle_is_never_bound_to_confirmed_product_context():
    result, _ = run({b"a": obs(names=[n(PRODUCT)], permits=[t(PERMIT)]), b"b": obs(permits=[t(OTHER_PERMIT)])})
    from datetime import datetime, timezone

    confirmed = ConfirmedProduct(product_id="p-2", official_product_name="虛構牌順暢測試優酪乳", permit_number=OTHER_PERMIT,
                                 confirmation_source="RESOLVER_VERIFIED", confirmed_at=datetime.now(timezone.utc),
                                 confirmed_request_id="old")
    assert bind_confirmed_product(result.normalized_request, confirmed) is result.normalized_request


def test_bundle_with_no_readable_content_asks_for_clearer_photos():
    result, _ = run({b"a": obs(), b"b": obs()})
    assert result.normalized_request.image_bundle.status == ImageBundleStatus.NO_READABLE_CONTENT
    assert result.response.response_type == ResponseType.MISSING_INFORMATION


def test_observation_with_invalid_image_index_is_ignored():
    fused = fuse(obs(1, names=[n(PRODUCT)]), obs(9, permits=[t(PERMIT)]), count=2)
    assert fused.permit_numbers == [] and fused.unreadable_images == [2]


def test_vision_failure_without_text_reports_error_instead_of_pretending():
    llm = fake_llm({b"a": FRONT, b"b": BACK})
    llm.fail.add("describe_images_structured")
    result = pipeline(llm).run(RawUserInput(input_type=InputType.IMAGE, media=media(b"a", b"b")))
    assert result.response.response_type == ResponseType.ERROR
    assert result.normalized_request is None


def test_bundle_models_serialize_without_image_bytes():
    fused = fuse(FRONT, BACK.model_copy(update={"image_index": 2}))
    dumped = json.dumps(fused.model_dump(mode="json"), ensure_ascii=False)
    assert PRODUCT in dumped and isinstance(FusedImageBundle.model_validate_json(fused.model_dump_json()), FusedImageBundle)
