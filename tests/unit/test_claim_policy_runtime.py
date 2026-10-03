"""Claim Policy Runtime Integration 回歸測試（結構化 claim-policy corpus 接入 Agent B / Risk / Reporter）。

* Evidence priority：該產品核准功效 → 結構化規則 / 官方例示 → claim-policy RAG → 官方網站。
* evidence_use 一路保留到 EvidenceBundle；只有 DECISION_ELIGIBLE 能參與 deterministic decision。
* CONTEXT_ONLY / NEEDS_REVIEW 不單獨下結論；EXCLUDED_SUPERSEDED 不出現在 current query。
* 結構化 policy 已足夠時不查 RAG / 官方網站；primary card 不放法條（FC-E8），法規依據放下一層。

全部使用虛構規則、虛構例示與虛構產品（TEST_ONLY），不含任何真實 corpus 內容。
"""

import json

import pytest

from app.agents.evidence_tools import EVIDENCE_TOOLS
from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent, _EvidenceAssessment
from app.ingestion.claim_policy_corpus import EvidenceUse
from app.pipeline import AnalysisPipeline
from app.response.actions import ACTION_LABELS, reachable_actions
from app.response.line_renderer import AdaptiveLineRenderer
from app.risk.assessment_rules import assess_claim
from app.risk.claim_policy import StructuredPolicyContext
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimEvidenceRelation,
    ClaimOrigin,
    ClaimPolicyResult,
    ClaimPolicyType,
    ClaimSemanticCategory,
    InputType,
    Intent,
    MedicalAssertionType,
    PolicyEvidenceUse,
    ProductVerificationStatus,
    ResponseAction,
    RiskLevel,
    SourceType,
)
from app.schemas.evidence import ApprovedClaimEvidence, ClaimEvidenceLink, SourceRef
from app.schemas.user_request import IntakeExtraction, RawUserInput
from app.tools import claim_policy_tools, rag_tools
from tests.builders import claim, extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore

AD = ClaimOrigin.ADVERTISEMENT_CLAIM
HF = ClaimSemanticCategory.HEALTH_FUNCTION
M = MedicalAssertionType
PRODUCT = "虛構牌順暢測試優酪乳"  # synthetic_data p-2
APPROVED_TEXT = "經測試證實：有助於改善胃腸功能。"
SLOW = {"plan_tool_calls", "search_tw_official_web"}

# ---------------------------------------------------------------- 虛構 corpus（TEST_ONLY）

PERMIT_TEXT = "TEST_ONLY 幫助虛構機能順暢。"
MEDICAL_TEXT = "TEST_ONLY 改善虛構症候群。"
CONTEXT_TEXT = "TEST_ONLY 讓虛構機能煥然一新。"
OLD_TEXT = "TEST_ONLY 舊版虛構詞句。"
CONDITIONAL_TEXT = "TEST_ONLY 在虛構條件下有助於虛構機能。"


def _src(sid: str, status="CURRENT") -> dict:
    return {"source_id": sid, "document_title": f"TEST_ONLY 虛構法規 {sid}", "authority": "TEST_ONLY 虛構主管機關",
            "source_url": f"https://example.invalid/{sid}", "retrieved_at": "2026-01-01T00:00:00+00:00", "status": status}


def _rule(rid, effect, category, use, *, rule_category="TEST_CATEGORY", status="CURRENT", review="SOURCE_TEXT_VERIFIED"):
    return {"rule_id": rid, "rule_category": rule_category, "policy_effect": effect, "claim_category": category,
            "rule_summary_zh": f"TEST_ONLY 虛構規則摘要 {rid}", "official_text": f"TEST_ONLY 虛構條文 {rid}",
            "conditions": None, "legal_basis": "TEST_ONLY 虛構法", "article_or_attachment": f"第{rid[-1]}條",
            "effective_date": "2026-01-01", "status": status, "review_status": review, "evidence_use": use,
            "quarantine_reasons": [] if use == "DECISION_ELIGIBLE" else ["TEST_ONLY"],
            "source": _src("SRC_T_OLD" if status == "SUPERSEDED" else "SRC_T")}


def _example(eid, rid, text, etype, use):
    return {"example_id": eid, "rule_id": rid, "example_text": text, "example_type": etype, "claim_category": "TEST",
            "conditions": None, "article_or_attachment": "附件 TEST", "evidence_use": use,
            "quarantine_reasons": [] if use == "DECISION_ELIGIBLE" else ["TEST_ONLY"], "source": _src("SRC_T")}


RULES = [
    _rule("RULE_T1", "PERMITTED", "GENERAL_WELLNESS", "DECISION_ELIGIBLE"),
    _rule("RULE_T2", "PROHIBITED", "MEDICAL_EFFICACY", "DECISION_ELIGIBLE", rule_category="MEDICAL_EFFICACY"),
    _rule("RULE_T3", "NEEDS_REVIEW", "MEDICAL_EFFICACY", "CONTEXT_ONLY", status="NEEDS_REVIEW", review="NEEDS_REVIEW"),
    _rule("RULE_T4", "PERMITTED", "GENERAL_WELLNESS", "EXCLUDED_SUPERSEDED", status="SUPERSEDED"),
    _rule("RULE_T5", "RESTRICTED", "TEST", "DECISION_ELIGIBLE"),
]
EXAMPLES = [
    _example("EX_T1", "RULE_T1", PERMIT_TEXT, "PERMITTED_EXAMPLE", "DECISION_ELIGIBLE"),
    _example("EX_T2", "RULE_T2", MEDICAL_TEXT, "MEDICAL_EFFICACY_EXAMPLE", "DECISION_ELIGIBLE"),
    _example("EX_T3", "RULE_T3", CONTEXT_TEXT, "MEDICAL_EFFICACY_EXAMPLE", "CONTEXT_ONLY"),
    _example("EX_T4", "RULE_T4", OLD_TEXT, "PERMITTED_EXAMPLE", "EXCLUDED_SUPERSEDED"),
    _example("EX_T5", "RULE_T5", CONDITIONAL_TEXT, "CONDITIONAL_EXAMPLE", "DECISION_ELIGIBLE"),
    # 與產品核准功效文字相同的醫療效能例示：產品自己的核准功效必須優先
    _example("EX_T6", "RULE_T2", APPROVED_TEXT, "MEDICAL_EFFICACY_EXAMPLE", "DECISION_ELIGIBLE"),
]
FUNCTIONS = [{"function_id": "HF_T1", "official_function_name": "胃腸功能改善", "official_definition": "TEST_ONLY 虛構功效定義",
              "evaluation_method_title": "TEST_ONLY 虛構評估方法", "legal_basis": None, "effective_date": "2026-01-01",
              "status": "CURRENT", "review_status": "SOURCE_TEXT_VERIFIED", "evidence_use": "DECISION_ELIGIBLE",
              "source": _src("SRC_T")}]


class FakeClaimPolicyRepo:
    def __init__(self, available=True):
        self.available = available
        self.calls: list[str] = []

    def corpus_available(self):
        return self.available

    def list_rules(self):
        self.calls.append("rules")
        return [dict(r) for r in RULES]

    def list_examples(self):
        self.calls.append("examples")
        return [dict(e) for e in EXAMPLES]

    def list_health_functions(self):
        return [dict(f) for f in FUNCTIONS]


def _chunk(cid, use, status="CURRENT", review="SOURCE_TEXT_VERIFIED", similarity=0.85):
    return {"chunk_id": cid, "document_id": "rd-policy", "chunk_index": 0,
            "content": f"TEST_ONLY 虛構法規段落 {cid}", "similarity": similarity, "title": "TEST_ONLY 虛構法規文件",
            "source_url": "https://example.invalid/policy", "source_name": "TEST_ONLY 虛構主管機關",
            "document_type": "regulation", "is_official": True, "retrieved_at": "2026-01-01T00:00:00+00:00",
            "chunk_metadata": {"corpus": "TEST_ONLY", "source_id": "SRC_T", "status": status, "review_status": review,
                               "evidence_use": use, "article_or_section": "第9條", "effective_date": "2026-01-01",
                               "source_url": "https://example.invalid/policy#9", "extraction_method": "HTML_TEXT"}}


def ad(text, medical=M.NONE, category=HF, absolute=False):
    c = claim(text, category, AD, absolute=absolute)
    c.medical_assertion = medical
    return c


def run(ex: IntakeExtraction, *, repo=None, rag_rows=None, assessment=None):
    structured = {IntakeExtraction: lambda _: ex.model_copy(deep=True)}
    if assessment is not None:
        structured[_EvidenceAssessment] = assessment
    llm = FakeLLM(structured=structured)
    pipe = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(rag_rows), llm,
                                   claim_policy_repo=repo),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    return pipe.run(RawUserInput(input_type=InputType.TEXT, text="TEST_ONLY 廣告文字")), llm


def tools(result):
    return [t.tool_name for t in result.evidence.tools_called]


def verified_ad(*claims):
    return extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT], claims=list(claims))


def no_product_ad(*claims):
    return extraction(intent=Intent.CLAIM_VERIFICATION, claims=list(claims), needs_claim_check=True)


# ================================================================ Tools / RAG metadata


def test_policy_evidence_use_enum_matches_corpus_importer():
    assert {e.value for e in PolicyEvidenceUse} == {e.value for e in EvidenceUse}


def test_tools_preserve_evidence_use_and_exclude_superseded_by_default():
    repo = FakeClaimPolicyRepo()
    rules = claim_policy_tools.search_claim_policy_rules(repo, rule_ids=["RULE_T1", "RULE_T3", "RULE_T4"])
    assert {r.rule_id: r.evidence_use for r in rules} == {
        "RULE_T1": PolicyEvidenceUse.DECISION_ELIGIBLE, "RULE_T3": PolicyEvidenceUse.CONTEXT_ONLY}
    assert claim_policy_tools.search_claim_policy_rules(repo, rule_ids=["RULE_T4"], include_superseded=True)
    medical = claim_policy_tools.search_claim_policy_rules(
        repo, rule_categories=["MEDICAL_EFFICACY"], policy_effects=["PROHIBITED"])
    assert [r.rule_id for r in medical] == ["RULE_T2"]
    r = rules[0]
    assert r.source.source_url == "https://example.invalid/SRC_T" and r.source.is_official
    assert r.source_id == "SRC_T" and r.article_or_section == "第1條" and r.source.source_type == SourceType.STRUCTURED_DB

    examples = claim_policy_tools.search_claim_policy_examples(repo, [" test_only 幫助虛構機能順暢 ", OLD_TEXT, "不相關"])
    assert [e.example_id for e in examples] == ["EX_T1"]  # 只比正規化後完全相同；superseded 不回傳
    assert examples[0].matched_claim_texts == [" test_only 幫助虛構機能順暢 "]
    assert claim_policy_tools.search_claim_policy_examples(repo, [OLD_TEXT], include_superseded=True)

    [hf] = claim_policy_tools.lookup_health_function(repo, ["胃腸功能改善", "不存在功效"])
    assert hf.official_definition == "TEST_ONLY 虛構功效定義" and hf.evidence_use == PolicyEvidenceUse.DECISION_ELIGIBLE


def test_claim_policy_tools_are_registered_evidence_tools():
    assert {"search_claim_policy_rules", "search_claim_policy_examples", "lookup_health_function"} <= set(EVIDENCE_TOOLS)


def test_rag_metadata_preserved_and_superseded_chunks_dropped():
    rows = [_chunk("c-ok", "DECISION_ELIGIBLE"), _chunk("c-ctx", "CONTEXT_ONLY", "NEEDS_REVIEW", "NEEDS_REVIEW"),
            _chunk("c-old", "EXCLUDED_SUPERSEDED", "SUPERSEDED"),
            {**_chunk("c-bad", "DECISION_ELIGIBLE"), "chunk_metadata": {"evidence_use": "NOT_A_VALUE"}},
            {**_chunk("c-plain", "DECISION_ELIGIBLE"), "chunk_metadata": {}}]
    out = {e.chunk_id: e for e in rag_tools.search_rag(InMemoryRagStore(rows), FakeLLM(), "TEST_ONLY 查詢", top_k=10)}
    assert set(out) == {"c-ok", "c-ctx", "c-bad", "c-plain"}
    meta = out["c-ok"].policy_metadata
    assert (meta.source_id, meta.status, meta.review_status, meta.evidence_use, meta.article_or_section,
            meta.effective_date, meta.source_url) == ("SRC_T", "CURRENT", "SOURCE_TEXT_VERIFIED",
                                                      PolicyEvidenceUse.DECISION_ELIGIBLE, "第9條", "2026-01-01",
                                                      "https://example.invalid/policy#9")
    assert out["c-ok"].decision_eligible
    assert out["c-ctx"].policy_metadata.evidence_use == PolicyEvidenceUse.CONTEXT_ONLY and not out["c-ctx"].decision_eligible
    assert out["c-bad"].policy_metadata.evidence_use == PolicyEvidenceUse.CONTEXT_ONLY  # 無法解析 → 不可決策
    assert out["c-plain"].policy_metadata is None and out["c-plain"].decision_eligible  # 非 corpus 文件沿用原規則


# ================================================================ claim policy 規則層

SRC = SourceRef(source_name="TEST_ONLY 官方紀錄", source_type=SourceType.STRUCTURED_DB, is_official=True)
APPROVED = ApprovedClaimEvidence(evidence_id="ac:1", product_id="p", approved_claim_id="ac-1",
                                 function_name="測試功效甲", claim_text="有助於維持測試機能甲", source=SRC)


def _ctx(*example_ids) -> StructuredPolicyContext:
    repo = FakeClaimPolicyRepo()
    texts = [e["example_text"] for e in EXAMPLES if e["example_id"] in example_ids]
    examples = claim_policy_tools.search_claim_policy_examples(repo, texts, include_superseded=True)
    rules = claim_policy_tools.search_claim_policy_rules(repo, rule_ids=[e.rule_id for e in examples], include_superseded=True)
    medical = claim_policy_tools.search_claim_policy_rules(repo, rule_categories=["MEDICAL_EFFICACY"], policy_effects=["PROHIBITED"])
    return StructuredPolicyContext(example_matches=examples, rules={r.rule_id: r for r in rules}, medical_rules=medical)


def _assess(c, ctx, links=(), approved=False, unresolved=False):
    ids = {APPROVED.evidence_id} if approved else set()
    return assess_claim(0, c, list(links), ids, ids, True, approved_claims=[APPROVED] if approved else [],
                        product_unresolved=unresolved, structured_policy=ctx)


def test_approved_claim_beats_general_example_rules():
    """產品已確認且宣稱與核准功效語意等價：即使文字也是官方醫療效能例示，仍以產品核准功效為準。"""
    link = ClaimEvidenceLink(evidence_id="ac:1", relation=ClaimEvidenceRelation.EQUIVALENT, rationale="TEST_ONLY")
    result = _assess(ad(MEDICAL_TEXT), _ctx("EX_T2"), [link], approved=True)
    assert result.policy.policy_result == ClaimPolicyResult.APPROVED_MATCH
    assert result.policy.policy_type == ClaimPolicyType.PRODUCT_SPECIFIC_APPROVED_CLAIM
    # 通常可使用例示不能覆蓋產品自己的核准範圍：不在核准功效中 → 仍是 UNSUPPORTED，不是 PERMITTED
    other = _assess(ad(PERMIT_TEXT), _ctx("EX_T1"), approved=True)
    assert other.policy.policy_result == ClaimPolicyResult.UNSUPPORTED_CLAIM


def test_permitted_rule_example_without_product_is_consistent():
    result = _assess(ad(PERMIT_TEXT), _ctx("EX_T1"))
    assert result.status == ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL
    p = result.policy
    assert p.policy_result == ClaimPolicyResult.PERMITTED_GENERAL_WORDING
    assert p.policy_rule_ids == ["RULE_T1"] and p.matched_example_id == "EX_T1"
    assert "policy_example:EX_T1" in p.source_evidence_ids and p.sources[0].source_url == "https://example.invalid/SRC_T"


def test_prohibited_medical_example_decides_without_llm_medical_flag():
    result = _assess(ad(MEDICAL_TEXT, medical=M.NONE), _ctx("EX_T2"))
    assert result.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert result.policy.policy_type == ClaimPolicyType.OFFICIAL_MEDICAL_EFFICACY_EXAMPLE
    assert result.policy.policy_rule_ids == ["RULE_T2"]


def test_context_only_and_conditional_examples_cannot_decide():
    for ex_id, text in (("EX_T3", CONTEXT_TEXT), ("EX_T5", CONDITIONAL_TEXT)):
        result = _assess(ad(text, medical=M.NONE), _ctx(ex_id))
        assert result.status == ClaimAssessmentStatus.UNKNOWN, ex_id
        assert result.policy.policy_result == ClaimPolicyResult.INSUFFICIENT_EVIDENCE
        assert result.policy.rule_id == "POLICY_CONTEXT_ONLY_NOT_DECISIVE"
        assert result.policy.context_evidence_ids == [f"policy_example:{ex_id}"]


def test_superseded_example_never_decides_even_if_passed_in():
    result = _assess(ad(OLD_TEXT), _ctx("EX_T4"))
    assert result.policy.policy_result != ClaimPolicyResult.PERMITTED_GENERAL_WORDING


def test_without_structured_context_behavior_is_unchanged():
    assert _assess(ad(PERMIT_TEXT), None).policy.rule_id == "POLICY_NO_OFFICIAL_SUPPORT"


# ================================================================ pipeline（Agent A → B → Risk → C）


def test_pipeline_approved_claim_priority_skips_general_examples():
    """宣稱與已確認產品的核准功效文字相同（同時也是一則虛構醫療效能例示）：核准功效優先、LOW、不查例示 / RAG / web。"""
    repo = FakeClaimPolicyRepo()
    result, _ = run(verified_ad(ad(APPROVED_TEXT)), repo=repo)
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert result.evidence.claim_assessment[0].policy.policy_result == ClaimPolicyResult.APPROVED_MATCH
    assert result.risk.risk_level == RiskLevel.LOW
    assert "search_claim_policy_examples" not in tools(result)
    assert not (set(tools(result)) & (SLOW | {"search_rag"}))


def test_pipeline_prohibited_medical_example_is_high_without_rag_or_web():
    repo = FakeClaimPolicyRepo()
    result, _ = run(verified_ad(ad(MEDICAL_TEXT, medical=M.NONE)), repo=repo)
    bundle = result.evidence
    ca = bundle.claim_assessment[0]
    assert ca.policy.policy_result == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM
    assert ca.policy.policy_type == ClaimPolicyType.OFFICIAL_MEDICAL_EFFICACY_EXAMPLE
    assert result.risk.risk_level == RiskLevel.HIGH
    used = set(tools(result))
    assert {"search_claim_policy_examples", "search_claim_policy_rules"} <= used
    assert not (used & (SLOW | {"search_rag"}))
    # evidence_use 一路保留到 EvidenceBundle
    assert {e.example_id: e.evidence_use for e in bundle.claim_policy_examples} == {
        "EX_T2": PolicyEvidenceUse.DECISION_ELIGIBLE}
    assert {r.rule_id for r in bundle.claim_policy_rules} == {"RULE_T2"}
    assert [h.function_id for h in bundle.health_functions] == ["HF_T1"]  # 已確認產品核准功效類別的官方定義


def test_pipeline_disease_claim_uses_structured_medical_rule_as_source_instead_of_rag():
    repo = FakeClaimPolicyRepo()
    c = ad("TEST_ONLY 可以治療虛構病", medical=M.TREATS_OR_CURES_DISEASE, category=ClaimSemanticCategory.MEDICAL_EFFICACY)
    result, _ = run(no_product_ad(c), repo=repo)
    ca = result.evidence.claim_assessment[0]
    assert ca.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert "RULE_T2" in ca.policy.policy_rule_ids
    assert any(s.source_url == "https://example.invalid/SRC_T" for s in ca.policy.sources)
    assert not (set(tools(result)) & {"search_rag", "search_tw_official_web"})


def test_pipeline_context_only_rag_cannot_produce_legal_conclusion():
    """NEEDS_REVIEW / CONTEXT_ONLY 的 RAG chunk 即使被評為「反駁」宣稱，也不能成為 final legal evidence。"""
    def contradicts(input_text):
        payload = json.loads(input_text)
        cands = payload["candidates_to_assess"]
        assert all(c.get("evidence_use") == "CONTEXT_ONLY" for c in cands)
        return _EvidenceAssessment.model_validate({
            "relevance": [{"evidence_id": c["evidence_id"], "relevance_score": 0.95, "evidence_scope": "CLAIM_REGULATION",
                           "supports_claim": False, "rationale": "TEST_ONLY"} for c in cands],
            "claim_links": [{"claim_index": 0, "evidence_id": c["evidence_id"], "relation": "contradicts",
                             "rationale": "TEST_ONLY"} for c in cands],
        })

    rows = [_chunk("c-ctx", "CONTEXT_ONLY", "NEEDS_REVIEW", "NEEDS_REVIEW")]
    result, _ = run(no_product_ad(ad("TEST_ONLY 讓虛構機能更好")), repo=FakeClaimPolicyRepo(available=False),
                    rag_rows=rows, assessment=contradicts)
    bundle = result.evidence
    [rag] = bundle.rag_evidence  # 保留作為脈絡（metadata 完整）
    assert rag.policy_metadata.evidence_use == PolicyEvidenceUse.CONTEXT_ONLY and rag.policy_metadata.status == "NEEDS_REVIEW"
    ca = bundle.claim_assessment[0]
    assert ca.status != ClaimAssessmentStatus.CONFLICT_WITH_OFFICIAL
    assert not ca.evidence_links and rag.source not in ca.policy.sources
    assert result.risk.risk_level != RiskLevel.HIGH


def test_pipeline_needs_review_example_does_not_auto_conclude():
    result, _ = run(no_product_ad(ad(CONTEXT_TEXT, medical=M.NONE)), repo=FakeClaimPolicyRepo())
    ca = result.evidence.claim_assessment[0]
    assert ca.status == ClaimAssessmentStatus.UNKNOWN
    assert ca.policy.rule_id == "POLICY_CONTEXT_ONLY_NOT_DECISIVE"
    assert {e.evidence_use for e in result.evidence.claim_policy_examples} == {PolicyEvidenceUse.CONTEXT_ONLY}
    assert result.risk.risk_level != RiskLevel.HIGH


def test_pipeline_superseded_policy_is_excluded_from_current_query():
    rows = [_chunk("c-old", "EXCLUDED_SUPERSEDED", "SUPERSEDED")]
    result, _ = run(no_product_ad(ad(OLD_TEXT)), repo=FakeClaimPolicyRepo(), rag_rows=rows)
    bundle = result.evidence
    assert not bundle.claim_policy_examples and not bundle.rag_evidence
    assert all(r.evidence_use != PolicyEvidenceUse.EXCLUDED_SUPERSEDED for r in bundle.claim_policy_rules)
    assert bundle.claim_assessment[0].policy.policy_result != ClaimPolicyResult.PERMITTED_GENERAL_WORDING


def test_pipeline_structured_policy_avoids_unnecessary_web_fallback():
    """同一則宣稱：沒有 corpus 時要查 RAG + 官方網站；corpus 例示已能決定時兩者都不查。"""
    without, _ = run(no_product_ad(ad(PERMIT_TEXT)), repo=None, rag_rows=[])
    assert "search_tw_official_web" in tools(without)

    with_policy, _ = run(no_product_ad(ad(PERMIT_TEXT)), repo=FakeClaimPolicyRepo(), rag_rows=[])
    used = set(tools(with_policy))
    assert not (used & {"search_tw_official_web", "search_rag"})
    assert with_policy.evidence.claim_assessment[0].policy.policy_result == ClaimPolicyResult.PERMITTED_GENERAL_WORDING
    assert not with_policy.evidence.official_web_search_attempted


def test_corpus_not_applied_is_skipped_without_source_error():
    repo = FakeClaimPolicyRepo(available=False)
    result, _ = run(no_product_ad(ad(PERMIT_TEXT)), repo=repo)
    assert repo.calls == [] and not result.evidence.errors
    assert not (set(tools(result)) & {"search_claim_policy_examples", "search_claim_policy_rules"})


# ================================================================ Reporter / UI（FC-E8）


def test_primary_card_stays_summary_and_policy_basis_is_on_demand():
    repo = FakeClaimPolicyRepo()
    result, llm = run(verified_ad(ad(MEDICAL_TEXT, medical=M.NONE), ad(CONTEXT_TEXT, medical=M.NONE)), repo=repo)
    payload = result.response
    card = json.dumps(AdaptiveLineRenderer().render_primary(payload), ensure_ascii=False)
    flat = " ".join([card, payload.summary, *payload.key_points])
    assert "虛構條文" not in flat and "虛構規則摘要" not in flat and "第2條" not in flat  # 法條不進 primary card
    assert ResponseAction.WHY in payload.available_actions
    assert ResponseAction.POLICY_BASIS in reachable_actions(payload.available_actions)

    reporter = ReporterAgent(llm)
    why = reporter.detail(ResponseAction.WHY, result.normalized_request, result.evidence, result.risk)
    assert why.available_actions == [ResponseAction.POLICY_BASIS, ResponseAction.SOURCES]
    basis = reporter.detail(ResponseAction.POLICY_BASIS, result.normalized_request, result.evidence, result.risk)
    body = "\n".join(basis.body)
    assert "【第2條】TEST_ONLY 虛構規則摘要 RULE_T2" in body  # 判定用到的規則在前
    assert "不單獨作為結論" in body  # 待複核資料明確標示
    assert "TEST_ONLY 虛構功效定義" in body
    assert basis.available_actions == [ResponseAction.SOURCES]
    assert ACTION_LABELS[ResponseAction.POLICY_BASIS] == "查看法規依據"
    flex = AdaptiveLineRenderer().render_detail(basis)
    assert flex["type"] == "flex"
    sources = {s.source_url for s in result.evidence.all_sources()}
    assert "https://example.invalid/SRC_T" in sources  # 官方來源卡可列出法規來源


def test_why_card_without_policy_evidence_is_unchanged():
    result, llm = run(verified_ad(ad(MEDICAL_TEXT, medical=M.NONE)), repo=None)
    why = ReporterAgent(llm).detail(ResponseAction.WHY, result.normalized_request, result.evidence, result.risk)
    assert why.available_actions == []


@pytest.mark.parametrize("use", ["CONTEXT_ONLY", "CORROBORATION_REQUIRED"])
def test_quarantined_rule_alone_is_not_a_medical_rule_source(use):
    rule = claim_policy_tools.rule_evidence({**RULES[1], "evidence_use": use})
    ctx = StructuredPolicyContext(medical_rules=[rule])
    assert ctx.decision_medical_rules() == []
