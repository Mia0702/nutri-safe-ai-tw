"""「查驗包裝宣稱」postback 的延遲修正。

Root cause：產品已由字號確認（VERIFIED），但該產品沒有核准功效 / 警語 / 成分（查驗登記 / 核備類別本來就沒有，
registration ≠ 保健功效核准），舊流程把它當成 `package_claim_declined:structured_evidence_missing` 而回到通用 llm_planner
（plan_tool_calls ×4、RAG ×3、assess ×2、官方網站 ×1，~94 秒）。

新路徑：沿用原 request 的已確認產品與 observed claims（不重跑 Controller、不讓 planner 重新找產品），
依固定 evidence priority：核准功效 → 結構化 claim-policy → 官方例示 → RAG（一次）→ 官方網站（最多一次）。
全部使用 TEST_ONLY 虛構產品；不 hard-code 任何宣稱字句。
"""

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import ASSESSMENT_STEP, PACKAGE_CLAIM_MODE, VerificationAgent
from app.pipeline import AnalysisPipeline, request_for_package_claims
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimContentSource,
    ClaimOrigin,
    ClaimSemanticCategory,
    InputType,
    Intent,
    ProductVerificationStatus,
    ResponseType,
    RiskLevel,
)
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from tests.builders import claim, extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore, web_result
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import _product

NO_CLAIM_PRODUCT = "虛構牌包裝宣稱測試錠"  # TEST_ONLY：已登記、沒有核准功效 / 警語 / 成分
GENERAL_TEXT = "TEST_ONLY 有助於維持測試機能乙"
MEDICAL_TEXT = "TEST_ONLY 可以治療虛構病"


@pytest.fixture(autouse=True)
def registered_product_without_health_claims(monkeypatch):
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [
        _product("pk-1", NO_CLAIM_PRODUCT, "c-9", "虛構登記測試股份有限公司", "Z00950")])


def label_claim(text, category=ClaimSemanticCategory.HEALTH_FUNCTION, condition=None):
    c = claim(text, category, ClaimOrigin.PRODUCT_LABEL_CLAIM, condition=condition)
    c.content_source = ClaimContentSource.IMAGE_CONTENT
    return c


def pipeline(llm):
    return AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))


def run_postback(claims, web=None, observed=None):
    """先跑一次圖片（產生已確認產品 + observed claims），再以 check_package_claims 的方式續跑；回傳續跑的結果與 llm。"""
    llm = FakeLLM()
    llm.describe_image = lambda **kw: f"TEST_ONLY 圖片文字：{NO_CLAIM_PRODUCT}"
    llm.structured[IntakeExtraction] = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[NO_CLAIM_PRODUCT],
                                                  claims=claims)
    if web is not None:
        llm.web = web
    pipe = pipeline(llm)
    first = pipe.run(RawUserInput(input_type=InputType.IMAGE,
                                  media=[MediaInput(kind="image", content=b"p", mime_type="image/jpeg")]))
    original = first.normalized_request
    if observed is not None:
        original = original.model_copy(update={"observed_package_claims": observed})
    assert original.observed_package_claims
    product = first.evidence.product_match.best_match.product
    assert first.evidence.product_match.status == ProductVerificationStatus.VERIFIED and product.product_id == "pk-1"
    first_controller = llm.calls.count(("parse_structured", "IntakeExtraction"))
    llm.calls.clear()
    result = pipe.resume_with_request(request_for_package_claims(original, product, "rid-2"))
    return result, llm, first_controller


def names(result):
    return [t.tool_name for t in result.evidence.tools_called]


def generic_plan_calls(llm):
    """通用多輪 planner（內部工具）；只允許官方網站查詢語句的那一輪。"""
    return [c for c in llm.calls if c[0] == "plan_step" and c[1] != ["search_tw_official_web"]]


GENERAL = [label_claim(GENERAL_TEXT), label_claim("TEST_ONLY 補充每日所需營養", ClaimSemanticCategory.NUTRIENT_SUPPLEMENT)]


# ============================================================ 13. 已知產品 + 包裝宣稱：不重新規劃 identity


def test_known_product_with_package_claims_does_not_replan_identity():
    result, llm, _ = run_postback(GENERAL)
    bundle = result.evidence
    assert bundle.product_match.status == ProductVerificationStatus.VERIFIED
    assert bundle.product_match.best_match.product.product_id == "pk-1"
    assert bundle.planner_mode == PACKAGE_CLAIM_MODE
    assert generic_plan_calls(llm) == []
    assert all(t.arguments.get("allowed_tools") == ["search_tw_official_web"]
               for t in bundle.tools_called if t.tool_name == "plan_tool_calls")
    assert names(result).count("lookup_product") <= 1 and names(result).count("lookup_permit") <= 1
    assert llm.calls.count(("parse_structured", "IntakeExtraction")) == 0  # 不重跑 Controller


# ============================================================ 14. 沒有核准功效 ≠ 通用 planner loop


def test_registered_product_without_approved_claims_does_not_cause_generic_planner_loop():
    result, llm, _ = run_postback(GENERAL)
    bundle = result.evidence
    assert not bundle.approved_claims and not bundle.warnings  # 預期情況
    assert bundle.fast_path_reason != "package_claim_declined:structured_evidence_missing"
    assert generic_plan_calls(llm) == []
    assert bundle.planner_mode == PACKAGE_CLAIM_MODE
    assert result.response is not None and len(result.claim_assessment) == len(GENERAL)


def test_no_approved_claim_does_not_imply_the_claim_is_illegal():
    result, _, _ = run_postback(GENERAL)
    assert all(c.status != ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM for c in result.claim_assessment)
    assert result.risk.risk_level != RiskLevel.HIGH


# ============================================================ 15. 結構化 policy 已決定 → 不查 web


def test_structured_policy_decision_needs_no_official_web():
    medical = label_claim(MEDICAL_TEXT, ClaimSemanticCategory.MEDICAL_EFFICACY, condition="虛構病")
    result, llm, _ = run_postback([label_claim(GENERAL_TEXT)], observed=[medical])
    assert not llm.called("web_search_structured") and "search_tw_official_web" not in names(result)
    assert generic_plan_calls(llm) == []


# ============================================================ 16. 無法由結構化資料決定 → 受控 fallback


def test_unresolved_claim_uses_controlled_fallback_only():
    result, llm, _ = run_postback(GENERAL)
    tools = names(result)
    assert tools.count(ASSESSMENT_STEP) <= 2  # 最多一次評估，加上官方網站有新證據時再評估一次
    assert tools.count("search_rag") <= len(GENERAL)  # 每則宣稱最多一次 RAG 查詢，沒有重複
    assert generic_plan_calls(llm) == []
    assert result.evidence.planner_mode == PACKAGE_CLAIM_MODE


# ============================================================ 17. 官方網站最多一次


def test_official_web_fallback_runs_at_most_once():
    found = web_result([{"title": "TEST_ONLY 官方頁", "url": "https://fda.gov.tw/test", "snippet": "TEST_ONLY 說明",
                         "relevance": 0.9}], ["https://fda.gov.tw/test"])
    result, llm, _ = run_postback(GENERAL, web=found)
    assert names(result).count("search_tw_official_web") <= 1
    assert sum(1 for c in llm.calls if c[0] == "web_search_structured") <= 1
    assert generic_plan_calls(llm) == []


# ============================================================ 18. 既有醫療宣稱 HIGH 行為不變


def test_medical_package_claim_through_postback_is_still_high():
    medical = label_claim(MEDICAL_TEXT, ClaimSemanticCategory.MEDICAL_EFFICACY, condition="虛構病")
    result, llm, _ = run_postback([label_claim(GENERAL_TEXT)], observed=[medical])
    assert [c.status for c in result.claim_assessment] == [ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM]
    assert result.risk.risk_level == RiskLevel.HIGH and result.response.response_type == ResponseType.RISK
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED  # 產品真偽與宣稱風險分開


# ============================================================ 其他：不重跑 Controller / 只在 postback 使用新條件


def test_postback_request_is_marked_and_original_request_is_not():
    result, _, first_controller = run_postback(GENERAL)
    assert result.normalized_request.package_claim_check and result.normalized_request.claims_from_package
    assert first_controller == 1  # 第一次圖片跑一次 Controller；postback 沒有再跑（上面 llm.calls 已清空後沒有 Controller）


def test_non_postback_package_claims_keep_existing_full_flow_when_evidence_is_missing():
    """未經 postback 的包裝宣稱（例如使用者明確要求查驗）：沒有核准功效 / 警語時仍回到既有流程（frozen 行為不變）。"""
    llm = FakeLLM()
    llm.describe_image = lambda **kw: f"TEST_ONLY 圖片文字：{NO_CLAIM_PRODUCT}"
    llm.structured[IntakeExtraction] = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[NO_CLAIM_PRODUCT],
                                                  claims=GENERAL)
    raw = RawUserInput(input_type=InputType.MIXED, text="TEST_ONLY 幫我查這個包裝說的是真的嗎",
                       media=[MediaInput(kind="image", content=b"p", mime_type="image/jpeg")])
    result = pipeline(llm).run(raw)
    assert not result.normalized_request.package_claim_check
    assert result.evidence.fast_path_reason == "package_claim_declined:structured_evidence_missing" or \
        result.evidence.planner_mode != PACKAGE_CLAIM_MODE
