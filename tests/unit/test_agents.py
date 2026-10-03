"""三個 Agent 的離線測試（fake LLM + in-memory TEST_ONLY 資料）。"""

import hashlib
import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import INCONCLUSIVE_HEADER, RISK_HEADERS, VERIFIED_LOW_HEADER, ReporterAgent
from app.agents.verification_agent import (
    MAX_CALLS_PER_ROUND,
    MAX_PLANNER_ROUNDS,
    VerificationAgent,
    _EvidenceAssessment,
)
from app.risk.risk_engine import DISCLAIMER_NOT_FOUND, DISCLAIMER_PRODUCT_CONTEXT, RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimOrigin,
    ClaimSemanticCategory,
    EntityType,
    ErrorCode,
    InputType,
    Intent,
    ProductReferenceStatus,
    ProductVerificationStatus,
    RiskLevel,
)
from app.schemas.user_request import (
    ContextEntity,
    ConversationContext,
    ConversationTurn,
    IntakeExtraction,
    RawUserInput,
)
from tests.builders import claim, extraction, request
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore, plan, web_result

MED = ClaimSemanticCategory.MEDICAL_EFFICACY
PRODUCT_1 = "虛構牌晶亮測試膠囊"
# planner 機制測試用：safety_inquiry 不屬於 structured fast path，一定走完整 LLM planning
FULL_PATH_INTENT = Intent.SAFETY_INQUIRY


def assessment(relevance=(), links=()):
    return _EvidenceAssessment.model_validate({
        "relevance": [
            {"evidence_id": e, "relevance_score": s, "evidence_scope": scope, "supports_claim": sup, "rationale": "TEST_ONLY"}
            for e, s, scope, sup in relevance
        ],
        "claim_links": [{"claim_index": i, "evidence_id": e, "relation": r, "rationale": "TEST_ONLY"} for i, e, r in links],
    })


def agent(llm=None, repo=None, graph=None, rag=None, **kw):
    return VerificationAgent(
        official_repo=InMemoryOfficialRepo() if repo is None else repo,
        graph_repo=InMemoryGraphRepo() if graph is None else graph,
        rag_store=InMemoryRagStore() if rag is None else rag,
        llm=llm or FakeLLM(),
        **kw,
    )


def tool_names(bundle, planned_by=None):
    return [t.tool_name for t in bundle.tools_called if planned_by is None or t.planned_by == planned_by]


# ======================= Agent A：statement role / context =======================


def _intake(ex: IntakeExtraction, context: ConversationContext | None = None, text="TEST_ONLY 輸入"):
    raw = RawUserInput(input_type=InputType.TEXT, text=text, context=context or ConversationContext())
    return IntakeAgent(FakeLLM(structured={IntakeExtraction: ex})).understand("r1", raw)


def test_intake_flag_consistency_and_explicit_reference():
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[f" {PRODUCT_1} ", PRODUCT_1],
                    ingredient_candidates=["虛構素甲"], claims=[claim(category=MED, origin=ClaimOrigin.ADVERTISEMENT_CLAIM)],
                    mentioned_country_codes=["jp"])
    req = _intake(ex)
    assert req.product_candidates == [PRODUCT_1]
    assert req.product_reference_status == ProductReferenceStatus.EXPLICIT
    assert req.needs_product_lookup and req.needs_claim_check and req.needs_graph_lookup and req.needs_rag
    assert req.foreign_country_codes == ["JP"]


def test_unresolvable_reference_is_missing_context():
    ex = extraction(refers_to_unspecified_product=True, claims=[claim(category=MED)])
    req = _intake(ex)
    assert req.product_reference_status == ProductReferenceStatus.MISSING_CONTEXT
    assert req.missing_product_context and req.product_candidates == []


def test_reference_resolved_from_context_uses_known_entity_with_id():
    ctx = ConversationContext(turns=[ConversationTurn(
        user_text="TEST_ONLY 前文", entities=[ContextEntity(entity_type=EntityType.PRODUCT, name=PRODUCT_1, entity_id="p-1")]
    )])
    ex = extraction(refers_to_unspecified_product=True, context_resolved_names=[PRODUCT_1], claims=[claim(category=MED)])
    req = _intake(ex, ctx)
    assert req.product_reference_status == ProductReferenceStatus.RESOLVED_FROM_CONTEXT
    assert req.product_candidates == [PRODUCT_1]
    assert req.context_entities_used[0].entity_id == "p-1"


def test_context_resolution_rejects_names_not_in_context():
    """LLM 回傳前文清單以外的名稱時一律丟棄，不可憑空產生產品。"""
    ctx = ConversationContext(turns=[ConversationTurn(
        user_text="x", entities=[ContextEntity(entity_type=EntityType.PRODUCT, name=PRODUCT_1)])])
    ex = extraction(refers_to_unspecified_product=True, context_resolved_names=["模型自己猜的產品"])
    req = _intake(ex, ctx)
    assert req.product_reference_status == ProductReferenceStatus.MISSING_CONTEXT
    assert req.product_candidates == []


def test_intake_sends_previous_messages_and_known_entities_to_llm():
    ctx = ConversationContext(turns=[ConversationTurn(
        user_text="前一句", entities=[ContextEntity(entity_type=EntityType.PRODUCT, name=PRODUCT_1, entity_id="p-1")])])
    llm = FakeLLM(structured={IntakeExtraction: extraction()})
    IntakeAgent(llm).understand("r", RawUserInput(input_type=InputType.TEXT, text="那它呢", context=ctx))
    payload = json.loads(llm.structured_inputs[0][1])
    assert payload["previous_user_messages"] == ["前一句"]
    assert payload["known_context_entities"] == [{"type": "PRODUCT", "name": PRODUCT_1}]
    assert "p-1" not in json.dumps(payload)  # canonical ID 不交給 LLM


def test_intake_empty_input_skips_llm():
    llm = FakeLLM()
    req = IntakeAgent(llm).understand("r", RawUserInput(input_type=InputType.TEXT, text="   "))
    assert req.intent == Intent.UNCLEAR and llm.calls == []


# ======================= Agent B：受控 tool planning =======================


def test_llm_planner_chooses_tools_and_uses_ids_from_results():
    llm = FakeLLM(plans=[
        plan(("lookup_product", {"product_names": [PRODUCT_1], "brand_names": []})),
        plan(("lookup_approved_claim", {"product_ids": ["p-1"]}),
             ("query_knowledge_graph", {"query_type": "product_ingredients", "ids": ["p-1"]})),
        plan(),
    ])
    b = agent(llm=llm).verify(request(product_candidates=[PRODUCT_1], intent=FULL_PATH_INTENT))
    assert b.planner_mode == "llm_planner"
    # 產品查詢在 LLM planning 前已由 deterministic resolver 完成；planner 重複的同參數查詢被拒絕
    assert tool_names(b, "guardrail")[0] == "lookup_product"
    assert tool_names(b, "llm_planner") == ["lookup_approved_claim", "query_knowledge_graph"]
    assert any("lookup_product" in r for r in b.planner_trace[0].rejected_calls)
    assert b.product_match.status == ProductVerificationStatus.VERIFIED
    assert b.approved_claims and any(g.relation == "CONTAINS" for g in b.graph_evidence)
    # planner 已做的事，guardrail 不重做
    assert "lookup_approved_claim" not in tool_names(b, "guardrail")


def test_planner_cannot_call_unknown_tool_or_invent_ids():
    llm = FakeLLM(plans=[
        plan(("delete_database", {}),
             ("lookup_approved_claim", {"product_ids": ["invented-id"]}),
             ("search_tw_official_web", {"query": "內部階段不可用"})),
        plan(),
    ])
    b = agent(llm=llm).verify(request(product_candidates=[PRODUCT_1], intent=FULL_PATH_INTENT))
    rejected = b.planner_trace[0].rejected_calls
    assert any("delete_database" in r for r in rejected)
    assert any("lookup_approved_claim" in r and "ID" in r for r in rejected)
    assert any("search_tw_official_web" in r for r in rejected)
    assert "delete_database" not in tool_names(b)
    assert not any(t.tool_name == "search_tw_official_web" and t.planned_by == "llm_planner" and t.arguments.get("query") == "內部階段不可用"
                   for t in b.tools_called)


def test_planner_invalid_arguments_rejected():
    llm = FakeLLM(plans=[plan(("query_knowledge_graph", {"query_type": "drop_all", "ids": []})), plan()])
    b = agent(llm=llm).verify(request(product_candidates=[PRODUCT_1], intent=FULL_PATH_INTENT))
    assert "參數格式錯誤" in b.planner_trace[0].rejected_calls[0]


def test_planner_round_and_per_round_limits():
    many = [("lookup_product", {"product_names": [f"名稱{i}"], "brand_names": []}) for i in range(MAX_CALLS_PER_ROUND + 3)]
    endless = [plan(("lookup_ingredient", {"names": [f"成分{r}"]})) for r in range(MAX_PLANNER_ROUNDS + 5)]
    llm = FakeLLM(plans=[plan(*many)] + endless)
    b = agent(llm=llm).verify(request(product_candidates=[PRODUCT_1], intent=FULL_PATH_INTENT))
    assert len(b.planner_trace[0].planned_calls) == MAX_CALLS_PER_ROUND
    assert len(b.planner_trace[0].rejected_calls) == 3
    planner_rounds = [t for t in b.tools_called if t.tool_name == "plan_tool_calls" and t.arguments.get("allowed_tools") != ["search_tw_official_web"]]
    assert len(planner_rounds) == MAX_PLANNER_ROUNDS


def test_duplicate_planned_call_is_not_executed_twice():
    same = ("lookup_product", {"product_names": [PRODUCT_1], "brand_names": []})
    llm = FakeLLM(plans=[plan(same), plan(same), plan()])
    b = agent(llm=llm).verify(request(product_candidates=[PRODUCT_1]))
    assert tool_names(b).count("lookup_product") == 1


def test_guardrails_fill_gaps_when_planner_skips_required_checks():
    """planner 什麼都不做時，必要查證仍由 deterministic guardrail 完成。"""
    req = request(product_candidates=[PRODUCT_1], claims=[claim("有助於維持測試機能甲")], needs_graph_lookup=True)
    b = agent(llm=FakeLLM(plans=[plan()])).verify(req)
    guard = tool_names(b, "guardrail")
    for required in ("lookup_product", "lookup_approved_claim", "lookup_product_warnings", "search_rag", "query_knowledge_graph"):
        assert required in guard
    assert b.product_match.status == ProductVerificationStatus.VERIFIED


def test_planner_failure_falls_back_to_deterministic_guardrails():
    llm = FakeLLM(fail={"plan_step"})
    b = agent(llm=llm).verify(request(product_candidates=[PRODUCT_1], intent=FULL_PATH_INTENT))
    assert b.planner_mode == "deterministic_fallback"
    assert b.product_match.status == ProductVerificationStatus.VERIFIED
    assert all(e.source != "openai" or e.code != ErrorCode.TIMEOUT for e in b.errors)  # planner 失敗不算資料缺漏


def test_alias_resolves_brand_to_company_without_hardcoding():
    llm = FakeLLM(plans=[plan(("lookup_company", {"names": ["虛構牌"]})), plan()])
    b = agent(llm=llm).verify(request(brand_candidates=["虛構牌"]))
    assert b.product_match.status == ProductVerificationStatus.PARTIAL_MATCH
    assert any("虛構測試生技股份有限公司" in n for n in b.product_match.notes)


# ======================= Agent B：Evidence Relevance Gate =======================


def _web_id(url):
    return "web:" + hashlib.sha1(url.encode()).hexdigest()[:16]


def test_official_but_unrelated_web_result_is_rejected():
    url = "https://www.fda.gov.tw/TEST_ONLY/unrelated-monitoring-list"
    llm = FakeLLM(
        web=web_result([{"title": "TEST_ONLY 某監測結果清單", "url": url, "snippet": "與問題無關的檢驗清單"}], [url]),
        structured={_EvidenceAssessment: assessment(relevance=[(_web_id(url), 0.15, "UNRELATED", None)])},
    )
    b = agent(llm=llm, rag=InMemoryRagStore(rows=[])).verify(request(product_candidates=["完全查不到的名字"]))
    assert b.official_web_search_attempted
    assert b.official_web_evidence == []
    assert b.rejected_evidence[0].evidence_id == _web_id(url)
    assert all(s.source_url != url for s in b.all_sources())


def test_official_and_relevant_web_result_enters_bundle_with_relevance():
    url = "https://www.fda.gov.tw/TEST_ONLY/related-rule"
    llm = FakeLLM(
        web=web_result([{"title": "TEST_ONLY 相關規範", "url": url, "snippet": "食品不得宣稱醫療效能"}], [url]),
        structured={_EvidenceAssessment: assessment(
            relevance=[(_web_id(url), 0.9, "CLAIM_REGULATION", False)],
            links=[(0, _web_id(url), "contradicts")],
        )},
    )
    req = request(product_candidates=["完全查不到的名字"], claims=[claim("可以治療某疾病", MED, ClaimOrigin.ADVERTISEMENT_CLAIM)])
    b = agent(llm=llm, rag=InMemoryRagStore(rows=[])).verify(req)
    assert [e.url for e in b.official_web_evidence] == [url]
    rel = b.official_web_evidence[0].relevance
    assert rel.relevance_score == 0.9 and rel.evidence_scope == "CLAIM_REGULATION" and rel.supports_claim is False
    assert b.official_web_evidence[0].source.source_type == "official_web"
    assert b.claim_assessment[0].evidence_links


def test_rag_chunk_without_relevance_assessment_is_not_citable():
    llm = FakeLLM(fail={"parse_structured"})
    b = agent(llm=llm).verify(request(claims=[claim(category=MED)], needs_rag=True))
    assert b.rag_evidence == []
    assert b.rejected_evidence and "未完成相關性評估" in b.rejected_evidence[0].reason
    assert any(e.code == ErrorCode.TIMEOUT for e in b.errors)  # 評估失敗要如實回報


def test_official_web_only_after_internal_evidence_insufficient():
    req = request(product_candidates=[PRODUCT_1], claims=[claim("有助於維持測試機能甲")])
    llm = FakeLLM(structured={_EvidenceAssessment: assessment(links=[(0, "approved_claim:pc-1", "supported")])})
    b = agent(llm=llm).verify(req)
    assert b.claim_assessment[0].status == ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL
    assert "search_tw_official_web" not in tool_names(b)
    assert not llm.called("web_search_structured")


def test_web_query_is_planned_by_llm_but_limited_to_one_search():
    llm = FakeLLM(web_plans=[plan(("search_tw_official_web", {"query": "planner 決定的查詢"}),
                                  ("search_tw_official_web", {"query": "第二次"}))])
    b = agent(llm=llm, rag=InMemoryRagStore(rows=[])).verify(request(product_candidates=["完全查不到的名字"]))
    web_calls = [t for t in b.tools_called if t.tool_name == "search_tw_official_web"]
    assert len(web_calls) == 1 and web_calls[0].planned_by == "llm_planner"
    assert web_calls[0].arguments["query"] == "planner 決定的查詢"


# ======================= Agent B：情境 =======================


@pytest.mark.parametrize("condition", ["某慢性病", "某消化道疾病", "某眼部疾病"])
def test_question_about_disease_for_registered_product_is_not_high(condition):
    req = request(product_candidates=[PRODUCT_1], claims=[claim(f"{PRODUCT_1}可以治療{condition}", MED, ClaimOrigin.USER_QUESTION)])
    b = agent().verify(req)
    assert b.claim_assessment[0].status != ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert RiskEngine().assess(req, b).risk_level != RiskLevel.HIGH


@pytest.mark.parametrize("origin", [ClaimOrigin.ADVERTISEMENT_CLAIM, ClaimOrigin.PRODUCT_LABEL_CLAIM])
def test_advertised_disease_treatment_is_high(origin):
    req = request(product_candidates=[PRODUCT_1], claims=[claim("可以治好某疾病", MED, origin)])
    b = agent().verify(req)
    assert b.claim_assessment[0].status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert RiskEngine().assess(req, b).risk_level == RiskLevel.HIGH


def test_missing_context_does_not_query_products_and_is_medium():
    req = request(refers_to_unspecified_product=True, intent=Intent.EFFICACY_INQUIRY,
                  claims=[claim("這個可以治療某疾病", MED, ClaimOrigin.USER_QUESTION)])
    repo = InMemoryOfficialRepo()
    b = agent(repo=repo).verify(req)
    assert b.product_match.status == ProductVerificationStatus.NOT_APPLICABLE
    assert "search_products" not in repo.calls
    assert any("missing product context" in m for m in b.missing_information)
    risk = RiskEngine().assess(req, b)
    assert risk.risk_level == RiskLevel.MEDIUM and DISCLAIMER_PRODUCT_CONTEXT in risk.required_disclaimers


def test_not_found_is_medium_with_disclaimer():
    req = request(product_candidates=["完全查不到的名字"])
    b = agent().verify(req)
    risk = RiskEngine().assess(req, b)
    assert b.product_match.status == ProductVerificationStatus.NOT_FOUND
    assert risk.risk_level == RiskLevel.MEDIUM and DISCLAIMER_NOT_FOUND in risk.required_disclaimers


def test_sources_unavailable_produce_structured_errors_not_crash():
    req = request(product_candidates=[PRODUCT_1], claims=[claim()], needs_graph_lookup=True)
    b = VerificationAgent(InMemoryOfficialRepo(fail_with=ErrorCode.UNAVAILABLE), None, None, None).verify(req)
    assert b.planner_mode == "deterministic_fallback"
    assert b.product_match.status == ProductVerificationStatus.NOT_FOUND and b.product_match.lookup_completed is False
    codes = {e.code for e in b.errors}
    assert ErrorCode.UNAVAILABLE in codes and ErrorCode.NOT_CONFIGURED in codes
    assert b.claim_assessment[0].status == ClaimAssessmentStatus.UNKNOWN


# ======================= Agent C =======================


def _full(req, llm=None):
    b = agent().verify(req)
    risk = RiskEngine().assess(req, b)
    return b, risk, ReporterAgent(llm if llm is not None else FakeLLM()).report(req, b, risk)


def test_reporter_header_and_disclaimers_are_deterministic():
    _, risk, out = _full(request(product_candidates=["完全查不到的名字"]))
    assert out.text.startswith(INCONCLUSIVE_HEADER) or out.text.startswith(RISK_HEADERS[risk.risk_level])
    assert DISCLAIMER_NOT_FOUND in out.text


def test_reporter_verified_low_header_mentions_product_scope():
    _, risk, out = _full(request(product_candidates=[PRODUCT_1]))
    assert risk.risk_level == RiskLevel.LOW and out.text.startswith(VERIFIED_LOW_HEADER)
    assert "資料來源" in out.text and out.cited_source_count >= 1


def test_reporter_summary_carries_statement_role():
    req = request(product_candidates=[PRODUCT_1], claims=[claim("可以治療某疾病", MED, ClaimOrigin.USER_QUESTION)])
    llm = FakeLLM()
    _full(req, llm=llm)
    sent = json.loads([c[1] for c in llm.calls if c[0] == "generate_text"][0])
    assert sent["user_request"]["claims"][0]["origin"] == "USER_QUESTION"
    assert sent["claim_assessment"][0]["origin"] == "USER_QUESTION"


def test_reporter_falls_back_when_llm_fails():
    _, _, out = _full(request(product_candidates=[PRODUCT_1]), llm=FakeLLM(fail={"generate_text"}))
    assert out.used_fallback_template and PRODUCT_1 in out.text


def test_reporter_out_of_scope():
    req = request(intent=Intent.OUT_OF_SCOPE)
    b = agent().verify(req)
    out = ReporterAgent(FakeLLM()).report(req, b, RiskEngine().assess(req, b))
    assert "保健食品" in out.text
