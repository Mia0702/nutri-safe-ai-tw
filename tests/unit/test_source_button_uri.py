"""來源卡按鈕 URI 回歸：按鈕一定是 type=uri、完整 https 絕對網址；不合格的值不建立按鈕（不留點不開的死按鈕）。

只用 synthetic 網址（TEST_ONLY / .invalid），不針對任何網域寫特例。
"""

from app.response.line_renderer import OPEN_OFFICIAL_LABEL, AdaptiveLineRenderer
from app.response.sources import SourceLink, display_source_url

R = AdaptiveLineRenderer()
OFFICIAL_PAGE = "https://test-only.fda.gov.tw/TEST_ONLY/page?id=1"
RAW_CSV = "https://data.fda.gov.tw/data/opendata/export/24/csv"


def _buttons(node):
    if isinstance(node, dict):
        if node.get("type") == "button":
            yield node
        for v in node.values():
            yield from _buttons(v)
    elif isinstance(node, list):
        for v in node:
            yield from _buttons(v)


def _render(url):
    link = SourceLink(title="TEST_ONLY 來源", domain="test-only.fda.gov.tw", is_official=True, url=url)
    return R.render_sources([link], "TEST_ONLY")


def test_valid_display_url_wins_and_renders_uri_action():
    assert display_source_url(RAW_CSV, OFFICIAL_PAGE) == OFFICIAL_PAGE


def test_valid_non_raw_source_url_is_fallback():
    assert display_source_url(OFFICIAL_PAGE, None) == OFFICIAL_PAGE


def test_raw_csv_export_has_no_button():
    assert display_source_url(RAW_CSV, None) is None
    assert list(_buttons(_render(display_source_url(RAW_CSV)))) == []


def test_bare_domain_and_invalid_values_produce_no_dead_button():
    for bad in ("test-only.fda.gov.tw", "//test-only.fda.gov.tw/x", "/relative/path", "", "   ", "https://", "javascript:alert(1)",
                "https://test-only.fda.gov.tw/a b", "https://test-only.fda.gov.tw/a\nb", "https://u:p@test-only.fda.gov.tw/x"):
        assert display_source_url(bad, None) is None, bad
        assert list(_buttons(_render(display_source_url(bad)))) == [], bad


def test_final_flex_action_is_uri_with_exact_full_https_url():
    buttons = list(_buttons(_render(OFFICIAL_PAGE)))
    assert len(buttons) == 1
    action = buttons[0]["action"]
    assert action == {"type": "uri", "label": OPEN_OFFICIAL_LABEL, "uri": OFFICIAL_PAGE}
    assert action["uri"].startswith("https://")


def test_official_http_is_upgraded_and_duplicate_slashes_collapsed():
    legacy = "http://test-only.fda.gov.tw//Food/Detail.aspx?nodeID=162&id=1"
    assert display_source_url(legacy) == "https://test-only.fda.gov.tw/Food/Detail.aspx?nodeID=162&id=1"


def test_non_official_http_is_not_rewritten_to_another_scheme():
    assert display_source_url("http://shop.example-mall.invalid/item/1") == "http://shop.example-mall.invalid/item/1"


def test_source_card_text_is_unchanged_by_url_normalisation():
    texts = {n["text"] for n in _walk_texts(_render(OFFICIAL_PAGE))}
    assert {"資料來源", "TEST_ONLY 來源", "官方來源｜test-only.fda.gov.tw"} <= texts


def _walk_texts(node):
    if isinstance(node, dict):
        if node.get("type") == "text":
            yield node
        for v in node.values():
            yield from _walk_texts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_texts(v)


# ---------------------------------------------------------------- 直接下載 / 附件端點：不建立按鈕（provenance 保留）

def test_direct_pdf_has_no_user_facing_url():
    for url in ("https://test-only.fda.gov.tw/files/TEST_ONLY.pdf", "https://test-only.fda.gov.tw/files/TEST_ONLY.PDF?v=2",
                "https://test-only.fda.gov.tw/files/TEST_ONLY.docx"):
        assert display_source_url(url) is None, url
        assert list(_buttons(_render(display_source_url(url)))) == []


def test_getfile_endpoint_has_no_user_facing_url():
    for url in ("https://test-only.fda.gov.tw/Portals/GetFile.ashx?fid=TEST_ONLY",
                "https://test-only.fda.gov.tw/Common/getfile.ashx?id=1", "https://test-only.fda.gov.tw/x/Download_File.aspx?id=1"):
        assert display_source_url(url) is None, url


def test_attachment_and_download_endpoints_have_no_user_facing_url():
    for url in ("https://test-only.fda.gov.tw/Attachment/1234", "https://test-only.fda.gov.tw/a/downloads/info",
                "https://test-only.fda.gov.tw/page?download=1&id=2", "https://test-only.fda.gov.tw/page?attachment=3"):
        assert display_source_url(url) is None, url


def test_normal_html_pages_keep_their_button():
    for url in ("https://test-only.fda.gov.tw/Food/InfoHealthFoodDetail.aspx?nodeID=162&id=1",
                "https://test-only.fda.gov.tw/TEST_ONLY/news/page.html", "https://test-only.fda.gov.tw/files-and-forms"):
        assert display_source_url(url) == url, url
        assert len(list(_buttons(_render(display_source_url(url))))) == 1


def test_normalized_consumer_html_url_keeps_its_button():
    legacy = "http://test-only.fda.gov.tw//Food/InfoHealthFoodDetail.aspx?nodeID=162&id=1"
    result = display_source_url(legacy)
    assert result == "https://test-only.fda.gov.tw/Food/InfoHealthFoodDetail.aspx?nodeID=162&id=1"
    assert len(list(_buttons(_render(result)))) == 1


def test_reviewed_display_url_beats_a_pdf_source_url():
    pdf = "https://test-only.fda.gov.tw/files/TEST_ONLY.pdf"
    assert display_source_url(pdf, OFFICIAL_PAGE) == OFFICIAL_PAGE


def test_provenance_source_url_is_unchanged_and_metadata_kept_without_button():
    from types import SimpleNamespace

    from app.response.sources import source_links
    from app.schemas.enums import SourceType
    from app.schemas.evidence import SourceRef

    pdf = "https://test-only.fda.gov.tw/files/TEST_ONLY.pdf"
    src = SourceRef(source_name="TEST_ONLY 公告", source_type=SourceType.RAG_DOCUMENT, source_url=pdf, is_official=True)
    record = SimpleNamespace(evidence=SimpleNamespace(all_sources=lambda: [src]), normalized_request=None)
    links = source_links(record)
    assert src.source_url == pdf  # canonical / raw provenance 不被改寫
    assert len(links) == 1 and links[0].title == "TEST_ONLY 公告" and links[0].is_official and links[0].url is None
    assert list(_buttons(R.render_sources(links, "TEST_ONLY"))) == []  # 來源名稱保留、沒有「開啟官方來源」
