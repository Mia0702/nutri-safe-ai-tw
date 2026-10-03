"""LINE webhook 簽章驗證（HMAC-SHA256(channel secret, raw body) → base64，比對 X-Line-Signature）。"""

import base64
import hashlib
import hmac


def compute_signature(channel_secret: str, body: bytes) -> str:
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_signature(channel_secret: str, body: bytes, signature: str | None) -> bool:
    if not channel_secret or not signature:
        return False
    return hmac.compare_digest(compute_signature(channel_secret, body), signature.strip())
