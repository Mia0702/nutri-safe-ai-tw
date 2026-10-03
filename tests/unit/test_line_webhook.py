"""LINE webhook：簽章、Text / URL / Image / Audio → 同一條 pipeline → 唯一一則 Flex、
任務型短期上下文、per-user FIFO queue、request-scoped 按鈕、Quick Reply session controls、逾時。

全部使用 TEST_ONLY fake（FakeLLM / in-memory repo / FakeLine），不呼叫外部服務。
"""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.line.client import LineApiError
from app.line.events import parse_events, split_urls
from app.line.handler import (
    SESSION_END_TEXT,
    SESSION_NEW_TEXT,
    LineWebhookHandler,
    parse_session_postback,
    session_postback_data,
)
from app.line.request_queue import PerUserRequestQueue, RequestState
from app.line.signature import compute_signature, verify_signature
from app.pipeline import AnalysisPipeline
from app.response.actions import build_postback_data, parse_postback_data
from app.response.service import TIMEOUT_SUMMARY, AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import EntityType, InputType, Intent, ProductReferenceStatus, ResponseAction, ResponseType
from app.schemas.user_request import ContextEntity, IntakeExtraction
from app.services.context_store import TaskContextStore
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore

PRODUCT_1 = "虛構牌晶亮測試膠囊"
PRODUCT_2 = "虛構牌順暢測試優酪乳"
USER = "U_TEST_ONLY_1"
SECRET = "TEST_ONLY_channel_secret"


class FakeLine:
    """記錄送出的訊息；可指定 reply 失敗。"""

    def __init__(self, reply_status: int | None = None):
        self.sent: list[tuple[str, str, dict]] = []  # (mode, target, message)
        self.loading: list[str] = []
        self.reply_status = reply_status
        self.contents = {"img-1": (b"\x89PNG TEST_ONLY", "image/png"), "aud-1": (b"TEST_ONLY audio", "audio/x-m4a")}
        self.lock = threading.Lock()

    def reply(self, reply_token, messages):
        if self.reply_status:
            raise LineApiError(self.reply_status, "/v2/bot/message/reply")
        with self.lock:
            self.sent += [("reply", reply_token, m) for m in messages]

    def push(self, to, messages, retry_key=None):
        with self.lock:
            self.sent += [("push", f"{to}|{retry_key}", m) for m in messages]

    def get_content(self, message_id):
        if message_id not in self.contents:
            raise LineApiError(404, "content")
        return self.contents[message_id]

    def start_loading(self, chat_id, seconds=60):
        self.loading.append(chat_id)

    def dumps(self, i=-1) -> str:
        return json.dumps(self.sent[i][2], ensure_ascii=False)


def smart_extraction(input_text: str) -> IntakeExtraction:
    """TEST_ONLY：依 payload 內容模擬 Agent A（只用於 fake，不是 production 規則）。"""
    payload = json.loads(input_text)
    content = payload["content"]
    known = [e["name"] for e in payload["known_context_entities"] if e["type"] == "PRODUCT"]
    if "它" in content or "這個" in content:
        if known:
            return extraction(intent=Intent.EFFICACY_INQUIRY, refers_to_unspecified_product=True,
                              context_resolved_names=[known[0]], normalized_text=content)
        return extraction(intent=Intent.EFFICACY_INQUIRY, refers_to_unspecified_product=True, normalized_text=content)
    for name in (PRODUCT_1, PRODUCT_2):
        if name in content:
            return extraction(product_candidates=[name], normalized_text=content)
    return extraction(intent=Intent.OUT_OF_SCOPE, normalized_text=content)


def build(timeout=5.0, pipeline=None, line=None, reply_max_age=50.0, clock=None):
    llm = FakeLLM(structured={IntakeExtraction: smart_extraction})
    llm.describe_image = lambda **kw: f"TEST_ONLY 圖片文字：{PRODUCT_1}"
    llm.transcribe_audio = lambda **kw: f"TEST_ONLY 語音：{PRODUCT_2}"
    url_fetcher = lambda url: __import__("app.tools.input_tools", fromlist=["x"]).InputToolOutput(  # noqa: E731
        tool_name="fetch_url_content", text=f"TEST_ONLY 廣告頁：{PRODUCT_1}", source_url=url)
    pipeline = pipeline or AnalysisPipeline(
        intake=IntakeAgent(llm, url_fetcher=url_fetcher),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    contexts = TaskContextStore()
    store = InMemoryRequestStore()
    svc = AdaptiveResponseService(pipeline, ReporterAgent(llm), store, timeout_seconds=timeout, context_store=contexts)
    line = line or FakeLine()
    kw = {"clock": clock} if clock else {}
    handler = LineWebhookHandler(svc, line, contexts, reply_token_max_age=reply_max_age, **kw)
    return handler, line, contexts, store


_counter = iter(range(10**6))


def event(kind="text", text="", message_id=None, data="", user=USER, event_id=None):
    n = next(_counter)
    base = {"type": "message" if kind != "postback" else "postback", "replyToken": f"rt-{n}",
            "source": {"type": "user", "userId": user}, "webhookEventId": event_id or f"ev-{n}",
            "deliveryContext": {"isRedelivery": False}}
    if kind == "postback":
        base["postback"] = {"data": data}
    else:
        base["message"] = {"type": kind, "id": message_id or f"m-{n}", **({"text": text} if kind == "text" else {})}
    return parse_events({"events": [base]})[0]


def send(handler, *events):
    rids = [handler.dispatch(e) for e in events]
    assert handler.queue.wait_idle(10)
    return rids


def buttons(message):
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "button":
                out.append(n["action"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(message.get("contents"))
    return out


def button_data(message, action):
    for b in buttons(message):
        parsed = parse_postback_data(b["data"])
        if parsed and parsed.action == action:
            return b["data"]
    raise AssertionError(f"no {action} button")


# ---------------------------------------------------------------- signature / events


def test_signature_verification():
    body = b'{"events":[]}'
    sig = compute_signature(SECRET, body)
    assert verify_signature(SECRET, body, sig)
    assert not verify_signature(SECRET, body + b" ", sig)
    assert not verify_signature(SECRET, body, None)


def test_webhook_rejects_bad_signature_and_accepts_valid(monkeypatch):
    from app import main
    from app.config.settings import Settings, get_settings

    settings = Settings(line_channel_secret=SECRET, line_channel_access_token="TEST_ONLY_token")
    main.app.dependency_overrides[get_settings] = lambda: settings
    handler, line, *_ = build()
    monkeypatch.setattr("app.api.line_webhook.get_line_handler", lambda: handler)
    try:
        client = TestClient(main.app)
        body = json.dumps({"events": [{"type": "message", "replyToken": "rt-x", "webhookEventId": "ev-x",
                                       "source": {"type": "user", "userId": USER},
                                       "message": {"type": "text", "id": "1", "text": PRODUCT_1}}]},
                          ensure_ascii=False).encode()
        assert client.post("/line/webhook", content=body, headers={"X-Line-Signature": "bad"}).status_code == 400
        ok = client.post("/line/webhook", content=body, headers={"X-Line-Signature": compute_signature(SECRET, body)})
        assert ok.status_code == 200
        assert handler.queue.wait_idle(10)
        assert len(line.sent) == 1 and line.sent[0][2]["type"] == "flex"
    finally:
        main.app.dependency_overrides.clear()


def test_split_urls():
    assert split_urls("看這個 https://example.com/a?b=1 可以嗎") == ("看這個 可以嗎", ["https://example.com/a?b=1"])
    assert split_urls("沒有網址") == ("沒有網址", [])


# ---------------------------------------------------------------- 四種輸入 → 同一條 pipeline → 一則 Flex


@pytest.mark.parametrize("ev_kwargs, tool, input_type", [
    ({"kind": "text", "text": f"{PRODUCT_1} 有登記嗎"}, None, InputType.TEXT),
    ({"kind": "text", "text": "https://example.com/ad"}, "fetch_url_content", InputType.URL),
    ({"kind": "image", "message_id": "img-1"}, "analyze_image", InputType.IMAGE),
    ({"kind": "audio", "message_id": "aud-1"}, "transcribe_audio", InputType.AUDIO),
])
def test_each_input_type_returns_exactly_one_flex_with_quick_reply(ev_kwargs, tool, input_type):
    handler, line, _, store = build()
    rid = send(handler, event(**ev_kwargs))[0]
    assert len(line.sent) == 1
    mode, _, msg = line.sent[0]
    assert mode == "reply" and msg["type"] == "flex"
    labels = [i["action"]["label"] for i in msg["quickReply"]["items"]]
    assert labels == ["開始新問題", "結束本次查證"]
    assert not any(b["label"] in labels for b in buttons(msg))  # session controls 不在 Flex 卡片裡
    record = store.get(rid)
    assert record.normalized_request.input_type == input_type
    assert [t.tool_name for t in record.normalized_request.input_tools_called] == ([tool] if tool else [])
    assert record.normalized_request.product_candidates  # 圖片 / 語音 / 網址內容真的進到 Agent A
    assert line.loading == [USER]


def test_failed_media_download_returns_one_notice():
    handler, line, *_ = build()
    send(handler, event("image", message_id="missing"))
    assert len(line.sent) == 1 and "沒有下載成功" in line.dumps()


# ---------------------------------------------------------------- 任務型短期上下文


def test_follow_up_pronoun_resolves_to_previous_product():
    handler, line, contexts, store = build()
    first, second = send(handler, event(text=f"{PRODUCT_1} 有登記嗎"), event(text="那它可以治療失眠嗎"))
    req = store.get(second).normalized_request
    assert req.product_reference_status == ProductReferenceStatus.RESOLVED_FROM_CONTEXT
    assert req.product_candidates == [PRODUCT_1]
    ctx = contexts.snapshot(USER)
    assert ctx.current_product_name == PRODUCT_1 and ctx.recent_request_ids == [first, second]


def test_pronoun_without_context_asks_for_product():
    handler, line, _, store = build()
    rid = send(handler, event(text="它可以吃嗎"))[0]
    assert store.get(rid).response.response_type == ResponseType.MISSING_INFORMATION


def test_start_new_question_clears_previous_product():
    handler, line, contexts, store = build()
    rids = send(handler, event(text=f"{PRODUCT_1} 有登記嗎"),
                event("postback", data=session_postback_data("new")), event(text="它可以吃嗎"))
    assert SESSION_NEW_TEXT in line.dumps(1)
    assert store.get(rids[2]).response.response_type == ResponseType.MISSING_INFORMATION
    assert contexts.snapshot(USER).current_product_name is None


def test_end_session_clears_context():
    handler, line, contexts, _ = build()
    send(handler, event(text=f"{PRODUCT_1} 有登記嗎"), event("postback", data=session_postback_data("end")))
    assert SESSION_END_TEXT in line.dumps()
    ctx = contexts.snapshot(USER)
    assert ctx.current_product_name is None and ctx.turns == [] and ctx.recent_request_ids == []


def test_task_context_ttl_and_stale_session_result_is_dropped():
    now = [datetime(2026, 10, 3, tzinfo=timezone.utc)]
    store = TaskContextStore(clock=lambda: now[0])
    entity = ContextEntity(entity_type=EntityType.PRODUCT, name=PRODUCT_1, entity_id="p1")
    old = store.snapshot(USER)
    store.reset(USER)  # 使用者按了「開始新問題」
    assert not store.record(USER, old.session_id, "r1", "舊題", Intent.PRODUCT_VERIFICATION, [entity])
    cur = store.snapshot(USER)
    assert store.record(USER, cur.session_id, "r2", "新題", Intent.PRODUCT_VERIFICATION, [entity])
    assert store.snapshot(USER).current_product_id == "p1"
    now[0] += timedelta(minutes=31)
    assert store.snapshot(USER).current_product_name is None  # 超過 TTL 自動清除


def test_task_context_keeps_only_recent_requests():
    store = TaskContextStore(max_requests=3)
    sid = store.snapshot(USER).session_id
    for i in range(5):
        store.record(USER, sid, f"r{i}", f"第{i}題", None, [])
    ctx = store.snapshot(USER)
    assert ctx.recent_request_ids == ["r2", "r3", "r4"] and len(ctx.turns) == 3


def test_session_postback_parsing():
    assert parse_session_postback("session=new") == "new"
    assert parse_session_postback("session=end") == "end"
    assert parse_session_postback("session=other") is None
    assert parse_session_postback("action=detail&request_id=x") is None


# ---------------------------------------------------------------- FIFO queue


class _GatedPipeline:
    """第一題卡住直到 release，用來模擬 A 尚未完成時 B 已送達。"""

    def __init__(self, inner, gate_text):
        self.inner, self.gate_text = inner, gate_text
        self.release, self.finished = threading.Event(), threading.Event()

    def run(self, raw, channel, request_id, conversation_id=None):
        if self.gate_text in raw.text:
            self.release.wait(5)
        try:
            return self.inner.run(raw, channel, request_id, conversation_id)
        finally:
            if self.gate_text in raw.text:
                self.finished.set()


def _gated(timeout=5.0):
    handler, *_ = build()
    inner = handler._service._pipeline
    gated = _GatedPipeline(inner, "GATE")
    return gated, build(timeout=timeout, pipeline=gated)


def test_replies_follow_fifo_order_per_user():
    gated, (handler, line, _, store) = _gated()
    a = handler.dispatch(event(text=f"GATE {PRODUCT_1}"))
    b = handler.dispatch(event(text=f"{PRODUCT_2}"))
    threading.Event().wait(0.2)
    assert handler.queue.state(a) == RequestState.PROCESSING and handler.queue.state(b) == RequestState.QUEUED
    assert line.sent == []  # B 不會先於 A 回覆
    gated.release.set()
    assert handler.queue.wait_idle(10)
    assert [PRODUCT_1 in line.dumps(0), PRODUCT_2 in line.dumps(1)] == [True, True]
    assert handler.queue.state(a) == handler.queue.state(b) == RequestState.DONE


def test_different_users_run_in_parallel():
    gated, (handler, line, *_) = _gated()
    handler.dispatch(event(text=f"GATE {PRODUCT_1}", user="U_A"))
    handler.dispatch(event(text=PRODUCT_2, user="U_B"))
    deadline = threading.Event()
    for _ in range(50):
        if line.sent:
            break
        deadline.wait(0.1)
    assert len(line.sent) == 1 and "U_A" not in line.sent[0][1]  # U_B 不被 U_A 卡住
    gated.release.set()
    assert handler.queue.wait_idle(10)


def test_timeout_sends_one_error_card_and_late_result_never_arrives():
    gated, (handler, line, contexts, store) = _gated(timeout=0.1)
    rid = send(handler, event(text=f"GATE {PRODUCT_1}"))[0]
    assert len(line.sent) == 1 and TIMEOUT_SUMMARY in line.dumps()
    gated.release.set()
    assert gated.finished.wait(5)
    threading.Event().wait(0.2)
    assert len(line.sent) == 1  # 晚到結果不補發
    assert store.get(rid).response.response_type == ResponseType.ERROR
    assert contexts.snapshot(USER).current_product_name is None  # 也不會寫進對話 context


# ---------------------------------------------------------------- request-scoped 按鈕


def test_old_card_sources_button_returns_old_request_after_new_question():
    handler, line, *_ = build()
    send(handler, event(text=f"{PRODUCT_1} 有登記嗎"), event(text=f"{PRODUCT_2} 有登記嗎"))
    old_card = line.sent[0][2]
    send(handler, event("postback", data=button_data(old_card, ResponseAction.SOURCES)))
    detail = line.dumps()
    assert PRODUCT_1 in detail and PRODUCT_2 not in detail


def test_old_card_retry_uses_its_own_question():
    gated, (handler, line, _, store) = _gated(timeout=0.1)
    send(handler, event(text=f"GATE {PRODUCT_1}"))
    gated.release.set()
    gated.finished.wait(5)
    retry = button_data(line.sent[0][2], ResponseAction.RETRY)
    send(handler, event(text=PRODUCT_2))
    send(handler, event("postback", data=retry))
    assert len(line.sent) == 3 and PRODUCT_1 in line.dumps(2)


# ---------------------------------------------------------------- 送出策略


def test_fresh_reply_token_uses_reply_and_stale_uses_push_with_retry_key():
    handler, line, *_ = build(clock=iter([0.0, 10.0]).__next__, reply_max_age=50)
    send(handler, event(text=PRODUCT_1))
    assert line.sent[0][0] == "reply"
    handler, line, *_ = build(clock=iter([0.0, 999.0]).__next__, reply_max_age=50)
    rid = send(handler, event(text=PRODUCT_1))[0]
    assert line.sent[0][0] == "push" and line.sent[0][1] == f"{USER}|{rid}"


def test_invalid_reply_token_falls_back_to_push_once():
    handler, line, *_ = build(line=FakeLine(reply_status=400))
    send(handler, event(text=PRODUCT_1))
    assert [m for m, *_ in line.sent] == ["push"]


def test_redelivered_event_is_answered_once():
    handler, line, *_ = build()
    send(handler, event(text=PRODUCT_1, event_id="dup"), event(text=PRODUCT_1, event_id="dup"))
    assert len(line.sent) == 1


def test_queue_job_failure_does_not_block_next_request():
    q = PerUserRequestQueue(max_workers=2)
    done = []
    q.submit(USER, "r1", lambda: 1 / 0)
    q.submit(USER, "r2", lambda: done.append("r2"))
    assert q.wait_idle(5)
    assert q.state("r1") == RequestState.FAILED and q.state("r2") == RequestState.DONE and done == ["r2"]


def test_postback_requires_action_and_request_id():
    rid = "00000000-0000-4000-8000-000000000001"
    assert parse_postback_data(build_postback_data(ResponseAction.SOURCES, rid)).request_id == rid
    assert parse_postback_data("action=sources") is None
