"""官方文字列表排版（DISPLAY-ONLY）/ 說法查證卡不重複核准原文 / 功效詢問與純產品名稱 fast path 回歸測試。

全部使用虛構產品（synthetic_data p-1：虛構牌晶亮測試膠囊，核准功效「測試功效甲」）與虛構核准文字。
"""

import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import ASSESSMENT_STEP, FAST_PATH_MODE, VerificationAgent
from app.pipeline import AnalysisPipeline
from app.response.line_renderer import SUB_BULLET, AdaptiveLineRenderer, split_list_items
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimOrigin,
    ClaimSemanticCategory,
    InputType,
    Intent,
    ResponseAction,
    ResponseType,
    RiskLevel,
)
from app.schemas.response import DetailPayload, ResponsePayload
from app.schemas.user_request import IntakeExtraction, RawUserInput
from tests.builders import claim, extraction, user_text
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data

PRODUCT = "虛構牌晶亮測試膠囊"
FUNCTION = "測試功效甲"
LIST_TEXT = "本產品經動物實驗結果:1.有助於降低虛構指標甲 2.有助於降低虛構指標乙 3.有助於降低虛構指標丙"
ITEMS = ["有助於降低虛構指標甲", "有助於降低虛構指標乙", "有助於降低虛構指標丙"]
SLOW = {"search_rag", "search_tw_official_web", "plan_tool_calls", ASSESSMENT_STEP}
R = AdaptiveLineRenderer()
RID = "00000000-0000-4000-8000-0000000000cc"


@pytest.fixture
def list_claim(monkeypatch):
    """TEST_ONLY：把 p-1 的核准功效原文換成含 1. 2. 3. 的虛構文字。"""
    monkeypatch.setattr(data, "APPROVED_CLAIMS", [dict(c, claim_text=LIST_TEXT) if c["product_id"] == "p-1" else c
                                                  for c in data.APPROVED_CLAIMS])


def run(raw_text="TEST_ONLY", *claims, intent=Intent.CLAIM_VERIFICATION, candidates=(PRODUCT,)):
    ex = extraction(intent=intent, product_candidates=list(candidates), claims=list(claims))
    llm = FakeLLM(structured={IntakeExtraction: lambda _: ex.model_copy(deep=True)})
    pipe = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    return pipe.run(RawUserInput(input_type=InputType.TEXT, text=raw_text if any(c in raw_text for c in candidates) else user_text(ex, raw_text))), llm


def tools(result):
    return [t.tool_name for t in result.evidence.tools_called]


def texts(message):
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(message)
    return out


def sub_items(message):
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "box" and node.get("contents") and node["contents"][0].get("text") == SUB_BULLET:
                found.append(node["contents"][1]["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(message)
    return found


# ================================================================ 1. 列表排版（display only）


def test_inline_numbered_list_becomes_three_separate_items():
    assert split_list_items("功能說明:1.TEST_ONLY 甲 2.TEST_ONLY 乙 3.TEST_ONLY 丙") == (
        "功能說明:", ["TEST_ONLY 甲", "TEST_ONLY 乙", "TEST_ONLY 丙"])
    msg = R.render_detail(DetailPayload(request_id=RID, action=ResponseAction.FULL_EFFICACY, title="完整核准功效",
                                        body=["【測試功效甲】\n功能說明:1.TEST_ONLY 甲 2.TEST_ONLY 乙 3.TEST_ONLY 丙"]))
    assert sub_items(msg) == ["TEST_ONLY 甲", "TEST_ONLY 乙", "TEST_ONLY 丙"]
    assert "功能說明:" in texts(msg) and not any("2.TEST_ONLY" in t for t in texts(msg))


@pytest.mark.parametrize("text", [
    "每日攝取 1.5 mg 以下", "含 0.5% 虛構成分", "依第1條及第2條規定", "許可證字號 衛部健食字第A00001號",
    "公告日期 2026.1.2", "每日2次，每次1顆", "1.只有單一項目", "可分為1、2兩種",
])
def test_numbers_that_are_not_list_markers_are_not_split(text):
    assert split_list_items(text) == (text, [])
    msg = R.render_detail(DetailPayload(request_id=RID, action=ResponseAction.WARNINGS, title="t", body=[text]))
    assert not sub_items(msg)


def test_list_formatting_applies_to_warnings_and_parenthesized_numbers():
    msg = R.render_detail(DetailPayload(request_id=RID, action=ResponseAction.WARNINGS, title="警語與注意事項",
                                        body=["・(1)TEST_ONLY 甲；(2)TEST_ONLY 乙；（3）TEST_ONLY 丙"]))
    shown = texts(msg)
    assert ["TEST_ONLY 甲", "TEST_ONLY 乙", "TEST_ONLY 丙"] == [t for t in shown if t.startswith("TEST_ONLY")]


def test_efficacy_card_splits_items_and_raw_approved_claim_is_unchanged(list_claim):
    result, llm = run(f"{PRODUCT}有什麼核准功效？", intent=Intent.EFFICACY_INQUIRY)
    assert [e.claim_text for e in result.evidence.approved_claims] == [LIST_TEXT]  # evidence 原文不變
    p = result.response
    assert p.response_type == ResponseType.PRODUCT_INFO and p.risk_level is None
    card = R.render_primary(p)
    assert sub_items(card) == [] and LIST_TEXT not in str(card)  # primary 只列功效名稱（Intent-first）
    detail = ReporterAgent(llm).detail(ResponseAction.FULL_EFFICACY, result.normalized_request, result.evidence,
                                       result.risk)
    assert LIST_TEXT in "\n".join(detail.body)  # payload 保留原文，只在顯示時拆項
    assert sub_items(R.render_detail(detail)) == ITEMS
    assert sub_items(R.render_detail(detail)) == ITEMS


# ================================================================ 2. 說法查證卡不重複核准原文


def test_approved_function_question_card_does_not_repeat_full_claim(list_claim):
    q = claim(f"{PRODUCT}可以{FUNCTION}嗎", ClaimSemanticCategory.HEALTH_FUNCTION, ClaimOrigin.USER_QUESTION,
              function=FUNCTION)
    result, llm = run("TEST_ONLY", q)
    p = result.response
    assert p.response_type == ResponseType.CLAIM_RESULT and p.claim_risk_level == RiskLevel.LOW
    card = json.dumps(R.render_primary(p), ensure_ascii=False)
    assert "官方核准的保健功效範圍內" in p.summary
    assert f"官方核准功效：{FUNCTION}" in p.key_points
    for item in ITEMS + ["本產品經動物實驗結果"]:
        assert item not in card and not any(item in k for k in p.key_points)
    # 完整核准原文（含 1. 2. 3.）在完整分析
    full = ReporterAgent(llm).detail(ResponseAction.DETAIL, result.normalized_request, result.evidence, result.risk)
    assert sub_items(R.render_detail(full)) == ITEMS


# ================================================================ 3A. 功效詢問 fast path


def test_efficacy_inquiry_with_approved_claim_uses_structured_evidence_only():
    result, _ = run(f"{PRODUCT}的核准功效是什麼？", intent=Intent.EFFICACY_INQUIRY)
    assert result.evidence.planner_mode == FAST_PATH_MODE
    assert result.evidence.approved_claims and not (set(tools(result)) & SLOW)
    assert result.response.response_type == ResponseType.PRODUCT_INFO


# ================================================================ 3B. 純產品名稱 fast path


@pytest.mark.parametrize("raw", [PRODUCT, f"「{PRODUCT}」", f" {PRODUCT}？"])
def test_exact_product_name_only_goes_to_product_info_fast_path(raw):
    result, _ = run(raw, intent=Intent.UNCLEAR)
    bundle = result.evidence
    assert bundle.planner_mode == FAST_PATH_MODE and bundle.fast_path_reason == "product_name_only_exact"
    assert not (set(tools(result)) & SLOW)
    p = result.response
    assert p.response_type == ResponseType.PRODUCT_INFO and p.risk_level is None


@pytest.mark.parametrize("raw, candidate", [
    (f"{PRODUCT}跟咖啡一起吃好嗎", PRODUCT),  # 有額外語意
    (f"{PRODUCT}還有嗎", PRODUCT),
    ("虛構牌晶亮測試", "虛構牌晶亮測試"),  # 只有部分 / 相似名稱，不是 exact
])
def test_unclear_input_with_extra_semantics_or_non_exact_name_is_not_product_fast_path(raw, candidate):
    result, _ = run(raw, intent=Intent.UNCLEAR, candidates=(candidate,))
    assert result.evidence.fast_path_reason != "product_name_only_exact"
    assert result.evidence.planner_mode != FAST_PATH_MODE


def test_unclear_without_product_is_not_product_fast_path():
    result, _ = run("TEST_ONLY 嗯", intent=Intent.UNCLEAR, candidates=())
    assert result.evidence.planner_mode != FAST_PATH_MODE


def test_primary_card_list_items_do_not_count_as_extra_key_points():
    p = ResponsePayload(request_id=RID, response_type=ResponseType.PRODUCT_INFO, title="產品官方資訊", summary="s",
                        key_points=[f"{FUNCTION}：{LIST_TEXT}", "TEST_ONLY 二", "TEST_ONLY 三", "TEST_ONLY 四"])
    msg = R.render_primary(p)
    assert sum(t == "・" for t in texts(msg)) == 3 and sub_items(msg) == ITEMS


@pytest.mark.parametrize("target", ["核准保健功效", "官方核准的保健功效", "保健效果"])
def test_efficacy_question_with_generic_compound_target_is_open_question_fast_path(target):
    """泛稱組合不是具體功效：開放式功效詢問不當成命題，直接以結構化核准功效回答。"""
    q = claim(f"{PRODUCT}可以使用它的{target}嗎", ClaimSemanticCategory.HEALTH_FUNCTION, ClaimOrigin.USER_QUESTION,
              function=target)
    result, _ = run(f"{PRODUCT}可以使用它的{target}嗎", q, intent=Intent.EFFICACY_INQUIRY)
    assert result.normalized_request.claims == []
    assert result.evidence.planner_mode == FAST_PATH_MODE and not (set(tools(result)) & SLOW)
    assert result.response.response_type == ResponseType.PRODUCT_INFO


def test_efficacy_question_with_specific_function_is_still_a_claim():
    q = claim(f"{PRODUCT}可以{FUNCTION}嗎", ClaimSemanticCategory.HEALTH_FUNCTION, ClaimOrigin.USER_QUESTION,
              function=FUNCTION)
    result, _ = run("TEST_ONLY", q, intent=Intent.EFFICACY_INQUIRY)
    assert len(result.normalized_request.claims) == 1
    assert result.response.response_type == ResponseType.CLAIM_RESULT and not (set(tools(result)) & SLOW)
