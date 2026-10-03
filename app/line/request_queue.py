"""Per-user FIFO request queue。

* 同一位使用者同時間只有一個 PROCESSING request；後送的為 QUEUED，依 FIFO 順序執行，
  因此 B 的回覆不可能早於 A。
* 不同使用者各自一條 queue，可以平行處理。
* 每個 job 自己負責送出唯一一則回覆；逾時由 AdaptiveResponseService 處理（回一張 ERROR card，
  晚到結果丟棄），queue 不會在之後補發任何東西。
* Queue release 保證：job 不論成功、拋例外或超過 job_timeout_seconds（watchdog），PROCESSING 狀態都會在
  finally 中清除，下一個 FIFO request 繼續執行。watchdog 逾時會呼叫該 job 的 on_timeout（送 ERROR card）；
  卡住的 job thread 無法被強制終止，但它之後的結果由呼叫端的「只送一次」保護丟棄。
"""

import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from enum import StrEnum
from threading import Condition
from typing import Callable

logger = logging.getLogger("nutri_safe_ai.line.queue")

MAX_TRACKED_STATES = 2000


class RequestState(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


_Job = tuple[str, Callable[[], None], Callable[[], None] | None]


class PerUserRequestQueue:
    def __init__(self, max_workers: int = 8, job_timeout_seconds: float | None = None):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="line-user")
        # job 在獨立 thread 執行，drain thread 才能在 watchdog 逾時後釋放 queue
        self._job_executor = ThreadPoolExecutor(max_workers=max_workers * 4, thread_name_prefix="line-job")
        self._job_timeout = job_timeout_seconds
        self._pending: dict[str, deque[_Job]] = {}
        self._active: set[str] = set()  # 正在 drain 的 user
        self._states: dict[str, RequestState] = {}
        self._cond = Condition()

    def submit(
        self, user_id: str, request_id: str, job: Callable[[], None], on_timeout: Callable[[], None] | None = None,
    ) -> RequestState:
        with self._cond:
            self._pending.setdefault(user_id, deque()).append((request_id, job, on_timeout))
            self._set_state(request_id, RequestState.QUEUED)
            if user_id in self._active:
                return RequestState.QUEUED
            self._active.add(user_id)
        self._executor.submit(self._drain, user_id)
        return RequestState.QUEUED

    def state(self, request_id: str) -> RequestState | None:
        with self._cond:
            return self._states.get(request_id)

    def _set_state(self, request_id: str, state: RequestState) -> None:
        self._states[request_id] = state
        while len(self._states) > MAX_TRACKED_STATES:
            self._states.pop(next(iter(self._states)))

    def _drain(self, user_id: str) -> None:
        while True:
            with self._cond:
                queue = self._pending.get(user_id)
                if not queue:
                    self._pending.pop(user_id, None)
                    self._active.discard(user_id)
                    self._cond.notify_all()
                    return
                request_id, job, on_timeout = queue.popleft()
                self._set_state(request_id, RequestState.PROCESSING)
            started = time.monotonic()
            logger.info("line_stage", extra={"request_id": request_id, "stage": "queue_started"})
            final = RequestState.FAILED
            try:
                final = self._run_job(request_id, job, on_timeout)
            except BaseException:  # noqa: BLE001 - 任何狀況都不得讓 user queue 停住
                logger.exception("line_job_failed", extra={"request_id": request_id, "stage": "error"})
            finally:
                with self._cond:
                    self._set_state(request_id, final)
                logger.info("line_stage", extra={
                    "request_id": request_id, "stage": "queue_released", "error_code": None
                    if final == RequestState.DONE else final.value,
                    "duration_ms": int((time.monotonic() - started) * 1000)})

    def _run_job(self, request_id: str, job: Callable[[], None], on_timeout: Callable[[], None] | None) -> RequestState:
        future = self._job_executor.submit(job)
        try:
            future.result(timeout=self._job_timeout)
            return RequestState.DONE
        except FutureTimeout:
            logger.warning("line_stage", extra={"request_id": request_id, "stage": "job_timeout", "error_code": "TIMEOUT"})
            if on_timeout is not None:
                try:
                    on_timeout()
                except Exception:  # noqa: BLE001
                    logger.exception("line_timeout_notice_failed", extra={"request_id": request_id})
            return RequestState.TIMEOUT
        except Exception:  # noqa: BLE001 - 單一 job 失敗不影響同一使用者後續的 request
            logger.exception("line_job_failed", extra={"request_id": request_id, "stage": "error"})
            return RequestState.FAILED

    def wait_idle(self, timeout: float | None = None) -> bool:
        """測試 / 關閉時使用：等所有 queue 清空。"""
        with self._cond:
            return self._cond.wait_for(lambda: not self._active, timeout=timeout)
