"""owner_key：LINE user id 的 HMAC-SHA256（hex）。新的狀態表只存 owner_key，不存原始 LINE user id。

* 使用專用 secret（LINE_STATE_HMAC_SECRET），不重用 LINE channel secret / Supabase key / OpenAI key。
* 同一個 user + secret 結果固定；不同 secret 結果不同。
* 不記錄原始 id 與 secret。
"""

import hashlib
import hmac

from app.config.settings import Settings
from app.schemas.enums import ErrorCode
from app.services.errors import ServiceError

MIN_SECRET_LENGTH = 16


class OwnerKeyHasher:
    def __init__(self, secret: str):
        if not secret or len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(f"LINE_STATE_HMAC_SECRET 至少需要 {MIN_SECRET_LENGTH} 個字元")
        self._secret = secret.encode("utf-8")

    def __call__(self, raw_user_id: str) -> str:
        if not raw_user_id:
            raise ValueError("owner id 不可為空")
        return hmac.new(self._secret, raw_user_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "OwnerKeyHasher(secret=***)"


def build_owner_key_hasher(settings: Settings) -> OwnerKeyHasher:
    secret = settings.line_state_hmac_secret
    if secret is None or not secret.get_secret_value():
        raise ServiceError(ErrorCode.NOT_CONFIGURED, "啟用 persistent state 需要設定 LINE_STATE_HMAC_SECRET", "line_state")
    return OwnerKeyHasher(secret.get_secret_value())
