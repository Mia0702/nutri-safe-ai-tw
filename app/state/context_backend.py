"""TaskContext 的 persistence backend：只負責「存取一列」與版本 compare-and-set，不含任何 TaskContext 規則。

TTL / session / pending / 截斷等凍結語意全部留在 TaskContextStore；這裡只保證：
* load：列不存在或 expires_at <= now 一律回傳 None（過期等同不存在，不依賴日後 purge）。
* create：只有「沒有未過期的列」時才建立（version = 1）；已存在回傳 False。過期的列會被取代。
* compare_and_set：UPDATE ... WHERE version = expected AND session_id = 該 session，成功才 version + 1；否則回傳 False（不覆蓋較新的內容）。
  一定要比對 session_id：reset 會重建列（version 回到 1），只比 version 會讓舊 session 的寫入誤通過（ABA）。
* delete：無條件刪除（reset / close / 清除疑似過時的 context）。
後端故障一律是 StateUnavailableError。
"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Protocol

from app.state.backend import StateBackend
from app.state.errors import StateSerializationError

TABLE = "line_task_contexts"
MAX_CONTEXTS = 5000


@dataclass(frozen=True)
class StoredContext:
    payload: dict[str, Any]
    version: int


class TaskContextBackend(Protocol):
    def load(self, owner_key: str, now: datetime) -> StoredContext | None: ...

    def create(self, owner_key: str, session_id: str, payload: dict[str, Any], now: datetime,
               expires_at: datetime) -> bool: ...

    def compare_and_set(self, owner_key: str, expected_version: int, session_id: str, payload: dict[str, Any],
                        now: datetime, expires_at: datetime) -> bool: ...

    def delete(self, owner_key: str) -> None: ...


class InMemoryTaskContextBackend:
    def __init__(self, max_contexts: int = MAX_CONTEXTS):
        self._rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._max = max_contexts
        self._lock = Lock()

    def load(self, owner_key, now):
        with self._lock:
            row = self._rows.get(owner_key)
            if row is None or row["expires_at"] <= now:
                return None
            return StoredContext(payload=row["payload"], version=row["version"])

    def create(self, owner_key, session_id, payload, now, expires_at):
        with self._lock:
            row = self._rows.get(owner_key)
            if row is not None and row["expires_at"] > now:
                return False
            self._rows.pop(owner_key, None)
            self._rows[owner_key] = {"payload": payload, "version": 1, "session_id": session_id, "expires_at": expires_at}
            while len(self._rows) > self._max:
                self._rows.popitem(last=False)
            return True

    def compare_and_set(self, owner_key, expected_version, session_id, payload, now, expires_at):
        with self._lock:
            row = self._rows.get(owner_key)
            if (row is None or row["version"] != expected_version or row["session_id"] != session_id
                    or row["expires_at"] <= now):
                return False
            self._rows[owner_key] = {"payload": payload, "version": expected_version + 1, "session_id": session_id,
                                     "expires_at": expires_at}
            self._rows.move_to_end(owner_key)
            return True

    def delete(self, owner_key):
        with self._lock:
            self._rows.pop(owner_key, None)


class SupabaseTaskContextBackend:
    def __init__(self, backend: StateBackend):
        self._backend = backend

    def load(self, owner_key, now):
        row = self._backend.select_one(TABLE, {"owner_key": owner_key})
        if row is None:
            return None
        try:
            if datetime.fromisoformat(row["expires_at"]) <= now:
                return None
            version, payload = int(row["version"]), row["context"]
        except (KeyError, TypeError, ValueError) as exc:
            raise StateSerializationError("持久化的 task context 欄位損毀") from exc
        if not isinstance(payload, dict):
            raise StateSerializationError("持久化的 task context 不是 JSON object")
        return StoredContext(payload=payload, version=version)

    def create(self, owner_key, session_id, payload, now, expires_at):
        self._backend.delete_expired(TABLE, {"owner_key": owner_key}, now)  # 過期的列視同不存在
        return self._backend.insert_if_absent(TABLE, {
            "owner_key": owner_key, "session_id": session_id, "context": payload, "version": 1,
            "updated_at": now.isoformat(), "expires_at": expires_at.isoformat()}, ("owner_key",))

    def compare_and_set(self, owner_key, expected_version, session_id, payload, now, expires_at):
        return self._backend.update_if(
            TABLE, {"owner_key": owner_key, "version": expected_version, "session_id": session_id},
            {"context": payload, "version": expected_version + 1,
             "updated_at": now.isoformat(), "expires_at": expires_at.isoformat()})

    def delete(self, owner_key):
        self._backend.delete(TABLE, {"owner_key": owner_key})
