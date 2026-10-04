"""台灣官方產品資料涵蓋範圍與產品類別的一般規則（deterministic，runtime 共用）。

* 類別（`ProductCategory`）決定「官方紀錄代表什麼」：
  - HEALTH_FOOD：健康食品許可，可能有官方核准保健功效。
  - 其他類別（國產維生素類錠狀／膠囊狀食品、輸入錠狀／膠囊狀食品）：只代表「查有官方查驗登記／核備資料」，
    不是健康食品核准，沒有核准保健功效；沒有核准功效不是異常。
* 官方法律狀態 vs 日期狀態：
  - EXPIRED / MERGED / CANCELLED / REVOKED 是官方資料給的狀態（許可已非有效）。
  - UNKNOWN 代表官方資料集沒有提供狀態，不等於失效。
  - 只有有效日期時，依日期比較的結果（`TemporalStatus`）只能顯示為日期狀態，不得當成官方法律狀態，也不影響風險。
* 這裡不放任何特定產品、字號或問句。
"""

from datetime import date, datetime, timedelta, timezone
from enum import StrEnum

from app.schemas.enums import ProductCategory, ProductStatus

TW_TZ = timezone(timedelta(hours=8))

# 目前系統查詢的台灣官方產品資料（依顯示順序）；不包含一般食品全體
COVERED_CATEGORIES: tuple[ProductCategory, ...] = (
    ProductCategory.HEALTH_FOOD,
    ProductCategory.DOMESTIC_VITAMIN_TABLET_CAPSULE,
    ProductCategory.IMPORTED_TABLET_CAPSULE,
)
CATEGORY_LABELS: dict[ProductCategory, str] = {
    ProductCategory.HEALTH_FOOD: "健康食品",
    ProductCategory.DOMESTIC_VITAMIN_TABLET_CAPSULE: "國產維生素類錠狀／膠囊狀食品",
    ProductCategory.IMPORTED_TABLET_CAPSULE: "輸入錠狀／膠囊狀食品",
}
# 只有這些類別的官方紀錄可能帶有核准保健功效
HEALTH_CLAIM_CATEGORIES = frozenset({ProductCategory.HEALTH_FOOD})
# 官方資料明確給出的「非有效」狀態；UNKNOWN（未提供）不在其中
OFFICIALLY_INACTIVE_STATUSES = frozenset({
    ProductStatus.EXPIRED, ProductStatus.MERGED, ProductStatus.CANCELLED, ProductStatus.REVOKED,
})

COVERAGE_TEXT = "目前系統查詢的台灣官方資料包含：" + "、".join(CATEGORY_LABELS[c] for c in COVERED_CATEGORIES) + "。"
NOT_FOUND_NOT_FAKE_TEXT = "查不到紀錄不代表產品是假貨或違法。"
NOT_FOUND_COVERAGE_TEXT = COVERAGE_TEXT + NOT_FOUND_NOT_FAKE_TEXT
COVERAGE_LIST_TEXT = "目前涵蓋：\n" + "\n".join(f"• {CATEGORY_LABELS[c]}" for c in COVERED_CATEGORIES)
REGISTRATION_RECORD_TEXT = "查有官方查驗登記／核備資料"
NO_HEALTH_CLAIM_APPROVAL_TEXT = "這類官方登記不是健康食品核准，不包含核准的保健功效。"
STATUS_NOT_PROVIDED_TEXT = "官方資料未提供登記狀態"


class TemporalStatus(StrEnum):
    """依有效日期的日期比較（當天仍算在有效日期內）。不是官方法律狀態，不寫入 DB。"""

    CURRENT_BY_DATE = "CURRENT_BY_DATE"
    EXPIRED_BY_DATE = "EXPIRED_BY_DATE"


TEMPORAL_STATUS_TEXT = {
    TemporalStatus.CURRENT_BY_DATE: "依日期仍在有效日期內",
    TemporalStatus.EXPIRED_BY_DATE: "依日期已超過有效日期",
}


def category_of(value: str | ProductCategory | None) -> ProductCategory:
    """DB 值 → 類別。沒有值（0004 之前的 view 沒有此欄位）視為既有的 health_food；無法辨識的值 → ValueError。"""
    if value is None or value == "":
        return ProductCategory.HEALTH_FOOD
    return ProductCategory(value)


def has_health_claim_approval(category: ProductCategory) -> bool:
    return category in HEALTH_CLAIM_CATEGORIES


def is_officially_inactive(status: ProductStatus) -> bool:
    """官方資料明確表示許可 / 登記已非有效。UNKNOWN 不算。"""
    return status in OFFICIALLY_INACTIVE_STATUSES


def today_tw() -> date:
    return datetime.now(TW_TZ).date()


def derived_temporal_status(expiry_date: str | None, as_of: date | None = None) -> TemporalStatus | None:
    if not expiry_date:
        return None
    try:
        expiry = date.fromisoformat(str(expiry_date)[:10])
    except ValueError:
        return None
    return TemporalStatus.CURRENT_BY_DATE if expiry >= (as_of or today_tw()) else TemporalStatus.EXPIRED_BY_DATE


def expiry_date_text(expiry_date: str | None, as_of: date | None = None) -> str | None:
    """「有效日期：YYYY-MM-DD（依日期…）」；只是日期比較，不是官方狀態。"""
    temporal = derived_temporal_status(expiry_date, as_of)
    if temporal is None:
        return None
    return f"有效日期：{str(expiry_date)[:10]}（{TEMPORAL_STATUS_TEXT[temporal]}，非官方狀態）"
