"""Rich Menu 說明入口（拍照查產品 / 直接問問題 / 語音詢問 / TFDA 官方資料查詢）：導覽，不是語意問題。

在 LINE 層固定回覆：不經 Controller / Researcher，不呼叫 LLM / DB / KG / RAG / 網路，也不改 task context。
"""

import json

import pytest

from app.line.handler import HELP_LABELS, HELP_TEXTS, help_key_for_text, parse_help_postback
from tests.unit.test_line_webhook import PRODUCT_1, build, event, send

CASES = {
    "拍照查產品": ("photo", "2–3 張"),
    "直接問問題": ("ask", "產品名稱"),
    "語音詢問": ("voice", "語音訊息"),
    "TFDA 官方資料查詢": ("tfda", "TFDA"),
}


class Exploding:
    def __getattr__(self, name):
        raise AssertionError(f"pipeline must not be used by help actions: {name}")


@pytest.fixture
def harness():
    handler, line, contexts, store = build()
    handler._service._pipeline = Exploding()
    return handler, line, contexts, store


@pytest.mark.parametrize("label", list(CASES))
def test_menu_text_returns_fixed_guidance_without_any_pipeline_call(harness, label):
    handler, line, contexts, store = harness
    send(handler, event(text=label))
    assert len(line.sent) == 1 and line.sent[0][2]["type"] == "flex"
    key, phrase = CASES[label]
    card = json.dumps(line.sent[0][2], ensure_ascii=False)
    assert json.dumps(HELP_TEXTS[key], ensure_ascii=False)[1:-1] in card and phrase in card
    assert [i["action"]["label"] for i in line.sent[0][2]["quickReply"]["items"]] == ["開始新問題", "結束本次查證"]
    assert not store._data and not contexts.snapshot("U_TEST_ONLY_1").turns  # 不是一題查證：沒有紀錄、不改 context


@pytest.mark.parametrize("key", list(HELP_TEXTS))
def test_menu_postback_returns_the_same_guidance(harness, key):
    handler, line, _, store = harness
    send(handler, event(kind="postback", data=f"help={key}"))
    assert json.dumps(HELP_TEXTS[key], ensure_ascii=False)[1:-1] in json.dumps(line.sent[0][2], ensure_ascii=False) and not store._data


def test_menu_matching_is_exact_not_semantic():
    assert help_key_for_text("  拍照查產品 ") == "photo" and help_key_for_text("TFDA官方資料查詢") == "tfda"
    for text in ("拍照查產品怎麼用", "我想拍照", "語音", PRODUCT_1, ""):
        assert help_key_for_text(text) is None
    assert parse_help_postback("help=photo") == "photo"
    for bad in ("help=unknown", "help=photo&x=1", "help", ""):
        assert parse_help_postback(bad) is None


def test_guidance_texts_are_short_and_make_no_data_claims():
    for text in HELP_TEXTS.values():
        assert len(text) <= 120 and "官方確認" not in text and "已查到" not in text
    assert set(HELP_LABELS.values()) == set(HELP_TEXTS)


def test_normal_questions_still_use_the_pipeline():
    handler, line, _, store = build()
    send(handler, event(text=f"{PRODUCT_1}有沒有通過健康食品認證？"))
    assert len(store._data) == 1  # 一般問題照常走 pipeline


# ---- 目前 Rich Menu 送出的兩則固定訊息（完全比對；相容用）

MENU_TEXT, MENU_VOICE = "我想詢問保健食品問題", "我想使用語音詢問"


@pytest.mark.parametrize("message, key, phrases", [
    (MENU_TEXT, "text", ("想問保健食品嗎？直接跟我說就可以！", "輸入產品名稱、成分、許可證字號", "『鋅和銅可以一起吃嗎？』", "『這個產品有哪些成分？』")),
    (MENU_VOICE, "audio", ("不想打字也沒問題！", "語音功能", "『鋅跟銅可以一起吃嗎？』"))])
def test_current_rich_menu_messages_return_static_guidance_and_touch_nothing(harness, message, key, phrases):
    handler, line, contexts, store = harness
    before = contexts.snapshot("U_TEST_ONLY_1").model_dump(exclude={"updated_at"})
    assert help_key_for_text(message) == key
    send(handler, event(text=message))  # harness 的 pipeline 一旦被碰到就會 AssertionError
    assert len(line.sent) == 1 and line.sent[0][2]["type"] == "flex"
    card = json.dumps(line.sent[0][2], ensure_ascii=False)
    for phrase in phrases:
        assert phrase in card
    assert not store._data
    assert contexts.snapshot("U_TEST_ONLY_1").model_dump(exclude={"updated_at"}) == before  # TaskContext 不變


def test_menu_messages_make_no_llm_or_data_calls():
    handler, line, contexts, store = build()
    for message in (MENU_TEXT, MENU_VOICE):
        send(handler, event(text=message))
    assert len(line.sent) == 2 and not store._data  # 沒有任何一題查證被建立（Controller / Researcher / LLM / DB / KG / RAG / 網路都沒有）


@pytest.mark.parametrize("message", [f"{MENU_TEXT}，魚油可以一起吃嗎？", f"{MENU_TEXT}：{PRODUCT_1}有哪些成分", f"{MENU_VOICE}，鋅跟銅可以一起吃嗎",
                                     f" 請{MENU_TEXT}", MENU_TEXT + "?", "我想詢問保健食品"])
def test_longer_real_queries_containing_the_menu_phrase_are_not_intercepted(message):
    assert help_key_for_text(message) is None
    handler, line, _, store = build()
    send(handler, event(text=message))
    assert len(store._data) == 1  # 走正常的 Controller / pipeline


def test_existing_help_postbacks_still_work_for_every_key(harness):
    handler, line, _, store = harness
    for key in ("text", "audio", "photo", "ask", "voice", "tfda"):
        send(handler, event(kind="postback", data=f"help={key}"))
    assert len(line.sent) == 6 and not store._data
