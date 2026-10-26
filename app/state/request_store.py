"""SupabaseRequestStore：RequestStore 介面的持久化實作（line_request_records）。

行為與 InMemoryRequestStore 一致：first-write-wins（資料庫端 INSERT ... ON CONFLICT DO NOTHING）、
24 小時過期（過期視為不存在）、不覆寫。後端不可用 → StateUnavailableError（不是「過期」）。
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.response.store import RECORD_TTL, RequestRecord
from app.state.backend import StateBackend
from app.state.errors import StateSerializationError
from app.state.serialization import deserialize_record, serialize_record

TABLE = "line_request_records"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SupabaseRequestStore:
    def __init__(self, backend: StateBackend, owner_key: Callable[[str], str], ttl: timedelta = RECORD_TTL,
                 clock: Callable[[], datetime] = _utc_now):
        self._backend, self._owner_key, self._ttl, self._clock = backend, owner_key, ttl, clock

    def owner_ref(self, owner_id: str | None) -> str | None:
        return self._owner_key(owner_id) if owner_id else None

    def save(self, record: RequestRecord) -> None:
        try:
            request_id = str(uuid.UUID(record.request_id))
        except ValueError as exc:
            raise StateSerializationError("request_id 不是有效的 UUID，無法持久化") from exc
        self._backend.insert_if_absent(TABLE, {
            "request_id": request_id,
            "owner_key": self.owner_ref(record.owner_id) or "",
            "record": serialize_record(record),
            "query_label": record.query_label,
            "received_at": (record.received_at or record.created_at).isoformat(),
            "created_at": record.created_at.isoformat(),
            "expires_at": (record.created_at + self._ttl).isoformat(),
        }, ("request_id",))

    def get(self, request_id: str) -> RequestRecord | None:
        try:
            key = str(uuid.UUID(request_id))
        except (ValueError, AttributeError, TypeError):
            return None
        row = self._backend.select_one(TABLE, {"request_id": key})
        if row is None:
            return None
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
            created_at = datetime.fromisoformat(row["created_at"])
            received_at = datetime.fromisoformat(row["received_at"]) if row.get("received_at") else None
        except (KeyError, TypeError, ValueError) as exc:
            raise StateSerializationError("持久化的 request record 時間欄位損毀") from exc
        if expires_at <= self._clock():
            return None
        return deserialize_record(row.get("record"), request_id=key, owner_key=row.get("owner_key"),
                                  query_label=row.get("query_label"), received_at=received_at, created_at=created_at)
