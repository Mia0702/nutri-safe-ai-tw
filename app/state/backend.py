"""持久化狀態表的最小 row-level 存取介面，以及 Supabase（PostgREST）實作。

介面刻意只有三個操作；first-write-wins / 一次性 claim 一律用資料庫端 INSERT ... ON CONFLICT DO NOTHING，
不做 read-then-write。時間值一律是 ISO-8601 字串。
"""

import logging
from datetime import datetime
from typing import Any, Protocol

from app.config.settings import Settings
from app.schemas.enums import ErrorCode
from app.services.errors import ServiceError
from app.state.errors import StateUnavailableError

logger = logging.getLogger(__name__)


class StateBackend(Protocol):
    def insert_if_absent(self, table: str, row: dict[str, Any], conflict_columns: tuple[str, ...]) -> bool:
        """原子地插入；主鍵 / unique 已存在則不做任何事。回傳 True = 這次真的插入。"""

    def select_one(self, table: str, match: dict[str, Any]) -> dict[str, Any] | None: ...

    def delete_expired(self, table: str, match: dict[str, Any], now: datetime) -> None:
        """只刪除符合 match 且 expires_at <= now 的列。"""

    def update_if(self, table: str, match: dict[str, Any], values: dict[str, Any]) -> bool:
        """原子地 UPDATE ... WHERE match（含 version 條件）；回傳 True = 有一列被更新（compare-and-set）。"""

    def delete(self, table: str, match: dict[str, Any]) -> None:
        """刪除符合 match 的列（不存在也不是錯誤）。"""


class SupabaseStateBackend:
    """透過 Supabase PostgREST（secret key，僅後端）存取 line_* 表。任何失敗一律轉成 StateUnavailableError。"""

    def __init__(self, settings: Settings):
        if not settings.supabase_configured:
            raise ServiceError(ErrorCode.NOT_CONFIGURED, "未設定 SUPABASE_URL / SUPABASE_SECRET_KEY", "supabase")
        from supabase import create_client

        self._client = create_client(settings.supabase_url, settings.supabase_secret_key.get_secret_value())

    @staticmethod
    def _fail(exc: Exception) -> StateUnavailableError:
        logger.warning("line_state_backend_error", extra={"error_type": type(exc).__name__})
        return StateUnavailableError(f"persistent state backend 不可用：{type(exc).__name__}")

    def insert_if_absent(self, table: str, row: dict[str, Any], conflict_columns: tuple[str, ...]) -> bool:
        try:
            result = self._client.table(table).upsert(
                row, on_conflict=",".join(conflict_columns), ignore_duplicates=True).execute()
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from exc
        return bool(result.data)  # ignore-duplicates：衝突時不回傳列

    def select_one(self, table: str, match: dict[str, Any]) -> dict[str, Any] | None:
        try:
            rows = self._client.table(table).select("*").match(match).limit(1).execute().data or []
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from exc
        return rows[0] if rows else None

    def delete_expired(self, table: str, match: dict[str, Any], now: datetime) -> None:
        try:
            self._client.table(table).delete().match(match).lte("expires_at", now.isoformat()).execute()
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from exc

    def update_if(self, table: str, match: dict[str, Any], values: dict[str, Any]) -> bool:
        try:
            result = self._client.table(table).update(values).match(match).execute()
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from exc
        return bool(result.data)

    def delete(self, table: str, match: dict[str, Any]) -> None:
        try:
            self._client.table(table).delete().match(match).execute()
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from exc
