"""Phone-smoke 修正：來源按鈕不可是 raw 匯出檔、社群網址讀不到的說明、一般網址失敗文案。

* 所有網址 / 文字皆為 synthetic（TEST_ONLY），不呼叫外部網路與 LLM。
"""

from types import SimpleNamespace

import httpx
import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent, format_sources
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.response.line_renderer import AdaptiveLineRenderer
from app.response.sources import display_source_url, is_raw_export_url, source_links
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import ErrorCode, InputType, Intent, ResponseType, SourceType
from app.schemas.evidence import SourceRef, ToolError
from app.schemas.user_request import IntakeExtraction, RawUserInput
from app.services.errors import ServiceError
from app.services.url_access import UrlAccessIssue, issue_from_fetch_error, url_access_text
from app.tools import input_tools
from app.tools.input_tools import InputToolOutput, fetch_url_content
from tests.builders import claim, extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore

RAW_CSV = "https://data.fda.gov.tw/data/opendata/export/37/csv"
RAW_JSON = "https://data.fda.gov.tw/data/opendata/export/19/json"
LANDING = "https://data.fda.gov.tw/opendata/TEST_ONLY_landing"
FB_URL = "https://www.facebook.com/TEST_ONLY/posts/123"
IG_URL = "https://www.instagram.com/p/TEST_ONLY/"
INTERNAL_CODES = ("INVALID_INPUT", "UPSTREAM_ERROR", "UNAVAILABLE", "TIMEOUT", "NOT_CONFIGURED", "INTERNAL_ERROR")


def _source(url, display=None, official=True, name="TEST_ONLY 官方資料集"):
    return SourceRef(source_name=name, source_type=SourceType.STRUCTURED_DB, source_url=url, display_url=display,
                     is_official=official)


def _record(*sources):
    return SimpleNamespace(evidence=SimpleNamespace(all_sources=lambda: list(sources)), normalized_request=None)


def _flex_uris(links) -> list[str]:
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "uri":
                out.append(node["uri"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(AdaptiveLineRenderer().render_sources(links, "TEST_ONLY"))
    return out


# ============================================================ A. 官方來源按鈕不可是 raw 匯出檔


def test_raw_csv_source_preserves_provenance_url():
    ref = _source(RAW_CSV)
    assert ref.source_url == RAW_CSV  # 稽核用的原始網址不改
    assert source_links(_record(ref))[0].title == ref.source_name  # 來源名稱仍顯示


def test_raw_csv_source_is_not_exposed_as_line_open_button():
    links = source_links(_record(_source(RAW_CSV), _source(RAW_JSON, name="TEST_ONLY 另一資料集")))
    assert [link.url for link in links] == [None, None]
    assert _flex_uris(links) == []
    assert RAW_CSV not in format_sources([_source(RAW_CSV)])


def test_human_readable_landing_url_is_preferred_and_raw_kept():
    ref = _source(RAW_CSV, display=LANDING)
    links = source_links(_record(ref))
    assert links[0].url == LANDING and _flex_uris(links) == [LANDING]
    assert ref.source_url == RAW_CSV
    assert LANDING in format_sources([ref]) and RAW_CSV not in format_sources([ref])


def test_missing_landing_url_omits_button_safely():
    links = source_links(_record(_source(RAW_CSV, display=None)))
    assert links[0].url is None and links[0].title and _flex_uris(links) == []
    # raw display_url 也不可當按鈕；一般網頁 source_url 照舊可用
    assert display_source_url(RAW_CSV, RAW_JSON) is None
    assert display_source_url("https://www.mohw.gov.tw/cp-16-1.html") == "https://www.mohw.gov.tw/cp-16-1.html"


@pytest.mark.parametrize("url,raw", [
    (RAW_CSV, True), (RAW_JSON, True), ("https://example.gov.tw/files/a.ZIP", True),
    ("https://example.gov.tw/dl?format=csv", True), ("https://example.gov.tw/download", True),
    ("https://www.fda.gov.tw/TC/site.aspx?sid=1", False), ("https://example.gov.tw/a.pdf", True),
    ("https://example.gov.tw/exports-policy", False), (None, False),
])
def test_is_raw_export_url(url, raw):
    assert is_raw_export_url(url) is raw


# ============================================================ B / C. 社群網址與一般網址讀取失敗


class _NoVerify:
    """Agent B 的替身：被呼叫就代表走進了 Controller / Researcher / 查證流程。"""

    def __init__(self):
        self.called = 0

    def verify(self, request):
        self.called += 1
        raise AssertionError("verify（Controller / Researcher / RAG / 官方網站）不應被呼叫")

    def route_bare_name(self, request):
        return request


def _pipeline(fetcher, ex=None, verifier=None):
    llm = FakeLLM(structured={IntakeExtraction: ex or extraction(intent=Intent.UNCLEAR)})
    verifier = verifier or _NoVerify()
    return AnalysisPipeline(intake=IntakeAgent(llm, url_fetcher=fetcher), verifier=verifier, risk_engine=RiskEngine(),
                            reporter=ReporterAgent(llm)), llm, verifier


def _run(pipeline, url):
    return pipeline.run(RawUserInput(input_type=InputType.URL, urls=[url]))


def _fail(code, reason=None):
    def fetcher(url):
        raise ServiceError(code, "TEST_ONLY 內部訊息", "fetch_url_content", reason)
    return fetcher


def _text_of(result) -> str:
    payload = result.response
    return "\n".join([payload.title, payload.summary, *payload.key_points, *payload.warnings])


def test_facebook_fetch_invalid_input_returns_restricted_social_guidance():
    pipeline, llm, verifier = _pipeline(_fail(ErrorCode.INVALID_INPUT, "unsupported_content"))
    result = _run(pipeline, FB_URL)
    assert result.response.response_type == ResponseType.MISSING_INFORMATION
    text = _text_of(result)
    assert "這個社群連結目前無法直接讀取完整內容" in text and "需要登入" in text
    assert "目前資訊不夠" not in text and "產品全名" not in text
    assert verifier.called == 0
    assert not llm.structured_inputs  # 連語意理解都不需要


def test_instagram_preview_only_fetch_returns_restricted_social_guidance():
    fetcher = lambda url: InputToolOutput(tool_name="fetch_url_content", text="Instagram\nLog in\nSign up",  # noqa: E731
                                          source_url=url, title="Instagram")
    pipeline, llm, verifier = _pipeline(fetcher, extraction(intent=Intent.UNCLEAR))
    result = _run(pipeline, IG_URL)
    assert "這個社群連結目前無法直接讀取完整內容" in _text_of(result)
    assert result.normalized_request.url_sources and verifier.called == 0
    assert result.evidence is None  # 沒有任何查證 / 產品查詢


def test_social_guidance_suggests_screenshot_text_and_product_photo():
    text = url_access_text(UrlAccessIssue.SOCIAL_RESTRICTED)
    assert "📷" in text and "截圖" in text and "📝" in text and "貼上" in text and "🧴" in text and "產品照片" in text
    assert "我會再幫你查證" in text


def test_social_access_failure_does_not_enter_controller_or_researcher():
    for fetcher in (_fail(ErrorCode.UPSTREAM_ERROR, "access_restricted"), _fail(ErrorCode.TIMEOUT, "network_unavailable")):
        pipeline, _, verifier = _pipeline(fetcher)
        result = _run(pipeline, "https://www.threads.net/@TEST_ONLY/post/1")
        assert "社群連結" in _text_of(result) and verifier.called == 0 and result.evidence is None


def test_readable_social_post_with_analyzable_content_follows_normal_pipeline():
    fetcher = lambda url: InputToolOutput(tool_name="fetch_url_content", text="TEST_ONLY 虛構牌晶亮測試膠囊 可以治療某疾病",  # noqa: E731
                                          source_url=url)
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=["虛構牌晶亮測試膠囊"],
                    claims=[claim("可以治療某疾病")])
    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm, url_fetcher=fetcher),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    result = _run(pipeline, IG_URL)
    assert result.evidence is not None and "社群連結目前無法" not in _text_of(result)


def test_readable_non_social_url_still_follows_normal_pipeline():
    fetcher = lambda url: InputToolOutput(tool_name="fetch_url_content", text="TEST_ONLY 廣告頁 虛構牌晶亮測試膠囊",  # noqa: E731
                                          source_url=url)
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=["虛構牌晶亮測試膠囊"], needs_product_lookup=True)
    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm, url_fetcher=fetcher),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    result = _run(pipeline, "https://example.com/ad")
    assert result.evidence is not None and result.evidence.product_match is not None


def test_non_social_unreadable_page_without_content_is_not_called_social():
    """一般網址讀到頁面但沒抽到內容：不套用社群說明（交給既有流程）。"""
    fetcher = lambda url: InputToolOutput(tool_name="fetch_url_content", text="TEST_ONLY 空殼", source_url=url)  # noqa: E731
    llm = FakeLLM(structured={IntakeExtraction: extraction(intent=Intent.UNCLEAR)})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm, url_fetcher=fetcher),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    assert "社群連結" not in _text_of(_run(pipeline, "https://example.com/blank"))


@pytest.mark.parametrize("code,reason,issue", [
    (ErrorCode.INVALID_INPUT, "invalid_url", UrlAccessIssue.INVALID_URL),
    (ErrorCode.UPSTREAM_ERROR, "access_restricted", UrlAccessIssue.ACCESS_RESTRICTED),
    (ErrorCode.TIMEOUT, "network_unavailable", UrlAccessIssue.NETWORK_UNAVAILABLE),
    (ErrorCode.INVALID_INPUT, "unsupported_content", UrlAccessIssue.UNSUPPORTED_CONTENT),
    (ErrorCode.TIMEOUT, None, UrlAccessIssue.NETWORK_UNAVAILABLE),
])
def test_non_social_failures_are_differentiated_with_recovery_action(code, reason, issue):
    assert issue_from_fetch_error(ToolError(code=code, message="x", source="fetch_url_content", reason=reason),
                                  "https://example.com/a") == issue
    pipeline, _, verifier = _pipeline(_fail(code, reason))
    result = _run(pipeline, "https://example.com/a")
    text = _text_of(result)
    assert verifier.called == 0 and "你可以" in text or "請確認" in text
    assert "社群連結" not in text and "目前資訊不夠" not in text


def test_internal_error_codes_never_shown_to_user():
    for issue in UrlAccessIssue:
        text = url_access_text(issue)
        assert not any(code in text for code in INTERNAL_CODES)
    for code in ErrorCode:
        pipeline, _, _ = _pipeline(_fail(code, None))
        for url in (FB_URL, "https://example.com/a"):
            text = _text_of(_run(pipeline, url))
            assert not any(c in text for c in INTERNAL_CODES), (code, url, text)


def test_fetch_tool_sets_failure_reasons():
    def fetch(fn, url="https://example.com/a"):
        client = httpx.Client(transport=httpx.MockTransport(fn))
        return fetch_url_content(url, client=client, resolve=lambda host: ["93.184.216.34"])

    def reason_of(fn, **kw):
        with pytest.raises(ServiceError) as err:
            fetch(fn, **kw)
        return err.value.reason

    assert reason_of(lambda req: httpx.Response(403)) == "access_restricted"
    assert reason_of(lambda req: httpx.Response(404)) == "invalid_url"
    assert reason_of(lambda req: httpx.Response(503)) == "network_unavailable"
    assert reason_of(lambda req: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})) \
        == "unsupported_content"
    assert reason_of(lambda req: httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})) \
        == "unsupported_content"
    assert reason_of(lambda req: httpx.Response(200), url="ftp://example.com/a") == "invalid_url"
    assert input_tools.fetch_url_content  # 正常讀取的行為由既有 test_demo_stabilization / test_tool_boundaries 守住
