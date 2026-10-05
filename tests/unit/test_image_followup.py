"""Sequential image follow-up：圖片 request 完成後，使用者緊接著傳的相關文字可以指回「已完成的圖片結果」。

* 沿用既有 per-user FIFO + 短期 TaskContext：文字排在圖片之後，圖片完成並寫回 context 後才處理；不合併進處理中的圖片 request。
* 產品追問：圖片得到唯一 VERIFIED 產品 → 既有 confirmed_product 綁定；沒有唯一確認 → 不替使用者選。
* 圖片追問：Controller 的結構化 `context_reference = PREVIOUS_IMAGE`（語意判斷）→ 以 `last_visual_request_id` 取回該題保存的
  結構化觀察（不複製圖片、不重跑 Vision / Controller 之外的任何步驟）。
全部使用 TEST_ONLY 虛構資料；不呼叫外部服務。
"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.intake_agent import IntakeAgent, resolve_context_reference
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.line.handler import LineWebhookHandler, session_postback_data
from app.pipeline import AnalysisPipeline
from app.response.service import PREVIOUS_IMAGE_MISSING_TEXT, AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimContentSource,
    ClaimOrigin,
    ClaimSemanticCategory,
    ContextReference,
    InputType,
    Intent,
    ProductReferenceStatus,
    ProductVerificationStatus,
    ResponseType,
)
from app.schemas.user_request import ConversationContext, IntakeExtraction, RawUserInput
from app.services.context_store import TaskContextStore
from tests.builders import claim, extraction, request
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import _product
from tests.unit.test_line_webhook import USER, FakeLine, event, send

PRODUCT = "虛構牌晶亮測試膠囊"  # p-1：唯一、有成分
FAMILY = "虛構牌追問測試飲"  # 同基本品名兩個品項 → 多候選
CLAIM_TEXT = "TEST_ONLY 有助於維持測試機能甲"
Q_INGREDIENTS, Q_IRON, Q_PACKAGE, Q_STANDALONE = "TEST_Q 它有哪些成分", "TEST_Q 它有鐵嗎", "TEST_Q 包裝上寫了什麼", "TEST_Q 鋅是什麼"
Q_INGREDIENTS_CP = "TEST_Q 成分CP"  # Controller 判 CURRENT_PRODUCT + 代名詞
Q_IRON_CP = "TEST_Q 鐵CP"
Q_PACKAGE_PRONOUN = "TEST_Q 那包寫了什麼"  # Controller 判 NONE，但有未解析的產品指稱
Q_CLAIMS_PRONOUN, Q_WARNINGS_PRONOUN = "TEST_Q 宣稱PRON", "TEST_Q 警語PRON"
Q_STANDALONE_ING = "TEST_Q 獨立成分"  # 沒有代名詞、自己給了成分
COMPANY = "虛構測試生技股份有限公司"
UNKNOWN = "完全不存在的虛構包裝品"


@pytest.fixture(autouse=True)
def family_data(monkeypatch):
    rows = [_product(f"fu-{i}", f"{FAMILY}({x})", "c-1", "虛構測試生技股份有限公司", f"Z0095{i}") for i, x in enumerate(("甲款", "乙款"))]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + rows)


def label_claim(text):
    c = claim(text, ClaimSemanticCategory.HEALTH_FUNCTION, ClaimOrigin.PRODUCT_LABEL_CLAIM)
    c.content_source = ClaimContentSource.IMAGE_CONTENT
    return c


IMAGES = {
    b"verified": extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT], claims=[label_claim(CLAIM_TEXT)],
                            ingredient_candidates=["虛構素甲"]),
    # 只讀到公司（PARTIAL_MATCH / COMPANY_ONLY），有成分文字與宣稱
    b"partial": extraction(intent=Intent.PRODUCT_VERIFICATION, company_candidates=[COMPANY], claims=[label_claim(CLAIM_TEXT)],
                           ingredient_candidates=["虛構素乙", "測試乳酸菌乙"]),
    b"partial_noing": extraction(intent=Intent.PRODUCT_VERIFICATION, company_candidates=[COMPANY], claims=[label_claim(CLAIM_TEXT)]),
    # 查無（NOT_FOUND）
    b"notfound": extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[UNKNOWN], claims=[label_claim(CLAIM_TEXT)],
                            ingredient_candidates=["虛構素丙"]),
    # 查無，但照片上有品名 / 品牌 / 其他文字（含與品名重複的文字）
    b"general": extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[UNKNOWN], brand_candidates=["虛構品牌丙"],
                           other_visible_text=["TEST_ONLY 60毫升", UNKNOWN], claims=[label_claim(CLAIM_TEXT)],
                           ingredient_candidates=["虛構素丙"]),
    b"ambiguous": extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[FAMILY], claims=[label_claim(CLAIM_TEXT)]),
}
TEXTS = {
    Q_INGREDIENTS: lambda known: extraction(intent=Intent.INGREDIENT_INQUIRY, refers_to_unspecified_product=True,
                                            context_resolved_names=known[:1]),
    Q_IRON: lambda known: extraction(intent=Intent.INGREDIENT_INQUIRY, refers_to_unspecified_product=True,
                                     ingredient_candidates=["鐵"], context_resolved_names=known[:1]),
    Q_PACKAGE: lambda known: extraction(intent=Intent.GENERAL_QUESTION, context_reference=ContextReference.PREVIOUS_IMAGE),
    Q_STANDALONE: lambda known: extraction(intent=Intent.GENERAL_QUESTION, context_reference=ContextReference.NONE),
    Q_INGREDIENTS_CP: lambda known: extraction(intent=Intent.INGREDIENT_INQUIRY, refers_to_unspecified_product=True,
                                               context_resolved_names=known[:1], context_reference=ContextReference.CURRENT_PRODUCT),
    Q_IRON_CP: lambda known: extraction(intent=Intent.INGREDIENT_INQUIRY, refers_to_unspecified_product=True, ingredient_candidates=["鐵"],
                                        context_resolved_names=known[:1], context_reference=ContextReference.CURRENT_PRODUCT),
    Q_PACKAGE_PRONOUN: lambda known: extraction(intent=Intent.GENERAL_QUESTION, refers_to_unspecified_product=True,
                                                context_resolved_names=known[:1]),
    Q_CLAIMS_PRONOUN: lambda known: extraction(intent=Intent.CLAIM_VERIFICATION, refers_to_unspecified_product=True),
    Q_WARNINGS_PRONOUN: lambda known: extraction(intent=Intent.SAFETY_INQUIRY, refers_to_unspecified_product=True),
    Q_STANDALONE_ING: lambda known: extraction(intent=Intent.INGREDIENT_INQUIRY, ingredient_candidates=["鋅"]),
}


def controller(input_text):
    """TEST_ONLY：依 payload 模擬 Controller（只用於 fake；不是 production 規則）。"""
    payload = json.loads(input_text)
    content = payload["content"]
    if "TEST_ONLY 圖片：" in content:
        return IMAGES[content.split("TEST_ONLY 圖片：", 1)[1].split()[0].encode()].model_copy(deep=True)
    known = [e["name"] for e in payload["known_context_entities"] if e["type"] == "PRODUCT"]
    return next(make(known) for q, make in TEXTS.items() if q in content)


class Harness:
    def __init__(self, ttl_clock=None, slow=0.0):
        self.llm = FakeLLM()
        self.vision = []
        # 圖片文字要包含 Controller 抽出的品名（與真實流程相同：名稱必須出現在輸入文字中）
        self.llm.describe_image = lambda **kw: self.vision.append(1) or (
            f"TEST_ONLY 圖片：{self.current_key.decode()} "
            + " ".join(IMAGES[self.current_key].product_candidates + IMAGES[self.current_key].company_candidates
                       + IMAGES[self.current_key].ingredient_candidates + IMAGES[self.current_key].brand_candidates))
        self.llm.structured[IntakeExtraction] = controller
        self.current_key = b"verified"
        self.payloads = []
        real = self.llm.parse_structured
        self.llm.parse_structured = lambda **kw: self.payloads.append(json.loads(kw["input_text"])) or real(**kw)
        self.verifier = VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), self.llm)
        pipeline = AnalysisPipeline(intake=IntakeAgent(self.llm), verifier=self.verifier, risk_engine=RiskEngine(),
                                    reporter=ReporterAgent(self.llm))
        if slow:
            pipeline = SlowFirst(pipeline, slow)
        self.contexts = TaskContextStore(**({"clock": ttl_clock} if ttl_clock else {}))
        self.store = InMemoryRequestStore()
        service = AdaptiveResponseService(pipeline, ReporterAgent(self.llm), self.store, timeout_seconds=5.0,
                                          context_store=self.contexts)
        self.line = FakeLine()
        self.line.contents.update({f"i-{k.decode()}": (k, "image/jpeg") for k in IMAGES})
        self.handler = LineWebhookHandler(service, self.line, self.contexts)

    def image(self, key):
        self.current_key = key
        return send(self.handler, event(kind="image", message_id=f"i-{key.decode()}"))[0]

    def text(self, q):
        return send(self.handler, event(text=q))[0]

    def record(self, rid):
        return self.store.get(rid)

    def flex(self, i=-1):
        return json.dumps(self.line.sent[i][2], ensure_ascii=False)

    @property
    def controller_calls(self):
        return sum(1 for c in self.llm.calls if c[0] == "parse_structured")


class SlowFirst:
    """第一個 run（圖片）慢；用來證明後面的文字 request 會排在它後面。"""

    def __init__(self, inner, delay):
        self._inner, self._delay, self._n = inner, delay, 0

    def run(self, *a, **kw):
        self._n += 1
        if self._n == 1:
            time.sleep(self._delay)
        return self._inner.run(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ============================================================ 1–2. 圖片 VERIFIED → 產品追問


def test_verified_image_then_ingredient_followup_resolves_the_product():
    h = Harness()
    h.image(b"verified")
    assert h.contexts.snapshot(USER).confirmed_product.official_product_name == PRODUCT
    rec = h.record(h.text(Q_INGREDIENTS))
    assert rec.normalized_request.product_reference_status == ProductReferenceStatus.RESOLVED_FROM_CONTEXT
    assert rec.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert rec.evidence.product_match.best_match.product.product_id == "p-1"
    assert rec.normalized_request.context_reference == ContextReference.NONE and len(h.vision) == 1  # 沒有重跑 Vision


def test_verified_image_then_nutrient_question_resolves_the_product():
    h = Harness()
    h.image(b"verified")
    rec = h.record(h.text(Q_IRON))
    assert rec.evidence.product_match.best_match.product.product_id == "p-1"
    assert rec.normalized_request.ingredient_candidates == ["鐵"] and h.contexts.snapshot(USER).confirmed_product is not None


# ============================================================ 3. 處理中的圖片：文字排在後面


def test_text_sent_while_image_is_processing_waits_in_fifo_and_then_resolves():
    h = Harness(slow=0.3)
    h.current_key = b"verified"
    h.handler.dispatch(event(kind="image", message_id="i-verified"))
    rid_text = h.handler.dispatch(event(text=Q_INGREDIENTS))  # 圖片還在處理：文字排隊，不合併進圖片 request
    assert h.handler.queue.wait_idle(10)
    assert len(h.line.sent) == 2 and PRODUCT in h.flex(0)  # 先回圖片，再回文字
    rec = h.record(rid_text)
    assert rec.evidence.product_match.best_match.product.product_id == "p-1"
    assert rec.normalized_request.product_reference_status == ProductReferenceStatus.RESOLVED_FROM_CONTEXT


# ============================================================ 4–5. 圖片沒有唯一確認產品


def test_ambiguous_image_product_followup_never_picks_a_product():
    h = Harness()
    h.image(b"ambiguous")
    assert h.contexts.snapshot(USER).confirmed_product is None and h.contexts.snapshot(USER).current_product_name is None
    h.verifier.verify = lambda request: (_ for _ in ()).throw(AssertionError("no product research"))
    rec = h.record(h.text(Q_INGREDIENTS))
    assert rec.normalized_request.context_reference == ContextReference.PREVIOUS_IMAGE and rec.evidence is None
    assert "沒有辨識到足夠的成分資訊" in h.flex() and PRODUCT not in h.flex()  # 沒有替使用者選任何產品


def test_ambiguous_image_still_answers_package_observation_followup_from_stored_observations():
    h = Harness()
    first = h.image(b"ambiguous")
    sent_before = len(h.line.sent)
    rid = h.text(Q_PACKAGE)
    assert len(h.line.sent) == sent_before + 1
    card = h.flex()
    assert "包裝上辨識到的內容" in card and CLAIM_TEXT in card  # 取自第一題保存的觀察
    rec = h.record(rid)
    assert rec.normalized_request.context_reference == ContextReference.PREVIOUS_IMAGE
    assert rec.normalized_request.visual_request_id == first
    assert rec.evidence is None and rec.normalized_request.product_candidates == []  # 沒有產品查證，也不替使用者選產品


# ============================================================ 6. 圖片追問不重跑 Vision / 不查證


def test_previous_image_followup_runs_no_vision_no_research():
    h = Harness()
    h.image(b"verified")
    vision_before, controller_before = len(h.vision), h.controller_calls

    def boom(request):
        raise AssertionError("Researcher must not run for a previous-image follow-up")

    h.verifier.verify = boom
    h.text(Q_PACKAGE)
    assert len(h.vision) == vision_before and h.controller_calls == controller_before + 1  # 只有 Controller 一次
    assert not h.llm.called("embed") and not h.llm.called("web_search_structured") and not h.llm.called("plan_step")
    assert CLAIM_TEXT in h.flex()
    # 產品 context 不被這個追問改動
    assert h.contexts.snapshot(USER).confirmed_product.official_product_name == PRODUCT


def test_controller_is_told_a_previous_image_is_available_only_when_there_is_one():
    h = Harness()
    h.text(Q_STANDALONE)
    assert "last_visual_request_available" not in h.payloads[-1]  # 沒有圖片：payload 與原本相同
    h.image(b"verified")
    h.text(Q_STANDALONE)
    assert h.payloads[-1]["last_visual_request_available"] is True
    assert "request_id" not in json.dumps(h.payloads[-1])  # 只有可用旗標，不給 Controller 任何內部 id


# ============================================================ 7. 不相關的獨立問題不綁定上一張圖片


def test_unrelated_standalone_question_does_not_bind_previous_image():
    h = Harness()
    h.image(b"verified")
    rec = h.record(h.text(Q_STANDALONE))
    assert rec.normalized_request.context_reference == ContextReference.NONE and rec.normalized_request.visual_request_id is None
    assert "包裝上辨識到的內容" not in h.flex()


def test_reference_guard_rejects_previous_image_without_context_or_with_new_entities():
    ctx = ConversationContext(last_visual_request_id="rid-1")
    raw = RawUserInput(input_type=InputType.TEXT, text="x", context=ctx)
    ok = request()
    resolve_context_reference(ok, ContextReference.PREVIOUS_IMAGE, raw, has_image=False)
    assert ok.context_reference == ContextReference.PREVIOUS_IMAGE and ok.visual_request_id == "rid-1"
    for case, kwargs in (("no context", {"raw": RawUserInput(input_type=InputType.TEXT, text="x")}),
                         ("has own image", {"has_image": True}),
                         ("has own url", {"raw": RawUserInput(input_type=InputType.TEXT, text="x", urls=["https://e.invalid"], context=ctx)})):
        r = request()
        resolve_context_reference(r, ContextReference.PREVIOUS_IMAGE, kwargs.get("raw", raw), has_image=kwargs.get("has_image", False))
        assert r.context_reference == ContextReference.NONE, case
    named = request(product_candidates=["TEST_ONLY 新產品"])  # 明確的新產品優先
    resolve_context_reference(named, ContextReference.PREVIOUS_IMAGE, raw, has_image=False)
    assert named.context_reference == ContextReference.NONE
    from app.schemas.enums import ProductConfirmationSource
    from app.schemas.user_request import ConfirmedProduct

    confirmed = ConfirmedProduct(product_id="p-1", official_product_name=PRODUCT,
                                 confirmation_source=ProductConfirmationSource.RESOLVER_VERIFIED,
                                 confirmed_at=datetime.now(timezone.utc), confirmed_request_id="r0")
    with_product = RawUserInput(input_type=InputType.TEXT, text="x", context=ConversationContext(
        last_visual_request_id="rid-1", confirmed_product=confirmed))
    current = request()
    resolve_context_reference(current, ContextReference.CURRENT_PRODUCT, with_product, has_image=False)
    assert current.context_reference == ContextReference.CURRENT_PRODUCT and current.visual_request_id is None  # 有已確認產品：維持
    # CURRENT_PRODUCT 但沒有已確認產品、前文有圖片觀察 → 改由圖片觀察回答；沒有圖片觀察 → 維持原樣
    no_product = request()
    resolve_context_reference(no_product, ContextReference.CURRENT_PRODUCT, raw, has_image=False)
    assert no_product.context_reference == ContextReference.PREVIOUS_IMAGE and no_product.visual_request_id == "rid-1"
    bare = request()
    resolve_context_reference(bare, ContextReference.CURRENT_PRODUCT, RawUserInput(input_type=InputType.TEXT, text="x"), has_image=False)
    assert bare.context_reference == ContextReference.CURRENT_PRODUCT
    plain = request()  # Controller 沒有指向目前產品、也沒有未解析的產品指稱：不會被重新導向
    resolve_context_reference(plain, ContextReference.NONE, raw, has_image=False)
    assert plain.context_reference == ContextReference.NONE


# ============================================================ 8–9. 新問題 / 結束 / TTL 清除兩個參照


def test_new_question_clears_product_and_visual_reference():
    h = Harness()
    h.image(b"verified")
    ctx = h.contexts.snapshot(USER)
    assert ctx.last_visual_request_id and ctx.confirmed_product
    send(h.handler, event(kind="postback", data=session_postback_data("new")))
    ctx = h.contexts.snapshot(USER)
    assert ctx.last_visual_request_id is None and ctx.confirmed_product is None
    rec = h.record(h.text(Q_PACKAGE))  # Controller 就算指向「上一張圖片」，守門也不會成立
    assert rec.normalized_request.context_reference == ContextReference.NONE
    assert "包裝上辨識到的內容" not in h.flex()


def test_end_verification_clears_both_references():
    h = Harness()
    h.image(b"verified")
    send(h.handler, event(kind="postback", data=session_postback_data("end")))
    ctx = h.contexts.snapshot(USER)
    assert ctx.last_visual_request_id is None and ctx.confirmed_product is None and ctx.current_product_id is None


def test_task_context_ttl_clears_visual_reference_and_expired_record_is_reported():
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    h = Harness(ttl_clock=lambda: now[0])
    h.image(b"verified")
    assert h.contexts.snapshot(USER).last_visual_request_id
    now[0] += timedelta(minutes=31)
    assert h.contexts.snapshot(USER).last_visual_request_id is None
    # request store 裡的紀錄已被清掉（過期 / 容量）：參照不再有效 → Controller 看不到「有前一張圖片」，也不會被導向圖片觀察
    h2 = Harness()
    h2.image(b"verified")
    h2.store._data.clear()
    h2.text(Q_PACKAGE)
    assert "last_visual_request_available" not in h2.payloads[-1] and "包裝上辨識到的內容" not in h2.flex()


def test_other_users_visual_request_is_never_used():
    h = Harness()
    h.image(b"verified")
    other = "U_TEST_ONLY_OTHER"
    send(h.handler, event(text=Q_PACKAGE, user=other))  # 另一位使用者沒有任何圖片 context
    assert "包裝上辨識到的內容" not in h.flex() and h.contexts.snapshot(other).last_visual_request_id is None


# ============================================================ 10. 既有文字 context 行為不變


def test_text_only_context_behavior_is_unchanged():
    h = Harness()
    h.text("TEST_Q 它有哪些成分")  # 沒有任何前文：指稱無法解析 → 補資訊
    assert h.contexts.snapshot(USER).last_visual_request_id is None
    assert "last_visual_request_available" not in h.payloads[-1]
    assert h.record(next(iter(h.store._data))).response.response_type == ResponseType.MISSING_INFORMATION


# ============================================================ 視覺 context 與產品 context 分開（真機回報的修正）


def no_research(h):
    def boom(request):
        raise AssertionError("Researcher / product resolver must not run for a previous-image follow-up")

    h.verifier.verify = boom


def assert_no_external_calls(h, vision_before, controller_before):
    assert len(h.vision) == vision_before and h.controller_calls == controller_before + 1
    for name in ("embed", "web_search_structured", "plan_step"):
        assert not h.llm.called(name)


def test_a_verified_image_then_current_product_followup_uses_official_ingredients():
    h = Harness()
    h.image(b"verified")
    rec = h.record(h.text(Q_INGREDIENTS_CP))
    assert rec.normalized_request.context_reference == ContextReference.CURRENT_PRODUCT
    assert rec.evidence.product_match.best_match.product.product_id == "p-1"
    assert rec.response.response_type == ResponseType.INGREDIENT_LIST and any("虛構素甲" in k for k in rec.response.key_points)
    assert "從剛剛照片" not in h.flex()  # 官方資料，不是照片觀察


def test_b_partial_image_ingredient_followup_answers_only_observed_ingredients_marked_as_image_observed():
    h = Harness()
    first = h.image(b"partial")
    assert h.record(first).evidence.product_match.status == ProductVerificationStatus.PARTIAL_MATCH
    assert h.contexts.snapshot(USER).last_visual_request_id == first  # PARTIAL 也保存視覺參照
    assert h.contexts.snapshot(USER).confirmed_product is None and h.contexts.snapshot(USER).current_product_name is None
    vision, ctrl = len(h.vision), h.controller_calls
    no_research(h)
    rec = h.record(h.text(Q_INGREDIENTS_CP))
    card = h.flex()
    assert rec.normalized_request.context_reference == ContextReference.PREVIOUS_IMAGE
    assert "從剛剛照片可辨識到的成分" in card and "虛構素乙" in card and "測試乳酸菌乙" in card
    assert "不是官方登記資料" in card and "官方登記成分" not in card  # 不冒充官方成分
    assert CLAIM_TEXT not in card  # 只顯示相關區塊（成分），不是整張包裝觀察
    assert_no_external_calls(h, vision, ctrl)


def test_b2_observed_ingredient_presence_is_stated_without_claiming_the_product_lacks_it():
    h = Harness()
    h.image(b"partial")
    h.text(Q_IRON_CP)
    card = h.flex()
    assert "沒有看到「鐵」" in card and "不代表產品一定不含" in card and "虛構素乙" in card


def test_c_partial_image_without_usable_ingredients_asks_for_more_information_without_planner_or_web():
    h = Harness()
    h.image(b"partial_noing")
    vision, ctrl = len(h.vision), h.controller_calls
    no_research(h)
    rec = h.record(h.text(Q_INGREDIENTS_CP))
    card = h.flex()
    assert rec.normalized_request.context_reference == ContextReference.PREVIOUS_IMAGE
    assert "沒有在官方資料中確認" in card and "沒有辨識到足夠的成分資訊" in card and "許可證字號" in card
    assert "官方登記成分" not in card
    assert_no_external_calls(h, vision, ctrl)


def test_d_not_found_image_package_followup_returns_stored_observations():
    h = Harness()
    h.image(b"notfound")
    vision, ctrl = len(h.vision), h.controller_calls
    no_research(h)
    rec = h.record(h.text(Q_PACKAGE_PRONOUN))  # Controller 判 NONE，但有未解析的產品指稱、也沒有已確認產品
    card = h.flex()
    assert rec.normalized_request.context_reference == ContextReference.PREVIOUS_IMAGE
    assert "包裝上辨識到的內容" in card and CLAIM_TEXT in card and "虛構素丙" in card
    assert_no_external_calls(h, vision, ctrl)


def test_e_not_found_image_does_not_create_current_product():
    h = Harness()
    h.image(b"notfound")
    ctx = h.contexts.snapshot(USER)
    assert ctx.confirmed_product is None and ctx.current_product_id is None and ctx.current_product_name is None
    assert ctx.last_visual_request_id is not None
    names = [e.name for e in ctx.recent_confirmed_entities] + [e.name for t in ctx.turns for e in t.entities]
    assert UNKNOWN not in names


def test_f_unresolved_image_after_a_verified_product_never_lets_the_pronoun_bind_the_old_product():
    h = Harness()
    h.image(b"verified")
    assert h.contexts.snapshot(USER).confirmed_product.official_product_name == PRODUCT
    second = h.image(b"notfound")
    ctx = h.contexts.snapshot(USER)
    assert ctx.confirmed_product is None and ctx.current_product_name is None and ctx.last_visual_request_id == second
    no_research(h)
    rec = h.record(h.text(Q_INGREDIENTS_CP))
    card = h.flex()
    assert rec.normalized_request.context_reference == ContextReference.PREVIOUS_IMAGE
    assert rec.normalized_request.visual_request_id == second and "虛構素丙" in card  # 用第二張圖片的觀察
    assert PRODUCT not in card and "虛構素甲" not in card  # 沒有用舊產品 A 的資料


def test_g_explicit_new_standalone_ingredient_question_is_not_bound_to_the_previous_image():
    h = Harness()
    h.image(b"partial")
    rec = h.record(h.text(Q_STANDALONE_ING))
    assert rec.normalized_request.context_reference == ContextReference.NONE and rec.normalized_request.visual_request_id is None
    assert "從剛剛照片" not in h.flex()


def test_i_image_result_writes_visual_context_before_the_queued_text_resolves():
    h = Harness(slow=0.3)
    h.current_key = b"partial"
    first = h.handler.dispatch(event(kind="image", message_id="i-partial"))
    rid = h.handler.dispatch(event(text=Q_INGREDIENTS_CP))  # 圖片還在處理中就送出
    assert h.handler.queue.wait_idle(10)
    rec = h.record(rid)
    assert rec.normalized_request.visual_request_id == first and "虛構素乙" in h.flex()
    assert len(h.line.sent) == 2


def test_followup_after_an_image_whose_observation_is_gone_is_not_treated_as_available():
    h = Harness()
    h.image(b"verified")
    h.store._data.clear()  # 觀察不在了
    h.text(Q_INGREDIENTS_CP)
    assert "從剛剛照片" not in h.flex()


# ============================================================ 一般的前一張圖片追問：完整的圖片觀察（只是照片辨識到的文字）


def text_nodes(card):
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                out.append(n["text"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(card)
    return out


def test_general_previous_image_followup_shows_all_observed_fields_without_any_external_call():
    h = Harness()
    h.image(b"general")
    vision, ctrl = len(h.vision), h.controller_calls
    no_research(h)
    rec = h.record(h.text(Q_PACKAGE_PRONOUN))
    nodes = text_nodes(h.line.sent[-1][2])
    assert rec.normalized_request.context_reference == ContextReference.PREVIOUS_IMAGE
    assert "包裝上辨識到的內容" in nodes and "以下為照片中辨識到的文字，尚未與官方資料逐項比對。" in nodes
    for heading in ("產品名稱", "品牌", "宣稱文字", "成分", "其他文字"):
        assert heading in nodes
    for shown in (UNKNOWN, "虛構品牌丙", "TEST_ONLY 60毫升", CLAIM_TEXT, "虛構素丙"):
        assert any(shown in n for n in nodes)
    assert_no_external_calls(h, vision, ctrl)


def test_observed_identity_is_not_presented_as_official_and_duplicates_are_removed():
    h = Harness()
    h.image(b"general")
    h.text(Q_PACKAGE_PRONOUN)
    nodes = text_nodes(h.line.sent[-1][2])
    card = " ".join(nodes)
    assert "官方登記" not in card and "官方核准" not in card and "官方確認" not in card
    assert nodes.count(UNKNOWN) == 1  # 品名只列一次：不在「其他文字」重複（標題的查詢標示不算內容）
    assert nodes.count("TEST_ONLY 60毫升") == 1


def test_general_followup_omits_empty_sections():
    h = Harness()
    h.image(b"partial_noing")  # 只有公司與宣稱
    h.text(Q_PACKAGE_PRONOUN)
    nodes = text_nodes(h.line.sent[-1][2])
    assert "公司" in nodes and "宣稱文字" in nodes
    for heading in ("產品名稱", "品牌", "許可證字號", "成分", "警語與注意事項", "其他文字"):
        assert heading not in nodes


def test_intent_specific_followups_stay_filtered():
    h = Harness()
    h.image(b"general")
    h.text(Q_CLAIMS_PRONOUN)
    claims_nodes = text_nodes(h.line.sent[-1][2])
    assert any(CLAIM_TEXT in n for n in claims_nodes) and not any("虛構品牌丙" in n or "虛構素丙" in n for n in claims_nodes)
    h.text(Q_INGREDIENTS_CP)
    ingredient_nodes = text_nodes(h.line.sent[-1][2])
    assert any("虛構素丙" in n for n in ingredient_nodes) and not any(CLAIM_TEXT in n or "虛構品牌丙" in n for n in ingredient_nodes)
    h.text(Q_WARNINGS_PRONOUN)  # 照片沒有警語：不顯示其他區塊，如實說明
    warning_nodes = text_nodes(h.line.sent[-1][2])
    assert any("沒有辨識到足夠的警語資訊" in n for n in warning_nodes) and not any(CLAIM_TEXT in n for n in warning_nodes)
