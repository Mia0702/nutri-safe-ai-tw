"""Confirmed-product context binding ＋ Flex 字體無障礙（scaling）回歸測試。

* 選定產品（文字 / 圖片 / 同名不同字號）後，「它 / 這個」或沒有實體的產品 follow-up 直接用 canonical 產品（字號）查證，
  不再以基本品名重新比對、不再出現候選卡。
* 明確的新產品 / 字號優先；開始新問題 / 結束本次查證 / TTL 過期後不沿用舊產品。
* Renderer：所有 text / button 跟隨 LINE 字體設定（scaling），text 換行，大小集中由 TEXT_SIZES 決定。

Fake Agent A 會模擬手機上觀察到的 LLM 行為（把前文的基本品名帶回 product_candidates），只用於測試。
全部使用 TEST_ONLY 虛構產品，不呼叫外部服務。
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.line.handler import SESSION_END, SESSION_NEW, LineWebhookHandler, session_postback_data
from app.pipeline import AnalysisPipeline
from app.response.actions import build_select_product_data
from app.response.line_renderer import BUTTON_HEIGHT, TEXT_SIZES, AdaptiveLineRenderer
from app.response.service import AdaptiveResponseService
from app.response.sources import SourceLink
from app.response.store import InMemoryRequestStore
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    Intent,
    ProductConfirmationSource,
    ProductMatchType,
    ProductReferenceStatus,
    ProductVerificationStatus,
    ResponseAction,
    ResponseType,
    RiskLevel,
)
from app.schemas.response import ProductChoice, ResponsePayload
from app.schemas.user_request import IntakeExtraction
from app.services.context_store import TaskContextStore
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product
from tests.unit.test_line_webhook import USER, FakeLine, event, send

BASE = "虛構牌綁定測試飲"  # TEST_ONLY：同一基本品名兩個品項
VARIANTS = [(f"{BASE}(原味)", "Z00800"), (f"{BASE}(無糖)", "Z00801")]
SAME_NAME = "虛構牌同名測試錠"  # TEST_ONLY：品名完全相同、字號不同
SAME_NAME_PERMITS = ["Z00810", "Z00811"]
OTHER = "虛構牌晶亮測試膠囊"  # synthetic_data 既有的唯一產品
INGREDIENT = "虛構素甲"


@pytest.fixture(autouse=True)
def binding_data(monkeypatch):
    products, claims, ingredients = [], [], []
    rows = [(f"bind-{i}", name, code) for i, (name, code) in enumerate(VARIANTS)]
    rows += [(f"same-{i}", SAME_NAME, code) for i, code in enumerate(SAME_NAME_PERMITS)]
    for pid, name, code in rows:
        products.append(_product(pid, name, "c-1", "虛構測試生技股份有限公司", code))
        claims.append({"product_claim_id": f"pc-{pid}", "product_id": pid, "approved_claim_id": f"ac-{pid}",
                       "function_name": "測試功效甲", "claim_text": f"TEST_ONLY {pid} 的核准敘述。", **SOURCE})
        ingredients.append({"product_id": pid, "ingredient_id": "i-1", "raw_text": f"虛構素甲（{pid}）"})
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + products)
    monkeypatch.setattr(data, "APPROVED_CLAIMS", data.APPROVED_CLAIMS + claims)
    monkeypatch.setattr(data, "PRODUCT_INGREDIENTS", data.PRODUCT_INGREDIENTS + ingredients)


def fake_intake(copy_base_name: bool):
    """TEST_ONLY 模擬 Agent A。copy_base_name=True：代名詞問題時把前文中的基本品名帶回 product_candidates
    （手機上觀察到的 LLM 行為）；False：只標記 refers_to_unspecified_product。"""
    def run(input_text: str) -> IntakeExtraction:
        payload = json.loads(input_text)
        content, previous = payload["content"], " ".join(payload["previous_user_messages"])
        intent = (Intent.INGREDIENT_INQUIRY if "成分" in content
                  else Intent.SAFETY_INQUIRY if "注意" in content else Intent.EFFICACY_INQUIRY)
        for name in (OTHER, SAME_NAME, BASE):
            if name in content:
                return extraction(intent=intent, product_candidates=[name], normalized_text=content)
        if INGREDIENT in content:
            return extraction(intent=intent, ingredient_candidates=[INGREDIENT], normalized_text=content)
        pronoun = any(w in content for w in ("它", "這個", "這罐"))
        copied = [n for n in (SAME_NAME, BASE) if n in previous][:1] if copy_base_name and pronoun else []
        return extraction(intent=intent, product_candidates=copied, refers_to_unspecified_product=pronoun,
                          normalized_text=content)
    return run


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def build(copy_base_name=True):
    llm = FakeLLM(structured={IntakeExtraction: fake_intake(copy_base_name)})
    llm.describe_image = lambda **kw: f"TEST_ONLY 圖片文字：{BASE}"
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
    return LineWebhookHandler(svc, line, contexts), line, contexts, store, clock


def ask(handler, text, **kw):
    return send(handler, event(text=text, **kw))[0]


def select(handler, rid, candidate_id):
    send(handler, event(kind="postback", data=build_select_product_data(rid, candidate_id)))


def last_record(store):
    return list(store._data.values())[-1]


def assert_answers_product(store, product_id, response_type):
    rec = last_record(store)
    match = rec.evidence.product_match
    assert match.status == ProductVerificationStatus.VERIFIED
    assert match.best_match.product.product_id == product_id
    assert match.match_type == ProductMatchType.PERMIT_NUMBER  # 以 canonical 字號確認，不是基本品名
    assert rec.response.response_type == response_type and not rec.response.product_choices
    return rec


# ============================================================ 1–2. 選定產品 → 代名詞 / 隱含 follow-up


@pytest.mark.parametrize("copy_base_name", [True, False])
def test_text_selection_then_pronoun_follow_up_uses_selected_product(copy_base_name):
    handler, line, contexts, store, _ = build(copy_base_name)
    rid = ask(handler, f"{BASE}的核准功效是什麼？")
    assert store.get(rid).response.product_choices  # 先出現確認卡
    select(handler, rid, "bind-1")
    ctx = contexts.snapshot(USER).confirmed_product
    assert ctx.product_id == "bind-1" and ctx.permit_number == "測試健食字第Z00801號"
    assert ctx.official_product_name == VARIANTS[1][0] and ctx.permit_id == "permit-bind-1"
    assert ctx.confirmation_source == ProductConfirmationSource.USER_SELECTED and ctx.confirmed_request_id
    ask(handler, "它有哪些成分？")
    rec = assert_answers_product(store, "bind-1", ResponseType.INGREDIENT_LIST)
    assert rec.normalized_request.product_reference_status == ProductReferenceStatus.RESOLVED_FROM_CONTEXT
    ask(handler, "這個的核准功效？")
    assert_answers_product(store, "bind-1", ResponseType.PRODUCT_INFO)
    assert "bind-1 的核准敘述" in json.dumps(line.sent[-1][2], ensure_ascii=False)
    # 確認來源與時間保留在原本的選擇
    assert contexts.snapshot(USER).confirmed_product.confirmation_source == ProductConfirmationSource.USER_SELECTED


def test_implicit_follow_up_without_pronoun_uses_selected_product():
    handler, _, _, store, _ = build()
    rid = ask(handler, f"{BASE}的核准功效是什麼？")
    select(handler, rid, "bind-0")
    ask(handler, "有哪些成分？")
    assert_answers_product(store, "bind-0", ResponseType.INGREDIENT_LIST)
    ask(handler, "要注意什麼？")  # 沒有命題、沒有風險發現：產品資訊（不推論產品安全性、不顯示風險燈）
    assert_answers_product(store, "bind-0", ResponseType.PRODUCT_INFO)


def test_base_name_of_selected_product_still_refers_to_selection():
    handler, _, _, store, _ = build()
    rid = ask(handler, f"{BASE}的核准功效是什麼？")
    select(handler, rid, "bind-0")
    ask(handler, f"{BASE}有哪些成分？")
    assert_answers_product(store, "bind-0", ResponseType.INGREDIENT_LIST)


def test_general_ingredient_question_is_not_bound_to_product():
    handler, _, _, store, _ = build()
    rid = ask(handler, f"{BASE}的核准功效是什麼？")
    select(handler, rid, "bind-0")
    ask(handler, f"{INGREDIENT}有什麼功效？")
    req = last_record(store).normalized_request
    assert req.product_candidates == [] and req.product_reference_status != ProductReferenceStatus.RESOLVED_FROM_CONTEXT


# ============================================================ 3. 明確的新產品優先


def test_explicit_new_product_overrides_context():
    handler, _, contexts, store, _ = build()
    rid = ask(handler, f"{BASE}的核准功效是什麼？")
    select(handler, rid, "bind-0")
    ask(handler, f"{OTHER}有哪些成分？")
    rec = last_record(store)
    assert rec.evidence.product_match.best_match.product.product_id == "p-1"
    ctx = contexts.snapshot(USER).confirmed_product
    assert ctx.product_id == "p-1" and ctx.confirmation_source == ProductConfirmationSource.RESOLVER_VERIFIED
    ask(handler, "它有哪些成分？")
    assert_answers_product(store, "p-1", ResponseType.INGREDIENT_LIST)


def test_explicit_new_ambiguous_product_clears_old_confirmation():
    handler, _, contexts, store, _ = build()
    rid = ask(handler, f"{BASE}的核准功效是什麼？")
    select(handler, rid, "bind-0")
    ask(handler, f"{SAME_NAME}有哪些成分？")  # 另一個產品、且無法唯一確認
    assert last_record(store).response.product_choices
    assert contexts.snapshot(USER).confirmed_product is None  # 不可再把「它」當成舊產品


# ============================================================ 5. 圖片 / 同名字號選擇寫入同一份 context


def test_image_candidate_selection_updates_same_context():
    handler, _, contexts, store, _ = build()
    [rid] = send(handler, event(kind="image", message_id="m-img"))
    assert store.get(rid).response.product_choices
    select(handler, rid, "bind-1")
    assert contexts.snapshot(USER).confirmed_product.product_id == "bind-1"
    ask(handler, "它有哪些成分？")
    assert_answers_product(store, "bind-1", ResponseType.INGREDIENT_LIST)


def test_same_name_permit_selection_binds_by_permit():
    handler, _, contexts, store, _ = build()
    rid = ask(handler, f"{SAME_NAME}的核准功效是什麼？")
    assert len(store.get(rid).response.product_choices) == 2
    select(handler, rid, "same-1")
    assert contexts.snapshot(USER).confirmed_product.permit_number == "測試健食字第Z00811號"
    ask(handler, "它有哪些成分？")  # 品名完全相同：只有字號能分辨
    assert_answers_product(store, "same-1", ResponseType.INGREDIENT_LIST)


# ============================================================ 4. 清除


@pytest.mark.parametrize("kind", [SESSION_NEW, SESSION_END])
def test_session_controls_clear_confirmed_product(kind):
    handler, _, contexts, store, _ = build(copy_base_name=False)
    rid = ask(handler, f"{BASE}的核准功效是什麼？")
    select(handler, rid, "bind-0")
    send(handler, event(kind="postback", data=session_postback_data(kind)))
    assert contexts.snapshot(USER).confirmed_product is None
    ask(handler, "它有哪些成分？")
    rec = last_record(store)
    assert rec.response.response_type == ResponseType.MISSING_INFORMATION  # 要求補產品資訊
    assert rec.evidence.product_match.best_match is None


def test_ttl_expiry_does_not_reuse_previous_product():
    handler, _, contexts, store, clock = build(copy_base_name=False)
    rid = ask(handler, f"{BASE}的核准功效是什麼？")
    select(handler, rid, "bind-0")
    clock.now += timedelta(minutes=31)
    ask(handler, "它有哪些成分？")
    assert last_record(store).response.response_type == ResponseType.MISSING_INFORMATION
    assert contexts.snapshot(USER).confirmed_product is None


# ============================================================ 6–7. Flex font scaling


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _assert_accessible(message):
    comps = list(_walk(message))
    texts = [c for c in comps if c.get("type") == "text"]
    btns = [c for c in comps if c.get("type") == "button"]
    assert texts
    for t in texts:
        assert t["scaling"] is True and t["wrap"] is True and "maxLines" not in t  # 大字時換行、不截斷
        assert t["size"] in TEXT_SIZES.values()
    for b in btns:
        assert b["scaling"] is True and b["adjustMode"] == "shrink-to-fit" and b["height"] == BUTTON_HEIGHT
        assert len(b["action"]["label"]) <= 20  # label 本身就短，不只靠 shrink-to-fit
    for icon in (c for c in comps if c.get("type") == "icon"):
        assert icon.get("scaling") is True
    return texts, btns


R = AdaptiveLineRenderer()
RID = "00000000-0000-4000-8000-000000000001"


def _risk_payload():
    return ResponsePayload(
        request_id=RID, response_type=ResponseType.RISK, risk_level=RiskLevel.HIGH,
        title="TEST_ONLY 一個比較長的風險標題，用來確認大字時會換行而不是被切掉", summary="TEST_ONLY 結論",
        key_points=[f"TEST_ONLY 重點 {i} " + "很長的說明" * 6 for i in range(5)], warnings=["TEST_ONLY 警語"],
        available_actions=[ResponseAction.DETAIL, ResponseAction.SOURCES, ResponseAction.OFFICIAL_INFO,
                           ResponseAction.WHY], source_count=2)


def test_typography_roles_follow_accessibility_baseline():
    assert TEXT_SIZES == {"section": "sm", "title": "xl", "body": "md", "note": "sm"} and BUTTON_HEIGHT == "md"


def test_risk_card_scales_and_stays_concise():
    message = R.render_primary(_risk_payload(), query_label="圖片辨識｜TEST_ONLY")
    texts, btns = _assert_accessible(message)
    sizes = {t["text"]: t["size"] for t in texts}
    assert sizes["🔴 TEST_ONLY 一個比較長的風險標題，用來確認大字時會換行而不是被切掉"] == "xl"
    assert sizes["風險判斷"] == "sm" and sizes["TEST_ONLY 結論"] == "md"
    assert len(btns) == 3  # 仍最多 3 個按鈕
    assert sum(1 for t in texts if t["text"].startswith("・")) == 3  # 仍最多 3 個重點，其餘 on-demand


@pytest.mark.parametrize("count", [2, 3, 5])
def test_candidate_card_is_large_font_safe(count):
    choices = [ProductChoice(product_id=f"c-{i}", label=f"TEST_ONLY 款式{i}" + "很長" * 8, full_name=f"TEST_ONLY 完整品名{i}",
                             permit_number=f"測試健食字第Z0{i}號", is_active=i != 1, status_text="許可證已失效")
               for i in range(count)]
    payload = ResponsePayload(request_id=RID, response_type=ResponseType.MISSING_INFORMATION, title="TEST_ONLY 請選擇",
                              summary="TEST_ONLY", product_choices=choices, choice_note="TEST_ONLY 都不是？")
    message = R.render_primary(payload)
    _, btns = _assert_accessible(message)
    assert len(btns) == count
    bubbles = message["contents"]["contents"] if count > 3 else [message["contents"]]
    if count <= 3:  # 同一張卡：候選之間以 separator 分開，每個候選的文字與按鈕各自一列（垂直排列不重疊）
        body = bubbles[0]["body"]
        assert body["layout"] == "vertical"
        assert sum(1 for c in body["contents"] if c["type"] == "separator") >= count
    else:
        assert len(bubbles) == count + 1 and all(b["body"]["layout"] == "vertical" for b in bubbles)


def test_source_card_scales_and_keeps_uri_buttons():
    links = [SourceLink(title="TEST_ONLY 官方來源標題" * 3, domain="test-only.fda.gov.tw", is_official=True,
                        url="https://test-only.fda.gov.tw/a"),
             SourceLink(title="TEST_ONLY 外部頁", domain="shop.example-mall.invalid", is_official=False,
                        url="https://shop.example-mall.invalid/p"),
             SourceLink(title="TEST_ONLY 無網址", is_official=True)]
    message = R.render_sources(links, "圖片辨識｜TEST_ONLY")
    _, btns = _assert_accessible(message)
    assert [b["action"]["type"] for b in btns] == ["uri", "uri"]


def test_detail_and_notice_cards_scale():
    from app.schemas.response import DetailPayload

    _assert_accessible(R.render_detail(DetailPayload(request_id=RID, action=ResponseAction.WHY, title="TEST_ONLY",
                                                     query_text="TEST_ONLY", body=["TEST_ONLY 段落"])))
    _assert_accessible(R.render_notice("TEST_ONLY 提示", retry_request_id=RID, query_label="圖片查詢｜TEST_ONLY"))
