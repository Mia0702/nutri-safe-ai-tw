"""依 request_id 保存每次分析的結果，供之後的按鈕（on-demand detail / 重新查詢）使用。

Phase 1 為 in-memory 實作（單一 process，有筆數與存活時間上限）；
上線多 instance 前需改為 Supabase 持久化（同一個 RequestStore 介面）。
"""

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Protocol

from pydantic import BaseModel, Field

from app.schemas.evidence import EvidenceBundle
from app.schemas.response import ResponsePayload
from app.schemas.risk import RiskAssessment
from app.schemas.user_request import NormalizedUserRequest, RawUserInput

MAX_RECORDS = 2000
RECORD_TTL = timedelta(hours=24)


class RequestRecord(BaseModel):
    request_id: str
    owner_id: str | None  # LINE user id；按鈕只能取回自己的查詢
    raw_input: RawUserInput  # 「重新查詢」用
    response: ResponsePayload
    normalized_request: NormalizedUserRequest | None = None
    evidence: EvidenceBundle | None = None
    risk: RiskAssessment | None = None
    # request-scoped query context（例如「圖片辨識｜…」）與收到時間：之後的按鈕 / 重新查詢一律沿用
    query_label: str | None = None
    received_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def has_evidence(self) -> bool:
        return self.normalized_request is not None and self.evidence is not None and self.risk is not None


class RequestStore(Protocol):
    def save(self, record: RequestRecord) -> None: ...

    def get(self, request_id: str) -> RequestRecord | None: ...


class InMemoryRequestStore:
    def __init__(self, max_records: int = MAX_RECORDS, ttl: timedelta = RECORD_TTL):
        self._data: OrderedDict[str, RequestRecord] = OrderedDict()
        self._max = max_records
        self._ttl = ttl
        self._lock = Lock()

    def save(self, record: RequestRecord) -> None:
        with self._lock:
            if record.request_id in self._data:  # 同一個 request_id 只保存第一次結果，避免被晚到的結果覆蓋
                return
            self._data[record.request_id] = record
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def get(self, request_id: str) -> RequestRecord | None:
        with self._lock:
            record = self._data.get(request_id)
        if record is None or datetime.now(timezone.utc) - record.created_at > self._ttl:
            return None
        return record
