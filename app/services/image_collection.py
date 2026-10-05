"""Multi-image collection session（LINE：使用者明確開啟「多照片辨識」後，先蒐集 2–3 張同一產品的照片）。

LINE 一次選多張照片會送出多個獨立的 image webhook event；不能靠時間推測哪幾張是同一組。
因此採明確的蒐集模式：

* COLLECTING 期間收到的圖片只「保存」，不做任何分析 / 查證 / 產品結論。
* 使用者按「開始分析」才把已蒐集的圖片組成一個 logical request（由呼叫端交給既有 pipeline 一次）。
* 最多 `MAX_COLLECTED_IMAGES` 張；超過的圖片不加入。TTL（距離最後一次活動）過期的 session 會被清除。
* 不做任何依時間自動分組。圖片只存在記憶體，分析成功 / 取消 / 過期 / 結束本次查證時清除。

所有操作都在每位使用者的 FIFO queue job 中進行（見 app/line/handler.py），因此「存第 1 張 → 存第 2 張 → 開始分析」
的順序由 queue 保證；store 本身以 lock 保護共享狀態。
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import Callable

from app.schemas.image_bundle import MAX_BUNDLE_IMAGES
from app.schemas.user_request import MediaInput

MAX_COLLECTED_IMAGES = MAX_BUNDLE_IMAGES
MIN_COLLECTED_IMAGES = 1  # 1 張走既有單張流程；2–3 張走 bundle（見 handler）
COLLECTION_TTL_SECONDS = 600.0  # 距離最後一次活動 10 分鐘


class CollectionState(StrEnum):
    COLLECTING = "COLLECTING"
    ANALYZING = "ANALYZING"


class AddOutcome(StrEnum):
    ADDED = "ADDED"
    FULL = "FULL"  # 已有上限張數：這張不加入
    NO_SESSION = "NO_SESSION"


@dataclass
class CollectedImage:
    media: MediaInput
    source_id: str | None  # LINE message id（只當識別碼；不含圖片內容）
    received_at: float


@dataclass
class MultiImageCollectionSession:
    session_id: str
    user_id: str
    started_at: float
    last_activity: float
    items: list[CollectedImage] = field(default_factory=list)
    state: CollectionState = CollectionState.COLLECTING

    @property
    def image_count(self) -> int:
        return len(self.items)

    @property
    def can_analyze(self) -> bool:
        return self.image_count >= MIN_COLLECTED_IMAGES


class ImageCollectionStore:
    def __init__(self, ttl_seconds: float = COLLECTION_TTL_SECONDS, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, MultiImageCollectionSession] = {}
        self._lock = Lock()

    def _purge_expired(self) -> None:
        now = self._clock()
        for user_id in [u for u, s in self._sessions.items() if now - s.last_activity > self._ttl]:
            del self._sessions[user_id]

    def start(self, user_id: str) -> MultiImageCollectionSession:
        """開始（或重新開始）蒐集：該使用者先前未完成的 session 會被丟棄。"""
        with self._lock:
            self._purge_expired()
            now = self._clock()
            session = MultiImageCollectionSession(session_id=str(uuid.uuid4()), user_id=user_id,
                                                  started_at=now, last_activity=now)
            self._sessions[user_id] = session
            return session

    def active(self, user_id: str) -> MultiImageCollectionSession | None:
        """目前正在蒐集的 session（已過期或已進入分析 → None）。"""
        with self._lock:
            self._purge_expired()
            session = self._sessions.get(user_id)
            return session if session is not None and session.state == CollectionState.COLLECTING else None

    def add(self, user_id: str, media: MediaInput, source_id: str | None = None
            ) -> tuple[AddOutcome, MultiImageCollectionSession | None]:
        with self._lock:
            self._purge_expired()
            session = self._sessions.get(user_id)
            if session is None or session.state != CollectionState.COLLECTING:
                return AddOutcome.NO_SESSION, None
            now = self._clock()
            session.last_activity = now
            if session.image_count >= MAX_COLLECTED_IMAGES:
                return AddOutcome.FULL, session
            session.items.append(CollectedImage(media=media, source_id=source_id, received_at=now))
            return AddOutcome.ADDED, session

    def begin_analysis(self, user_id: str, session_id: str) -> MultiImageCollectionSession | None:
        """「開始分析」：session 必須是同一位使用者目前這一組、仍在蒐集、且至少 2 張；成功後進入 ANALYZING。"""
        with self._lock:
            self._purge_expired()
            session = self._sessions.get(user_id)
            if (session is None or session.session_id != session_id or session.state != CollectionState.COLLECTING
                    or not session.can_analyze):
                return None
            session.state = CollectionState.ANALYZING
            return session

    def finish(self, user_id: str, session_id: str) -> None:
        """分析成功：清除 session（圖片不再保留）。"""
        self._drop(user_id, session_id)

    def reopen(self, user_id: str, session_id: str) -> None:
        """分析中途發生例外：回到蒐集狀態，使用者可以再按一次「開始分析」。"""
        with self._lock:
            session = self._sessions.get(user_id)
            if session is not None and session.session_id == session_id:
                session.state = CollectionState.COLLECTING
                session.last_activity = self._clock()

    def cancel(self, user_id: str, session_id: str | None = None) -> bool:
        return self._drop(user_id, session_id)

    def clear(self, user_id: str) -> bool:
        return self._drop(user_id, None)

    def _drop(self, user_id: str, session_id: str | None) -> bool:
        with self._lock:
            session = self._sessions.get(user_id)
            if session is None or (session_id is not None and session.session_id != session_id):
                return False
            del self._sessions[user_id]
            return True
