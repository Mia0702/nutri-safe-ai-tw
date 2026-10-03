"""資料來源卡的來源清單（只取自該 request 保存的 evidence / source metadata）。

* 網址只能直接來自 stored SourceRef.source_url 或使用者提供網址的 UrlSource（實際讀取的網址），
  不由 LLM 產生、不依來源名稱拼接、不猜測。
* 只接受通過 validation 的 http / https 網址；不合格的來源只顯示文字，不建立按鈕。
"""

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


def _domain(url: str | None) -> str | None:
    try:
        return (urlparse(url or "").hostname or "").lower() or None
    except ValueError:
        return None


def source_links(record) -> list[SourceLink]:
    """依該 request 的 EvidenceBundle 來源與使用者提供的網址列出來源（官方在前，同網址只列一次）。"""
    links: list[SourceLink] = []
    seen: set[str] = set()

    def add(title: str, raw_url: str | None, is_official: bool) -> None:
        url = safe_source_url(raw_url)
        key = url or f"name:{title}"
        if key in seen:
            return
        seen.add(key)
        links.append(SourceLink(title=title or (_domain(url) or "資料來源"), domain=_domain(url),
                                is_official=is_official, url=url))

    if record.evidence is not None:
        for src in record.evidence.all_sources():
            add(src.source_name, src.source_url, src.is_official)
    if record.normalized_request is not None:
        for page in record.normalized_request.url_sources:  # 使用者提供的原始頁面（官方頁已在 evidence 中）
            if page.fetched:
                add(page.title or page.source_domain or "", page.source_url, page.is_official)
    return sorted(links, key=lambda link: not link.is_official)
