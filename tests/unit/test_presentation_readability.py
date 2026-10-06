"""LINE Flex 可讀性（純呈現）：內文深色、中繼資料較淺、語意色不變、不重複渲染相同附註。"""
from app.response.line_renderer import (COLOR_BODY, COLOR_LABEL, COLOR_MUTED, PARAGRAPH_SPACING, TONE_COLORS,
                                        AdaptiveLineRenderer)
from app.schemas.enums import ResponseAction, ResponseType, RiskLevel, SemanticTone
from app.schemas.response import ResponsePayload

R = AdaptiveLineRenderer()
RID = "00000000-0000-4000-8000-000000000002"


def _texts(node):
    if isinstance(node, dict):
        if node.get("type") == "text":
            yield node
        for v in node.values():
            yield from _texts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _texts(v)


def _payload(**kw):
    base = dict(request_id=RID, response_type=ResponseType.RISK, risk_level=RiskLevel.HIGH, title="TEST_ONLY 標題",
                summary="TEST_ONLY 結論", key_points=["TEST_ONLY 重點"], warnings=["TEST_ONLY 警語"],
                available_actions=[ResponseAction.DETAIL], source_count=1)
    return ResponsePayload(**{**base, **kw})


def test_color_hierarchy_is_ordered_and_body_is_dark():
    assert (COLOR_BODY, COLOR_LABEL, COLOR_MUTED) == ("#333333", "#555555", "#777777")


def test_body_warning_and_query_label_colors():
    msg = R.render_primary(_payload(), query_label="圖片辨識｜TEST_ONLY")
    colors = {t["text"]: t["color"] for t in _texts(msg)}
    assert colors["TEST_ONLY 警語"] == COLOR_BODY and colors["TEST_ONLY 結論"] == COLOR_BODY
    assert colors["圖片辨識｜TEST_ONLY"] == COLOR_MUTED
    assert colors["官方資料來源 1 筆"] == COLOR_LABEL


def test_semantic_colors_unchanged():
    assert TONE_COLORS == {SemanticTone.GREEN: "#1B7F3B", SemanticTone.ORANGE: "#B45309", SemanticTone.RED: "#C62828"}


def test_paragraph_spacing_on_containers_only():
    msg = R.render_primary(_payload())
    assert msg["contents"]["body"]["spacing"] == PARAGRAPH_SPACING


def test_warning_identical_to_shown_key_point_is_not_rendered_twice():
    dup = "TEST_ONLY 目前涵蓋說明。"
    msg = R.render_primary(_payload(key_points=[dup], warnings=[dup, "TEST_ONLY 另一則警語"]))
    texts = [t["text"] for t in _texts(msg)]
    assert texts.count(dup) == 1 and "TEST_ONLY 另一則警語" in texts
