"""C2d：TaskContext 持久化的共用行為流程（TEST_ONLY）。

同一組流程由 unit（memory backend、dict-backed Supabase backend）與 db（真實 SQL）測試呼叫。
「重啟」= 丟掉 TaskContextStore / service / handler，用同一個 backend 建立新的。
"""

import threading
from datetime import datetime, timedelta, timezone

from app.line.handler import SESSION_END, SESSION_NEW, LineWebhookHandler, session_postback_data
from app.response.service import STATE_UNAVAILABLE_TEXT
from app.schemas.enums import EntityType, Intent, PendingClarificationType, ProductConfirmationSource
from app.schemas.user_request import ConfirmedProduct, ContextEntity, PendingClarification
from app.services.context_store import TaskContextStore
from app.state.errors import StateStoreError, StateUnavailableError
from app.state.owner_key import OwnerKeyHasher
from tests.unit.test_context_binding_fonts import Clock
from tests.unit.test_line_webhook import USER, event, send
from tests.unit.test_pending_clarification import OFFICIAL, OBSERVED, build, image_pending, last_record, last_text

HASH = OwnerKeyHasher("TEST_ONLY_secret_0123456789")
T0 = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
PRODUCT = ContextEntity(entity_type=EntityType.PRODUCT, name="虛構牌晶亮測試膠囊", entity_id="p-1")


def make_store(backend, clock, owner_key=None, **kw):
    return TaskContextStore(clock=clock, backend=backend, owner_key=owner_key, **kw)


def confirmed(request_id="r0"):
    return ConfirmedProduct(product_id="p-1", official_product_name=PRODUCT.name,
                            confirmation_source=ProductConfirmationSource.USER_SELECTED, confirmed_at=T0,
                            confirmed_request_id=request_id)


def pending_for(request_id="r0"):
    return PendingClarification(clarification_type=PendingClarificationType.PRODUCT_CANDIDATE_CONFIRMATION,
                                candidate_product_id="p-1", candidate_product_name=PRODUCT.name,
                                previous_request_id=request_id, unresolved_reason="LOW_MATCH_CONFIDENCE")


class Env:
    """一個共用 backend + 可調時鐘；restart() 回傳全新的 store（同一個 backend）。"""

    def __init__(self, backend, owner_key=None, **kw):
        self.backend, self.owner_key, self.kw = backend, owner_key, kw
        self.clock = Clock()
        self.clock.now = T0
        self.store = self.restart()

    def restart(self):
        self.store = make_store(self.backend, self.clock, self.owner_key, **self.kw)
        return self.store


def _record(store, session_id, rid="r1", **kw):
    return store.record(USER, session_id, rid, "TEST_ONLY 問題", Intent.PRODUCT_VERIFICATION, [PRODUCT], **kw)


# ---------------------------------------------------------------- store 層：重啟 / TTL / pending

def product_context_survives_restart(env, raw):
    s = env.store.snapshot(USER)
    assert _record(env.store, s.session_id, confirmed=confirmed(), visual_request_id="img-1", pending=pending_for())
    after = env.restart().snapshot(USER)
    assert after.session_id == s.session_id and after.user_id == USER
    assert after.confirmed_product.product_id == "p-1" and after.current_product_name == PRODUCT.name
    assert after.last_intent == Intent.PRODUCT_VERIFICATION and after.last_visual_request_id == "img-1"
    assert [t.user_text for t in after.turns] == ["TEST_ONLY 問題"] and after.recent_request_ids == ["r1"]
    assert after.pending_clarification is not None and after.pending_clarification.created_at == T0
    ctx = after.conversation_context()
    assert ctx.confirmed_product.product_id == "p-1" and ctx.last_visual_request_id == "img-1"
    raw_dump = str(raw())
    assert USER not in raw_dump  # 持久化資料裡沒有原始 LINE user id（memory 以外）


def limits_are_preserved_through_persistence(env, raw):
    s = env.store.snapshot(USER)
    for i in range(8):
        _record(env.store, s.session_id, rid=f"r{i}")
    after = env.restart().snapshot(USER)
    assert after.recent_request_ids == [f"r{i}" for i in range(3, 8)] and len(after.turns) == 5
    long = env.store.record(USER, s.session_id, "rL", "字" * 1000, None, [])
    assert long and len(env.restart().snapshot(USER).turns[-1].user_text) == 300


def pending_ttl_and_context_ttl_survive_restart(env, raw):
    s = env.store.snapshot(USER)
    _record(env.store, s.session_id, pending=pending_for())
    env.clock.now = T0 + timedelta(minutes=9)
    assert env.restart().snapshot(USER).pending_clarification is not None  # 重啟不重置仍有效的 pending
    env.clock.now = T0 + timedelta(minutes=11)
    after = env.restart().snapshot(USER)
    assert after.pending_clarification is None and after.session_id == s.session_id  # pending 過期，context 仍在
    env.clock.now = T0 + timedelta(minutes=31)
    fresh = env.restart().snapshot(USER)  # 30 分鐘後：等同沒有 context
    assert fresh.session_id != s.session_id and fresh.turns == [] and fresh.confirmed_product is None


def activity_extends_the_30_minute_window(env, raw):
    s = env.store.snapshot(USER)
    env.clock.now = T0 + timedelta(minutes=20)
    assert _record(env.store, s.session_id)
    env.clock.now = T0 + timedelta(minutes=45)  # 距離上次寫入 25 分鐘
    assert env.restart().snapshot(USER).session_id == s.session_id
    env.clock.now = T0 + timedelta(minutes=45 + 31)
    assert env.restart().snapshot(USER).session_id != s.session_id


def reset_and_close_survive_restart(env, raw):
    a = env.store.snapshot(USER)
    _record(env.store, a.session_id, pending=pending_for(), confirmed=confirmed())
    b = env.store.reset(USER)
    after = env.restart().snapshot(USER)
    assert after.session_id == b.session_id != a.session_id
    assert after.pending_clarification is None and after.confirmed_product is None and after.turns == []
    _record(env.store, b.session_id)
    env.store.close(USER)
    c = env.restart().snapshot(USER)
    assert c.session_id not in (a.session_id, b.session_id) and c.turns == [] and c.last_intent is None


# ---------------------------------------------------------------- stale session / CAS

def stale_session_cannot_overwrite_newer_session(env, raw):
    a = env.store.snapshot(USER)
    b = env.store.reset(USER)
    assert _record(env.store, a.session_id, rid="stale", confirmed=confirmed()) is False
    env.store.clear_pending(USER, a.session_id)
    after = env.restart().snapshot(USER)
    assert after.session_id == b.session_id and after.turns == [] and after.confirmed_product is None
    assert _record(env.restart(), b.session_id, rid="fresh") is True


def concurrent_writers_do_not_lose_updates(env, raw):
    s = env.store.snapshot(USER)
    writers, results, barrier = 4, [], threading.Barrier(4)

    def go(i):
        barrier.wait()
        results.append(_record(env.store, s.session_id, rid=f"w{i}"))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(writers)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(results)
    assert sorted(env.restart().snapshot(USER).recent_request_ids) == [f"w{i}" for i in range(writers)]


class _Interleave:
    """包住真實 backend：第一次 compare_and_set 之前先插入一次「別的寫入者」的 hook（模擬版本衝突）。"""

    def __init__(self, inner):
        self._inner, self.hook = inner, None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def compare_and_set(self, *a, **kw):
        if self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        return self._inner.compare_and_set(*a, **kw)


def conflict_reloads_and_merges_without_overwriting(env, raw):
    wrapped = _Interleave(env.backend)
    store = make_store(wrapped, env.clock, env.owner_key)
    other = make_store(env.backend, env.clock, env.owner_key)
    s = store.snapshot(USER)
    wrapped.hook = lambda: _record(other, s.session_id, rid="other")
    assert _record(store, s.session_id, rid="mine") is True
    assert env.restart().snapshot(USER).recent_request_ids == ["other", "mine"]  # 兩邊都保留


def conflict_with_reset_drops_the_stale_write(env, raw):
    wrapped = _Interleave(env.backend)
    store = make_store(wrapped, env.clock, env.owner_key)
    other = make_store(env.backend, env.clock, env.owner_key)
    a = store.snapshot(USER)
    holder = {}
    wrapped.hook = lambda: holder.setdefault("b", other.reset(USER))
    assert _record(store, a.session_id, rid="late", confirmed=confirmed()) is False  # 重新驗證 session → 丟棄
    after = env.restart().snapshot(USER)
    assert after.session_id == holder["b"].session_id and after.turns == [] and after.confirmed_product is None


# ---------------------------------------------------------------- 故障（僅有可故障的 backend）

def read_failure_fails_closed_not_new_session(env, raw, flaky):
    s = env.store.snapshot(USER)
    flaky.broken = True
    try:
        try:
            env.restart().snapshot(USER)
            raise AssertionError("snapshot 不應在故障時回傳 context")
        except StateUnavailableError:
            pass
    finally:
        flaky.broken = False
    assert env.restart().snapshot(USER).session_id == s.session_id  # 恢復後原 session 還在，沒有被當成新 session


def write_failure_never_resurrects_stale_context(env, raw, flaky):
    s = env.store.snapshot(USER)
    _record(env.store, s.session_id, rid="r1", confirmed=confirmed(), pending=pending_for())
    flaky.broken = True
    try:
        assert _record(env.store, s.session_id, rid="r2") is False  # 不丟例外；回答照常送出
        try:
            env.store.snapshot(USER)  # 清不掉過時資料 → 仍 fail closed
            raise AssertionError("故障中不應回傳可能過時的 context")
        except StateUnavailableError:
            pass
    finally:
        flaky.broken = False
    after = env.store.snapshot(USER)  # 恢復後：先刪除過時的列，再開新 session（不會有舊的「它」/ pending）
    assert after.session_id != s.session_id and after.confirmed_product is None and after.pending_clarification is None


def reset_and_close_failure_raise_and_do_not_pretend(env, raw, flaky):
    s = env.store.snapshot(USER)
    _record(env.store, s.session_id, pending=pending_for())
    flaky.broken = True
    for op in (env.store.reset, env.store.close):
        try:
            op(USER)
            raise AssertionError("reset / close 在故障時必須丟出例外")
        except StateStoreError:
            pass
    flaky.broken = False
    assert env.store.snapshot(USER).pending_clarification is None  # 舊 pending 不會復活


# ---------------------------------------------------------------- handler / service 層（pending clarification 實例）

def _rebind(handler, contexts):
    handler._contexts = contexts
    handler._service._contexts = contexts


def _swap_in(handler, env):
    _rebind(handler, env.restart())
    return env.store


def pending_confirm_and_follow_up_work_after_restart(env, raw):
    handler, line, _, store, clock, intake = build()
    env.clock = clock
    _swap_in(handler, env)
    rid, pending = image_pending(handler, env.store)
    _swap_in(handler, env)  # 重啟：全新 store，pending 仍在
    assert env.store.snapshot(USER).pending_clarification is not None
    calls = len(intake.calls)
    send(handler, event(text="是同一個"))
    assert len(intake.calls) == calls  # 仍是 deterministic resume，不重跑 Agent A
    assert last_record(store).normalized_request.user_confirmed_target.product_id == "pend-1"
    _swap_in(handler, env)  # 再重啟一次：「它…」仍能解析到已確認的產品
    send(handler, event(text="它有哪些成分？"))
    follow = last_record(store)
    assert follow.normalized_request.user_confirmed_target.product_id == "pend-1"
    assert follow.evidence.product_match.best_match.product.product_id == "pend-1"


def expired_pending_does_not_resume_after_restart(env, raw):
    handler, line, _, store, clock, intake = build()
    env.clock = clock
    _swap_in(handler, env)
    image_pending(handler, env.store)
    clock.now += timedelta(minutes=11)
    _swap_in(handler, env)
    calls = len(intake.calls)
    send(handler, event(text="是"))
    assert len(intake.calls) == calls + 1 and last_record(store).normalized_request.user_confirmed_target is None


def session_postbacks_after_restart(env, raw):
    for how in (SESSION_NEW, SESSION_END):
        handler, line, _, store, clock, intake = build()
        env.clock = clock
        _swap_in(handler, env)
        image_pending(handler, env.store)
        _swap_in(handler, env)
        send(handler, event(kind="postback", data=session_postback_data(how)))
        _swap_in(handler, env)
        assert env.store.snapshot(USER).pending_clarification is None
        calls = len(intake.calls)
        send(handler, event(text="是"))
        assert len(intake.calls) == calls + 1  # 不會 resume 舊候選


def outage_returns_neutral_notice_for_any_message(env, raw, flaky):
    handler, line, _, store, clock, intake = build()
    env.clock = clock
    _swap_in(handler, env)
    image_pending(handler, env.store)
    calls, saved = len(intake.calls), len(store._data)
    flaky.broken = True
    try:
        send(handler, event(text="是"))  # context 讀不到：不 resume、不重跑 Agent A、不憑空決定產品
        assert STATE_UNAVAILABLE_TEXT in last_text(line)
        send(handler, event(kind="postback", data=session_postback_data(SESSION_NEW)))
        assert STATE_UNAVAILABLE_TEXT in last_text(line)  # 沒有假裝已重設
    finally:
        flaky.broken = False
    assert len(intake.calls) == calls and len(store._data) == saved
    assert env.store.snapshot(USER).pending_clarification is None  # 重設失敗後被標記過時，恢復時清掉


ALL_FLOWS = [product_context_survives_restart, limits_are_preserved_through_persistence,
             pending_ttl_and_context_ttl_survive_restart, activity_extends_the_30_minute_window,
             reset_and_close_survive_restart, stale_session_cannot_overwrite_newer_session,
             concurrent_writers_do_not_lose_updates, conflict_reloads_and_merges_without_overwriting,
             conflict_with_reset_drops_the_stale_write, pending_confirm_and_follow_up_work_after_restart,
             expired_pending_does_not_resume_after_restart, session_postbacks_after_restart]
OUTAGE_FLOWS = [read_failure_fails_closed_not_new_session, write_failure_never_resurrects_stale_context,
                reset_and_close_failure_raise_and_do_not_pretend, outage_returns_neutral_notice_for_any_message]
