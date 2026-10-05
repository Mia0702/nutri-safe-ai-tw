"""Multi-image Phase 2：LINE 明確蒐集模式（多照片辨識 → 蒐集 2–3 張 → 開始分析 → 一次 bundle pipeline）。

LINE 選多張照片會送出多個獨立的 image event；不依時間推測分組，也不在蒐集期間對任何一張做分析。
全部使用虛構的 Vision 觀察與 TEST_ONLY 產品，不呼叫外部服務。
"""

import threading
import time
from urllib.parse import parse_qs

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.line.handler import (
    MULTI_ANALYZE_LABEL,
    MULTI_CANCEL_LABEL,
    MULTI_FULL_TEXT,
    MULTI_NEED_MORE_TEXT,
    MULTI_NO_SESSION_TEXT,
    MULTI_START_LABEL,
    MULTI_START_COMMAND,
    LineWebhookHandler,
    parse_multi_postback,
    session_postback_data,
)
from app.pipeline import AnalysisPipeline
from app.response.service import AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import Intent, ResponseType
from app.schemas.image_bundle import ImageBundleObservation, ImageBundleStatus, ImageKind
from app.schemas.user_request import IntakeExtraction
from app.services.context_store import TaskContextStore
from app.services.image_collection import ImageCollectionStore
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.unit.test_image_bundle_fusion import (
    BACK,
    COMPANY,
    FRONT,
    OTHER_PERMIT,
    PERMIT,
    PERMIT_IMG,
    PRODUCT,
    n,
    obs,
    t,
)
from tests.unit.test_line_webhook import USER, FakeLine, buttons, event, send

IMAGES = {"i1": FRONT, "i2": BACK, "i3": PERMIT_IMG}


class SlowLine(FakeLine):
    """第一張圖片下載很慢：若 queue 不保證順序，開始分析會先於它執行。"""

    def get_content(self, message_id):
        if message_id == "i1":
            time.sleep(0.15)
        return super().get_content(message_id)


class FakeTimer:
    """可手動觸發的 timer（不真的等待）；force=True 模擬「cancel 來不及」的競態。"""

    def __init__(self, registry, interval, fn, args):
        self.interval, self.fn, self.args, self.cancelled, self.started = interval, fn, args, False, False
        registry.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self, force=False):
        if force or not self.cancelled:
            self.fn(*self.args)


class Harness:
    def __init__(self, by_image=None, intake=None, ttl=600.0, line=None, burst_ms=0, real_timers=False):
        self.timers: list[FakeTimer] = []
        self.now = [0.0]
        self.llm = FakeLLM()
        self.single_vision = []
        self.llm.describe_image = lambda **kw: self.single_vision.append(1) or f"TEST_ONLY 圖片文字：{PRODUCT}"
        by_image = by_image or IMAGES
        self.llm.structured[ImageBundleObservation] = lambda images: ImageBundleObservation(images=[
            by_image[c.decode()].model_copy(update={"image_index": i}) for i, (c, _) in enumerate(images, start=1)])
        self.llm.structured[IntakeExtraction] = intake or extraction(intent=Intent.PRODUCT_VERIFICATION,
                                                                     product_candidates=[PRODUCT])
        self.verifier = VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), self.llm)
        self.verify_calls = 0
        real_verify = self.verifier.verify

        def counted(request):
            self.verify_calls += 1
            return real_verify(request)

        self.verifier.verify = counted
        pipeline = AnalysisPipeline(intake=IntakeAgent(self.llm), verifier=self.verifier, risk_engine=RiskEngine(),
                                    reporter=ReporterAgent(self.llm))
        self.contexts, self.store = TaskContextStore(), InMemoryRequestStore()
        service = AdaptiveResponseService(pipeline, ReporterAgent(self.llm), self.store, timeout_seconds=5.0,
                                          context_store=self.contexts)
        self.line = line or FakeLine()
        self.line.contents.update({k.encode().decode(): (k.encode(), "image/jpeg") for k in by_image})
        self.collections = ImageCollectionStore(ttl_seconds=ttl, clock=lambda: self.now[0])
        factory = (lambda interval, fn, args: FakeTimer(self.timers, interval, fn, args)) if burst_ms and not real_timers else None
        self.handler = LineWebhookHandler(service, self.line, self.contexts, collections=self.collections,
                                          image_burst_window_ms=burst_ms, burst_timer_factory=factory)

    # ---- 計數
    def count(self, name):
        return sum(1 for c in self.llm.calls if c[0] == name)

    @property
    def vision_bundle_calls(self):
        return self.count("describe_images_structured")

    @property
    def controller_calls(self):
        return sum(1 for c in self.llm.calls if c[0] == "parse_structured" and c[1] == "IntakeExtraction")

    # ---- 動作
    def start(self):
        send(self.handler, event(text=MULTI_START_COMMAND))
        return self.session_id()

    def image(self, mid):
        return event(kind="image", message_id=mid)

    def session_id(self):
        return self.collections.active(USER).session_id

    def post(self, action, sid=None):
        return event(kind="postback", data=f"multi={action}&sid={sid or self.session_id()}")

    def message(self, i=-1):
        return self.line.sent[i][2]

    def text(self, i=-1):
        import json

        return json.dumps(self.message(i), ensure_ascii=False)

    def card_labels(self, i=-1):
        return [b["label"] for b in buttons(self.message(i))]

    def quick_labels(self, i=-1):
        return [x["action"]["label"] for x in self.message(i).get("quickReply", {}).get("items", [])]


# ============================================================ 1–3. 開始蒐集 / 蒐集中只保存


def test_start_collection_prompts_and_sends_no_pipeline():
    h = Harness()
    h.start()
    assert len(h.line.sent) == 1 and "請傳同一產品的 1–3 張照片" in h.text() and "照片傳完後按『開始分析』" in h.text()
    assert h.card_labels() == [MULTI_ANALYZE_LABEL, MULTI_CANCEL_LABEL]  # 按鈕在卡片內（傳照片後不會消失）
    assert h.llm.calls == [] and h.collections.active(USER).image_count == 0


def test_images_are_stored_silently_while_collecting_without_any_analysis():
    h = Harness()
    h.start()
    send(h.handler, h.image("i1"), h.image("i2"), h.image("i3"))
    assert len(h.line.sent) == 1  # 只有進入蒐集模式時的那一則控制訊息；沒有任何「已收到 n/3 張」回條
    assert h.collections.active(USER).image_count == 3
    # 蒐集期間：沒有任何 Vision / Controller / Researcher / 產品結論
    assert h.llm.calls == [] and h.single_vision == [] and h.verify_calls == 0 and len(h.store._data) == 0


# ============================================================ 4. 開始分析 → 一次 bundle pipeline


def test_start_analysis_runs_one_bundle_pipeline_and_clears_session():
    h = Harness()
    h.start()
    send(h.handler, h.image("i1"), h.image("i2"))
    send(h.handler, h.post("analyze"))
    assert h.vision_bundle_calls == 1 and h.controller_calls == 1 and h.verify_calls == 1
    assert h.single_vision == [] and len(h.line.sent) == 2  # 控制訊息 + 一則最終結果（蒐集期間靜默）
    final = h.message()
    assert final["type"] == "flex" and PRODUCT in h.text()
    assert h.collections.active(USER) is None
    [record] = h.store._data.values()
    bundle = record.normalized_request.image_bundle
    assert bundle.image_count == 2 and bundle.image_source_ids == {1: "i1", 2: "i2"}
    assert bundle.metrics.vision_bundle_ms is not None and bundle.metrics.semantic_ms is not None
    assert bundle.metrics.total_ms is not None and bundle.status == ImageBundleStatus.FUSED
    assert [m.source_id for m in record.raw_input.media] == ["i1", "i2"]


def test_second_press_of_start_analysis_has_no_session_and_runs_nothing():
    h = Harness()
    sid = h.start()
    send(h.handler, h.image("i1"), h.image("i2"))
    send(h.handler, h.post("analyze", sid))
    send(h.handler, h.post("analyze", sid))
    assert MULTI_NO_SESSION_TEXT in h.text() and h.vision_bundle_calls == 1 and h.controller_calls == 1


def test_start_analysis_with_no_image_asks_for_photos_and_keeps_session():
    h = Harness()
    h.start()
    send(h.handler, h.post("analyze"))
    assert MULTI_NEED_MORE_TEXT in h.text() and h.llm.calls == [] and h.card_labels() == [MULTI_ANALYZE_LABEL, MULTI_CANCEL_LABEL]
    assert h.collections.active(USER).image_count == 0


# ============================================================ 5–6. 3 張可以、第 4 張拒絕


def test_three_images_supported_and_fourth_is_rejected():
    h = Harness()
    h.line.contents["i4"] = (b"i4", "image/jpeg")
    h.start()
    send(h.handler, h.image("i1"), h.image("i2"), h.image("i3"))
    assert len(h.line.sent) == 1
    send(h.handler, h.image("i4"))
    assert MULTI_FULL_TEXT in h.text() and h.collections.active(USER).image_count == 3
    assert h.card_labels() == [MULTI_ANALYZE_LABEL, MULTI_CANCEL_LABEL] and h.llm.calls == []
    send(h.handler, h.post("analyze"))
    assert h.vision_bundle_calls == 1 and h.llm.calls[0][1] == ["image/jpeg"] * 3  # 只有前三張


def test_non_image_content_download_failure_is_not_stored():
    h = Harness()
    h.start()
    send(h.handler, event(kind="image", message_id="missing"))
    assert "沒有下載成功" in h.text() and h.card_labels() == [MULTI_ANALYZE_LABEL, MULTI_CANCEL_LABEL]
    assert h.collections.active(USER).image_count == 0
    h.line.contents["bad"] = (b"x", "application/pdf")
    send(h.handler, h.image("bad"))
    assert h.collections.active(USER).image_count == 0 and h.llm.calls == []


# ============================================================ 7–8. 取消 / TTL


def test_cancel_clears_session_and_later_images_are_single_image_requests():
    h = Harness()
    sid = h.start()
    send(h.handler, h.image("i1"), h.image("i2"))
    send(h.handler, h.post("cancel", sid))
    assert "已取消" in h.text() and h.collections.active(USER) is None
    send(h.handler, h.post("analyze", sid))
    assert MULTI_NO_SESSION_TEXT in h.text() and h.vision_bundle_calls == 0
    send(h.handler, h.image("i1"))  # 不在蒐集模式：既有單張流程
    assert len(h.single_vision) == 1 and h.vision_bundle_calls == 0


def test_ttl_expiry_clears_session():
    h = Harness(ttl=100.0)
    sid = h.start()
    send(h.handler, h.image("i1"), h.image("i2"))
    h.now[0] = 101.0
    assert h.collections.active(USER) is None
    send(h.handler, h.post("analyze", sid))
    assert MULTI_NO_SESSION_TEXT in h.text() and h.vision_bundle_calls == 0 and h.controller_calls == 0
    # 活動會延長 TTL：沒有過期的 session 不受影響
    h2 = Harness(ttl=100.0)
    h2.start()
    send(h2.handler, h2.image("i1"))
    h2.now[0] = 60.0
    send(h2.handler, h2.image("i2"))
    h2.now[0] = 120.0
    assert h2.collections.active(USER).image_count == 2


def test_new_question_and_end_session_clear_collection():
    for kind in ("new", "end"):
        h = Harness()
        h.start()
        send(h.handler, h.image("i1"))
        send(h.handler, event(kind="postback", data=session_postback_data(kind)))
        assert h.collections.active(USER) is None


# ============================================================ 9. FIFO：快速連送 image1 / image2 / 開始分析


def test_rapid_images_then_start_are_processed_in_fifo_order():
    h = Harness(line=SlowLine())
    sid = h.start()
    # 三個事件連續 dispatch、不等待：i1 下載最慢，開始分析仍必須在兩張都存好之後才執行
    rids = [h.handler.dispatch(e) for e in (h.image("i1"), h.image("i2"), h.post("analyze", sid))]
    assert h.handler.queue.wait_idle(15) and all(rids)
    assert len(h.line.sent) == 2 and PRODUCT in h.text(1)  # 控制訊息 + 結果；存圖靜默
    assert h.vision_bundle_calls == 1 and h.llm.calls[0][1] == ["image/jpeg"] * 2 and h.controller_calls == 1


# ============================================================ 10. 單張圖片（蒐集模式外）不變


def test_single_image_outside_collection_mode_is_unchanged():
    h = Harness()
    send(h.handler, h.image("i1"))
    assert len(h.single_vision) == 1 and h.vision_bundle_calls == 0 and h.controller_calls == 1
    assert len(h.line.sent) == 1 and PRODUCT in h.text()
    assert h.quick_labels() == ["開始新問題", "結束本次查證"]  # 一般訊息的 Quick Reply 不變


# ============================================================ 11–12. 身份衝突 / 沒有中間結果


def test_identity_conflict_in_collected_images_returns_clarification_only():
    by_image = {"c1": obs(names=[n(PRODUCT)], permits=[t(PERMIT)]),
                "c2": obs(kind=ImageKind.PERMIT_AREA, permits=[t(OTHER_PERMIT)])}
    h = Harness(by_image=by_image)
    h.start()
    send(h.handler, h.image("c1"), h.image("c2"))
    send(h.handler, h.post("analyze"))
    final = h.text()
    assert "不同的許可證字號" in final and "分開傳送" in final
    record = next(iter(h.store._data.values()))
    assert record.response.response_type == ResponseType.MISSING_INFORMATION
    assert record.normalized_request.image_bundle.status == ImageBundleStatus.IDENTITY_CONFLICT
    assert h.controller_calls == 0 and h.vision_bundle_calls == 1  # 衝突時不跑語意理解


def test_real_case_company_only_then_exact_name_gives_one_fused_result_without_intermediate_not_found():
    """真機：第 1 張只讀到公司（單張時是 COMPANY_ONLY / 找不到），第 2 張有完整品名（單張時是 VERIFIED）。"""
    by_image = {"r1": obs(companies=[t(COMPANY)]), "r2": obs(kind=ImageKind.BACK_LABEL, names=[n(PRODUCT)])}
    h = Harness(by_image=by_image)
    h.start()
    send(h.handler, h.image("r1"))
    send(h.handler, h.image("r2"))
    send(h.handler, h.post("analyze"))
    assert len(h.line.sent) == 2  # 控制訊息、一則最終結果；沒有中間的「找不到」
    assert "找不到" not in h.text(0) and PRODUCT not in h.text(0)
    assert PRODUCT in h.text() and "找不到" not in h.text()
    assert h.vision_bundle_calls == 1 and h.single_vision == [] and h.controller_calls == 1 and h.verify_calls == 1
    record = next(iter(h.store._data.values()))
    fused = record.normalized_request.image_bundle
    assert [(v.value, v.observed_in) for v in fused.companies] == [(COMPANY, [1])]
    assert [(v.value, v.observed_in) for v in fused.product_names] == [(PRODUCT, [2])]


# ============================================================ 其他邊界


def test_other_users_cannot_use_or_cancel_my_session():
    h = Harness()
    sid = h.start()
    send(h.handler, h.image("i1"), h.image("i2"))
    send(h.handler, event(kind="postback", data=f"multi=analyze&sid={sid}", user="U_OTHER"))
    assert MULTI_NO_SESSION_TEXT in h.text() and h.vision_bundle_calls == 0
    send(h.handler, event(kind="postback", data=f"multi=cancel&sid={sid}", user="U_OTHER"))
    assert h.collections.active(USER).image_count == 2


def test_stale_session_id_cannot_analyze_a_new_session():
    h = Harness()
    old = h.start()
    new = h.start()  # 重新開始：舊 session 丟棄
    assert old != new
    send(h.handler, h.image("i1"), h.image("i2"))
    send(h.handler, h.post("analyze", old))
    assert MULTI_NO_SESSION_TEXT in h.text() and h.vision_bundle_calls == 0
    assert h.collections.active(USER).image_count == 2


def test_multi_postback_parsing_is_strict():
    assert parse_multi_postback("multi=analyze&sid=abc") == ("analyze", "abc")
    for bad in ("multi=analyze", "multi=drop&sid=a", "multi=analyze&sid=a&x=1", "sid=a", "", "multi=analyze&multi=cancel&sid=a"):
        assert parse_multi_postback(bad) is None
    assert parse_qs("multi=cancel&sid=1")["multi"] == ["cancel"]


def test_concurrent_adds_never_exceed_max_images():
    store = ImageCollectionStore()
    store.start("u")
    from app.schemas.user_request import MediaInput

    threads = [threading.Thread(target=store.add, args=("u", MediaInput(kind="image", content=b"x", mime_type="image/png")))
              for _ in range(10)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert store.active("u").image_count == 3


# ============================================================ UX：按鈕進入 / 1 張走單張 / 2–3 張走 bundle


def new_question_message(h):
    send(h.handler, event(kind="postback", data=session_postback_data("new")))
    return h.message()


def test_button_after_new_question_enters_collection_mode_and_text_command_still_works():
    h = Harness()
    msg = new_question_message(h)
    labels = [x["action"]["label"] for x in msg["quickReply"]["items"]]
    assert labels == ["開始新問題", "結束本次查證", MULTI_START_LABEL]
    entry = next(x["action"]["data"] for x in msg["quickReply"]["items"] if x["action"]["label"] == MULTI_START_LABEL)
    assert h.collections.active(USER) is None
    send(h.handler, event(kind="postback", data=entry))
    assert "請傳同一產品的 1–3 張照片" in h.text() and h.collections.active(USER) is not None
    assert h.card_labels() == [MULTI_ANALYZE_LABEL, MULTI_CANCEL_LABEL] and h.llm.calls == []
    h.collections.clear(USER)
    send(h.handler, event(text=MULTI_START_COMMAND))  # 舊的文字指令仍可用（相容捷徑）
    assert h.collections.active(USER) is not None


def test_entry_button_is_not_on_ordinary_result_cards():
    h = Harness()
    send(h.handler, h.image("i1"))
    assert MULTI_START_LABEL not in h.quick_labels() and h.quick_labels() == ["開始新問題", "結束本次查證"]


def test_one_collected_image_uses_existing_single_image_path_not_bundle():
    h = Harness()
    h.start()
    send(h.handler, h.image("i1"))
    send(h.handler, h.post("analyze"))
    assert h.vision_bundle_calls == 0 and len(h.single_vision) == 1 and h.controller_calls == 1 and h.verify_calls == 1
    [record] = h.store._data.values()
    assert record.normalized_request.image_bundle is None
    assert [r.tool_name for r in record.normalized_request.input_tools_called] == ["analyze_image"]
    assert h.collections.active(USER) is None and len(h.line.sent) == 2  # 控制訊息 + 一則結果（存圖靜默）
    assert PRODUCT not in h.text(0)  # 蒐集期間沒有產品結論


@pytest.mark.parametrize("count", [2, 3])
def test_two_or_three_collected_images_use_bundle_once(count):
    h = Harness()
    h.start()
    for key in ("i1", "i2", "i3")[:count]:
        send(h.handler, h.image(key))
    send(h.handler, h.post("analyze"))
    assert h.vision_bundle_calls == 1 and h.single_vision == [] and h.controller_calls == 1 and h.verify_calls == 1
    [record] = h.store._data.values()
    assert record.normalized_request.image_bundle.image_count == count
    assert len(h.line.sent) == 2 and PRODUCT not in h.text(0) and "找不到" not in h.text(0)


# ============================================================ 按鈕底色


def test_collection_control_buttons_use_dark_333333_background_with_white_text():
    h = Harness()
    h.start()
    footer = h.message()["contents"]["footer"]["contents"]
    primary = next(b for b in footer if b["action"]["label"] == MULTI_ANALYZE_LABEL)
    assert primary["style"] == "primary" and primary["color"] == "#333333"
    assert "#9c7c53" not in h.text().lower()  # 強調色不當按鈕底色
    # 一般結果卡的按鈕維持原樣（不受影響）
    send(h.handler, event(kind="postback", data=session_postback_data("new")))
    send(h.handler, h.image("i1"))
    assert h.message()["contents"]["footer"]["contents"][0]["style"] == "link"
