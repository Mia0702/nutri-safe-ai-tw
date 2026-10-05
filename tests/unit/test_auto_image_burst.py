"""Automatic Image Burst Gate：使用者直接從 LINE 相簿一次選 2–3 張照片（不需要先按「多照片辨識」）。

* image webhook 先進 gate（debounce window），finalize 後才建立「一個」一般分析 job：1 張 = 既有單張流程；2–3 張 = bundle 一次。
* 不在 per-user FIFO queue 裡等待；舊 timer 不能 finalize 已被新圖片延長的 burst。
* 明確蒐集模式優先；分組不代表同一產品（既有 fusion / identity 規則照舊）。
Timer 以 FakeTimer 手動觸發（不 sleep）；另有一個真實 timer 的小型併發測試。
"""

import logging
import time

from app.line.handler import AUTO_BURST_FULL_TEXT, MULTI_START_COMMAND, session_postback_data
from app.schemas.image_bundle import ImageBundleStatus, ImageKind
from tests.unit.test_image_bundle_fusion import FRONT, OTHER_PERMIT, PERMIT, PRODUCT, n, obs, t
from tests.unit.test_line_webhook import event
from tests.unit.test_multi_image_line import Harness

USER_B = "U_TEST_ONLY_B"


def img(h, mid, user=None):
    kwargs = {"user": user} if user else {}
    return h.handler.dispatch(event(kind="image", message_id=mid, **kwargs))


def settle(h):
    assert h.handler.queue.wait_idle(10)


def fire_last(h):
    h.timers[-1].fire()
    settle(h)


# ============================================================ 1. 一張：window 後單張流程一次


def test_one_image_waits_for_window_then_runs_single_pipeline_once():
    h = Harness(burst_ms=1800)
    img(h, "i1")
    settle(h)
    assert h.line.sent == [] and h.llm.calls == [] and h.single_vision == []  # window 內沒有任何處理
    assert h.timers[-1].interval == 1.8 and h.handler.bursts.pending_count("U_TEST_ONLY_1") == 1
    fire_last(h)
    assert len(h.single_vision) == 1 and h.vision_bundle_calls == 0 and h.controller_calls == 1 and h.verify_calls == 1
    assert len(h.line.sent) == 1 and PRODUCT in h.text()
    assert h.quick_labels() == ["開始新問題", "結束本次查證"]  # 結果卡 / Quick Reply 與原本相同
    [record] = h.store._data.values()
    assert record.normalized_request.image_bundle is None
    assert [r.tool_name for r in record.normalized_request.input_tools_called] == ["analyze_image"]
    assert record.query_label.startswith("圖片辨識｜")


# ============================================================ 2–4. 兩張 / 三張連續：bundle 一次、沒有中間回覆


def test_two_rapid_images_use_bundle_once_with_no_intermediate_response():
    h = Harness(burst_ms=1800)
    img(h, "i1")
    img(h, "i2")
    settle(h)
    assert h.line.sent == [] and h.llm.calls == []  # 沒有逐張回條、沒有「需要更多資訊」、沒有任何分析
    fire_last(h)
    assert h.vision_bundle_calls == 1 and h.single_vision == [] and h.controller_calls == 1 and h.verify_calls == 1
    assert len(h.line.sent) == 1 and PRODUCT in h.text() and "找不到" not in h.text()
    [record] = h.store._data.values()
    bundle = record.normalized_request.image_bundle
    assert bundle.image_count == 2 and bundle.image_source_ids == {1: "i1", 2: "i2"} and bundle.status == ImageBundleStatus.FUSED


def test_three_rapid_images_use_bundle_once():
    h = Harness(burst_ms=1800)
    for mid in ("i1", "i2", "i3"):
        img(h, mid)
    fire_last(h)
    assert h.vision_bundle_calls == 1 and h.llm.calls[0][1] == ["image/jpeg"] * 3 and h.single_vision == []
    assert h.controller_calls == 1 and h.verify_calls == 1 and len(h.line.sent) == 1
    assert next(iter(h.store._data.values())).normalized_request.image_bundle.image_count == 3


# ============================================================ 5–6. debounce 延長 / 舊 timer 不能 finalize


def test_each_new_image_extends_the_window_and_cancels_the_old_timer():
    h = Harness(burst_ms=1800)
    img(h, "i1")
    img(h, "i2")
    assert len(h.timers) == 2 and h.timers[0].cancelled and not h.timers[1].cancelled
    assert h.timers[1].args[2] == h.timers[0].args[2] + 1 and h.timers[0].args[1] == h.timers[1].args[1]  # 同一 burst、generation +1


def test_stale_timer_cannot_finalize_a_burst_extended_by_a_newer_image():
    h = Harness(burst_ms=1800)
    img(h, "i1")
    img(h, "i2")
    h.timers[0].fire(force=True)  # cancel 來不及的競態：舊 timer 仍然觸發
    settle(h)
    assert h.line.sent == [] and h.llm.calls == [] and h.handler.bursts.pending_count("U_TEST_ONLY_1") == 2
    h.timers[1].fire()
    settle(h)
    assert h.vision_bundle_calls == 1 and h.single_vision == [] and len(h.line.sent) == 1  # 不會先單張處理第 1 張
    h.timers[1].fire(force=True)  # finalize 之後重複觸發也不會再跑一次
    settle(h)
    assert h.vision_bundle_calls == 1 and len(h.line.sent) == 1


# ============================================================ 7. 第 4 張：沿用上限政策


def test_fourth_image_in_the_same_burst_is_rejected_with_a_light_warning():
    h = Harness(burst_ms=1800)
    h.line.contents["i4"] = (b"i4", "image/jpeg")
    for mid in ("i1", "i2", "i3"):
        img(h, mid)
    timers_before = len(h.timers)
    img(h, "i4")
    settle(h)
    assert len(h.timers) == timers_before  # 被拒絕的圖片不延長 window
    assert AUTO_BURST_FULL_TEXT in h.text() and h.handler.bursts.pending_count("U_TEST_ONLY_1") == 3
    assert h.llm.calls == []  # 警告不是分析
    fire_last(h)
    assert h.vision_bundle_calls == 1 and h.llm.calls[0][1] == ["image/jpeg"] * 3  # 只有前三張


# ============================================================ 8. 明確蒐集模式優先


def test_explicit_collection_bypasses_the_auto_gate():
    h = Harness(burst_ms=1800)
    h.start()
    img(h, "i1")
    img(h, "i2")
    settle(h)
    assert h.timers == [] and h.handler.bursts.pending_count("U_TEST_ONLY_1") == 0  # 沒有走 gate
    assert h.collections.active("U_TEST_ONLY_1").image_count == 2 and len(h.line.sent) == 1  # 靜默保存
    assert h.llm.calls == []


def test_explicit_start_still_queued_when_images_arrive_takes_precedence_at_finalize():
    h = Harness(burst_ms=1800)
    h.handler.dispatch(event(text=MULTI_START_COMMAND))
    img(h, "i1")  # 開始指令的 job 可能還沒執行，圖片先進 gate
    settle(h)
    if h.timers:  # 進了 gate：finalize 時（FIFO 之後）蒐集模式已啟用 → 靜默保存，不分析
        fire_last(h)
    assert h.collections.active("U_TEST_ONLY_1").image_count == 1 and h.llm.calls == [] and len(h.line.sent) == 1


# ============================================================ 9. 單張行為不變（除了 window）


def test_single_image_result_is_identical_to_immediate_processing_except_for_delay():
    gated, direct = Harness(burst_ms=1800), Harness(burst_ms=0)
    img(gated, "i1")
    fire_last(gated)
    img(direct, "i1")
    settle(direct)
    import re

    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    assert uuid_re.sub("RID", gated.text()) == uuid_re.sub("RID", direct.text())  # 同樣的卡片（只有 request_id 不同）
    assert gated.llm.calls == direct.llm.calls and gated.single_vision == direct.single_vision


# ============================================================ 10. 不同產品：fusion 規則照舊


def test_grouping_does_not_weaken_identity_safety():
    conflict = {"c1": obs(names=[n(PRODUCT)], permits=[t(PERMIT)]), "c2": obs(kind=ImageKind.PERMIT_AREA, permits=[t(OTHER_PERMIT)])}
    h = Harness(by_image=conflict, burst_ms=1800)
    img(h, "c1")
    img(h, "c2")
    fire_last(h)
    assert "不同的許可證字號" in h.text() and h.controller_calls == 0 and h.vision_bundle_calls == 1
    [record] = h.store._data.values()
    assert record.normalized_request.image_bundle.status == ImageBundleStatus.IDENTITY_CONFLICT
    different = {"d1": obs(names=[n(PRODUCT)]), "d2": obs(names=[n("另一虛構保健測試錠")])}
    h2 = Harness(by_image=different, burst_ms=1800)
    img(h2, "d1")
    img(h2, "d2")
    fire_last(h2)
    [record2] = h2.store._data.values()
    assert record2.normalized_request.image_bundle.status == ImageBundleStatus.SAME_PRODUCT_UNCERTAIN
    assert h2.verify_calls == 1 and record2.evidence.product_match.best_match is None  # 沒有任選一個


# ============================================================ 11. 新問題 / 結束清除 pending burst；其他事件先 flush


def test_new_question_and_end_discard_pending_burst():
    for kind in ("new", "end"):
        h = Harness(burst_ms=1800)
        img(h, "i1")
        img(h, "i2")
        h.handler.dispatch(event(kind="postback", data=session_postback_data(kind)))
        settle(h)
        assert h.handler.bursts.pending_count("U_TEST_ONLY_1") == 0 and all(t.cancelled for t in h.timers)
        h.timers[-1].fire(force=True)  # 舊 timer 之後觸發也不會分析
        settle(h)
        assert h.llm.calls == [] and h.vision_bundle_calls == 0 and len(h.line.sent) == 1  # 只有新問題 / 結束的回覆


def test_other_event_flushes_pending_burst_first_so_replies_stay_in_order():
    h = Harness(burst_ms=1800)
    img(h, "i1")
    img(h, "i2")
    h.handler.dispatch(event(text=PRODUCT))  # 圖片後面緊接著的文字：圖片這組先 finalize
    settle(h)
    assert len(h.line.sent) == 2 and h.vision_bundle_calls == 1
    assert h.verify_calls == 2  # 圖片組一次 + 文字一次
    first = h.text(0)
    assert PRODUCT in first  # 第一則是圖片這組的結果（先送出），第二則是文字


# ============================================================ 12. 不同使用者各自獨立


def test_different_users_have_independent_bursts():
    h = Harness(burst_ms=1800)
    img(h, "i1")
    img(h, "i2")
    img(h, "i3", user=USER_B)
    assert len(h.timers) == 3
    h.timers[1].fire()  # A 的 window 到期：B 不受影響
    settle(h)
    assert h.vision_bundle_calls == 1 and h.single_vision == [] and h.handler.bursts.pending_count(USER_B) == 1
    h.timers[2].fire()
    settle(h)
    assert len(h.single_vision) == 1 and len(h.line.sent) == 2
    targets = {s[1] for s in h.line.sent}
    assert len(targets) == 2


# ============================================================ 13. 既有 FIFO：分析順序正確


def test_two_bursts_are_analyzed_in_order():
    h = Harness(burst_ms=1800)
    img(h, "i1")
    img(h, "i2")
    h.timers[-1].fire()
    img(h, "i3")  # 第一組已 finalize（job 排隊 / 執行中）；這張是新的一組
    settle(h)
    h.timers[-1].fire()
    settle(h)
    assert h.vision_bundle_calls == 1 and len(h.single_vision) == 1 and len(h.line.sent) == 2


# ============================================================ logging / 設定 / 真實 timer


def test_burst_logging_has_started_extended_finalized_with_ids_and_counts(caplog):
    h = Harness(burst_ms=1800)
    with caplog.at_level(logging.INFO):
        img(h, "i1")
        img(h, "i2")
        fire_last(h)
    stages = {r.stage: r for r in caplog.records if getattr(r, "stage", "").startswith("auto_image_burst")}
    assert set(stages) >= {"auto_image_burst_started", "auto_image_burst_extended", "auto_image_burst_finalized"}
    started, extended, final = (stages[k] for k in ("auto_image_burst_started", "auto_image_burst_extended",
                                                    "auto_image_burst_finalized"))
    assert started.burst_id == extended.burst_id == final.burst_id
    assert (started.image_count, extended.image_count, final.image_count) == (1, 2, 2)
    assert final.collection_mode == "AUTO" and isinstance(final.burst_wait_ms, int)
    from app.observability import LOG_FIELDS

    assert {"burst_id", "collection_mode", "burst_wait_ms"} <= set(LOG_FIELDS)


def test_gate_is_disabled_by_default_and_configurable_in_settings():
    from app.config.settings import Settings

    assert Harness().handler.bursts.enabled is False
    assert Settings().auto_image_burst_window_ms == 1800


def test_real_timer_groups_two_rapid_images_into_one_bundle():
    h = Harness(burst_ms=150, real_timers=True)
    img(h, "i1")
    img(h, "i2")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not h.line.sent:
        time.sleep(0.02)
    settle(h)
    assert len(h.line.sent) == 1 and h.vision_bundle_calls == 1 and h.single_vision == [] and h.controller_calls == 1
