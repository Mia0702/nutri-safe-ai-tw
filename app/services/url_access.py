"""使用者提供網址「讀不到」時的 deterministic 判斷與回覆（不呼叫 LLM、不查資料庫、不查任何工具）。

* 社群平台（Facebook / Instagram / Threads）常需要登入或限制外部程式取得貼文：不繞過登入或平台存取限制，
  讀不到、或只拿到預覽外殼而沒有可分析的內容時，如實說明原因並請使用者改傳截圖 / 貼文字 / 產品照片。
* 其他網址讀取失敗：分成「網址有誤 / 需要登入或限制存取 / 連線逾時或暫時連不上 / 內容無法讀取」四類，
  每類一句原因加一個可執行的下一步；內部錯誤碼（INVALID_INPUT 等）不會出現在使用者可見的文字中。
"""

from enum import StrEnum
from urllib.parse import urlparse

from app.schemas.enums import ErrorCode
from app.schemas.evidence import ToolError
from app.schemas.user_request import NormalizedUserRequest

SOCIAL_HOST_SUFFIXES = (
    "facebook.com", "fb.com", "fb.watch", "fb.me", "instagram.com", "instagr.am", "threads.net", "threads.com",
)

# ServiceError.reason（fetch_url_content 設定）
REASON_INVALID_URL = "invalid_url"
REASON_ACCESS_RESTRICTED = "access_restricted"
REASON_NETWORK = "network_unavailable"
REASON_UNSUPPORTED = "unsupported_content"


class UrlAccessIssue(StrEnum):
    SOCIAL_RESTRICTED = "SOCIAL_RESTRICTED"
    INVALID_URL = "INVALID_URL"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"


_ISSUE_BY_REASON = {
    REASON_INVALID_URL: UrlAccessIssue.INVALID_URL,
    REASON_ACCESS_RESTRICTED: UrlAccessIssue.ACCESS_RESTRICTED,
    REASON_NETWORK: UrlAccessIssue.NETWORK_UNAVAILABLE,
    REASON_UNSUPPORTED: UrlAccessIssue.UNSUPPORTED_CONTENT,
}

_FALLBACK_ACTIONS = ["📷 傳送廣告或貼文截圖", "📝 直接貼上文字內容", "🧴 如果是在問產品，也可以傳產品照片"]

URL_ACCESS_SUMMARY = {
    UrlAccessIssue.SOCIAL_RESTRICTED: "這個社群連結目前無法直接讀取完整內容。",
    UrlAccessIssue.INVALID_URL: "這個網址目前打不開，可能是網址不完整或有誤。",
    UrlAccessIssue.ACCESS_RESTRICTED: "這個網頁需要登入，或不允許外部程式讀取，所以讀不到內容。",
    UrlAccessIssue.NETWORK_UNAVAILABLE: "剛剛連不上這個網頁，可能是網站暫時沒有回應或網路不穩。",
    UrlAccessIssue.UNSUPPORTED_CONTENT: "這個連結的內容不是一般的網頁文字（例如檔案、圖片，或要用程式才能顯示的頁面），讀不到可分析的內容。",
}
URL_ACCESS_POINTS = {
    UrlAccessIssue.SOCIAL_RESTRICTED: [
        "Facebook、Instagram 等平台可能需要登入，或限制外部程式取得貼文內容，因此目前無法可靠分析這則貼文。",
        "你可以改用：", *_FALLBACK_ACTIONS, "我會再幫你查證。"],
    UrlAccessIssue.INVALID_URL: ["請確認網址完整後再傳一次；或改用：", *_FALLBACK_ACTIONS],
    UrlAccessIssue.ACCESS_RESTRICTED: ["你可以改用：", *_FALLBACK_ACTIONS],
    UrlAccessIssue.NETWORK_UNAVAILABLE: ["你可以稍後再傳一次網址；或改用：", *_FALLBACK_ACTIONS],
    UrlAccessIssue.UNSUPPORTED_CONTENT: ["你可以改用：", *_FALLBACK_ACTIONS],
}


def url_access_text(issue: UrlAccessIssue) -> str:
    return "\n".join([URL_ACCESS_SUMMARY[issue], "", *URL_ACCESS_POINTS[issue]])


def is_social_url(url: str | None) -> bool:
    try:
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in SOCIAL_HOST_SUFFIXES)


def issue_from_fetch_error(error: ToolError, url: str | None) -> UrlAccessIssue:
    """fetch_url_content 的失敗 → 使用者可理解的類別。社群網址的任何讀取失敗都視為平台存取限制。"""
    if is_social_url(url):
        return UrlAccessIssue.SOCIAL_RESTRICTED
    issue = _ISSUE_BY_REASON.get(error.reason or "")
    if issue is not None:
        return issue
    if error.code in (ErrorCode.TIMEOUT, ErrorCode.UNAVAILABLE):  # 沒有細分原因時依錯誤碼
        return UrlAccessIssue.NETWORK_UNAVAILABLE
    return UrlAccessIssue.UNSUPPORTED_CONTENT


def has_analyzable_content(request: NormalizedUserRequest) -> bool:
    """Agent A 是否從輸入中抽到任何可查證的內容（產品 / 品牌 / 公司 / 成分 / 字號 / 宣稱 / 交互作用對象）。"""
    return bool(request.claims or request.product_candidates or request.brand_candidates
                or request.company_candidates or request.ingredient_candidates
                or request.permit_number_candidates or request.interaction_subjects
                or request.official_source_statements)


def social_content_unavailable(request: NormalizedUserRequest) -> bool:
    """URL 都是社群網址（讀取成功但只有預覽外殼 / 登入頁），且沒有抽到任何可分析內容 → 平台存取限制。

    讀到足夠可分析的內容（有產品 / 宣稱等）時不算失敗；有任何非社群網址時交給一般 URL 流程。
    """
    sources = request.url_sources
    return bool(sources) and all(is_social_url(u.requested_url) or is_social_url(u.source_url) for u in sources) \
        and not has_analyzable_content(request)
