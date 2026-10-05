"""Automatic Image Burst Gate：使用者直接從 LINE 相簿一次選多張照片時，自動把「短時間內連續到達」的圖片合成一組。

LINE 一次選多張照片會送出多個獨立的 image webhook event。這個 gate 在「建立一般分析 job 之前」緩衝：

    image webhook → AutoImageBurstStore.add（只存 event 識別碼，不下載、不分析）
                  → debounce window（每張新圖片重新計時）
                  → finalize → 只 enqueue 一個一般分析 job（1 張 = 單張流程；2–3 張 = bundle 流程）

* 不在 per-user FIFO queue 裡 sleep / 等待：否則第 1 張會擋住排在它後面的第 2 張，永遠無法同組。
* 不用 LLM 決定分組；只看到達時間與張數（最多 `MAX_BURST_IMAGES` 張，第 4 張不加入）。
* generation / burst_id 保護：舊 timer 在新圖片延長 window 之後不能 finalize 舊的 burst。
* 分組不代表「同一產品」：之後仍由既有 fusion 規則決定（同一產品 / 字號衝突 / 明確不同產品）。
* 明確蒐集模式（ImageCollectionSession）優先：在 handler 端判斷，不經過這個 gate。
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from app.line.events import LineEvent
from app.schemas.image_bundle import MAX_BUNDLE_IMAGES

logger = logging.getLogger("nutri_safe_ai.line.image_burst")

AUTO_IMAGE_BURST_WINDOW_MS = 1800  # 預設 debounce window（可由設定覆寫；0 = 停用 gate）
MAX_BURST_IMAGES = MAX_BUNDLE_IMAGES
COLLECTION_MODE_AUTO = "AUTO"


class BurstAddKind(StrEnum):
    STARTED = "STARTED"
    EXTENDED = "EXTENDED"
    OVERFLOW = "OVERFLOW"  # 已有上限張數：這張不加入


@dataclass
class BurstItem:
    event: LineEvent
    received_at: float  # handler clock（用來判斷 reply token 是否仍新鮮）


@dataclass
class ImageBurst:
    burst_id: str
    user_id: str
    started_at: float
    last_at: float
    generation: int = 0
    items: list[BurstItem] = field(default_factory=list)

    @property
    def image_count(self) -> int:
        return len(self.items)

    @property
    def wait_ms(self) -> int:
        return int((self.last_at - self.started_at) * 1000)


def _default_timer(interval: float, fn: Callable[..., None], args: tuple) -> Any:
    timer = threading.Timer(interval, fn, args=args)
    timer.daemon = True
    return timer


class AutoImageBurstStore:
    def __init__(self, window_ms: int, on_finalize: Callable[[str, str, int], None],
                 clock: Callable[[], float] = time.monotonic,
                 timer_factory: Callable[[float, Callable[..., None], tuple], Any] = _default_timer):
        self._window = max(window_ms, 0) / 1000.0
        self._on_finalize = on_finalize
        self._clock = clock
        self._timer_factory = timer_factory
        self._bursts: dict[str, ImageBurst] = {}
        self._timers: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._window > 0

    @property
    def window_ms(self) -> int:
        return int(self._window * 1000)

    def add(self, user_id: str, item: BurstItem) -> tuple[BurstAddKind, ImageBurst]:
        with self._lock:
            burst = self._bursts.get(user_id)
            if burst is None:
                now = self._clock()
                burst = ImageBurst(burst_id=str(uuid.uuid4()), user_id=user_id, started_at=now, last_at=now)
                self._bursts[user_id] = burst
                kind = BurstAddKind.STARTED
            elif burst.image_count >= MAX_BURST_IMAGES:
                return BurstAddKind.OVERFLOW, burst  # 被拒絕的圖片不延長 window
            else:
                kind = BurstAddKind.EXTENDED
            burst.items.append(item)
            burst.last_at = self._clock()
            burst.generation += 1  # 每張新圖片使舊 timer 失效
            self._arm(user_id, burst)
            return kind, burst

    def _arm(self, user_id: str, burst: ImageBurst) -> None:
        old = self._timers.pop(user_id, None)
        if old is not None:
            old.cancel()
        timer = self._timer_factory(self._window, self._on_finalize, (user_id, burst.burst_id, burst.generation))
        self._timers[user_id] = timer
        timer.start()

    def take(self, user_id: str, burst_id: str, generation: int) -> ImageBurst | None:
        """timer 到期：只有 burst_id 與 generation 都還是目前這一組才 finalize（舊 timer 回傳 None）。"""
        with self._lock:
            burst = self._bursts.get(user_id)
            if burst is None or burst.burst_id != burst_id or burst.generation != generation:
                return None
            self._bursts.pop(user_id, None)
            self._timers.pop(user_id, None)
            return burst

    def flush(self, user_id: str) -> ImageBurst | None:
        """同一位使用者的其他事件到達：先 finalize 這一組（維持 FIFO 順序）。"""
        with self._lock:
            burst = self._bursts.pop(user_id, None)
            timer = self._timers.pop(user_id, None)
        if timer is not None:
            timer.cancel()
        return burst

    def discard(self, user_id: str) -> bool:
        """開始新問題 / 結束本次查證：丟棄尚未 finalize 的圖片。"""
        return self.flush(user_id) is not None

    def pending_count(self, user_id: str) -> int:
        with self._lock:
            burst = self._bursts.get(user_id)
            return burst.image_count if burst else 0
