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
from app.line.image_burst import (
    AUTO_IMAGE_BURST_WINDOW_MS,
    COLLECTION_MODE_AUTO,
    MAX_BURST_IMAGES,
    AutoImageBurstStore,
    BurstAddKind,
    BurstItem,
    ImageBurst,
)
from app.line.request_queue import PerUserRequestQueue
from app.response.actions import parse_postback_data
from app.response.line_renderer import AdaptiveLineRenderer
from app.response.query_context import image_query_label
from app.agents.reporter_agent import ERROR_SUMMARY, error_payload
from app.response.service import TIMEOUT_SUMMARY, AdaptiveResponseService
from app.schemas.enums import InputType
from app.schemas.user_request import MediaInput, RawUserInput
from app.services.context_store import TaskContextStore
from app.services.image_collection import (
    MAX_COLLECTED_IMAGES,
    MIN_COLLECTED_IMAGES,
    AddOutcome,
    ImageCollectionStore,
    MultiImageCollectionSession,
)
from app.tools.input_tools import IMAGE_MIME_TYPES, MAX_IMAGE_BYTES

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


# ---- 多照片辨識（明確蒐集模式；不依時間推測哪幾張是同一組）
MULTI_START_COMMAND = "多照片辨識"
MULTI_ANALYZE_LABEL = "開始分析"
MULTI_CANCEL_LABEL = "取消"
MULTI_START_LABEL = "多照片辨識"
MULTI_START_TEXT = f"請傳同一產品的 {MIN_COLLECTED_IMAGES}–{MAX_COLLECTED_IMAGES} 張照片。\n照片傳完後按『開始分析』。"
AUTO_BURST_FULL_TEXT = f"一次最多只能比對 {MAX_BURST_IMAGES} 張照片，這張沒有加入。"
MULTI_CANCELLED_TEXT = "已取消多照片辨識，剛才收到的照片已清除。"
MULTI_NO_SESSION_TEXT = "目前沒有待分析的多張照片。需要的話請先傳「多照片辨識」。"
MULTI_NEED_MORE_TEXT = "還沒有收到照片，請先傳至少 1 張同一產品的照片，再按『開始分析』。"
MULTI_FULL_TEXT = f"最多只能放 {MAX_COLLECTED_IMAGES} 張，這張沒有加入。請按「開始分析」，或取消後重新開始。"


def multi_postback_data(action: str, session_id: str) -> str:
    return f"multi={action}&sid={session_id}"


def parse_multi_postback(data: str) -> tuple[str, str] | None:
    try:
        fields = parse_qs(data or "", strict_parsing=True, max_num_fields=3)
    except ValueError:
        return None
    action, sid = fields.get("multi", []), fields.get("sid", [])
    if fields.keys() == {"multi"} and action == ["start"]:  # 進入蒐集模式的按鈕（還沒有 session）
        return "start", ""
    if set(fields) != {"multi", "sid"} or len(action) != 1 or len(sid) != 1 or action[0] not in ("analyze", "cancel"):
        return None
    return action[0], sid[0]


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


def with_session_controls(message: dict[str, Any], multi_entry: bool = False) -> dict[str, Any]:
    """聊天室底部的 Quick Reply（不放進 Flex Card）；使用者不按也能直接提問。
    multi_entry：只在「開始新問題」的回覆附上「多照片辨識」入口（不放在每張結果卡上）。"""
    items = [
        {"type": "action", "action": {"type": "postback", "label": label, "data": session_postback_data(kind),
                                      "displayText": label}}
        for kind, label in ((SESSION_NEW, SESSION_NEW_LABEL), (SESSION_END, SESSION_END_LABEL))
    ]
    if multi_entry:
        items.append({"type": "action", "action": {"type": "postback", "label": MULTI_START_LABEL,
                                                   "data": "multi=start", "displayText": MULTI_START_LABEL}})
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
        collections: ImageCollectionStore | None = None,
        image_burst_window_ms: int = 0,
        burst_timer_factory: Callable[..., Any] | None = None,
    ):
        """image_burst_window_ms：Automatic Image Burst Gate 的 debounce window（毫秒）；0 = 停用（圖片立即處理）。
        正式環境由設定提供（預設 AUTO_IMAGE_BURST_WINDOW_MS）。"""
        self._collections = collections if collections is not None else ImageCollectionStore()
        burst_kwargs = {"timer_factory": burst_timer_factory} if burst_timer_factory else {}
        self._bursts = AutoImageBurstStore(image_burst_window_ms, self._finalize_burst, clock=clock, **burst_kwargs)
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

    @property
    def bursts(self) -> AutoImageBurstStore:
        return self._bursts

    def dispatch(self, event: LineEvent) -> str | None:
        """把一個事件排進該使用者的 FIFO queue；回傳 request_id（忽略的事件回傳 None）。

        圖片（不在明確蒐集模式）先進 Automatic Image Burst Gate（debounce 後才建立「一個」分析 job）；
        其他事件到達時，這位使用者尚未 finalize 的圖片先 finalize（維持 FIFO 順序），
        「開始新問題 / 結束本次查證」則直接丟棄它們。
        """
        if event.user_id is None or event.event_type not in ("message", "postback") or self._already_seen(event):
            return None
        if self._is_auto_burst_image(event):
            return self._buffer_burst_image(event)
        if self._bursts.pending_count(event.user_id):
            if event.event_type == "postback" and parse_session_postback(event.postback_data) is not None:
                self._bursts.discard(event.user_id)
                logger.info("line_stage", extra={"stage": "auto_image_burst_discarded", "collection_mode": COLLECTION_MODE_AUTO})
            else:
                self._submit_burst(self._bursts.flush(event.user_id))
        request_id = str(uuid.uuid4())
        received_at = self._clock()
        sent_at = (datetime.fromtimestamp(event.timestamp_ms / 1000, timezone.utc) if event.timestamp_ms
                   else datetime.now(timezone.utc))
        # 圖片：request-scoped query context 在收到時就固定（排隊中的多張圖片各自有自己的時間）
        label = image_query_label(sent_at) if event.event_type == "message" and event.message_type == "image" else None
        logger.info("line_stage", extra={"request_id": request_id, "stage": "request_received",
                                         "input_type": event.message_type or event.event_type,
                                         **self._postback_log_fields(event)})
        self._queue.submit(event.user_id, request_id, lambda: self._run(event, request_id, received_at, sent_at, label),
                           on_timeout=lambda: self._deliver_error(event, request_id, received_at, TIMEOUT_SUMMARY,
                                                                  query_label=label))
        return request_id

    @staticmethod
    def _postback_log_fields(event: LineEvent) -> dict[str, str]:
        """postback 的 log 欄位：postback_action（統一名稱）＋ source_request_id（被點的那一張卡片所屬的原 request；
        目前這個 postback 自己的 request_id 另外記在 request_id）。不記錄 postback 原文。"""
        if event.event_type != "postback":
            return {}
        if (session := parse_session_postback(event.postback_data)) is not None:
            return {"postback_action": f"session_{session}"}
        if (multi := parse_multi_postback(event.postback_data)) is not None:
            return {"postback_action": f"multi_{multi[0]}"}
        if (act := parse_postback_data(event.postback_data)) is not None:
            return {"postback_action": act.action.value, "source_request_id": act.request_id}
        return {"postback_action": "invalid"}

    # ------------------------------------------------------------ Automatic Image Burst Gate

    def _is_auto_burst_image(self, event: LineEvent) -> bool:
        """明確蒐集模式（ImageCollectionSession）優先：蒐集中的圖片走原本的靜默保存，不經過 gate。"""
        return (self._bursts.enabled and event.event_type == "message" and event.message_type == "image"
                and bool(event.message_id) and self._collections.active(event.user_id) is None)

    def _buffer_burst_image(self, event: LineEvent) -> str | None:
        """只緩衝 event 識別碼（不下載、不分析、不佔用 FIFO queue）；window 由 timer 計時，不在 queue 裡 sleep。"""
        kind, burst = self._bursts.add(event.user_id, BurstItem(event=event, received_at=self._clock()))
        if kind == BurstAddKind.OVERFLOW:  # 超過 3 張：沿用既有上限政策，只回輕量提示
            request_id = str(uuid.uuid4())
            received_at = self._clock()
            self._queue.submit(event.user_id, request_id, lambda: self._run(
                event, request_id, received_at, build=lambda: self._renderer.render_notice(AUTO_BURST_FULL_TEXT)))
            return request_id
        logger.info("line_stage", extra={
            "stage": "auto_image_burst_started" if kind == BurstAddKind.STARTED else "auto_image_burst_extended",
            "burst_id": burst.burst_id, "image_count": burst.image_count, "collection_mode": COLLECTION_MODE_AUTO})
        return burst.burst_id

    def _finalize_burst(self, user_id: str, burst_id: str, generation: int) -> None:
        """timer 到期（timer thread）：burst_id / generation 已被新圖片更新的舊 timer 不得 finalize。"""
        burst = self._bursts.take(user_id, burst_id, generation)
        if burst is None:
            logger.info("line_stage", extra={"stage": "auto_image_burst_stale_timer", "burst_id": burst_id})
            return
        self._submit_burst(burst)

    def _submit_burst(self, burst: ImageBurst | None) -> None:
        """finalize：只建立「一個」一般分析 job（1 張 = 單張流程；2–3 張 = bundle 流程，由 Agent A 依張數決定）。"""
        if burst is None or not burst.items:
            return
        request_id = str(uuid.uuid4())
        last = burst.items[-1]
        first_event = burst.items[0].event
        sent_at = (datetime.fromtimestamp(first_event.timestamp_ms / 1000, timezone.utc) if first_event.timestamp_ms
                   else datetime.now(timezone.utc))
        label = image_query_label(sent_at)
        actual_wait_ms = int((self._clock() - burst.started_at) * 1000)
        logger.info("line_stage", extra={
            "request_id": request_id, "stage": "auto_image_burst_finalized", "burst_id": burst.burst_id,
            "image_count": burst.image_count, "collection_mode": COLLECTION_MODE_AUTO,
            "configured_window_ms": self._bursts.window_ms,  # 設定的 debounce window
            "burst_duration_ms": burst.wait_ms,  # 第一張到最後一張圖片到達的間隔
            "actual_wait_ms": actual_wait_ms,  # 第一張圖片到達 → finalize（使用者實際等待 gate 的時間）
            "burst_wait_ms": actual_wait_ms})  # 相容舊欄位：同 actual_wait_ms
        self._queue.submit(
            burst.user_id, request_id,
            lambda: self._run(last.event, request_id, last.received_at, sent_at, label,
                              build=lambda: self._build_burst_message(burst, request_id, sent_at)),
            on_timeout=lambda: self._deliver_error(last.event, request_id, last.received_at, TIMEOUT_SUMMARY,
                                                   query_label=label))

    def _build_burst_message(self, burst: ImageBurst, request_id: str, sent_at: datetime) -> dict[str, Any] | None:
        user_id = burst.user_id
        if self._collections.active(user_id) is not None:
            # 明確蒐集模式在 burst 等待期間開始：以蒐集模式為準（靜默保存，不分析）
            for item in burst.items:
                raw = self._media_input(item.event, request_id)
                if raw is not None:
                    self._collections.add(user_id, raw.media[0].model_copy(update={"source_id": item.event.message_id}),
                                          source_id=item.event.message_id)
            return None
        media = []
        for item in burst.items:  # 在 queue job 內下載（逾時 / 失敗由既有 watchdog 與 ERROR card 處理）
            raw = self._media_input(item.event, request_id)
            if raw is None:  # 任何一張下載失敗：請使用者重傳，不默默少掉其中一張
                return self._renderer.render_primary(
                    error_payload(request_id, CONTENT_FAILED_TEXT).model_copy(update={"available_actions": []}),
                    query_label=image_query_label(sent_at))
            media.append(raw.media[0].model_copy(update={"source_id": item.event.message_id}))
        return self._service.respond(RawUserInput(input_type=InputType.IMAGE, media=media), owner_id=user_id,
                                     request_id=request_id, received_at=sent_at)

    # ------------------------------------------------------------ job（在 queue worker 中執行）

    def _run(self, event: LineEvent, request_id: str, received_at: float, sent_at: datetime | None = None,
             query_label: str | None = None, build: Callable[[], dict[str, Any] | None] | None = None) -> None:
        """不論成功或任何例外，都恰好送出一則訊息（失敗時為一張 ERROR card）。"""
        try:
            self._line.start_loading(event.user_id)
        except Exception:  # noqa: BLE001 - loading animation 只是輔助
            logger.info("line_loading_failed", extra={"request_id": request_id})
        try:
            message = build() if build is not None else self._build_message(event, request_id, sent_at, query_label)
        except Exception:  # noqa: BLE001 - pipeline 以外的未預期錯誤也要回覆，不得讓使用者無回應
            logger.exception("line_stage", extra={"request_id": request_id, "stage": "error"})
            self._deliver_error(event, request_id, received_at, ERROR_SUMMARY, query_label=query_label)
            return
        # 「開始新問題」回覆自帶含「多照片辨識」入口的 Quick Reply；其餘訊息附上一般的 session controls
        if message is None:  # 蒐集模式中靜默保存圖片：不送任何訊息
            return
        self._deliver(event, request_id, received_at,
                      message if "quickReply" in message else with_session_controls(message))

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
                self._collections.clear(user_id)
                return with_session_controls(self._renderer.render_notice(SESSION_NEW_TEXT), multi_entry=True)
            if session == SESSION_END:
                self._contexts.close(user_id)
                self._collections.clear(user_id)
                return self._renderer.render_notice(SESSION_END_TEXT)
            if (multi := parse_multi_postback(event.postback_data)) is not None:
                return self._handle_multi_postback(multi, user_id, request_id, sent_at)
            return self._service.handle_action(event.postback_data, owner_id=user_id, request_id=request_id)

        if event.message_type == "text" and event.text.strip() == MULTI_START_COMMAND:
            return self._start_collection(user_id)
        if event.message_type == "image" and event.message_id and self._collections.active(user_id) is not None:
            return self._collect_image(event, request_id)  # 蒐集模式：只保存，不分析
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

    # ------------------------------------------------------------ 多照片辨識（蒐集 → 一次分析）

    def _control_card(self, text: str, session: MultiImageCollectionSession) -> dict[str, Any]:
        """蒐集期間的控制卡：開始分析 / 取消按鈕在卡片內（不會像 Quick Reply 一樣在使用者傳照片後消失）。"""
        return self._renderer.render_collection_control(
            text, multi_postback_data("analyze", session.session_id), multi_postback_data("cancel", session.session_id),
            MULTI_ANALYZE_LABEL, MULTI_CANCEL_LABEL)

    def _start_collection(self, user_id: str) -> dict[str, Any]:
        session = self._collections.start(user_id)
        return self._control_card(MULTI_START_TEXT, session)

    def _collect_image(self, event: LineEvent, request_id: str) -> dict[str, Any] | None:
        """蒐集模式中的圖片：下載（驗證格式 / 大小）後「靜默」保存（回傳 None = 不送任何訊息）；
        不呼叫 Vision / Controller / Researcher，也不回任何產品結論。失敗 / 超過上限才會回提示。"""
        raw = self._media_input(event, request_id)
        media = raw.media[0] if raw is not None else None
        if (media is None or media.mime_type not in IMAGE_MIME_TYPES or not media.content
                or len(media.content) > MAX_IMAGE_BYTES):
            session = self._collections.active(event.user_id)
            return self._control_card(CONTENT_FAILED_TEXT, session) if session is not None                 else self._renderer.render_notice(CONTENT_FAILED_TEXT)
        media = media.model_copy(update={"source_id": event.message_id})
        outcome, session = self._collections.add(event.user_id, media, source_id=event.message_id)
        if outcome == AddOutcome.NO_SESSION:  # 排隊期間 session 被取消 / 過期：不當成單張分析，請使用者重新開始
            return self._renderer.render_notice(MULTI_NO_SESSION_TEXT)
        logger.info("line_stage", extra={"request_id": request_id, "stage": "multi_image_collected",
                                         "image_count": session.image_count})
        if outcome == AddOutcome.FULL:
            return self._control_card(MULTI_FULL_TEXT, session)
        return None

    def _handle_multi_postback(self, multi: tuple[str, str], user_id: str, request_id: str,
                               sent_at: datetime | None) -> dict[str, Any]:
        action, session_id = multi
        if action == "start":
            return self._start_collection(user_id)
        if action == "cancel":
            cancelled = self._collections.cancel(user_id, session_id)
            return self._renderer.render_notice(MULTI_CANCELLED_TEXT if cancelled else MULTI_NO_SESSION_TEXT)
        session = self._collections.begin_analysis(user_id, session_id)
        if session is None:
            current = self._collections.active(user_id)
            if current is not None and current.session_id == session_id:  # 還不到 2 張
                return self._control_card(MULTI_NEED_MORE_TEXT, current)
            return self._renderer.render_notice(MULTI_NO_SESSION_TEXT)
        # 1 張 → 既有單張流程（analyze_image，不走 bundle）；2–3 張 → analyze_image_bundle 一次（由 Agent A 依張數決定）
        raw = RawUserInput(input_type=InputType.IMAGE, media=[item.media for item in session.items])
        logger.info("line_stage", extra={"request_id": request_id, "stage": "multi_image_analysis_started",
                                         "image_count": session.image_count})
        try:
            # 一個 logical request：Vision bundle / fusion / Controller / Researcher / Reporter 各一次
            message = self._service.respond(raw, owner_id=user_id, request_id=request_id, received_at=sent_at)
        except BaseException:
            self._collections.reopen(user_id, session_id)
            raise
        self._collections.finish(user_id, session_id)  # 逾時 / 錯誤卡可用「重新查詢」（原 request 保存了這組圖片）
        return message

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
