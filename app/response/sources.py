"""資料來源卡的來源清單（只取自該 request 保存的 evidence / source metadata）。

* 網址只能直接來自 stored SourceRef.source_url 或使用者提供網址的 UrlSource（實際讀取的網址），
  不由 LLM 產生、不依來源名稱拼接、不猜測。
* 只接受通過 validation 的 http / https 網址；不合格的來源只顯示文字，不建立按鈕。
* 「來源按鈕」是給人看的：raw 匯出檔（csv / json / zip / export endpoint）在手機上會直接下載，不可當按鈕；
  這類網址只保留在 SourceRef.source_url 作為 provenance（稽核用），顯示改用已審核的人類可讀網址（display_url），
  沒有就只顯示來源名稱、不建立按鈕。不推測、不拼接網址。
"""

import re
from urllib.parse import urlparse

from pydantic import BaseModel

MAX_SOURCE_URL_CHARS = 1000  # LINE URI action 上限


class SourceLink(BaseModel):
    title: str
    domain: str | None = None
    is_official: bool
    url: str | None = None  # 只有通過 safe_source_url 的網址


def safe_source_url(url: str | None) -> str | None:
    """http / https、有主機、無帳密、只含可見 ASCII（無空白 / 控制字元）、長度在 LINE 上限內；否則 None。"""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > MAX_SOURCE_URL_CHARS or any(not (0x21 <= ord(ch) <= 0x7E) for ch in url):
        return None
    try:
        parsed = urlparse(url)
        parsed.port  # noqa: B018 - 不合法的 port 會拋 ValueError
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return url


_RAW_FILE_SUFFIXES = (".csv", ".tsv", ".json", ".xml", ".zip", ".gz", ".xls", ".xlsx", ".ods", ".rar", ".7z",
                      ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".odt")
_RAW_FORMAT_NAMES = frozenset({"csv", "tsv", "json", "xml", "zip", "xls", "xlsx", "ods"})
_RAW_EXPORT_PATH = re.compile(r"/(export|download|downloads)(/|$)")
# 一般性的「下載 / 附件」端點辨識（只看路徑結構，不含任何網站或 ID）：
# 檔名主體（去副檔名）是 GetFile / DownloadFile / Download / Attachment 之類，或路徑中有 download / attachment 區段
_DOWNLOAD_ENDPOINT_STEMS = frozenset({"getfile", "getfiles", "downloadfile", "download", "filedownload", "getattachment",
                                       "attachment", "attachments", "openfile", "showfile"})
_DOWNLOAD_PATH_SEGMENTS = frozenset({"download", "downloads", "attachment", "attachments"})
_DOWNLOAD_QUERY_KEYS = frozenset({"download", "attachment"})


def is_raw_export_url(url: str | None) -> bool:
    """機器可讀的匯出 / 直接下載網址（檔案副檔名含 pdf / office 文件、/export/…/csv、/download、GetFile 類下載端點、
    attachment 區段、format=csv / download= 查詢參數）；一般網頁回傳 False。這些網址不當作使用者按鈕（provenance 仍保留）。"""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    path = parsed.path.lower().rstrip("/")
    if path.endswith(_RAW_FILE_SUFFIXES):
        return True
    if _RAW_EXPORT_PATH.search(path + "/") and path.rsplit("/", 1)[-1] in _RAW_FORMAT_NAMES | {"export", "download"}:
        return True
    segments = [seg for seg in path.split("/") if seg]
    if segments and segments[-1].split(".")[0].replace("_", "").replace("-", "") in _DOWNLOAD_ENDPOINT_STEMS:
        return True
    if any(seg in _DOWNLOAD_PATH_SEGMENTS for seg in segments):
        return True
    query = parsed.query.lower()
    if any(part.split("=")[0] in _DOWNLOAD_QUERY_KEYS for part in query.split("&") if part):
        return True
    return any(f"{key}={fmt}" in query for key in ("format", "type", "output", "filetype") for fmt in _RAW_FORMAT_NAMES)


def _button_friendly(url: str) -> str:
    """給手機按鈕用的一般性正規化（不改網站、路徑內容與查詢參數）：
    * 官方網域（Phase 1 台灣官方網域清單）的 http 升級成 https（官方站都支援 https；手機瀏覽器對純 http 常擋 / 轉址失敗）；
    * 路徑中連續的 // 合併成 /（官方資料裡常見 host//path，部分伺服器會 404）。"""
    from app.tools.official_web_tools import TW_OFFICIAL_DOMAINS  # 延後匯入：避免 response ↔ tools 循環相依

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    scheme = parsed.scheme.lower()
    if scheme == "http" and any(host == d or host.endswith("." + d) for d in TW_OFFICIAL_DOMAINS):
        scheme = "https"
    path = re.sub(r"/{2,}", "/", parsed.path)
    return parsed._replace(scheme=scheme, path=path).geturl()


def display_source_url(source_url: str | None, display_url: str | None = None) -> str | None:
    """給使用者按的網址：優先已審核的人類可讀網址，其次非 raw 的 source_url；只有 raw 匯出時回傳 None。
    回傳值一定是完整的 http(s) 絕對網址（沒有 scheme 的網域、相對路徑、含空白 / 控制字元的值都回傳 None → 不建立按鈕）。"""
    for candidate in (display_url, source_url):
        safe = safe_source_url(candidate)
        if safe is not None and not is_raw_export_url(safe):
            return _button_friendly(safe)
    return None


def _domain(url: str | None) -> str | None:
    try:
        return (urlparse(url or "").hostname or "").lower() or None
    except ValueError:
        return None


def source_links(record) -> list[SourceLink]:
    """依該 request 的 EvidenceBundle 來源與使用者提供的網址列出來源（官方在前，同網址只列一次）。"""
    links: list[SourceLink] = []
    seen: set[str] = set()

    def add(title: str, raw_url: str | None, is_official: bool, display_url: str | None = None) -> None:
        url = display_source_url(raw_url, display_url)  # raw 匯出網址不建立按鈕（provenance 仍保留在 SourceRef.source_url）
        key = url or f"name:{title}"
        if key in seen:
            return
        seen.add(key)
        links.append(SourceLink(title=title or (_domain(url) or "資料來源"), domain=_domain(url),
                                is_official=is_official, url=url))

    if record.evidence is not None:
        for src in record.evidence.all_sources():
            add(src.source_name, src.source_url, src.is_official, src.display_url)
    if record.normalized_request is not None:
        for page in record.normalized_request.url_sources:  # 使用者提供的原始頁面（官方頁已在 evidence 中）
            if page.fetched:
                add(page.title or page.source_domain or "", page.source_url, page.is_official)
    return sorted(links, key=lambda link: not link.is_official)
