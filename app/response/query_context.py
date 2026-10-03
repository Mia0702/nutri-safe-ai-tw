"""Request-scoped query context：讓使用者知道這張卡片在回答哪一則輸入（目前用於圖片）。

* 不儲存圖片、不建立公開圖片 URL、不做縮圖。
* label 只由「這個 request 自己」的資料產生：收到時間（LINE event timestamp）與這一題的分析結果；
  存在 RequestRecord 上，之後的結果 / 逾時 / 按鈕一律使用原 request 的 label，
  不依最新 conversation state 猜是哪一張圖片。
"""

from datetime import datetime, timedelta, timezone

from app.schemas.enums import ProductVerificationStatus
from app.schemas.user_request import RawUserInput

TW_TZ = timezone(timedelta(hours=8), "Asia/Taipei")  # 台灣無日光節約時間
IMAGE_RECOGNIZED_PREFIX = "圖片辨識"
IMAGE_QUERY_PREFIX = "圖片查詢"
MAX_LABEL_NAME_CHARS = 16


def is_image_input(raw: RawUserInput | None) -> bool:
    return raw is not None and any(m.kind == "image" for m in raw.media)


def received_time_text(received_at: datetime) -> str:
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at.astimezone(TW_TZ).strftime("%m/%d %H:%M:%S")


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_LABEL_NAME_CHARS else text[: MAX_LABEL_NAME_CHARS - 1] + "…"


def recognized_name(result) -> str | None:
    """這一題圖片辨識出的可靠名稱：已確認的官方產品優先，其次是 Agent A 從圖片文字讀出的產品名稱。"""
    if result is None:
        return None
    bundle, req = result.evidence, result.normalized_request
    match = bundle.product_match if bundle is not None else None
    if match is not None and match.status == ProductVerificationStatus.VERIFIED and match.best_match is not None:
        return match.best_match.product.product_name_zh
    if req is not None and req.product_candidates:
        return req.product_candidates[0]
    return None


def image_query_label(received_at: datetime, result=None) -> str:
    name = recognized_name(result)
    if name and _clip(name):
        return f"{IMAGE_RECOGNIZED_PREFIX}｜{_clip(name)}"
    return f"{IMAGE_QUERY_PREFIX}｜{received_time_text(received_at)}"
