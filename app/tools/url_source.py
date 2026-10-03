"""使用者提供網址的來源角色分類（URL source role）。

只依「網址實際所在的網域」與既有台灣官方網域 allowlist（TW_OFFICIAL_DOMAINS）判斷，
不看頁面內容怎麼自稱（商城頁寫「官方認證」不會變成官方來源），也不針對任何特定網址。

* OFFICIAL_REGULATORY_SOURCE：allowlist 網域（含子網域）。頁面內容可作為官方證據，
  但只代表來源角色，證據範圍（evidence scope）仍依頁面實際能對應到的內容決定。
* UNKNOWN_EXTERNAL_SOURCE：無法可靠判斷——網址無法解析、主機是 IP、或是 allowlist 以外的政府網域
  （可能是官方，但不在 Phase 1 查證範圍，不猜為官方）。
* NON_OFFICIAL_EXTERNAL_SOURCE：其他一般網域（商城、廣告、新聞、社群、品牌頁）。
"""

import ipaddress
from urllib.parse import urlparse

from app.schemas.enums import UrlSourceRole
from app.tools.official_web_tools import TW_OFFICIAL_DOMAINS, is_allowed_official_url

# 政府網域字尾：不在 allowlist 時不能當官方證據，但也不能斷定是非官方
_GOVERNMENT_SUFFIXES = (".gov.tw", ".gov")


def url_domain(url: str | None) -> str | None:
    try:
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return host or None


def classify_url_source(url: str | None, allowed_domains: tuple[str, ...] = TW_OFFICIAL_DOMAINS) -> UrlSourceRole:
    host = url_domain(url)
    if not host or urlparse(url or "").scheme not in ("http", "https"):
        return UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE
    if is_allowed_official_url(url, allowed_domains):
        return UrlSourceRole.OFFICIAL_REGULATORY_SOURCE
    try:
        ipaddress.ip_address(host.strip("[]"))
        return UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE
    except ValueError:
        pass
    if host.endswith(_GOVERNMENT_SUFFIXES):
        return UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE
    return UrlSourceRole.NON_OFFICIAL_EXTERNAL_SOURCE
