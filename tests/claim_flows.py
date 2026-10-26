"""C2c-2：webhook 去重 / ASR 一次性 的共用行為流程（TEST_ONLY）。

同一組流程由 unit（InMemoryClaimStore、dict backend）與 db（真實 SQL）測試呼叫；
「重啟」= 丟掉 handler / service，用同一個 ClaimStore（資料庫）建立新的。
"""

import threading
from datetime import datetime, timedelta, timezone

from app.response.service import (ASR_EXPIRED_TEXT, ASR_REJECTED_TEXT, EXPIRED_ACTION_TEXT, STATE_UNAVAILABLE_TEXT,
                                  AdaptiveResponseService)
from app.schemas.enums import ResponseAction
from tests.unit.test_interaction_reporter import texts_of
from tests.unit.test_line_webhook import PRODUCT_1, build, event, send
from tests.unit.test_overview_and_asr_buttons import HEARD, asr_chat, research


def new_handler(claims):
    handler, line, *_ = build()
    handler._claims = claims
    return handler, line


def webhook_duplicate_survives_restart(claims):
    h1, line1 = new_handler(claims)
    send(h1, event(text=PRODUCT_1, event_id="evt-restart"))
    assert len(line1.sent) == 1
    h2, line2 = new_handler(claims)  # 重啟：全新 handler，記憶體中沒有任何 seen 集合
    assert send(h2, event(text=PRODUCT_1, event_id="evt-restart")) == [None]
    assert line2.sent == []
    send(h2, event(text=PRODUCT_1, event_id="evt-other"))  # 不同 event 不受影響
    assert len(line2.sent) == 1


def webhook_outage_is_not_a_duplicate(claims, backend):
    h, line = new_handler(claims)
    backend.broken = True
    try:
        send(h, event(text=PRODUCT_1, event_id="evt-outage"))
    finally:
        backend.broken = False
    assert len(line.sent) == 1  # fail-open：照常處理，不被當成重複
    send(h, event(text=PRODUCT_1, event_id="evt-outage"))  # 故障時沒有 claim 成功，所以恢復後這次才算第一次
    assert len(line.sent) == 2


def _service_with(chat, claims):
    chat.svc = AdaptiveResponseService(chat.pipeline, chat.svc._reporter, chat.store, timeout_seconds=10.0,
                                       context_store=chat.contexts, claim_store=claims)


def asr_ready(claims):
    chat = asr_chat()
    _service_with(chat, claims)
    flex, rec = chat.speak()
    cid = next(s for s in research(rec).subjects if s.text == HEARD).candidates[0].entity_id
    return chat, rec, cid


def asr_select_is_one_use_across_restart(claims):
    chat, rec, cid = asr_ready(claims)
    n = len(chat.store._data)
    assert ASR_EXPIRED_TEXT not in texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid))
    assert len(chat.store._data) == n + 1
    _service_with(chat, claims)  # 重啟
    assert ASR_EXPIRED_TEXT in texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid))
    assert len(chat.store._data) == n + 1  # 沒有第二次查詢


def asr_reject_is_one_use_across_restart(claims):
    chat, rec, cid = asr_ready(claims)
    assert ASR_REJECTED_TEXT in texts_of(chat.press(rec, ResponseAction.REJECT_ASR_ENTITY))
    _service_with(chat, claims)  # 重啟
    assert ASR_EXPIRED_TEXT in texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid))


def invalid_attempts_do_not_consume_claim(claims):
    chat, rec, cid = asr_ready(claims)
    assert EXPIRED_ACTION_TEXT in texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid, owner="U_OTHER"))  # 錯的人
    chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, "NOT_OFFERED_ID")  # 不合法候選
    saved = rec.created_at
    rec.created_at = datetime.now(timezone.utc) - timedelta(minutes=11)  # 過期
    assert ASR_EXPIRED_TEXT in texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid))
    rec.created_at = saved
    n = len(chat.store._data)
    out = chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid)  # 正確的人、有效的候選仍可成功
    assert ASR_EXPIRED_TEXT not in texts_of(out) and len(chat.store._data) == n + 1


def concurrent_valid_taps_have_one_winner(claims, taps=8):
    chat, rec, cid = asr_ready(claims)
    n = len(chat.store._data)
    outs, barrier = [], threading.Barrier(taps)

    def tap():
        barrier.wait()
        outs.append(texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid)))

    ts = [threading.Thread(target=tap) for _ in range(taps)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(ASR_EXPIRED_TEXT in o for o in outs) == taps - 1
    assert len(chat.store._data) == n + 1


def asr_outage_is_state_unavailable_not_expired(claims, backend):
    chat, rec, cid = asr_ready(claims)
    backend.broken = True
    try:
        out = texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid))
        rej = texts_of(chat.press(rec, ResponseAction.REJECT_ASR_ENTITY))
    finally:
        backend.broken = False
    assert STATE_UNAVAILABLE_TEXT in out and ASR_EXPIRED_TEXT not in out and STATE_UNAVAILABLE_TEXT in rej
    assert ASR_EXPIRED_TEXT not in texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid))  # 沒被消耗


ALL_FLOWS = [webhook_duplicate_survives_restart, asr_select_is_one_use_across_restart, asr_reject_is_one_use_across_restart,
             invalid_attempts_do_not_consume_claim, concurrent_valid_taps_have_one_winner]
OUTAGE_FLOWS = [webhook_outage_is_not_a_duplicate, asr_outage_is_state_unavailable_not_expired]
