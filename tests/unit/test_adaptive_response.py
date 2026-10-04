"""Agent C ResponsePayload、AdaptiveLineRenderer、request-scoped actions、單一 primary response。"""

import json
import threading
import uuid

import pytest
from pydantic import ValidationError

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent, compose_payload, error_payload
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.response.actions import build_postback_data, parse_postback_data
from app.response.line_renderer import MAX_BUTTONS, MAX_KEY_POINTS, AdaptiveLineRenderer
from app.response.service import EXPIRED_ACTION_TEXT, TIMEOUT_SUMMARY, UNAVAILABLE_ACTION_TEXT, AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.risk.risk_engine import DISCLAIMER_NOT_FOUND, RiskEngine
from app.schemas.enums import ClaimOrigin, ClaimSemanticCategory, InputType, Intent, ResponseAction, ResponseType, RiskLevel
from app.schemas.response import ResponsePayload
from app.schemas.user_request import IntakeExtraction, RawUserInput
from tests.builders import claim, extraction, request
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore

PRODUCT_1 = "虛構牌晶亮測試膠囊"
PRODUCT_2 = "虛構牌順暢測試優酪乳"
RISK_MARKS = ("🟢", "🟠", "🔴")
renderer = AdaptiveLineRenderer()


def compose(req):
    bundle = VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), FakeLLM()).verify(req)
    return compose_payload(req, bundle, RiskEngine().assess(req, bundle))


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def buttons(message):
    return [n["action"] for n in walk(message) if n.get("type") == "button"]


# ---------------------------------------------------------------- response_type 選擇


def test_ingredient_inquiry_has_no_risk_level():
    p = compose(request(intent=Intent.INGREDIENT_INQUIRY, ingredient_candidates=["虛構素甲"]))
    assert p.response_type == ResponseType.INGREDIENT_LIST and p.risk_level is None
    assert not any(m in json.dumps(renderer.render_primary(p), ensure_ascii=False) for m in RISK_MARKS)


def test_advertisement_claim_is_risk_card_with_high_mark():
    p = compose(request(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT_1], claims=[
        claim("可以治療某疾病", ClaimSemanticCategory.MEDICAL_EFFICACY, ClaimOrigin.ADVERTISEMENT_CLAIM)]))
    assert p.response_type == ResponseType.RISK and p.risk_level == RiskLevel.HIGH
    assert renderer.heading(p).startswith("🔴")


def test_verified_product_verification_is_product_info_without_risk_light():
    p = compose(request(product_candidates=[PRODUCT_1]))
    assert p.response_type == ResponseType.PRODUCT_INFO and p.risk_level is None
    assert not renderer.heading(p).startswith("🟢") and p.source_count >= 1
    assert ResponseAction.OFFICIAL_INFO in p.available_actions


def test_unknown_product_is_medium_and_not_called_fake():
    p = compose(request(product_candidates=["完全查不到的名字"]))
    assert p.risk_level == RiskLevel.MEDIUM and not p.is_conclusive and renderer.heading(p).startswith("🟠")
    assert DISCLAIMER_NOT_FOUND in p.warnings  # NOT_FOUND 不等於假貨，必要警語保留在 primary card


def test_efficacy_inquiry_on_verified_product_is_product_info():
    p = compose(request(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_2]))
    assert p.response_type == ResponseType.PRODUCT_INFO and p.risk_level is None
    assert any("胃腸功能改善" in k for k in p.key_points)


def test_user_question_claim_is_claim_result_unless_high_risk():
    p = compose(request(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT_2],
                        claims=[claim("可以改善胃腸功能", origin=ClaimOrigin.USER_QUESTION)]))
    expected = ResponseType.RISK if p.risk_level == RiskLevel.HIGH else ResponseType.CLAIM_RESULT
    assert p.response_type == expected


def test_out_of_scope_and_missing_context():
    assert compose(request(intent=Intent.OUT_OF_SCOPE)).response_type == ResponseType.OUT_OF_SCOPE
    p = compose(request(intent=Intent.SAFETY_INQUIRY, refers_to_unspecified_product=True))
    assert p.response_type == ResponseType.MISSING_INFORMATION and p.risk_level is None


def test_payload_risk_level_only_for_risk_type():
    with pytest.raises(ValidationError):
        ResponsePayload(request_id="r", response_type=ResponseType.PRODUCT_INFO, risk_level=RiskLevel.LOW, title="t", summary="s")
    with pytest.raises(ValidationError):
        ResponsePayload(request_id="r", response_type=ResponseType.RISK, title="t", summary="s")


def test_error_payload_offers_only_retry():
    p = error_payload(str(uuid.uuid4()))
    assert p.response_type == ResponseType.ERROR and p.available_actions == [ResponseAction.RETRY]


# ---------------------------------------------------------------- Renderer


def _big_payload(rtype=ResponseType.RISK):
    return ResponsePayload(
        request_id=str(uuid.uuid4()), response_type=rtype, risk_level=RiskLevel.MEDIUM if rtype == ResponseType.RISK else None,
        title="TEST_ONLY 標題", summary="TEST_ONLY 結論", key_points=[f"重點{i}" for i in range(6)],
        warnings=["警語1", "警語2", "警語3"], available_actions=list(ResponseAction)[:5], source_count=2,
    )


@pytest.mark.parametrize("rtype", list(ResponseType))
def test_every_response_type_renders_one_neutral_flex_message(rtype):
    msg = renderer.render_primary(_big_payload(rtype))
    assert msg["type"] == "flex" and msg["contents"]["type"] == "bubble" and len(msg["altText"]) <= 400
    assert not any("backgroundColor" in n for n in walk(msg))  # 不使用色塊背景
    texts = [n["text"] for n in walk(msg) if n.get("type") == "text"]
    assert sum(t.startswith("・") for t in texts) <= MAX_KEY_POINTS
    assert len(buttons(msg)) <= MAX_BUTTONS
    assert any(m in json.dumps(msg, ensure_ascii=False) for m in RISK_MARKS) == (rtype == ResponseType.RISK)


def test_every_button_is_request_scoped_postback():
    p = _big_payload()
    for action in buttons(renderer.render_primary(p)):
        parsed = parse_postback_data(action["data"])
        assert action["type"] == "postback" and parsed is not None and parsed.request_id == p.request_id


def test_postback_round_trip_and_rejects_malformed():
    rid = str(uuid.uuid4())
    assert parse_postback_data(build_postback_data(ResponseAction.DETAIL, rid)).request_id == rid
    for bad in ["", "action=detail", f"action=hack&request_id={rid}", "action=detail&request_id=latest",
                f"action=detail&request_id={rid}&extra=1", f"action=detail&action=why&request_id={rid}"]:
        assert parse_postback_data(bad) is None


# ---------------------------------------------------------------- Service：單一 primary、request-scoped、timeout


def _service(extractions, timeout=5.0, llm=None, pipeline=None):
    llm = llm or FakeLLM(structured={IntakeExtraction: lambda _: extractions.pop(0)})
    pipeline = pipeline or AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    return llm, AdaptiveResponseService(pipeline, ReporterAgent(llm), InMemoryRequestStore(), timeout_seconds=timeout)


def _text(raw="TEST_ONLY 問題"):
    return RawUserInput(input_type=InputType.TEXT, text=raw)


def _rid(message, action=None):
    for a in buttons(message):
        parsed = parse_postback_data(a["data"])
        if action is None or parsed.action == action:
            return parsed.request_id
    raise AssertionError("no button")


def test_primary_response_is_one_message_without_llm_report():
    llm, svc = _service([extraction(product_candidates=[PRODUCT_1], normalized_text="第一題")])
    msg = svc.respond(_text(), owner_id="U1")
    assert msg["type"] == "flex"
    assert not llm.called("generate_text")  # 完整分析只在按鈕後才產生


def test_old_button_returns_old_request_after_new_question():
    llm, svc = _service([
        extraction(product_candidates=[PRODUCT_1], normalized_text="第一題 晶亮"),
        extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_2], normalized_text="第二題 順暢"),
    ])
    first = svc.respond(_text("第一題"), owner_id="U1")
    svc.respond(_text("第二題"), owner_id="U1")
    rid = _rid(first, ResponseAction.OFFICIAL_INFO)
    detail = svc.handle_action(build_postback_data(ResponseAction.OFFICIAL_INFO, rid), owner_id="U1")
    dumped = json.dumps(detail, ensure_ascii=False)
    assert PRODUCT_1 in dumped and PRODUCT_2 not in dumped and "第一題" in dumped


def test_detail_generates_full_analysis_on_demand_only():
    llm, svc = _service([extraction(product_candidates=["完全查不到的名字"])])  # 風險卡才有「查看完整分析」
    rid = _rid(svc.respond(_text(), owner_id="U1"), ResponseAction.DETAIL)
    assert not llm.called("generate_text")
    detail = svc.handle_action(build_postback_data(ResponseAction.DETAIL, rid), owner_id="U1")
    assert llm.called("generate_text") and "TEST_ONLY 白話回覆正文" in json.dumps(detail, ensure_ascii=False)


def test_action_of_other_user_or_unknown_request_is_rejected():
    _, svc = _service([extraction(product_candidates=[PRODUCT_1])])
    rid = _rid(svc.respond(_text(), owner_id="U1"))
    for data, owner in [(build_postback_data(ResponseAction.WHY, rid), "U2"),
                        (build_postback_data(ResponseAction.WHY, str(uuid.uuid4())), "U1")]:
        assert EXPIRED_ACTION_TEXT in json.dumps(svc.handle_action(data, owner_id=owner), ensure_ascii=False)


def test_action_not_offered_for_that_request_is_rejected():
    _, svc = _service([extraction(intent=Intent.OUT_OF_SCOPE)])
    msg = svc.respond(_text(), owner_id="U1")
    assert buttons(msg) == []
    rid = next(iter(svc._store._data))
    out = svc.handle_action(build_postback_data(ResponseAction.DETAIL, rid), owner_id="U1")
    assert UNAVAILABLE_ACTION_TEXT in json.dumps(out, ensure_ascii=False)


class _SlowPipeline:
    def __init__(self, release: threading.Event, inner: AnalysisPipeline):
        self.release, self.inner, self.finished = release, inner, threading.Event()

    def run(self, raw, channel, request_id, conversation_id=None):
        self.release.wait(5)
        try:
            return self.inner.run(raw, channel, request_id, conversation_id)
        finally:
            self.finished.set()


def test_timeout_returns_error_card_and_late_result_is_discarded():
    llm = FakeLLM(structured={IntakeExtraction: extraction(product_candidates=[PRODUCT_1])})
    inner = AnalysisPipeline(IntakeAgent(llm), VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(),
                             InMemoryRagStore(), llm), RiskEngine(), ReporterAgent(llm))
    release = threading.Event()
    slow = _SlowPipeline(release, inner)
    _, svc = _service([], timeout=0.05, llm=llm, pipeline=slow)
    msg = svc.respond(_text(), owner_id="U1")
    assert TIMEOUT_SUMMARY in json.dumps(msg, ensure_ascii=False)
    rid = _rid(msg, ResponseAction.RETRY)
    release.set()
    assert slow.finished.wait(5)
    record = svc._store.get(rid)  # 晚到的結果不會覆蓋或補發
    assert record.response.response_type == ResponseType.ERROR and record.evidence is None


def test_retry_runs_again_with_new_request_id():
    llm, svc = _service([extraction(product_candidates=[PRODUCT_1]), extraction(product_candidates=[PRODUCT_1])])
    first = svc.respond(_text(), owner_id="U1")
    rid = _rid(first)
    again = svc.handle_action(build_postback_data(ResponseAction.RETRY, rid), owner_id="U1")
    assert again["type"] == "flex" and _rid(again) != rid
