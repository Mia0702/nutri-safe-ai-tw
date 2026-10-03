"""Pending Clarification Context 回歸測試。

上一題系統在問「是不是這一項官方紀錄」時，使用者回「是 / 對 / 是同一個 / 不是…」：
* CONFIRM → 以該候選繼續上一題（不重跑 Agent A），只代表使用者確認目標，名稱衝突保留；
* REJECT → 清掉候選、請補產品資訊；明確提出其他產品 → 新產品優先；
* 開始新問題 / 結束本次查證 / TTL 過期 → pending 清除。

Fake Agent A 只認得下方 TEST_ONLY 虛構名稱，其他訊息一律回 unclear（模擬原本的 bug）。不呼叫外部服務。
"""

import json
from datetime import timedelta

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.line.handler import SESSION_END, SESSION_NEW, LineWebhookHandler, session_postback_data
from app.pipeline import AnalysisPipeline
from app.response.service import REJECTED_CANDIDATE_TEXT, AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    DialogueAct,
    Intent,
    PendingClarificationType,
    ProductConfirmationSource,
    ProductVerificationStatus,
)
from app.schemas.user_request import IntakeExtraction, PendingClarification
from app.services.clarification import classify_dialogue_act
from app.services.context_store import TaskContextStore
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product
from tests.unit.test_context_binding_fonts import Clock
from tests.unit.test_line_webhook import USER, FakeLine, event, send

OFFICIAL = "樣本果確認測試膠囊"  # TEST_ONLY 官方紀錄品名
PERMIT = "Z00900"
OBSERVED = "樣本果確認測試錠"  # TEST_ONLY 圖片讀到的品名：只相似、未達確認門檻
CONFLICT_NAME = "完全不同名稱飲"  # TEST_ONLY 與字號對應品名明顯不同
OTHER = "虛構牌晶亮測試膠囊"  # synthetic_data 既有產品 p-1
OTHER_PERMIT = "Z00001"


@pytest.fixture(autouse=True)
def pending_data(monkeypatch):
    pid = "pend-1"
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [
        _product(pid, OFFICIAL, "c-9", "樣本確認測試有限公司", PERMIT)])
    monkeypatch.setattr(data, "APPROVED_CLAIMS", data.APPROVED_CLAIMS + [
        {"product_claim_id": f"pc-{pid}", "product_id": pid, "approved_claim_id": f"ac-{pid}",
         "function_name": "測試功效甲", "claim_text": "TEST_ONLY 核准敘述。", **SOURCE}])
    monkeypatch.setattr(data, "PRODUCT_INGREDIENTS", data.PRODUCT_INGREDIENTS + [
        {"product_id": pid, "ingredient_id": "i-1", "raw_text": "虛構素甲"}])


class Intake:
    """TEST_ONLY Fake Agent A：記錄被呼叫的內容。"""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, input_text: str) -> IntakeExtraction:
        content = json.loads(input_text)["content"]
        self.calls.append(content)
        permits = [c for c in (PERMIT, OTHER_PERMIT) if c in content]
        names = [n for n in (CONFLICT_NAME, OBSERVED, OTHER) if n in content]
        if names or permits:
            return extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=names[:1],
                              permit_number_candidates=permits, normalized_text=content)
        if "它" in content:
            return extraction(intent=Intent.INGREDIENT_INQUIRY, refers_to_unspecified_product=True,
                              normalized_text=content)
        return extraction(intent=Intent.UNCLEAR, normalized_text=content)


def build():
    intake = Intake()
    llm = FakeLLM(structured={IntakeExtraction: intake})
    llm.describe_image = lambda **kw: f"TEST_ONLY 圖片文字：{OBSERVED}"
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    clock = Clock()
    contexts, store = TaskContextStore(clock=clock), InMemoryRequestStore()
    svc = AdaptiveResponseService(pipeline, ReporterAgent(llm), store, timeout_seconds=5.0, context_store=contexts)
    line = FakeLine()
    line.contents = {"m-img": (b"TEST_ONLY", "image/jpeg")}
    return LineWebhookHandler(svc, line, contexts), line, contexts, store, clock, intake


def last_record(store):
    return list(store._data.values())[-1]


def last_text(line) -> str:
    return json.dumps(line.sent[-1][2], ensure_ascii=False)


def image_pending(handler, contexts):
    """只傳圖片 → 唯一的低信心候選 → 系統詢問是不是這一項。"""
    rid = send(handler, event(kind="image", message_id="m-img"))[0]
    pending = contexts.snapshot(USER).pending_clarification
    assert pending is not None
    return rid, pending


# ============================================================ dialogue act（一般規則）


@pytest.mark.parametrize("text,act", [
    ("是", DialogueAct.CONFIRM), ("對！", DialogueAct.CONFIRM), ("嗯，是同一個", DialogueAct.CONFIRM),
    ("就是這個啊", DialogueAct.CONFIRM), ("Yes", DialogueAct.CONFIRM), ("沒錯", DialogueAct.CONFIRM),
    ("不是", DialogueAct.REJECT), ("不對喔", DialogueAct.REJECT), ("不是這個", DialogueAct.REJECT),
    ("no", DialogueAct.REJECT),
    ("是不是", DialogueAct.OTHER), ("對不對", DialogueAct.OTHER), ("這個", DialogueAct.OTHER),
    ("是，它的成分有哪些", DialogueAct.OTHER), (f"不是，是{OTHER}", DialogueAct.OTHER),
    (f"不是，字號是{OTHER_PERMIT}", DialogueAct.NEW_ENTITY),
    (f"是 {PERMIT}", DialogueAct.OTHER),  # 同一字號不是新產品，但含字號就不是單純短回覆 → 正常流程
])
def test_dialogue_act_rules(text, act):
    pending = PendingClarification(
        clarification_type=PendingClarificationType.PRODUCT_CANDIDATE_CONFIRMATION, candidate_product_id="x",
        candidate_product_name=OFFICIAL, permit_number=f"測試健食字第{PERMIT}號", previous_request_id="r",
        unresolved_reason="LOW_MATCH_CONFIDENCE")
    assert classify_dialogue_act(text, pending) == act
    assert classify_dialogue_act(text, None) == DialogueAct.OTHER  # 沒有 pending 時不做判定


# ============================================================ pending → confirm


@pytest.mark.parametrize("reply", ["是", "對", "是同一個", "就是這個"])
def test_pending_then_confirm_resumes_candidate_without_agent_a(reply):
    handler, line, contexts, store, _, intake = build()
    rid, pending = image_pending(handler, contexts)
    first = store.get(rid)
    assert first.evidence.product_match.status == ProductVerificationStatus.PARTIAL_MATCH
    assert pending.clarification_type == PendingClarificationType.PRODUCT_CANDIDATE_CONFIRMATION
    assert pending.candidate_product_id == "pend-1" and pending.previous_request_id == rid
    assert PERMIT in pending.permit_number and OBSERVED in pending.discrepancy and OFFICIAL in pending.discrepancy
    calls = len(intake.calls)

    send(handler, event(text=reply))

    assert len(intake.calls) == calls  # deterministic resume：不重跑 Agent A
    rec = last_record(store)
    match = rec.evidence.product_match
    assert match.best_match.product.product_id == "pend-1"
    assert rec.query_label == first.query_label  # 沿用原圖片的 context
    assert rec.normalized_request.user_confirmed_target.product_id == "pend-1"
    assert rec.response.key_points[0].startswith("你確認是同一產品") and OBSERVED in rec.response.key_points[0]
    ctx = contexts.snapshot(USER)
    assert ctx.pending_clarification is None
    assert ctx.confirmed_product.product_id == "pend-1"
    assert ctx.confirmed_product.confirmation_source == ProductConfirmationSource.USER_CONFIRMED_TARGET
    assert ctx.confirmed_product.discrepancy == pending.discrepancy


def test_confirm_without_pending_is_not_resumed():
    handler, _, contexts, store, _, intake = build()
    send(handler, event(text="是"))
    assert len(intake.calls) == 1  # 沒有 pending：照正常流程交給 Agent A
    assert last_record(store).normalized_request.user_confirmed_target is None


# ============================================================ pending → reject


def test_pending_then_reject_clears_candidate_and_asks_for_product():
    handler, line, contexts, store, _, intake = build()
    image_pending(handler, contexts)
    calls, records = len(intake.calls), len(store._data)

    send(handler, event(text="不是"))

    assert REJECTED_CANDIDATE_TEXT in last_text(line)
    assert len(intake.calls) == calls and len(store._data) == records
    ctx = contexts.snapshot(USER)
    assert ctx.pending_clarification is None and ctx.confirmed_product is None
    send(handler, event(text="是"))  # 之後的「是」不會再指回已否定的候選
    assert len(intake.calls) == calls + 1
    assert last_record(store).normalized_request.user_confirmed_target is None


# ============================================================ pending → explicit other product


@pytest.mark.parametrize("reply", [f"不是，我要查{OTHER}", f"不是，字號是{OTHER_PERMIT}"])
def test_pending_then_explicit_other_product_takes_priority(reply):
    handler, _, contexts, store, _, intake = build()
    image_pending(handler, contexts)
    calls = len(intake.calls)

    send(handler, event(text=reply))

    assert len(intake.calls) == calls + 1  # 新產品走正常語意流程
    rec = last_record(store)
    assert rec.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert rec.evidence.product_match.best_match.product.product_id == "p-1"
    assert rec.normalized_request.user_confirmed_target is None
    ctx = contexts.snapshot(USER)
    assert ctx.pending_clarification is None
    assert ctx.confirmed_product.product_id == "p-1"


# ============================================================ confirmation does not erase conflict


def test_confirmation_does_not_erase_name_conflict():
    handler, _, contexts, store, _, intake = build()
    rid = send(handler, event(text=f"{CONFLICT_NAME} 字號{PERMIT}"))[0]
    assert store.get(rid).evidence.product_match.status == ProductVerificationStatus.CONFLICT
    pending = contexts.snapshot(USER).pending_clarification
    assert pending.clarification_type == PendingClarificationType.PRODUCT_IDENTITY_CONFLICT
    assert pending.unresolved_reason == "CONFLICT" and CONFLICT_NAME in pending.discrepancy

    send(handler, event(text="是同一個"))

    rec = last_record(store)
    match = rec.evidence.product_match
    assert match.status == ProductVerificationStatus.CONFLICT  # 使用者確認 ≠ 官方 VERIFIED
    assert match.best_match.product.product_id == "pend-1"
    note = rec.response.key_points[0]
    assert "你確認是同一產品" in note and "不一致" in note and "不是官方比對結果" in note
    ctx = contexts.snapshot(USER)
    assert ctx.pending_clarification is None  # 不會因同一衝突再次詢問而無限循環
    assert ctx.confirmed_product.confirmation_source == ProductConfirmationSource.USER_CONFIRMED_TARGET
    assert "不一致" in ctx.confirmed_product.discrepancy

    calls = len(intake.calls)
    send(handler, event(text="它有哪些成分？"))  # 沿用使用者確認的產品：差異說明仍保留
    assert len(intake.calls) == calls + 1
    follow = last_record(store)
    assert follow.normalized_request.user_confirmed_target.discrepancy == ctx.confirmed_product.discrepancy
    assert "不一致" in follow.response.key_points[0]


# ============================================================ start / end / TTL clear pending


@pytest.mark.parametrize("how", ["new", "end", "pending_ttl", "task_ttl"])
def test_start_end_ttl_clear_pending(how):
    handler, _, contexts, store, clock, intake = build()
    image_pending(handler, contexts)
    if how == "new":
        send(handler, event(kind="postback", data=session_postback_data(SESSION_NEW)))
    elif how == "end":
        send(handler, event(kind="postback", data=session_postback_data(SESSION_END)))
    elif how == "pending_ttl":  # pending 過期（10 分鐘），task context 仍有效
        clock.now += timedelta(minutes=11)
    else:  # task context 過期（30 分鐘）
        clock.now += timedelta(minutes=31)
    assert contexts.snapshot(USER).pending_clarification is None
    calls = len(intake.calls)

    send(handler, event(text="是"))

    assert len(intake.calls) == calls + 1  # 不再 resume 舊候選，交給正常流程
    rec = last_record(store)
    assert rec.normalized_request.user_confirmed_target is None
    assert rec.normalized_request.intent == Intent.UNCLEAR
