"""多候選產品確認 → LINE 直接選擇（request-scoped postback）回歸測試。

* 候選 <= 3：同一張確認卡內直接點選；> 3：一則 carousel；仍只有一則 primary message。
* postback：action=select_product&request_id=<原題>&candidate_id=<canonical product id>。
* 驗證：request 屬於同一位使用者、candidate 是那一題當初的候選；失敗不猜。
* 選擇後沿用原題 intent 直接回答（不重新跑 Agent A），所選產品成為目前 task context。

全部使用 TEST_ONLY 虛構產品與 fake，不呼叫外部服務。
"""

import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent, _short_labels
from app.agents.verification_agent import FAST_PATH_MODE, VerificationAgent
from app.line.handler import SESSION_NEW_LABEL, LineWebhookHandler
from app.pipeline import AnalysisPipeline
from app.response.actions import build_select_product_data, parse_postback_data
from app.response.service import EXPIRED_ACTION_TEXT, INVALID_ACTION_TEXT, INVALID_CHOICE_TEXT, AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import Intent, ProductVerificationStatus, ResponseAction, ResponseType
from app.schemas.user_request import IntakeExtraction
from app.services.context_store import TaskContextStore
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product
from tests.unit.test_line_webhook import USER, FakeLine, buttons, event, send

BASE_2 = "虛構牌選擇測試飲"  # TEST_ONLY：兩個品項（其中一個許可證已失效）
VARIANTS_2 = [(f"{BASE_2}(原味)", "ACTIVE"), (f"{BASE_2}(無糖)", "EXPIRED")]
BASE_4 = "虛構牌多款測試粉"  # TEST_ONLY：四個品項 → carousel
VARIANTS_4 = [(f"{BASE_4}({x})", "ACTIVE") for x in ("甲款", "乙款", "丙款", "丁款")]
OTHER_PRODUCT = "虛構牌晶亮測試膠囊"  # synthetic_data 既有的唯一產品
OTHER_USER = "U_TEST_ONLY_OTHER"


@pytest.fixture(autouse=True)
def variant_data(monkeypatch):
    products, claims, ingredients = [], [], []
    for i, (name, status) in enumerate(VARIANTS_2 + VARIANTS_4):
        pid = f"sel-{i}"
        products.append(_product(pid, name, "c-1", "虛構測試生技股份有限公司", f"Z0070{i}", status=status))
        claims.append({"product_claim_id": f"pcs-{i}", "product_id": pid, "approved_claim_id": f"acs-{i}",
                       "function_name": "測試功效甲", "claim_text": f"TEST_ONLY 品項{i}的核准敘述。", **SOURCE})
        ingredients.append({"product_id": pid, "ingredient_id": "i-1", "raw_text": "虛構素甲"})
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + products)
    monkeypatch.setattr(data, "APPROVED_CLAIMS", data.APPROVED_CLAIMS + claims)
    monkeypatch.setattr(data, "PRODUCT_INGREDIENTS", data.PRODUCT_INGREDIENTS + ingredients)


def fake_intake(input_text: str) -> IntakeExtraction:
    """TEST_ONLY：依內容模擬 Agent A（只用於 fake）。"""
    content = json.loads(input_text)["content"]
    intent = Intent.INGREDIENT_INQUIRY if "成分" in content else Intent.EFFICACY_INQUIRY
    for name in (OTHER_PRODUCT, BASE_2, BASE_4):
        if name in content:
            return extraction(intent=intent, product_candidates=[name], normalized_text=content)
    return extraction(intent=Intent.OUT_OF_SCOPE, normalized_text=content)


def build():
    llm = FakeLLM(structured={IntakeExtraction: fake_intake})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    contexts, store = TaskContextStore(), InMemoryRequestStore()
    svc = AdaptiveResponseService(pipeline, ReporterAgent(llm), store, timeout_seconds=5.0, context_store=contexts)
    line = FakeLine()
    return LineWebhookHandler(svc, line, contexts), line, contexts, store, llm


def select_buttons(message) -> list[dict]:
    return [b for b in buttons(message)
            if (p := parse_postback_data(b["data"])) and p.action == ResponseAction.SELECT_PRODUCT]


def intake_calls(llm) -> int:
    return llm.calls.count(("parse_structured", "IntakeExtraction"))


def ask(handler, line, text, user=USER):
    rid = send(handler, event(text=text, user=user))[0]
    return rid, line.sent[-1][2]


# ============================================================ UI


def test_two_variants_render_inline_choices_with_permit_and_inactive_status():
    handler, line, *_ = build()
    rid, msg = ask(handler, line, f"{BASE_2}的核准功效是什麼？")
    assert len(line.sent) == 1 and msg["contents"]["type"] == "bubble"  # 一則、一張卡片
    picks = select_buttons(msg)
    assert [parse_postback_data(b["data"]).candidate_id for b in picks] == ["sel-0", "sel-1"]
    assert all(parse_postback_data(b["data"]).request_id == rid for b in picks)
    assert [b["label"] for b in picks] == ["選擇：原味", "選擇：無糖"]  # 能區分品項的短名稱
    text = json.dumps(msg, ensure_ascii=False)
    assert "許可證字號 測試健食字第Z00700號" in text and "許可證字號 測試健食字第Z00701號" in text
    assert "許可證已失效（仍可查詢）" in text  # 失效清楚標示，但仍可選
    # session controls 只在 Quick Reply，不在卡片內
    assert "session=" not in json.dumps(msg["contents"], ensure_ascii=False)
    assert [i["action"]["label"] for i in msg["quickReply"]["items"]][0] == SESSION_NEW_LABEL


def test_more_than_three_candidates_use_one_carousel_message():
    handler, line, *_ = build()
    _, msg = ask(handler, line, f"{BASE_4}的核准功效是什麼？")
    assert len(line.sent) == 1 and msg["type"] == "flex" and msg["contents"]["type"] == "carousel"
    picks = select_buttons(msg)
    assert len(picks) == 4 and {b["label"] for b in picks} == {"選擇：甲款", "選擇：乙款", "選擇：丙款", "選擇：丁款"}
    assert "quickReply" in msg and "session=" not in json.dumps(msg["contents"], ensure_ascii=False)


def test_short_labels_are_derived_generally():
    assert _short_labels(["某品(原味)", "某品(無糖)"]) == ["原味", "無糖"]
    assert _short_labels(["某品甲款", "某品乙款"]) == ["甲款", "乙款"]
    assert _short_labels(["某品(原味)", "某品(原味)"]) == [None, None]  # 同名：改用許可證字號區分
    assert _short_labels(["某品", "某品(原味)"]) == [None, None]  # 去前綴後有空字串：用完整品名


# ============================================================ 選擇 → 沿用原題直接回答


@pytest.mark.parametrize("question, expected_type", [
    (f"{BASE_2}的核准功效是什麼？", ResponseType.PRODUCT_INFO),
    (f"{BASE_2}有哪些成分？", ResponseType.INGREDIENT_LIST),
])
def test_selecting_candidate_resumes_original_intent_without_rerunning_agent_a(question, expected_type):
    handler, line, contexts, store, llm = build()
    rid, msg = ask(handler, line, question)
    original = store.get(rid).normalized_request
    choice = select_buttons(msg)[0]
    new_rid = send(handler, event("postback", data=choice["data"]))[0]
    assert intake_calls(llm) == 1  # 選擇後不重新做語意理解
    record = store.get(new_rid)
    assert record.normalized_request.intent == original.intent and record.normalized_request.question == original.question
    match = record.evidence.product_match
    assert match.status == ProductVerificationStatus.VERIFIED and match.best_match.product.product_id == "sel-0"
    assert record.evidence.planner_mode == FAST_PATH_MODE  # 結構化資料足夠 → 既有 fast path
    assert record.response.response_type == expected_type
    reply = json.dumps(line.sent[-1][2], ensure_ascii=False)
    if expected_type == ResponseType.PRODUCT_INFO:
        assert "TEST_ONLY 品項0的核准敘述" in reply and "品項1" not in reply
    assert contexts.snapshot(USER).current_product_id == "sel-0"  # 所選產品成為目前產品
    assert len(line.sent) == 2  # 每個 request 一則


def test_inactive_permit_candidate_can_still_be_selected():
    handler, line, _, store, _ = build()
    _, msg = ask(handler, line, f"{BASE_2}的核准功效是什麼？")
    new_rid = send(handler, event("postback", data=select_buttons(msg)[1]["data"]))[0]
    match = store.get(new_rid).evidence.product_match
    assert match.status == ProductVerificationStatus.VERIFIED and match.best_match.product.product_id == "sel-1"
    assert match.best_match.product.status.value == "EXPIRED"


def test_old_card_uses_its_own_candidates_after_user_asked_something_else():
    handler, line, contexts, store, llm = build()
    _, msg = ask(handler, line, f"{BASE_4}的核准功效是什麼？")
    old_choice = select_buttons(msg)[2]
    ask(handler, line, f"{OTHER_PRODUCT}有哪些成分？")  # 之後問了別題（另一個產品、另一種 intent）
    assert contexts.snapshot(USER).current_product_id == "p-1"
    new_rid = send(handler, event("postback", data=old_choice["data"]))[0]
    record = store.get(new_rid)
    assert record.evidence.product_match.best_match.product.product_id == parse_postback_data(old_choice["data"]).candidate_id
    assert record.normalized_request.intent == Intent.EFFICACY_INQUIRY  # 依舊題的 intent，不是最新那題
    assert contexts.snapshot(USER).current_product_id == "sel-4"
    assert intake_calls(llm) == 2


# ============================================================ 驗證失敗不猜


def test_selection_from_another_user_is_rejected():
    handler, line, _, store, _ = build()
    _, msg = ask(handler, line, f"{BASE_2}的核准功效是什麼？")
    send(handler, event("postback", data=select_buttons(msg)[0]["data"], user=OTHER_USER))
    assert EXPIRED_ACTION_TEXT in json.dumps(line.sent[-1][2], ensure_ascii=False)


@pytest.mark.parametrize("candidate", ["p-1", "sel-3", "not-a-candidate"])
def test_candidate_not_offered_by_that_request_is_rejected(candidate):
    """存在於資料庫的其他產品、其他題目的候選、不存在的 ID，都不屬於這一題 → 不猜。"""
    handler, line, contexts, store, llm = build()
    rid, _ = ask(handler, line, f"{BASE_2}的核准功效是什麼？")
    before = contexts.snapshot(USER).current_product_id
    send(handler, event("postback", data=build_select_product_data(rid, candidate)))
    assert INVALID_CHOICE_TEXT in json.dumps(line.sent[-1][2], ensure_ascii=False)
    assert contexts.snapshot(USER).current_product_id == before and intake_calls(llm) == 1


@pytest.mark.parametrize("data", [
    "action=select_product&request_id=not-a-uuid&candidate_id=sel-0",
    "action=select_product&request_id=00000000-0000-0000-0000-000000000000",  # 缺 candidate_id
    "action=select_product&request_id=00000000-0000-0000-0000-000000000000&candidate_id=a%20b",
    "action=detail&request_id=00000000-0000-0000-0000-000000000000&candidate_id=sel-0",  # 其他 action 不可帶 candidate
])
def test_malformed_select_postback_is_rejected(data):
    assert parse_postback_data(data) is None
    handler, line, *_ = build()
    send(handler, event("postback", data=data))
    assert INVALID_ACTION_TEXT in json.dumps(line.sent[-1][2], ensure_ascii=False)


def test_unknown_request_id_is_rejected():
    handler, line, *_ = build()
    send(handler, event("postback", data=build_select_product_data("00000000-0000-0000-0000-000000000000", "sel-0")))
    assert EXPIRED_ACTION_TEXT in json.dumps(line.sent[-1][2], ensure_ascii=False)
