"""LINE webhook event → 內部結構（只取 Phase 1 需要的欄位）。"""

import re
from typing import Any

from pydantic import BaseModel

URL_RE = re.compile(r"https?://[^\s<>\"'，。、）)】」]+", re.IGNORECASE)
MAX_URLS_PER_MESSAGE = 2


class LineEvent(BaseModel):
    event_type: str  # message / postback / follow / ...
    webhook_event_id: str | None = None
    is_redelivery: bool = False
    user_id: str | None = None  # 只處理 1:1 聊天（source.type = user）
    reply_token: str | None = None
    message_type: str | None = None  # text / image / audio / ...
    message_id: str | None = None
    text: str = ""
    postback_data: str = ""
    timestamp_ms: int | None = None  # LINE event timestamp（使用者送出的時間）


def parse_events(payload: dict[str, Any]) -> list[LineEvent]:
    events = []
    for raw in payload.get("events") or []:
        source = raw.get("source") or {}
        message = raw.get("message") or {}
        events.append(LineEvent(
            event_type=raw.get("type", ""),
            webhook_event_id=raw.get("webhookEventId"),
            is_redelivery=bool((raw.get("deliveryContext") or {}).get("isRedelivery")),
            user_id=source.get("userId") if source.get("type") == "user" else None,
            reply_token=raw.get("replyToken"),
            message_type=message.get("type"),
            message_id=message.get("id"),
            text=message.get("text", "") if message.get("type") == "text" else "",
            postback_data=(raw.get("postback") or {}).get("data", ""),
            timestamp_ms=raw.get("timestamp") if isinstance(raw.get("timestamp"), int) else None,
        ))
    return events


def split_urls(text: str) -> tuple[str, list[str]]:
    """把文字中的網址分出來：回傳（去掉網址後的文字, 網址清單）。"""
    urls = URL_RE.findall(text or "")[:MAX_URLS_PER_MESSAGE]
    rest = URL_RE.sub(" ", text or "")
    return " ".join(rest.split()), urls
