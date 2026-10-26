"""一次性 claim：claim(kind, key) -> bool。

True = 呼叫者第一個取得 claim；False = 已被 claim 且尚未過期。過期的 claim 可被重新取得。
key 只放識別碼（webhook event id、request_id），不放使用者文字。
"""

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Callable, Protocol

from app.state.backend import StateBackend

CLAIM_TTL = timedelta(hours=24)
MAX_CLAIMS = 5000
TABLE = "line_event_claims"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate(kind: str, key: str) -> None:
    if not kind or not key:
        raise ValueError("claim 的 kind / key 不可為空")


class ClaimStore(Protocol):
    def claim(self, kind: str, key: str) -> bool: ...


class InMemoryClaimStore:
    def __init__(self, ttl: timedelta = CLAIM_TTL, max_claims: int = MAX_CLAIMS,
                 clock: Callable[[], datetime] = _utc_now):
        self._claims: OrderedDict[tuple[str, str], datetime] = OrderedDict()
        self._ttl, self._max, self._clock = ttl, max_claims, clock
        self._lock = Lock()

    def claim(self, kind: str, key: str) -> bool:
        _validate(kind, key)
        now = self._clock()
        with self._lock:
            expires = self._claims.get((kind, key))
            if expires is not None and expires > now:
                return False
            self._claims.pop((kind, key), None)
            self._claims[(kind, key)] = now + self._ttl
            while len(self._claims) > self._max:
                self._claims.popitem(last=False)
            return True


class SupabaseClaimStore:
    def __init__(self, backend: StateBackend, ttl: timedelta = CLAIM_TTL, clock: Callable[[], datetime] = _utc_now):
        self._backend, self._ttl, self._clock = backend, ttl, clock

    def claim(self, kind: str, key: str) -> bool:
        _validate(kind, key)
        now = self._clock()
        # 先清掉「這一個」已過期的 claim，再原子 INSERT ... ON CONFLICT DO NOTHING；
        # 兩個呼叫者同時重新取得時，主鍵保證只有一個成功。
        self._backend.delete_expired(TABLE, {"kind": kind, "key": key}, now)
        return self._backend.insert_if_absent(
            TABLE, {"kind": kind, "key": key, "created_at": now.isoformat(), "expires_at": (now + self._ttl).isoformat()},
            ("kind", "key"))
