"""Request-scoped postback actions。

每個按鈕的 postback data 都帶 action 與 request_id，例如：

    action=detail&request_id=<uuid>
    action=select_product&request_id=<uuid>&candidate_id=<canonical product id>

按下按鈕時一律依 request_id 取回「那一題」保存的結果，
絕不依「目前最新的 conversation state」猜是哪一題（使用者可能已經問了下一題）。
select_product 的 candidate_id 必須是那一題當初產生的候選之一（由 service 驗證）。
"""

import re

import uuid
from urllib.parse import parse_qs, urlencode

from pydantic import BaseModel

from app.schemas.enums import ResponseAction

MAX_POSTBACK_DATA_CHARS = 300  # LINE postback data 上限

ACTION_LABELS = {
    ResponseAction.DETAIL: "查看完整分析",
    ResponseAction.SOURCES: "查看資料來源",
    ResponseAction.INGREDIENTS: "查看完整成分",
    ResponseAction.OFFICIAL_INFO: "查看產品資訊",
    ResponseAction.REGISTRATION_INFO: "查看產品登記資料",
    ResponseAction.WHY: "為什麼？",
    ResponseAction.RETRY: "重新查詢",
    ResponseAction.CHECK_PACKAGE_CLAIMS: "查驗包裝宣稱",
    ResponseAction.FULL_EFFICACY: "查看完整功效",
    ResponseAction.WARNINGS: "警語與注意事項",
    ResponseAction.POLICY_BASIS: "查看法規依據",
}

# 詳細卡片上的下一層按鈕：只有 primary card 提供了上一層，才允許按下一層（仍是同一個 request_id）
DETAIL_FOLLOW_UPS: dict[ResponseAction, tuple[ResponseAction, ...]] = {
    ResponseAction.OFFICIAL_INFO: (ResponseAction.FULL_EFFICACY, ResponseAction.WARNINGS, ResponseAction.SOURCES),
    # 為什麼 → 法規依據（結構化 claim policy 的條次 / 摘要）→ 官方來源（既有可點擊來源卡）
    ResponseAction.WHY: (ResponseAction.POLICY_BASIS, ResponseAction.SOURCES),
    ResponseAction.REGISTRATION_INFO: (ResponseAction.SOURCES,),
    # 完整成分 → 成分資料的官方來源
    ResponseAction.INGREDIENTS: (ResponseAction.SOURCES,),
}


def reachable_actions(primary_actions: list[ResponseAction]) -> set[ResponseAction]:
    out = set(primary_actions)
    for action in primary_actions:
        out.update(DETAIL_FOLLOW_UPS.get(action, ()))
    return out


_CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class RequestAction(BaseModel):
    action: ResponseAction
    request_id: str
    candidate_id: str | None = None  # 只有 select_product 會有


def build_postback_data(action: ResponseAction, request_id: str) -> str:
    data = urlencode({"action": action.value, "request_id": str(uuid.UUID(request_id))})
    if len(data) > MAX_POSTBACK_DATA_CHARS:
        raise ValueError("postback data 超過 LINE 上限")
    return data


def build_select_product_data(request_id: str, candidate_id: str) -> str:
    if not _CANDIDATE_ID_RE.match(candidate_id or ""):
        raise ValueError("candidate_id 格式不正確")
    data = urlencode({"action": ResponseAction.SELECT_PRODUCT.value, "request_id": str(uuid.UUID(request_id)),
                      "candidate_id": candidate_id})
    if len(data) > MAX_POSTBACK_DATA_CHARS:
        raise ValueError("postback data 超過 LINE 上限")
    return data


def parse_postback_data(data: str) -> RequestAction | None:
    """格式不符（缺欄位、多欄位、未知 action、request_id 不是 UUID、candidate_id 不合法）一律回傳 None，不猜測。"""
    try:
        fields = parse_qs(data or "", strict_parsing=True, max_num_fields=4)
    except ValueError:
        return None
    if any(len(v) != 1 for v in fields.values()):
        return None
    try:
        action = ResponseAction(fields.get("action", [""])[0])
        request_id = str(uuid.UUID(fields.get("request_id", [""])[0]))
    except ValueError:
        return None
    if action == ResponseAction.SELECT_PRODUCT:
        candidate = fields.get("candidate_id", [""])[0]
        if set(fields) != {"action", "request_id", "candidate_id"} or not _CANDIDATE_ID_RE.match(candidate):
            return None
        return RequestAction(action=action, request_id=request_id, candidate_id=candidate)
    if set(fields) != {"action", "request_id"}:
        return None
    return RequestAction(action=action, request_id=request_id)
