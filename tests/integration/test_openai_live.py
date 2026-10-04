"""真實呼叫 OpenAI 的 integration tests（Architecture Correction Gate 驗證）。

驗證的是「能力」：statement role、context 解析、受控 tool planning、Evidence Relevance Gate。
資料面使用 TEST_ONLY in-memory fixture，不寫入任何正式資料庫。
"""

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import DISCLAIMER_NOT_FOUND, RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimOrigin,
    ClaimSemanticCategory,
    InputType,
    ProductReferenceStatus,
    ProductVerificationStatus,
    RiskLevel,
)
from app.schemas.user_request import RawUserInput
from app.services.context_store import InMemoryConversationStore
from app.tools.official_web_tools import TW_OFFICIAL_DOMAINS, is_allowed_official_url, search_tw_official_web
from tests.fakes import InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore, web_result
from tests.unit.test_claim_policy_runtime import FakeClaimPolicyRepo  # 虛構 claim-policy corpus（TEST_ONLY）

pytestmark = pytest.mark.integration

PRODUCT_1 = "虛構牌晶亮測試膠囊"
PRODUCT_2 = "虛構牌順暢測試優酪乳"


def pipeline(llm, store=None, rag=None):
    return AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), rag or InMemoryRagStore(), llm),
        risk_engine=RiskEngine(),
        reporter=ReporterAgent(llm),
        conversation_store=store,
    )


def show(title, result):
    req = result.normalized_request
    print(f"\n===== {title} =====")
    print("reference:", req.product_reference_status.value, "| products:", req.product_candidates)
    print("claims:", [(c.claim_text, c.origin.value, c.semantic_category.value) for c in req.claims])
    print("planner:", result.evidence.planner_mode,
          [(t.tool_name, t.planned_by, t.ok) for t in result.tools_called if t.tool_name != "plan_tool_calls"])
    print("product:", result.product_verification.status.value,
          "| claim_assessment:", [(c.status.value, c.rule_ids) for c in result.claim_assessment])
    print("risk:", result.risk.risk_level.value, [h.rule_id for h in result.risk.triggered_rules])
    print("rejected evidence:", [(r.evidence_id, r.reason) for r in result.evidence.rejected_evidence])
    print("--- final_answer ---\n" + result.final_answer)


def run(llm, text, **kw):
    store = kw.pop("store", None)
    return pipeline(llm, store=store, rag=kw.pop("rag", None)).run(
        RawUserInput(input_type=InputType.TEXT, text=text), conversation_id=kw.pop("conversation_id", None)
    )


# ---------------------------------------------------------------- 1. 單純詢問


def test_1_user_question_about_disease_is_not_an_advertised_claim(openai_service):
    """提問仍是 USER_QUESTION（不是產品方宣稱）；命題若作為產品功效宣稱屬醫療效能 → claim risk HIGH，
    產品風險不因提問升級；結構化 claim-policy（虛構 corpus）已足以決定，不需要 planner / RAG / 官方網站。"""
    llm = openai_service
    pipe = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm,
                                   claim_policy_repo=FakeClaimPolicyRepo()),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    result = pipe.run(RawUserInput(input_type=InputType.TEXT, text=f"請問{PRODUCT_1}可以治療高血壓嗎？"))
    show("1. 單純詢問產品能否治療疾病", result)
    claims = result.normalized_request.claims
    assert claims and all(c.origin == ClaimOrigin.USER_QUESTION for c in claims)
    assert result.product_verification.status == ProductVerificationStatus.VERIFIED
    assessments = result.claim_assessment
    assert all(c.origin == ClaimOrigin.USER_QUESTION and not c.is_product_side_assertion for c in assessments)
    assert any(c.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM for c in assessments)
    assert any(c.claim_risk == RiskLevel.HIGH for c in assessments)
    assert result.risk.risk_level != RiskLevel.HIGH  # 產品風險不因提問升級
    assert result.response.risk_level is None and result.response.claim_risk_level == RiskLevel.HIGH
    used = {t.tool_name for t in result.tools_called}
    assert not used & {"plan_tool_calls", "search_rag", "search_tw_official_web"}, used
    assert "產品宣稱" not in result.final_answer and "這項產品宣稱" not in result.final_answer


# ---------------------------------------------------------------- 2. 廣告文字


def test_2_explicit_advertisement_claiming_disease_cure_is_high(openai_service):
    ad = f"【限時優惠】{PRODUCT_2}，每天一瓶，三個月根治胃潰瘍，醫師都推薦！現在下單買二送一"
    result = run(openai_service, ad)
    show("2. 廣告文字宣稱治療疾病", result)
    assert any(
        c.origin in (ClaimOrigin.ADVERTISEMENT_CLAIM, ClaimOrigin.PRODUCT_LABEL_CLAIM)
        and c.semantic_category == ClaimSemanticCategory.MEDICAL_EFFICACY
        for c in result.normalized_request.claims
    )
    assert result.risk.risk_level == RiskLevel.HIGH
    assert "RISK_HIGH_PRODUCT_SIDE_MEDICAL_CLAIM" in [h.rule_id for h in result.risk.triggered_rules]


# ---------------------------------------------------------------- 3. 無 context


def test_3_reference_without_context_is_missing_product_context(openai_service):
    result = run(openai_service, "這個可以治療痛風嗎？")
    show("3. 無產品 context 的「這個可以治療 XXX 嗎？」", result)
    req = result.normalized_request
    assert req.product_reference_status == ProductReferenceStatus.MISSING_CONTEXT
    assert req.claims and all(c.origin == ClaimOrigin.USER_QUESTION for c in req.claims)
    assert result.risk.risk_level == RiskLevel.MEDIUM
    assert not result.risk.is_conclusive
    assert "RISK_MED_PRODUCT_CONTEXT_MISSING" in [h.rule_id for h in result.risk.triggered_rules]


# ---------------------------------------------------------------- 4. 有 context


def test_4_reference_resolved_from_previous_turn(openai_service):
    store = InMemoryConversationStore()
    first = run(openai_service, f"{PRODUCT_2}有官方資料嗎？", store=store, conversation_id="live-conv")
    show("4a. 前文", first)
    result = run(openai_service, "那它可以治療大腸激躁症嗎？", store=store, conversation_id="live-conv")
    show("4b. 有前文產品 context 的「那它可以治療 XXX 嗎？」", result)
    req = result.normalized_request
    assert req.product_reference_status == ProductReferenceStatus.RESOLVED_FROM_CONTEXT
    assert req.context_entities_used and req.context_entities_used[0].entity_id == "p-2"
    assert result.product_verification.status == ProductVerificationStatus.VERIFIED
    assert result.product_verification.best_match.product.product_id == "p-2"
    assert all(c.origin == ClaimOrigin.USER_QUESTION for c in req.claims)
    assert result.risk.risk_level != RiskLevel.HIGH


# ---------------------------------------------------------------- 5 / 6. Relevance Gate


class _RealLLMWithFixtureWeb:
    """真實 OpenAI（理解、planning、證據評估、回覆），只有官方網站搜尋結果使用固定 fixture。"""

    def __init__(self, real, fixture):
        self._real, self._fixture = real, fixture

    def __getattr__(self, name):
        return getattr(self._real, name)

    def web_search_structured(self, **kwargs):
        return self._fixture


UNRELATED_URL = "https://www.fda.gov.tw/TEST_ONLY/mycotoxin-monitoring"
RELATED_URL = "https://www.fda.gov.tw/TEST_ONLY/health-food-rules"
UNRELATED = {"title": "TEST_ONLY 市售食品真菌毒素監測計畫檢驗結果",
             "url": UNRELATED_URL, "snippet": "本季抽驗市售穀類及堅果製品共 120 件，檢驗黃麴毒素，結果均符合規定。"}
RELATED = {"title": "TEST_ONLY 健康食品管理相關規定",
           "url": RELATED_URL, "snippet": "健康食品之保健功效非屬治療、矯正人類疾病之醫療效能；健康食品之標示或廣告不得涉及醫療效能之內容。"}


def test_5_official_but_unrelated_web_result_is_rejected(openai_service):
    llm = _RealLLMWithFixtureWeb(openai_service, web_result([UNRELATED], [UNRELATED_URL]))
    result = run(llm, "星河宇宙超能量膠囊可以治療糖尿病嗎？", rag=InMemoryRagStore(rows=[]))
    show("5. 官方但與問題無關的 web result", result)
    assert result.evidence.official_web_search_attempted
    assert all(e.url != UNRELATED_URL for e in result.evidence.official_web_evidence)
    assert any(r.source.source_url == UNRELATED_URL for r in result.evidence.rejected_evidence)
    assert UNRELATED_URL not in result.final_answer
    assert all(src.source_url != UNRELATED_URL for src in result.response.sources)
    assert DISCLAIMER_NOT_FOUND in result.final_answer


def test_6_official_and_relevant_web_result_is_accepted(openai_service):
    llm = _RealLLMWithFixtureWeb(openai_service, web_result([RELATED, UNRELATED], [RELATED_URL, UNRELATED_URL]))
    result = run(llm, "星河宇宙超能量膠囊可以治療糖尿病嗎？", rag=InMemoryRagStore(rows=[]))
    show("6. 官方且高度相關的 web result", result)
    accepted = {e.url: e for e in result.evidence.official_web_evidence}
    assert RELATED_URL in accepted and UNRELATED_URL not in accepted
    rel = accepted[RELATED_URL].relevance
    assert rel.relevance_score >= 0.6 and rel.assessed_by == "llm"
    # 通過 gate 的證據才會出現在來源（primary payload 的 sources；按「查看資料來源」才顯示）
    assert any(src.source_url == RELATED_URL for src in result.response.sources)
    assert result.risk.risk_level != RiskLevel.HIGH  # 仍然只是使用者提問


# ---------------------------------------------------------------- 其他能力


def test_embedding_dimension(openai_service):
    [vec] = openai_service.embed(["健康食品標示規定"])
    assert len(vec) == 1536


def test_official_web_search_only_returns_allowed_domains(openai_service):
    out = search_tw_official_web(openai_service, "健康食品 許可證 查詢 食藥署")
    for e in out.evidence:
        assert is_allowed_official_url(e.url, TW_OFFICIAL_DOMAINS), e.url
