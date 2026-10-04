"""Demo smoke test（10/7 展示主線）：真實 LINE webhook 簽章 → 真實 pipeline（OpenAI / Supabase / Neo4j）
→ AdaptiveLineRenderer → 每則訊息送 LINE 官方 validate API 確認 Flex 被接受。

限制（無法在自動測試中做到的部分）：
* 沒有真實 LINE 使用者，因此不呼叫 reply / push / loading；改把要送出的訊息交給
  `/v2/bot/message/validate/reply`（LINE 只驗證、不送出）。
* LINE 的圖片 / 語音 content 只存在於真實使用者訊息；這裡用 Windows 內建 System.Drawing / System.Speech
  產生 TEST_ONLY 合成圖片與語音，作為「下載到的內容」交給 Agent A。

執行：pytest tests/integration/test_line_demo_live.py -m integration -s
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.intake_agent import REINSPECTED_NOTE, needs_identity_reinspection
from app.line.client import LineMessagingClient
from app.line.handler import LineWebhookHandler, session_postback_data
from app.line.signature import compute_signature
from app.response.actions import parse_postback_data
from app.response.service import AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.schemas.enums import ProductReferenceStatus, ResponseAction, ResponseType
from app.services.context_store import TaskContextStore
from tests.integration.conftest import skip_external

pytestmark = pytest.mark.integration

# TEST_ONLY synthetic 內容（虛構產品，不對應任何真實產品）
PRODUCT = "虛構牌晶亮測試膠囊"
IMAGE_TEXT = f"{PRODUCT}\n保證根治失眠\n三天見效"
AUDIO_TEXT = f"請問{PRODUCT}可以降血糖嗎"
DEMO_URL = os.getenv("E2E_DEMO_URL", "https://www.fda.gov.tw/TC/newsContent.aspx?cid=4&id=t634723")
MEDIA_SCRIPT = Path(__file__).with_name("make_demo_media.ps1")
WAIT_SECONDS = 400


class ValidatingLine:
    """reply / push 改為呼叫 LINE validate API（不送出），並記錄訊息與順序。"""

    def __init__(self, real: LineMessagingClient):
        self.real = real
        self.sent: list[tuple[str, dict]] = []  # (target user, message)
        self.contents: dict[str, tuple[bytes, str]] = {}

    def _validate(self, user, messages):
        for m in messages:
            self.real.validate_reply([m])  # 格式錯誤會拋 LineApiError（status 400 + detail）
            self.sent.append((user, m))

    def reply(self, reply_token, messages):
        self._validate(reply_token.split("|")[0], messages)

    def push(self, to, messages, retry_key=None):
        self._validate(to, messages)

    def get_content(self, message_id):
        return self.contents[message_id]

    def start_loading(self, chat_id, seconds=60):
        pass  # 需要真實使用者


def _make_media(kind: str, text: str, out: Path) -> bytes:
    if sys.platform != "win32":
        skip_external("合成圖片 / 語音需要 Windows 內建 System.Drawing / System.Speech")
    env = {**os.environ, "DEMO_KIND": kind, "DEMO_TEXT": text, "DEMO_OUT": str(out)}
    subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(MEDIA_SCRIPT)],
                   env=env, check=True, capture_output=True, timeout=120)
    return out.read_bytes()


@pytest.fixture(scope="module")
def demo(live_settings):
    if not (live_settings.openai_configured and live_settings.line_configured):
        skip_external("需要 OPENAI_API_KEY 與 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN")
    from app import main
    from app.dependencies import build_pipeline

    pipeline = build_pipeline(live_settings)
    contexts, store = TaskContextStore(), InMemoryRequestStore()
    service = AdaptiveResponseService(pipeline, pipeline.reporter, store,
                                      timeout_seconds=live_settings.response_timeout_seconds, context_store=contexts)
    line = ValidatingLine(LineMessagingClient(live_settings.line_channel_access_token))
    with tempfile.TemporaryDirectory(prefix="nutri_demo_media_") as tmp:
        line.contents["img-demo"] = (_make_media("image", IMAGE_TEXT, Path(tmp) / "demo.png"), "image/png")
        line.contents["aud-demo"] = (_make_media("audio", AUDIO_TEXT, Path(tmp) / "demo.wav"), "audio/wav")
    handler = LineWebhookHandler(service, line, contexts)
    original = main.app.dependency_overrides.copy()
    import app.api.line_webhook as webhook

    saved = webhook.get_line_handler
    webhook.get_line_handler = lambda: handler
    client = TestClient(main.app)
    yield {"client": client, "line": line, "store": store, "contexts": contexts, "handler": handler,
           "secret": live_settings.line_channel_secret.get_secret_value()}
    webhook.get_line_handler = saved
    main.app.dependency_overrides = original


_n = iter(range(10**6))


def _event(user, kind="text", text="", message_id=None, data=""):
    n = next(_n)
    ev = {"type": "postback" if kind == "postback" else "message", "replyToken": f"{user}|rt-{n}",
          "webhookEventId": f"E2E-{time.time_ns()}-{n}", "source": {"type": "user", "userId": user},
          "deliveryContext": {"isRedelivery": False}}
    if kind == "postback":
        ev["postback"] = {"data": data}
    else:
        ev["message"] = {"type": kind, "id": message_id or f"m-{n}", **({"text": text} if kind == "text" else {})}
    return ev


def _post(demo, *events):
    body = json.dumps({"destination": "TEST_ONLY", "events": list(events)}, ensure_ascii=False).encode("utf-8")
    resp = demo["client"].post("/line/webhook", content=body,
                               headers={"X-Line-Signature": compute_signature(demo["secret"], body)})
    assert resp.status_code == 200
    assert demo["handler"].queue.wait_idle(WAIT_SECONDS)


def _messages(demo, user):
    return [m for u, m in demo["line"].sent if u == user]


def _show(title, message, record=None):
    contents = json.dumps(message["contents"], ensure_ascii=False)
    print(f"\n===== {title} =====\naltText: {message['altText']}")
    if record is not None and record.normalized_request is not None:
        req = record.normalized_request
        print("input_tools:", [t.tool_name for t in req.input_tools_called], "| products:", req.product_candidates,
              "| reference:", req.product_reference_status.value)
        print("response_type:", record.response.response_type.value, "| risk:", record.response.risk_level,
              "| actions:", [a.value for a in record.response.available_actions])
    print("flex bytes:", len(contents))


def _record_for(demo, message, user=None):
    for b in _walk_buttons(message):
        parsed = parse_postback_data(b["data"])
        if parsed:
            return demo["store"].get(parsed.request_id)
    # 沒有按鈕的卡片（例如查無產品的 MISSING_INFORMATION，2026-10-04 起不放按鈕）：以 altText 中的結論對應該題紀錄
    alt = message.get("altText", "")
    matches = [r for r in demo["store"]._data.values() if r.response is not None and r.response.summary
               and r.response.summary[:20] in alt and (user is None or r.owner_id == user)]
    return matches[0] if len(matches) == 1 else None


def _walk_buttons(message):
    found = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "button":
                found.append(n["action"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(message["contents"])
    return found


def _assert_primary(message):
    assert message["type"] == "flex"
    assert [i["action"]["label"] for i in message["quickReply"]["items"]] == ["開始新問題", "結束本次查證"]


@pytest.mark.parametrize("kind", ["text", "image", "audio", "url"])
def test_input_to_line_flex(demo, kind):
    user = f"U_E2E_{kind}"
    ev = {"text": _event(user, text=f"{PRODUCT}有沒有通過健康食品認證？"),
          "image": _event(user, "image", message_id="img-demo"),
          "audio": _event(user, "audio", message_id="aud-demo"),
          "url": _event(user, text=DEMO_URL)}[kind]
    _post(demo, ev)
    msgs = _messages(demo, user)
    assert len(msgs) == 1  # 每個 request 只有一則 primary response，且已通過 LINE validate API
    _assert_primary(msgs[0])
    record = _record_for(demo, msgs[0])
    _show(kind, msgs[0], record)
    assert record is None or record.response.response_type != ResponseType.ERROR, msgs[0]["altText"]
    if record is not None and kind == "image":
        _assert_image_perception_contract(record)
    elif record is not None and kind != "text":
        expected = {"audio": "transcribe_audio", "url": "fetch_url_content"}[kind]
        assert [t.tool_name for t in record.normalized_request.input_tools_called] == [expected]


def _assert_image_perception_contract(record):
    """FC-B6：analyze_image 一定是第一個圖片讀取工具；首次辨識後身份仍未確認才允許最多一次局部重讀；
    首次已確認（字號 / exact identity）時不得重讀。"""
    req = record.normalized_request
    tools = [t.tool_name for t in req.input_tools_called]
    assert tools[0] == "analyze_image"
    rereads = tools.count("reinspect_image_identity")
    assert rereads <= 1 and set(tools) <= {"analyze_image", "reinspect_image_identity"}
    if rereads:
        assert tools == ["analyze_image", "reinspect_image_identity"] and REINSPECTED_NOTE in req.adapter_notes
    else:  # 沒有重讀：首次結果必須是不需要重讀的狀態（已確認、或不符合重讀條件）
        assert not needs_identity_reinspection(req, record.evidence, has_image=True)


def test_follow_up_pronoun_and_session_controls(demo):
    user = "U_E2E_context"
    _post(demo, _event(user, text=f"{PRODUCT}有沒有登記？"))
    _post(demo, _event(user, text="那它可以治療失眠嗎？"))
    msgs = _messages(demo, user)
    assert len(msgs) == 2
    second = _record_for(demo, msgs[1])
    _show("follow-up", msgs[1], second)
    if second is None:
        pytest.fail("follow-up 沒有可追溯的 request（可能是逾時 notice）")
    req = second.normalized_request
    assert req.product_reference_status == ProductReferenceStatus.RESOLVED_FROM_CONTEXT
    assert PRODUCT in req.product_candidates

    _post(demo, _event(user, "postback", data=session_postback_data("new")))
    assert demo["contexts"].snapshot(user).current_product_name is None
    _post(demo, _event(user, text="它可以吃嗎？"))
    third = _record_for(demo, _messages(demo, user)[-1])
    _show("after new session", _messages(demo, user)[-1], third)
    assert third is None or third.normalized_request.product_reference_status != ProductReferenceStatus.RESOLVED_FROM_CONTEXT

    _post(demo, _event(user, "postback", data=session_postback_data("end")))
    ctx = demo["contexts"].snapshot(user)
    assert ctx.current_product_name is None and ctx.turns == []


def test_fifo_order_and_old_card_sources(demo):
    user = "U_E2E_fifo"
    _post(demo, _event(user, text=f"{PRODUCT}有沒有登記？"), _event(user, text="魚油可以降血壓嗎？"))
    msgs = _messages(demo, user)
    assert len(msgs) == 2
    first, second = (_record_for(demo, m, user) for m in msgs)
    assert first is not None and second is not None
    assert PRODUCT in first.normalized_request.original_text  # A 的回覆一定在 B 之前
    assert "魚油" in second.normalized_request.original_text

    sources = [b for b in _walk_buttons(msgs[0]) if parse_postback_data(b["data"]).action == ResponseAction.SOURCES]
    if not sources:
        pytest.skip(f"第一題沒有官方來源（{first.response.response_type.value}），無法在 live 驗證「查看資料來源」；"
                    "unit test 已覆蓋")
    _post(demo, _event(user, "postback", data=sources[0]["data"]))
    detail = json.dumps(_messages(demo, user)[-1], ensure_ascii=False)
    assert first.response.sources[0].source_name in detail
