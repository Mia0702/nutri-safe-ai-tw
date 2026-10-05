"""「檢視包裝宣稱」：包裝觀察（deterministic、只用已保存的結構化圖片觀察，不是官方查證）。

* 不呼叫 Vision / Controller / 資料庫 / RAG / 官方網站，也不與核准功效比對。
* 呈現：一般成分 / 營養補充 → 中性；一般功效宣稱 → 橘色「建議多留意」；疾病療效說法 → 沿用既有 deterministic 條件標示高風險說法
  （只標示那一句，不把整個產品升級為 HIGH）。
全部使用 TEST_ONLY 虛構資料。
"""

import json
import re

from app.response.line_renderer import AdaptiveLineRenderer
from app.response.package_observation import (
    PACKAGE_OBSERVATION_NOTICE,
    ObservationTone,
    build_package_observation,
)
from app.schemas.enums import ClaimContentSource, ClaimOrigin, ClaimSemanticCategory, ResponseAction
from app.schemas.image_bundle import FusedImageBundle, FusedValue
from tests.builders import claim, request
from tests.unit.test_image_routing import GENERAL_CLAIMS, MEDICAL, build_line, flex
from tests.unit.test_line_webhook import event, send
from app.response.actions import build_postback_data


def label(text, category=ClaimSemanticCategory.HEALTH_FUNCTION, condition=None):
    c = claim(text, category, ClaimOrigin.PRODUCT_LABEL_CLAIM, condition=condition)
    c.content_source = ClaimContentSource.IMAGE_CONTENT
    return c


def texts(node):
    """Flex 卡片中所有 text 節點（含顏色）。"""
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                out.append((n["text"], n.get("color")))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(node)
    return out


def render(req):
    return AdaptiveLineRenderer().render_package_observation(build_package_observation(req), "圖片辨識｜測試")


def test_tones_follow_existing_deterministic_claim_classification():
    req = request(observed_package_claims=[
        label("TEST_ONLY 有助於維持測試機能甲"),  # 一般功效宣稱 → 橘
        label("TEST_ONLY 補充每日所需營養", ClaimSemanticCategory.NUTRIENT_SUPPLEMENT),  # 營養補充 → 中性
        label("TEST_ONLY 可以治療虛構病", ClaimSemanticCategory.MEDICAL_EFFICACY, condition="虛構病"),  # 疾病療效 → 高風險說法
    ])
    tones = {c.text: c.tone for c in build_package_observation(req).claims}
    assert tones == {"TEST_ONLY 有助於維持測試機能甲": ObservationTone.ORANGE,
                     "TEST_ONLY 補充每日所需營養": ObservationTone.NEUTRAL,
                     "TEST_ONLY 可以治療虛構病": ObservationTone.RED}


def test_card_states_it_is_unverified_and_colors_claims_by_tone():
    req = request(observed_package_claims=[
        label("TEST_ONLY 有助於維持測試機能甲"),
        label("TEST_ONLY 補充每日所需營養", ClaimSemanticCategory.NUTRIENT_SUPPLEMENT),
        label("TEST_ONLY 可以治療虛構病", ClaimSemanticCategory.MEDICAL_EFFICACY, condition="虛構病")],
        ingredient_candidates=["虛構素甲"], other_visible_text=["TEST_ONLY 60 粒"])
    card = render(req)
    rows = dict(texts(card))
    assert PACKAGE_OBSERVATION_NOTICE in rows
    orange = next(v for k, v in rows.items() if "有助於維持測試機能甲" in k)
    red = next(v for k, v in rows.items() if "可以治療虛構病" in k)
    neutral = next(v for k, v in rows.items() if "補充每日所需營養" in k)
    assert orange == "#B45309" and red == "#C62828" and neutral == "#333333"
    body = json.dumps(card, ensure_ascii=False)
    assert "建議多留意" in body and "疾病療效說法，屬高風險說法" in body
    assert "虛構素甲" in body and "60 粒" in body
    assert "風險判斷" not in body and "官方核准" not in body.replace(PACKAGE_OBSERVATION_NOTICE, "")  # 沒有風險等級 / 官方比對結論


def test_source_images_are_kept_internally_but_never_shown_in_the_ui():
    bundle = FusedImageBundle(image_count=3, ingredients=[FusedValue(value="虛構素甲", observed_in=[2])],
                              warnings=[FusedValue(value="TEST_ONLY 請依標示食用", observed_in=[3])],
                              other_visible_text=[FusedValue(value="TEST_ONLY 60 粒", observed_in=[1, 2])])
    c = label("TEST_ONLY 有助於維持測試機能甲")
    c.source_images = [1]
    observation = build_package_observation(request(observed_package_claims=[c], image_bundle=bundle))
    # 結構化資料仍保留來源圖片
    assert observation.claims[0].source_images == [1] and observation.ingredients[0].source_images == [2]
    assert observation.warnings[0].source_images == [3] and observation.other_text[0].source_images == [1, 2]
    body = json.dumps(AdaptiveLineRenderer().render_package_observation(observation, "圖片辨識｜測試"), ensure_ascii=False)
    for shown in ("有助於維持測試機能甲", "虛構素甲", "請依標示食用", "60 粒"):
        assert shown in body
    assert not re.search(r"圖\s*[1-9]", body) and "建議多留意" in body


def test_button_renders_stored_observation_with_no_external_calls():
    handler, line, store, _, llm = build_line()
    [rid] = send(handler, event(kind="image", message_id="package"))
    calls = list(llm.calls)
    send(handler, event(kind="postback", data=build_postback_data(ResponseAction.CHECK_PACKAGE_CLAIMS, rid)))
    assert llm.calls == calls  # Vision / Controller / RAG(embed) / 官方網站 / planner 全部 0 次新呼叫
    for name in ("describe_image", "embed", "web_search_structured", "plan_step"):
        assert sum(1 for c in llm.calls if c[0] == name) == sum(1 for c in calls if c[0] == name)
    rows = dict(texts(line.sent[-1][2]))
    assert PACKAGE_OBSERVATION_NOTICE in rows
    # 三句包裝宣稱都列出：一般功效 / 誇大 → 橘；營養補充 → 中性
    assert sum(1 for k, v in rows.items() if v == "#B45309" and "建議多留意" in k) == 2


def test_medical_wording_safety_is_preserved_and_product_is_not_marked_high():
    """包裝有醫療宣稱：自動查證路徑不變（HIGH 風險卡）；按「檢視」只標示那一句，不再產生整體風險結論。"""
    handler, line, store, _, llm = build_line()
    [rid] = send(handler, event(kind="image", message_id="medical"))
    primary = flex(line)
    assert "🔴" in primary  # 既有行為：醫療宣稱 → HIGH 風險卡
    record = store.get(rid)
    assert record.normalized_request.claims_from_package
    if ResponseAction.CHECK_PACKAGE_CLAIMS in record.response.available_actions:
        send(handler, event(kind="postback", data=build_postback_data(ResponseAction.CHECK_PACKAGE_CLAIMS, rid)))
        card = flex(line)
        assert "疾病療效說法，屬高風險說法" in card and "風險判斷" not in card
    obs = build_package_observation(record.normalized_request)
    assert any(c.tone == ObservationTone.RED for c in obs.claims)


def test_provenance_marker_copied_into_text_by_the_controller_is_not_shown():
    c = label("TEST_ONLY 有助於維持測試機能甲（圖1）")
    observation = build_package_observation(request(observed_package_claims=[c], ingredient_candidates=["虛構素甲 (圖2、3)"]))
    assert observation.claims[0].text == "TEST_ONLY 有助於維持測試機能甲" and observation.ingredients[0].text == "虛構素甲"
    assert not re.search(r"圖\s*[1-9]", json.dumps(render(request(observed_package_claims=[c])), ensure_ascii=False))


def test_empty_observation_is_not_available():
    assert build_package_observation(request()).is_empty


def test_identity_fields_come_from_the_image_observation_and_multi_image_keeps_sources():
    single = build_package_observation(request(product_candidates=["TEST_ONLY 品名"], brand_candidates=["TEST_ONLY 品牌"],
                                               company_candidates=["TEST_ONLY 公司"], permit_number_candidates=["TEST_ONLY 字號"],
                                               other_visible_text=["TEST_ONLY 品名", "TEST_ONLY 60毫升"]))
    assert [i.text for i in single.product_names] == ["TEST_ONLY 品名"] and [i.text for i in single.brands] == ["TEST_ONLY 品牌"]
    assert [i.text for i in single.other_text] == ["TEST_ONLY 60毫升"]  # 與品名重複的文字不再列在其他文字
    bundle = FusedImageBundle(image_count=2, product_names=[FusedValue(value="TEST_ONLY 品名", observed_in=[1])],
                              brands=[FusedValue(value="TEST_ONLY 品牌", observed_in=[2])])
    multi = build_package_observation(request(image_bundle=bundle))
    assert multi.product_names[0].source_images == [1] and multi.brands[0].source_images == [2]
    body = json.dumps(AdaptiveLineRenderer().render_package_observation(multi), ensure_ascii=False)
    assert "TEST_ONLY 品名" in body and not re.search(r"圖\s*[1-9]", body)
