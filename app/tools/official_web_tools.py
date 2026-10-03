"""台灣官方網站 fallback 查證 tool。

* 只在內部證據不足時由 Agent B 呼叫。
* 使用 OpenAI Responses API web_search，並以 allowed_domains 限制搜尋範圍。
* 回傳前再以程式檢查網域，任何非允許網域的結果一律丟棄。
"""

import hashlib
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.agents.prompts import OFFICIAL_WEB_SEARCH_INSTRUCTIONS
from app.schemas.enums import SourceType
from app.schemas.evidence import OfficialWebEvidence, SourceRef, utc_now

# Phase 1 只允許的台灣官方網域（子網域亦允許，例如 consumer.fda.gov.tw）
TW_OFFICIAL_DOMAINS: tuple[str, ...] = (
    "fda.gov.tw",
    "data.fda.gov.tw",
    "mohw.gov.tw",
    "data.gov.tw",
)


class _WebFinding(BaseModel):
    title: str
    url: str
    snippet: str = Field(description="頁面中與問題直接相關的內容摘錄或精確摘要，不加入頁面沒有的資訊")


class _WebSearchOutput(BaseModel):
    findings: list[_WebFinding]


class OfficialWebSearchOutput(BaseModel):
    evidence: list[OfficialWebEvidence]
    rejected_urls: list[str] = Field(default_factory=list)

    @property
    def result_count(self) -> int:
        return len(self.evidence)


def is_allowed_official_url(url: str, allowed_domains: tuple[str, ...] = TW_OFFICIAL_DOMAINS) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


def search_tw_official_web(llm, query: str, allowed_domains: tuple[str, ...] = TW_OFFICIAL_DOMAINS) -> OfficialWebSearchOutput:
    """llm 需實作 LLMClient.web_search_structured。"""
    query = (query or "").strip()
    if not query:
        return OfficialWebSearchOutput(evidence=[])
    result = llm.web_search_structured(
        instructions=OFFICIAL_WEB_SEARCH_INSTRUCTIONS,
        input_text=query,
        output_model=_WebSearchOutput,
        allowed_domains=list(allowed_domains),
    )
    parsed: _WebSearchOutput | None = result.parsed
    visited = {s.url.rstrip("/") for s in result.sources}
    retrieved_at = utc_now()
    evidence: list[OfficialWebEvidence] = []
    rejected: list[str] = []
    for finding in parsed.findings if parsed else []:
        url = finding.url.strip()
        # 只接受官方網域，且（若 API 有回傳來源清單）必須是實際搜尋到的頁面
        if not is_allowed_official_url(url, allowed_domains) or (visited and url.rstrip("/") not in visited):
            rejected.append(url)
            continue
        domain = (urlparse(url).hostname or "").lower()
        evidence.append(
            OfficialWebEvidence(
                evidence_id="web:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
                title=finding.title,
                url=url,
                domain=domain,
                snippet=finding.snippet,
                source=SourceRef(
                    source_name=finding.title,
                    source_type=SourceType.OFFICIAL_WEB,
                    source_url=url,
                    retrieved_at=retrieved_at,
                    is_official=True,
                ),
            )
        )
    return OfficialWebSearchOutput(evidence=evidence, rejected_urls=rejected)
