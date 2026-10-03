"""Demo UX stabilization：圖片結果 provenance（request-scoped query context）與可點擊的資料來源。

全部使用虛構產品 / 網域（TEST_ONLY fake），不呼叫外部服務。
"""

import json
import time
from datetime import datetime, timezone

import pytest

from app.line.events import parse_events
from app.response.actions import build_postback_data
from app.response.line_renderer import AdaptiveLineRenderer
from app.response.query_context import received_time_text
from app.response.sources import SourceLink, safe_source_url, source_links
from app.schemas.enums import ResponseAction
from app.schemas.user_request import IntakeExtraction
from tests.unit.test_line_webhook import PRODUCT_1, PRODUCT_2, USER, FakeLine, build, send

TS_1 = 1_759_471_200_000  # TEST_ONLY 固定的 LINE event timestamp（ms）
TS_2 = TS_1 + 7_000
OTHER_USER = "U_TEST_ONLY_OTHER"

_n = iter(range(10**6))


def raw_event(kind, *, text="", message_id=None, data="", user=USER, ts=TS_1):
    n = next(_n)
    base = {"type": "postback" if kind == "postback" else "message", "replyToken": f"rt-ctx-{n}",
            "source": {"type": "user", "userId": user}, "webhookEventId": f"ev-ctx-{n}", "timestamp": ts,
            "deliveryContext": {"isRedelivery": False}}
    if kind == "postback":
        base["postback"] = {"data": data}
    else:
        base["message"] = {"type": kind, "id": message_id or f"m-ctx-{n}", **({"text": text} if kind == "text" else {})}
    return parse_events({"events": [base]})[0]


def flex(line, i=-1) -> str:
    return json.dumps(line.sent[i][2], ensure_ascii=False)


def setup_images(**kw):
    """圖片內容決定讀到哪個產品（TEST_ONLY）：b"img-1" → PRODUCT_1、b"img-2" → PRODUCT_2、其他 → 無法辨識。"""
    handler, line, contexts, store = build(**kw)
    llm = handler._service._pipeline._intake._media
    names = {b"img-1": PRODUCT_1, b"img-2": PRODUCT_2}
    llm.describe_image = lambda *, instructions, image, mime_type: f"TEST_ONLY 圖片文字：{names.get(image, '無法辨識')}"
    line.contents = {"m-img-1": (b"img-1", "image/jpeg"), "m-img-2": (b"img-2", "image/jpeg"),
                     "m-img-x": (b"img-x", "image/jpeg")}
    return handler, line, store


# ============================================================ 1. image result context


def test_image_result_card_shows_recognized_name_and_is_stored_on_request():
    handler, line, store = setup_images()
    [rid] = send(handler, raw_event("image", message_id="m-img-1"))
    assert f"圖片辨識｜{PRODUCT_1}"[:16] in flex(line)
    assert store.get(rid).query_label.startswith("圖片辨識｜")


def test_image_without_reliable_name_uses_received_time():
    handler, line, store = setup_images()
    [rid] = send(handler, raw_event("image", message_id="m-img-x"))
    expected = "圖片查詢｜" + received_time_text(datetime.fromtimestamp(TS_1 / 1000, timezone.utc))
    assert expected in flex(line)
    assert store.get(rid).query_label == expected


def test_text_request_has_no_image_label():
    handler, line, _, store = build()
    [rid] = send(handler, raw_event("text", text=PRODUCT_1))
    assert store.get(rid).query_label is None and "圖片" not in flex(line)


# ============================================================ 2. image timeout / error context


class SlowPipeline:
    def __init__(self, inner, delay):
        self._inner, self._delay = inner, delay

    def run(self, *a, **kw):
        time.sleep(self._delay)
        return self._inner.run(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_image_timeout_card_and_retry_keep_original_request_context():
    handler, line, store = setup_images(timeout=0.05)
    svc = handler._service
    svc._pipeline = SlowPipeline(svc._pipeline, 0.5)
    [rid] = send(handler, raw_event("image", message_id="m-img-1"))
    label = store.get(rid).query_label
    assert label.startswith("圖片查詢｜") and label in flex(line)  # 逾時：沒有結果，用收到時間
    assert "查詢未完成" in flex(line)
    # 稍後按「重新查詢」：仍是同一張圖片的收到時間（不是按下按鈕的時間）
    svc._pipeline = svc._pipeline._inner
    send(handler, raw_event("postback", data=build_postback_data(ResponseAction.RETRY, rid), ts=TS_2 + 60_000))
    assert f"圖片辨識｜{PRODUCT_1}"[:16] in flex(line)  # 這次有結果：同一張圖片的辨識名稱
    retry_records = [r for r in store._data.values() if r.request_id != rid]
    assert retry_records and retry_records[0].received_at == store.get(rid).received_at


def test_image_download_failure_card_shows_image_context():
    handler, line, _ = setup_images()
    send(handler, raw_event("image", message_id="m-missing"))
    assert "圖片查詢｜" in flex(line) and len(line.sent) == 1


def test_image_watchdog_timeout_card_shows_image_context():
    """job 卡住超過 queue watchdog：逾時卡仍標示是哪一張圖片（收到時就固定的 context），晚到結果不補發。"""
    from app.line.request_queue import PerUserRequestQueue

    handler, line, _ = setup_images(timeout=5.0)
    handler._queue = PerUserRequestQueue(job_timeout_seconds=0.2)
    svc = handler._service
    svc._pipeline = SlowPipeline(svc._pipeline, 0.6)
    send(handler, raw_event("image", message_id="m-img-1"))
    time.sleep(0.8)  # 讓卡住的 job 完成：結果必須被丟棄
    assert len(line.sent) == 1
    assert "圖片查詢｜" in flex(line) and "查詢未完成" in flex(line)


# ============================================================ 3. multi-image isolation


def test_queued_images_each_answer_with_their_own_context_and_old_card_postback():
    handler, line, store = setup_images()
    rid1, rid2 = send(handler, raw_event("image", message_id="m-img-1", ts=TS_1),
                      raw_event("image", message_id="m-img-2", ts=TS_2))
    l1, l2 = store.get(rid1).query_label, store.get(rid2).query_label
    assert l1 != l2 and PRODUCT_1[:5] in l1 and PRODUCT_2[:5] in l2
    assert l1 in flex(line, 0) and l2 in flex(line, 1)  # FIFO：各自的卡片標示各自的圖片
    # 第二張之後才按第一張卡片的按鈕：仍是第一張圖片的 context
    for action in (ResponseAction.SOURCES, ResponseAction.OFFICIAL_INFO):
        send(handler, raw_event("postback", data=build_postback_data(action, rid1), ts=TS_2 + 1000))
        assert l1 in flex(line) and l2 not in flex(line)


# ============================================================ 4. source URI renderer


def _buttons(message):
    contents = message["contents"]["body"]["contents"]
    return [c["action"] for c in contents if c.get("type") == "button"]


def test_source_card_uses_uri_actions_only_for_valid_stored_urls():
    links = [
        SourceLink(title="TEST_ONLY 官方資料", domain="test-only.fda.gov.tw", is_official=True,
                   url="https://test-only.fda.gov.tw/TEST_ONLY/1"),
        SourceLink(title="TEST_ONLY 商品頁", domain="shop.example-mall.invalid", is_official=False,
                   url="https://shop.example-mall.invalid/item/1"),
        SourceLink(title="TEST_ONLY 沒有網址的來源", is_official=True, url=None),
    ]
    message = AdaptiveLineRenderer().render_sources(links, "圖片辨識｜TEST_ONLY")
    text = json.dumps(message, ensure_ascii=False)
    assert _buttons(message) == [
        {"type": "uri", "label": "開啟官方來源", "uri": "https://test-only.fda.gov.tw/TEST_ONLY/1"},
        {"type": "uri", "label": "開啟原始頁面", "uri": "https://shop.example-mall.invalid/item/1"},
    ]
    assert "官方來源｜test-only.fda.gov.tw" in text and "外部來源｜shop.example-mall.invalid" in text
    assert "TEST_ONLY 沒有網址的來源" in text and "查詢：圖片辨識｜TEST_ONLY" in text


def test_renderer_revalidates_url_before_building_button():
    bad = SourceLink(title="TEST_ONLY", is_official=True, url="javascript:alert(1)")
    assert _buttons(AdaptiveLineRenderer().render_sources([bad])) == []


def test_primary_card_has_no_uri_buttons():
    handler, line, _, _ = build()
    send(handler, raw_event("text", text=PRODUCT_1))
    assert '"uri"' not in flex(line)


# ============================================================ 5. invalid URL rejection


@pytest.mark.parametrize("url", [
    None, "", "javascript:alert(1)", "data:text/html,TEST", "ftp://example.invalid/a", "https://", "//example.invalid/a",
    "https://user:pw@example.invalid/a", "https://example.invalid/a b", "https://example.invalid/測試",
    "https://example.invalid:99999/a", "https://example.invalid/" + "a" * 1000, "line://app/x", "tel:0912",
    "https://example.invalid/a\nb", "https://example.invalid/a\tb",
])
def test_invalid_urls_are_rejected(url):
    assert safe_source_url(url) is None


@pytest.mark.parametrize("url", ["https://test-only.fda.gov.tw/a?id=1&b=2", "http://example.invalid/a#x"])
def test_valid_urls_pass_unchanged(url):
    assert safe_source_url(url) == url


def test_source_links_from_record_drop_buttons_for_invalid_stored_urls():
    handler, line, _, store = build()
    [rid] = send(handler, raw_event("text", text=PRODUCT_1))
    record = store.get(rid)
    ev = record.evidence
    items = [ev.product_match.best_match.product] + [
        e for group in (ev.approved_claims, ev.warnings, ev.illegal_ad_evidence, ev.graph_evidence,
                        ev.rag_evidence, ev.official_web_evidence) for e in group]
    for item in items:  # 模擬 stored metadata 中的不合格網址
        item.source.source_url = "javascript:alert(1)"
    links = source_links(record)
    assert links and all(link.url is None for link in links)
    assert _buttons(AdaptiveLineRenderer().render_sources(links)) == []


# ============================================================ 6. old request source postback / security


def test_old_card_sources_show_that_request_sources_only():
    handler, line, _, store = build()
    url_a, url_b = "https://shop-a.example-mall.invalid/p/1", "https://shop-b.example-mall.invalid/p/2"
    rid_a, rid_b = send(handler, raw_event("text", text=url_a), raw_event("text", text=url_b))
    assert ResponseAction.SOURCES in store.get(rid_a).response.available_actions
    send(handler, raw_event("postback", data=build_postback_data(ResponseAction.SOURCES, rid_a)))
    card = flex(line)
    assert url_a in card and url_b not in card  # 舊卡片 → 舊 request 的來源
    assert "開啟原始頁面" in card and "外部來源｜shop-a.example-mall.invalid" in card
    assert "開啟官方來源" in card  # 該題的官方結構化資料來源


def test_sources_postback_from_another_user_is_rejected():
    handler, line, _, store = build()
    url_a = "https://shop-a.example-mall.invalid/p/1"
    [rid] = send(handler, raw_event("text", text=url_a))
    send(handler, raw_event("postback", data=build_postback_data(ResponseAction.SOURCES, rid), user=OTHER_USER))
    card = flex(line)
    assert url_a not in card and '"uri"' not in card
