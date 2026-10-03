"""LINE webhook handler：事件 → per-user FIFO queue → 同一條 pipeline → 唯一一則 LINE 訊息。

Text / URL / Image / Audio 都轉成 RawUserInput 交給 AdaptiveResponseService.respond()
（Agent A → Agent B → Risk Engine → Agent C → ResponsePayload → AdaptiveLineRenderer）；
Flex 按鈕的 postback 交給 handle_action()（依 request_id 取回那一題）；
Quick Reply 的「開始新問題 / 結束本次查證」只操作 task context。

送出策略
* reply token 只跟著該 job，不另外保存；處理完成時若仍新鮮（預設 < 50 秒）用 reply，否則改用 push。
* reply 回 400（token 失效）才改 push；push 帶 X-Line-Retry-Key = request_id，避免重送造成重複訊息。
* 處理中只顯示 LINE loading animation，不送「查詢中」文字。
"""

import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable
from urllib.parse import parse_qs

from app.line.client import LineApiError, LineMessenger
from app.line.events import LineEvent, split_urls
from app.line.request_queue import PerUserRequestQueue
from app.response.line_renderer import AdaptiveLineRenderer
from app.response.query_context import image_query_label
from app.agents.reporter_agent import ERROR_SUMMARY, error_payload
from app.response.service import TIMEOUT_SUMMARY, AdaptiveResponseService
from app.schemas.enums import InputType
from app.schemas.user_request import MediaInput, RawUserInput
from app.services.context_store import TaskContextStore

logger = logging.getLogger("nutri_safe_ai.line.handler")

REPLY_TOKEN_MAX_AGE_SECONDS = 50.0
MAX_TEXT_CHARS = 6000
MAX_SEEN_EVENTS = 2000

SESSION_NEW = "new"
SESSION_END = "end"
SESSION_NEW_LABEL = "開始新問題"
SESSION_END_LABEL = "結束本次查證"
SESSION_NEW_TEXT = "好的，已開始新的問題。請直接傳產品名稱、照片、語音或網址給我。"
SESSION_END_TEXT = "本次查證已結束，剛才的產品資訊已清除。有需要隨時再傳訊息給我。"
UNSUPPORTED_MESSAGE_TEXT = "目前可以傳文字、圖片、語音或網址給我查證。"
CONTENT_FAILED_TEXT = "這則圖片或語音沒有下載成功，請再傳一次。"

AUDIO_EXTENSIONS = {"audio/x-m4a": "m4a", "audio/m4a": "m4a", "audio/mp4": "m4a", "audio/aac": "aac",
                    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/ogg": "ogg"}


def session_postback_data(kind: str) -> str:
    return f"session={kind}"


def parse_session_postback(data: str) -> str | None:
    try:
        fields = parse_qs(data or "", strict_parsing=True, max_num_fields=2)
    except ValueError:
        return None
    values = fields.get("session", [])
    if set(fields) != {"session"} or len(values) != 1 or values[0] not in (SESSION_NEW, SESSION_END):
        return None
    return values[0]


def with_session_controls(message: dict[str, Any]) -> dict[str, Any]:
    """聊天室底部的 Quick Reply（不放進 Flex Card）；使用者不按也能直接提問。"""
    items = [
        {"type": "action", "action": {"type": "postback", "label": label, "data": session_postback_data(kind),
                                      "displayText": label}}
        for kind, label in ((SESSION_NEW, SESSION_NEW_LABEL), (SESSION_END, SESSION_END_LABEL))
    ]
    return {**message, "quickReply": {"items": items}}


class LineWebhookHandler:
    def __init__(
        self,
        service: AdaptiveResponseService,
        line: LineMessenger,
        contexts: TaskContextStore,
        queue: PerUserRequestQueue | None = None,
        renderer: AdaptiveLineRenderer | None = None,
        reply_token_max_age: float = REPLY_TOKEN_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._service = service
        self._line = line
        self._contexts = contexts
        self._queue = queue or PerUserRequestQueue()
        self._renderer = renderer or AdaptiveLineRenderer()
        self._reply_max_age = reply_token_max_age
        self._clock = clock
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._delivered: OrderedDict[str, None] = OrderedDict()
        self._seen_lock = Lock()

    @property
    def queue(self) -> PerUserRequestQueue:
        return self._queue

    def _already_seen(self, event: LineEvent) -> bool:
        """LINE 重送（redelivery）同一個 webhookEventId 時不重複回答。"""
        if not event.webhook_event_id:
            return False
        with self._seen_lock:
            if event.webhook_event_id in self._seen:
                return True
            self._seen[event.webhook_event_id] = None
            while len(self._seen) > MAX_SEEN_EVENTS:
                self._seen.popitem(last=False)
            return False

    def dispatch(self, event: LineEvent) -> str | None:
        """把一個事件排進該使用者的 FIFO queue；回傳 request_id（忽略的事件回傳 None）。"""
        if event.user_id is None or event.event_type not in ("message", "postback") or self._already_seen(event):
            return None
        request_id = str(uuid.uuid4())
        received_at = self._clock()
        sent_at = (datetime.fromtimestamp(event.timestamp_ms / 1000, timezone.utc) if event.timestamp_ms
                   else datetime.now(timezone.utc))
        # 圖片：request-scoped query context 在收到時就固定（排隊中的多張圖片各自有自己的時間）
        label = image_query_label(sent_at) if event.event_type == "message" and event.message_type == "image" else None
        logger.info("line_stage", extra={"request_id": request_id, "stage": "request_received",
                                         "input_type": event.message_type or event.event_type})
        self._queue.submit(event.user_id, request_id, lambda: self._run(event, request_id, received_at, sent_at, label),
                           on_timeout=lambda: self._deliver_error(event, request_id, received_at, TIMEOUT_SUMMARY,
                                                                  query_label=label))
        return request_id

    # ------------------------------------------------------------ job（在 queue worker 中執行）

    def _run(self, event: LineEvent, request_id: str, received_at: float, sent_at: datetime | None = None,
             query_label: str | None = None) -> None:
        """不論成功或任何例外，都恰好送出一則訊息（失敗時為一張 ERROR card）。"""
        try:
            self._line.start_loading(event.user_id)
        except Exception:  # noqa: BLE001 - loading animation 只是輔助
            logger.info("line_loading_failed", extra={"request_id": request_id})
        try:
            message = self._build_message(event, request_id, sent_at, query_label)
        except Exception:  # noqa: BLE001 - pipeline 以外的未預期錯誤也要回覆，不得讓使用者無回應
            logger.exception("line_stage", extra={"request_id": request_id, "stage": "error"})
            self._deliver_error(event, request_id, received_at, ERROR_SUMMARY, query_label=query_label)
            return
        self._deliver(event, request_id, received_at, with_session_controls(message))

    def _deliver_error(self, event: LineEvent, request_id: str, received_at: float, summary: str,
                       retry: bool = True, query_label: str | None = None) -> None:
        payload = error_payload(request_id, summary)
        if not retry:
            payload = payload.model_copy(update={"available_actions": []})
        message = self._renderer.render_primary(payload, query_label=query_label)
        self._deliver(event, request_id, received_at, with_session_controls(message))

    def _claim_delivery(self, request_id: str) -> bool:
        """每個 request 只送一次：watchdog 逾時卡與晚到的正常結果，先到者送出，後到者丟棄。"""
        with self._seen_lock:
            if request_id in self._delivered:
                return False
            self._delivered[request_id] = None
            while len(self._delivered) > MAX_SEEN_EVENTS:
                self._delivered.popitem(last=False)
            return True

    def _build_message(self, event: LineEvent, request_id: str, sent_at: datetime | None = None,
                       query_label: str | None = None) -> dict[str, Any]:
        user_id = event.user_id
        if event.event_type == "postback":
            session = parse_session_postback(event.postback_data)
            if session == SESSION_NEW:
                self._contexts.reset(user_id)
                return self._renderer.render_notice(SESSION_NEW_TEXT)
            if session == SESSION_END:
                self._contexts.close(user_id)
                return self._renderer.render_notice(SESSION_END_TEXT)
            return self._service.handle_action(event.postback_data, owner_id=user_id, request_id=request_id)

        if event.message_type in ("image", "audio") and event.message_id:
            raw = self._media_input(event, request_id)
            if raw is None:  # 下載失敗：一張 ERROR card（請使用者重傳，不提供無法重現的「重新查詢」）
                return self._renderer.render_primary(
                    error_payload(request_id, CONTENT_FAILED_TEXT).model_copy(update={"available_actions": []}),
                    query_label=query_label)
        else:
            raw = self._raw_input(event)
            if raw is None:
                return self._renderer.render_notice(UNSUPPORTED_MESSAGE_TEXT)
        return self._service.respond(raw, owner_id=user_id, request_id=request_id, received_at=sent_at)

    @staticmethod
    def _raw_input(event: LineEvent) -> RawUserInput | None:
        if event.message_type == "text":
            text, urls = split_urls(event.text[:MAX_TEXT_CHARS])
            if not urls:
                return RawUserInput(input_type=InputType.TEXT, text=text)
            return RawUserInput(input_type=InputType.MIXED if text else InputType.URL, text=text, urls=urls)
        return None

    def _media_input(self, event: LineEvent, request_id: str) -> RawUserInput | None:
        started = time.monotonic()
        logger.info("line_stage", extra={"request_id": request_id, "stage": "media_download_started",
                                         "input_type": event.message_type})
        try:
            content, mime = self._line.get_content(event.message_id)
        except Exception as exc:  # noqa: BLE001 - LineApiError / 網路錯誤 / 逾時都視為下載失敗
            logger.warning("line_stage", extra={
                "request_id": request_id, "stage": "media_download_failed",
                "error_code": str(getattr(exc, "status_code", type(exc).__name__)),
                "duration_ms": int((time.monotonic() - started) * 1000)})
            return None
        logger.info("line_stage", extra={"request_id": request_id, "stage": "media_download_done",
                                         "duration_ms": int((time.monotonic() - started) * 1000)})
        if event.message_type == "image":
            return RawUserInput(input_type=InputType.IMAGE,
                                media=[MediaInput(kind="image", content=content, mime_type=mime)])
        ext = AUDIO_EXTENSIONS.get(mime, "m4a")
        media = MediaInput(kind="audio", content=content, mime_type=mime, filename=f"line_audio.{ext}")
        return RawUserInput(input_type=InputType.AUDIO, media=[media])

    def _deliver(self, event: LineEvent, request_id: str, received_at: float, message: dict[str, Any]) -> None:
        if not self._claim_delivery(request_id):
            logger.info("line_stage", extra={"request_id": request_id, "stage": "late_result_discarded"})
            return
        age = self._clock() - received_at
        if event.reply_token and age < self._reply_max_age:
            try:
                self._line.reply(event.reply_token, [message])
                logger.info("line_stage", extra={"request_id": request_id, "stage": "delivery", "channel": "reply"})
                return
            except LineApiError as exc:
                if exc.status_code != 400:  # 非 token 失效：狀態不明，不改 push 以免重複
                    raise
        self._line.push(event.user_id, [message], retry_key=request_id)
        logger.info("line_stage", extra={"request_id": request_id, "stage": "delivery", "channel": "push"})
