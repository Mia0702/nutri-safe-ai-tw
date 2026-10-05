"""Image-only routing ＋ image latency 回歸測試。

* 只傳產品 / 包裝圖片：預設目標是辨識產品（PRODUCT_INFO / 確認卡 / 請補充），包裝宣稱存成 observed content，
  不自動進完整 claim verification（不跑 planner / RAG / assess / 官方網站）。
* 自動查證只有：使用者文字明確要求查驗、或圖片上有被宣稱的醫療效能（既有 claim-role / medical 規則）。
* 「查驗包裝宣稱」：使用原 request 保存的 observed claims 與已確認產品，不重新跑 Vision / Agent A。
* 檢查 tool-call path 與呼叫次數，不檢查秒數。

全部使用虛構的圖片分析輸出與 TEST_ONLY 產品，不使用任何真實照片。
"""

import json
import time

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import (
    ASSESSMENT_STEP,
    CLARIFICATION_MODE,
    FAST_PATH_MODE,
    IMAGE_IDENTIFICATION_MODE,
    PACKAGE_CLAIM_MODE,
    VerificationAgent,
)
from app.line.request_queue import PerUserRequestQueue
from app.pipeline import AnalysisPipeline
from app.response.actions import build_postback_data, build_select_product_data
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimContentSource,
    ClaimOrigin,
    ClaimSemanticCategory,
    InputType,
    Intent,
    ResponseAction,
    ResponseType,
    RiskLevel,
)
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from tests.builders import claim, extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product
from tests.unit.test_line_webhook import USER, FakeLine, event, send

PRODUCT = "虛構牌晶亮測試膠囊"  # synthetic：唯一、有核准功效與成分
FAMILY = "虛構牌圖片測試飲"  # TEST_ONLY：同一基本品名兩個品項
UNKNOWN = "完全不存在的虛構包裝品"
SLOW_TOOLS = {"plan_tool_calls", "search_rag", ASSESSMENT_STEP, "search_tw_official_web"}
IMG = ClaimContentSource.IMAGE_CONTENT


@pytest.fixture(autouse=True)
def family_data(monkeypatch):
    rows = [_product(f"img-{i}", f"{FAMILY}({x})", "c-1", "虛構測試生技股份有限公司", f"Z0090{i}")
            for i, x in enumerate(("甲款", "乙款"))]
    claims = [{"product_claim_id": f"pc-{p['product_id']}", "product_id": p["product_id"], "approved_claim_id": "ac-x",
               "function_name": "測試功效甲", "claim_text": "TEST_ONLY 有助於維持測試機能甲。", **SOURCE} for p in rows]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + rows)
    monkeypatch.setattr(data, "APPROVED_CLAIMS", data.APPROVED_CLAIMS + claims)


def label_claim(text, category=ClaimSemanticCategory.HEALTH_FUNCTION, absolute=False, condition=None):
    c = claim(text, category, ClaimOrigin.PRODUCT_LABEL_CLAIM, absolute=absolute, condition=condition)
    c.content_source = IMG
    return c


GENERAL_CLAIMS = [label_claim("TEST_ONLY 有助於維持測試機能甲"),
                  label_claim("TEST_ONLY 補充每日所需營養", ClaimSemanticCategory.NUTRIENT_SUPPLEMENT),
                  label_claim("TEST_ONLY 效果看得見", ClaimSemanticCategory.EXAGGERATED_EFFICACY, absolute=True)]
MEDICAL = label_claim("TEST_ONLY 可以治療虛構病", ClaimSemanticCategory.MEDICAL_EFFICACY, condition="虛構病")

# 圖片內容（bytes）→ 虛構的 Agent A 抽取結果；最壞情況：LLM 把只傳圖片也判成 claim_verification
SCENARIOS = {
    b"package": extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT], claims=GENERAL_CLAIMS),
    b"medical": extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT],
                           claims=GENERAL_CLAIMS[:1] + [MEDICAL]),
    b"family": extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[FAMILY], claims=GENERAL_CLAIMS[:1]),
    b"unknown": extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[UNKNOWN], claims=GENERAL_CLAIMS),
    b"blank": extraction(intent=Intent.UNCLEAR),
    b"not_health": extraction(intent=Intent.OUT_OF_SCOPE),
}


def fake_llm(user_text_extraction=None):
    llm = FakeLLM()
    llm.describe_image = lambda *, instructions, image, mime_type: (
        llm.calls.append(("describe_image", mime_type))
        or f"TEST_ONLY 圖片：{image.decode()} {' '.join(SCENARIOS[image].product_candidates)}")

    def intake(input_text):
        content = json.loads(input_text)["content"]
        if user_text_extraction is not None and "【使用者文字】" in content:
            return user_text_extraction
        key = content.split("TEST_ONLY 圖片：", 1)[1].split()[0].encode()
        return SCENARIOS[key].model_copy(deep=True)

    llm.structured[IntakeExtraction] = intake
    return llm


def pipeline(llm):
    return AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )


def run_image(key: bytes, text: str = "", llm=None):
    llm = llm or fake_llm()
    raw = RawUserInput(input_type=InputType.MIXED if text else InputType.IMAGE, text=text,
                       media=[MediaInput(kind="image", content=key, mime_type="image/jpeg")])
    return pipeline(llm).run(raw), llm


def tools(result):
    return [t.tool_name for t in result.evidence.tools_called]


# ============================================================ 1–2. image-only → product-first


def test_image_only_package_routes_to_product_info_without_claim_verification():
    result, llm = run_image(b"package")
    req, bundle = result.normalized_request, result.evidence
    assert req.intent == Intent.PRODUCT_VERIFICATION and req.image_identification
    assert req.claims == [] and len(req.observed_package_claims) == 3  # 宣稱仍保存，只是不自動查
    assert bundle.planner_mode == FAST_PATH_MODE
    assert not (set(tools(result)) & SLOW_TOOLS) and not llm.called("plan_step") and not llm.called("embed")
    assert "search_illegal_ad_cases" in tools(result)  # 安全檢查不省略
    payload = result.response
    assert payload.response_type == ResponseType.PRODUCT_INFO and payload.risk_level is None
    assert payload.available_actions[0] == ResponseAction.CHECK_PACKAGE_CLAIMS
    assert any("檢視包裝宣稱" in p for p in payload.key_points)


def test_observed_exaggerated_label_claims_do_not_force_verification():
    result, _ = run_image(b"package")
    assert result.claim_assessment == []
    assert {c.semantic_category for c in result.normalized_request.observed_package_claims} >= {
        ClaimSemanticCategory.EXAGGERATED_EFFICACY, ClaimSemanticCategory.NUTRIENT_SUPPLEMENT}


def test_image_ambiguous_product_uses_existing_candidate_selection():
    result, llm = run_image(b"family")
    assert result.evidence.planner_mode == CLARIFICATION_MODE
    assert result.response.response_type == ResponseType.MISSING_INFORMATION
    assert len(result.response.product_choices) == 2
    assert not (set(tools(result)) & SLOW_TOOLS)


def test_image_not_found_asks_for_more_info_without_hard_search():
    result, llm = run_image(b"unknown")
    bundle, payload = result.evidence, result.response
    assert bundle.planner_mode == IMAGE_IDENTIFICATION_MODE
    assert not (set(tools(result)) & SLOW_TOOLS) and not llm.called("web_search_structured")
    assert payload.response_type == ResponseType.MISSING_INFORMATION and payload.risk_level is None
    text = payload.plain_text()
    assert "不代表產品是假貨" in text and "許可證字號" in text
    assert payload.recognized_content == [f"產品名稱：{UNKNOWN}"]  # 讓使用者知道系統看到了什麼


def test_image_without_product_text_asks_for_clearer_photo():
    result, _ = run_image(b"blank")
    assert result.evidence.planner_mode == IMAGE_IDENTIFICATION_MODE
    assert result.response.response_type == ResponseType.MISSING_INFORMATION
    assert "沒有辨識到產品名稱" in result.response.summary


def test_non_health_image_stays_out_of_scope():
    result, _ = run_image(b"not_health")
    assert not result.normalized_request.image_identification
    assert result.response.response_type == ResponseType.OUT_OF_SCOPE


# ============================================================ 3. 自動查證的兩種情況


def test_high_risk_medical_label_claim_triggers_risk_verification():
    result, llm = run_image(b"medical")
    req = result.normalized_request
    assert [c.semantic_category for c in req.claims] == [ClaimSemanticCategory.MEDICAL_EFFICACY]
    assert len(req.observed_package_claims) == 1 and req.claims_from_package
    assert result.claim_assessment[0].status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert result.risk.risk_level == RiskLevel.HIGH and result.response.response_type == ResponseType.RISK
    # 產品已確認 + 結構化證據足夠：不跑多輪 planner，最多一次評估；醫療宣稱由規則決定，不查官方網站
    assert result.evidence.planner_mode == PACKAGE_CLAIM_MODE
    assert tools(result).count(ASSESSMENT_STEP) <= 1 and not llm.called("plan_step")
    assert not llm.called("web_search_structured")


def test_explicit_user_request_verifies_image_claims():
    ask = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT], claims=GENERAL_CLAIMS)
    result, llm = run_image(b"package", text="TEST_ONLY 幫我查這個包裝說的是真的嗎", llm=fake_llm(ask))
    req = result.normalized_request
    assert len(req.claims) == 3 and req.observed_package_claims == [] and req.claims_from_package
    assert not req.image_identification
    assert len(result.claim_assessment) == 3
    assert result.evidence.planner_mode == PACKAGE_CLAIM_MODE
    # 沒有內部多輪 planner；宣稱沒有內部證據可對照時，只允許一次官方網站查詢語句規劃
    assert all(c[1] == ["search_tw_official_web"] for c in llm.calls if c[0] == "plan_step")
    assert tools(result).count(ASSESSMENT_STEP) <= 2


# ============================================================ LINE：on-demand / selection / context / queue


def build_line(timeout=5.0, llm=None):
    from app.line.handler import LineWebhookHandler
    from app.response.service import AdaptiveResponseService
    from app.response.store import InMemoryRequestStore
    from app.services.context_store import TaskContextStore

    llm = llm or fake_llm()
    contexts, store = TaskContextStore(), InMemoryRequestStore()
    svc = AdaptiveResponseService(pipeline(llm), ReporterAgent(llm), store, timeout_seconds=timeout,
                                  context_store=contexts)
    line = FakeLine()
    line.contents = {k.decode(): (k, "image/jpeg") for k in SCENARIOS}
    return LineWebhookHandler(svc, line, contexts), line, store, contexts, llm


def flex(line, i=-1):
    return json.dumps(line.sent[i][2], ensure_ascii=False)


def vision_calls(llm):
    return sum(1 for c in llm.calls if c[0] == "describe_image")


def test_on_demand_package_observation_uses_stored_claims_without_any_pipeline():
    """「檢視包裝宣稱」（2026-10-05 起是包裝內容檢視，不是官方查證）：只用該題保存的觀察，不跑任何 pipeline / Vision / 查詢。"""
    handler, line, store, contexts, llm = build_line()
    [rid] = send(handler, event(kind="image", message_id="package"))
    label = store.get(rid).query_label
    assert label.startswith("圖片辨識｜") and "檢視包裝宣稱" in flex(line)
    calls_before, records_before = list(llm.calls), len(store._data)

    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"pipeline must not be used: {name}")

    handler._service._pipeline = Exploding()
    send(handler, event(kind="postback", data=build_postback_data(ResponseAction.CHECK_PACKAGE_CLAIMS, rid)))
    assert llm.calls == calls_before and len(store._data) == records_before  # 沒有任何 LLM / 查詢 / 新紀錄
    card = flex(line)
    assert "包裝上辨識到的內容" in card and "尚未與官方資料逐項比對" in card
    assert "有助於維持測試機能甲" in card and "建議多留意" in card


def test_check_package_claims_postback_from_other_user_is_rejected():
    handler, line, store, _, _ = build_line()
    [rid] = send(handler, event(kind="image", message_id="package"))
    n = len(store._data)
    send(handler, event(kind="postback", data=build_postback_data(ResponseAction.CHECK_PACKAGE_CLAIMS, rid),
                        user="U_TEST_ONLY_OTHER"))
    assert len(store._data) == n  # 沒有執行查驗


def test_image_candidate_selection_keeps_observed_claims_and_context():
    handler, line, store, contexts, _ = build_line()
    [rid] = send(handler, event(kind="image", message_id="family"))
    assert len(store.get(rid).response.product_choices) == 2
    send(handler, event(kind="postback", data=build_select_product_data(rid, "img-1")))
    rec = list(store._data.values())[-1]
    assert rec.response.response_type == ResponseType.PRODUCT_INFO
    assert ResponseAction.CHECK_PACKAGE_CLAIMS in rec.response.available_actions
    assert rec.query_label == store.get(rid).query_label
    assert contexts.snapshot(USER).confirmed_product.product_id == "img-1"  # 圖片選擇寫入同一份 context


class FailingPipeline:
    def __init__(self, inner):
        self._inner = inner

    def run(self, raw, *a, **kw):
        if raw.media and raw.media[0].content == b"package":
            raise RuntimeError("TEST_ONLY simulated failure")
        return self._inner.run(raw, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_image_exception_sends_one_error_card_and_releases_queue():
    handler, line, store, _, _ = build_line()
    handler._service._pipeline = FailingPipeline(handler._service._pipeline)
    send(handler, event(kind="image", message_id="package"), event(kind="image", message_id="unknown"))
    assert len(line.sent) == 2  # 每則各一張；第一張失敗不卡住第二張
    assert "查詢未完成" in flex(line, 0) and "圖片查詢｜" in flex(line, 0)
    assert "圖片辨識｜" in flex(line, 1)


class SlowPipeline:
    def __init__(self, inner, delay):
        self._inner, self._delay = inner, delay

    def run(self, *a, **kw):
        time.sleep(self._delay)
        return self._inner.run(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_image_watchdog_timeout_releases_queue_and_discards_late_result():
    handler, line, _, _, _ = build_line()
    handler._queue = PerUserRequestQueue(job_timeout_seconds=0.2)
    handler._service._pipeline = SlowPipeline(handler._service._pipeline, 0.5)
    send(handler, event(kind="image", message_id="package"))
    time.sleep(0.6)
    assert len(line.sent) == 1 and "圖片查詢｜" in flex(line)
    handler._service._pipeline = handler._service._pipeline._inner
    send(handler, event(kind="image", message_id="unknown"))  # queue 已釋放
    assert len(line.sent) == 2
