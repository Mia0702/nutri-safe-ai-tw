"""Claim policy / advertisement claim verification 回歸測試。

* 已確認產品的官方核准功效優先於一般字面規則：包裝 / 廣告文字與核准功效相同或等價 → 不得 HIGH。
* 超出核准範圍 → EXCEEDS_APPROVED_SCOPE；明確疾病治療 → MEDICAL_TREATMENT_CLAIM → HIGH。
* 使用者提問不因問題本身 HIGH；產品未確認時，一般宣稱先請使用者確認產品，明確療效宣稱只對「宣稱」判 HIGH。
* 廣告圖片：產品識別 / 宣稱 / 其他可見文字分開，每條宣稱個別查證。
* 已確認產品 + 核准功效可直接比對：不跑 planner、不查官方網站、最多一次語意比對。

全部使用虛構產品與虛構宣稱（tests/fixtures/synthetic_data.py），不使用任何真實產品、廣告或照片。
"""

import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import CLAIM_LEVEL_HIGH_NOTE, CLAIMS_NEED_PRODUCT, ReporterAgent, claim_policy_line
from app.agents.verification_agent import (
    ASSESSMENT_STEP,
    CLAIM_COMPARISON_MODE,
    CLAIMS_PENDING_PRODUCT_MODE,
    PACKAGE_CLAIM_MODE,
    VerificationAgent,
)
from app.pipeline import AnalysisPipeline
from app.risk.assessment_rules import assess_claim
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimContentSource,
    ClaimEvidenceRelation,
    ClaimOrigin,
    ClaimPolicyResult,
    ClaimPolicyType,
    ClaimSemanticCategory,
    EvidenceScope,
    InputType,
    Intent,
    MedicalAssertionType,
    ProductVerificationStatus,
    ResponseType,
    RiskLevel,
    SourceType,
)
from app.schemas.evidence import ApprovedClaimEvidence, ClaimEvidenceLink, SourceRef
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from tests.builders import claim, extraction, user_text
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product

AD = ClaimOrigin.ADVERTISEMENT_CLAIM
LABEL = ClaimOrigin.PRODUCT_LABEL_CLAIM
HF = ClaimSemanticCategory.HEALTH_FUNCTION
MED = ClaimSemanticCategory.MEDICAL_EFFICACY
M = MedicalAssertionType

# synthetic：唯一產品，官方核准功效敘述含「改善」字樣（最容易被一般字面規則誤判的情形）
PRODUCT = "虛構牌順暢測試優酪乳"
APPROVED_TEXT = "經測試證實：有助於改善胃腸功能。"
FAMILY = "虛構牌政策測試飲"  # TEST_ONLY：同一基本品名兩個品項（產品無法唯一確認）
SLOW = {"plan_tool_calls", "search_tw_official_web"}

SRC = SourceRef(source_name="TEST_ONLY 官方紀錄", source_type=SourceType.STRUCTURED_DB,
                source_url="https://example.invalid/approved", is_official=True)
REG_SRC = SourceRef(source_name="TEST_ONLY 虛構法規", source_type=SourceType.RAG_DOCUMENT,
                    source_url="https://example.invalid/rule", is_official=True)
APPROVED = ApprovedClaimEvidence(evidence_id="ac:1", product_id="p", approved_claim_id="ac-1",
                                 function_name="測試功效甲", claim_text="有助於維持測試機能甲", source=SRC)


@pytest.fixture(autouse=True)
def family_data(monkeypatch):
    rows = [_product(f"pol-{i}", f"{FAMILY}({x})", "c-1", "虛構測試生技股份有限公司", f"Z0070{i}")
            for i, x in enumerate(("甲款", "乙款"))]
    claims = [{"product_claim_id": f"pc-{p['product_id']}", "product_id": p["product_id"], "approved_claim_id": "ac-p",
               "function_name": "測試功效甲", "claim_text": "TEST_ONLY 有助於維持測試機能甲。", **SOURCE} for p in rows]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + rows)
    monkeypatch.setattr(data, "APPROVED_CLAIMS", data.APPROVED_CLAIMS + claims)


def ad(text, category=HF, medical=M.NONE, origin=AD, absolute=False, condition=None, source=None):
    c = claim(text, category, origin, absolute=absolute, condition=condition)
    c.medical_assertion = medical
    c.content_source = source
    return c


def link(relation, eid="ac:1", rationale="TEST_ONLY"):
    return ClaimEvidenceLink(evidence_id=eid, relation=relation, rationale=rationale)


def assess(c, links=(), *, approved=True, regulation=False, unresolved=False, completed=True):
    ids = {APPROVED.evidence_id} if approved else set()
    official = ids | ({"rag:1"} if regulation else set())
    return assess_claim(
        0, c, list(links), ids, official, True,
        approved_claims=[APPROVED] if approved else [],
        regulation_sources={"rag:1": REG_SRC} if regulation else {},
        product_unresolved=unresolved, comparison_completed=completed,
    )


# ============================================================ assess_claim / claim policy（規則層）


def test_1_verified_equivalent_package_claim_is_approved_match_even_if_llm_marks_medical():
    """包裝文字與核准功效語意等價：即使 Agent A 把它標成醫療類（最壞情況），核准功效比對優先。"""
    c = ad("TEST_ONLY 幫助維持測試機能甲", MED, M.PREVENTS_OR_IMPROVES_DISEASE, origin=LABEL)
    result = assess(c, [link(ClaimEvidenceRelation.EQUIVALENT)])
    assert result.status == ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL
    p = result.policy
    assert p.policy_result == ClaimPolicyResult.APPROVED_MATCH
    assert p.policy_type == ClaimPolicyType.PRODUCT_SPECIFIC_APPROVED_CLAIM
    assert p.matched_approved_claim_text == APPROVED.claim_text and p.match_method == "semantic_equivalent"
    assert p.sources[0].source_url == SRC.source_url and p.sources[0].source_record_id == "ac-1"
    assert p.evidence_scope == EvidenceScope.PRODUCT_SPECIFIC


def test_1b_exact_approved_text_matches_deterministically_without_semantic_link():
    result = assess(ad("有助於維持 測試機能甲。", MED, M.PREVENTS_OR_IMPROVES_DISEASE), [], completed=False)
    assert result.policy.policy_result == ClaimPolicyResult.APPROVED_MATCH
    assert result.policy.match_method == "exact_text"


def test_1c_more_conservative_wording_is_scope_consistent():
    result = assess(ad("TEST_ONLY 可以幫忙維持一點測試機能甲"), [link(ClaimEvidenceRelation.SUPPORTED)])
    assert result.status == ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL
    assert result.policy.policy_result == ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT


def test_2_approved_claim_plus_extra_effect_exceeds_scope():
    diff = "TEST_ONLY 官方核准的是維持測試機能甲，宣稱另外加上增強測試機能乙"
    result = assess(ad("TEST_ONLY 維持測試機能甲還能增強測試機能乙"), [link(ClaimEvidenceRelation.EXCEEDS, rationale=diff)])
    assert result.status == ClaimAssessmentStatus.POTENTIALLY_EXAGGERATED
    p = result.policy
    assert p.policy_result == ClaimPolicyResult.EXCEEDS_APPROVED_SCOPE
    assert p.matched_approved_claim_text == APPROVED.claim_text and p.difference == diff


def test_2b_absolute_wording_on_approved_topic_exceeds_scope():
    result = assess(ad("TEST_ONLY 保證維持測試機能甲", absolute=True), [link(ClaimEvidenceRelation.SUPPORTED)])
    assert result.policy.policy_result == ClaimPolicyResult.EXCEEDS_APPROVED_SCOPE


def test_3_explicit_treatment_is_medical_even_if_comparison_says_supported():
    c = ad("TEST_ONLY 可以治療虛構病", MED, M.TREATS_OR_CURES_DISEASE, condition="虛構病")
    result = assess(c, [link(ClaimEvidenceRelation.SUPPORTED)], regulation=True)
    assert result.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    p = result.policy
    assert p.policy_type == ClaimPolicyType.STATUTORY_MEDICAL_EFFICACY_PROHIBITION
    assert p.sources == [REG_SRC] and p.evidence_scope == EvidenceScope.CLAIM_REGULATION


def test_3b_disease_claim_not_in_approved_claims_is_medical_after_comparison():
    c = ad("TEST_ONLY 可以改善虛構病", MED, M.PREVENTS_OR_IMPROVES_DISEASE, condition="虛構病")
    assert assess(c, []).policy.policy_result == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM


def test_4_user_question_about_treatment_keeps_role_with_high_proposition_risk():
    c = ad("TEST_ONLY 可以治療虛構病嗎", MED, M.TREATS_OR_CURES_DISEASE, origin=ClaimOrigin.USER_QUESTION,
           condition="虛構病")
    result = assess(c, [])
    assert result.origin == ClaimOrigin.USER_QUESTION and not result.is_product_side_assertion
    assert result.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM and result.claim_risk == RiskLevel.HIGH
    assert result.policy.policy_result == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM


def test_5_unresolved_product_ordinary_claim_is_insufficient_not_high():
    result = assess(ad("TEST_ONLY 有助於維持測試機能甲"), [], approved=False, unresolved=True)
    assert result.status == ClaimAssessmentStatus.UNKNOWN
    assert result.policy.policy_result == ClaimPolicyResult.INSUFFICIENT_EVIDENCE


def test_6_unresolved_product_explicit_treatment_is_claim_level_medical():
    c = ad("TEST_ONLY 可以治療虛構病", MED, M.TREATS_OR_CURES_DISEASE, condition="虛構病")
    result = assess(c, [], approved=False, unresolved=True)
    assert result.policy.policy_result == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM
    assert result.policy.product_identity_unresolved
    assert "不代表產品真偽" in claim_policy_line(result)


def test_8_failed_comparison_does_not_turn_approved_topic_into_high():
    """語意比對失敗時，「沒有比對結果」不能解讀成「不在核准範圍」→ INSUFFICIENT_EVIDENCE，不是 MEDICAL。"""
    c = ad("TEST_ONLY 幫助改善測試機能甲", MED, M.PREVENTS_OR_IMPROVES_DISEASE)
    result = assess(c, [], completed=False)
    assert result.policy.policy_result == ClaimPolicyResult.INSUFFICIENT_EVIDENCE
    assert result.status == ClaimAssessmentStatus.UNKNOWN


def test_8b_legacy_medical_category_without_assertion_type_still_yields_to_approved_claim():
    c = claim("TEST_ONLY 調整測試機能甲", MED, AD)  # medical_assertion 未提供：只退回 semantic_category
    assert c.medical_assertion is None
    assert assess(c, [link(ClaimEvidenceRelation.EQUIVALENT)]).policy.policy_result == ClaimPolicyResult.APPROVED_MATCH


# ============================================================ pipeline（Agent A → B → Risk → C）


def _assessment(relation: ClaimEvidenceRelation | None, rationale="TEST_ONLY"):
    from app.agents.verification_agent import _EvidenceAssessment

    def fn(input_text):
        payload = json.loads(input_text)
        approved = [r["evidence_id"] for r in payload["established_references"] if r["kind"].startswith("該產品官方核准")]
        links = [{"claim_index": c["claim_index"], "evidence_id": approved[0], "relation": relation.value,
                  "rationale": rationale} for c in payload["request"]["claims"]] if relation and approved else []
        return _EvidenceAssessment.model_validate({
            "relevance": [{"evidence_id": c["evidence_id"], "relevance_score": 0.9, "evidence_scope": "CLAIM_REGULATION",
                           "supports_claim": None, "rationale": "TEST_ONLY"} for c in payload["candidates_to_assess"]],
            "claim_links": links,
        })
    return fn


def run(ex: IntakeExtraction, relation=None, *, image: bytes | None = None, fail=None):
    from app.agents.verification_agent import _EvidenceAssessment

    llm = FakeLLM(structured={IntakeExtraction: lambda _: ex.model_copy(deep=True),
                              _EvidenceAssessment: _assessment(relation)}, fail=fail,
                  image_text=user_text(ex, "TEST_ONLY 廣告圖片文字"))
    pipe = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    if image is not None:
        raw = RawUserInput(input_type=InputType.IMAGE, media=[MediaInput(kind="image", content=image, mime_type="image/png")])
    else:
        raw = RawUserInput(input_type=InputType.TEXT, text=user_text(ex, "TEST_ONLY 廣告文字"))
    return pipe.run(raw), llm


def tools(result):
    return [t.tool_name for t in result.evidence.tools_called]


def test_pipeline_1_verified_product_label_claim_with_approved_wording_is_low_and_fast():
    """包裝文字與核准功效完全相同（含「改善」字樣）：不 HIGH、不跑 planner / web / 語意比對。"""
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT],
                    claims=[ad(APPROVED_TEXT, MED, M.PREVENTS_OR_IMPROVES_DISEASE, origin=LABEL)])
    result, llm = run(ex)
    bundle, risk = result.evidence, result.risk
    assert bundle.product_match.status == ProductVerificationStatus.VERIFIED
    assert bundle.claim_assessment[0].policy.policy_result == ClaimPolicyResult.APPROVED_MATCH
    assert risk.risk_level != RiskLevel.HIGH and risk.risk_level == RiskLevel.LOW
    assert bundle.planner_mode == CLAIM_COMPARISON_MODE
    assert not (set(tools(result)) & (SLOW | {ASSESSMENT_STEP, "search_rag"})) and not llm.called("plan_step")
    assert any("與該產品官方核准的保健功效一致" in p and APPROVED_TEXT in p for p in result.response.key_points)


def test_pipeline_2_ad_exceeding_approved_scope_one_assessment_no_planner_no_web():
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT],
                    claims=[ad("TEST_ONLY 改善胃腸功能，還能讓測試機能乙變強")])
    result, llm = run(ex, ClaimEvidenceRelation.EXCEEDS, )
    ca = result.evidence.claim_assessment[0]
    assert ca.policy.policy_result == ClaimPolicyResult.EXCEEDS_APPROVED_SCOPE
    assert result.risk.risk_level == RiskLevel.MEDIUM
    assert tools(result).count(ASSESSMENT_STEP) == 1 and not (set(tools(result)) & SLOW)
    assert any(f"官方核准的是「{APPROVED_TEXT}」" in p for p in result.response.key_points)


def test_pipeline_3_verified_product_ad_treating_fictional_disease_is_high():
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT],
                    claims=[ad("TEST_ONLY 可以治療虛構病", MED, M.TREATS_OR_CURES_DISEASE, condition="虛構病")])
    result, _ = run(ex, ClaimEvidenceRelation.EXCEEDS)
    assert result.evidence.claim_assessment[0].status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert result.risk.risk_level == RiskLevel.HIGH
    assert result.response.response_type == ResponseType.RISK
    assert not (set(tools(result)) & SLOW)  # 規則已決定結論，不查官方網站


def test_pipeline_4_user_question_is_not_high():
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT],
                    claims=[ad("TEST_ONLY 可以治療虛構病嗎", MED, M.TREATS_OR_CURES_DISEASE,
                               origin=ClaimOrigin.USER_QUESTION, condition="虛構病")])
    result, _ = run(ex)
    assert result.evidence.claim_assessment[0].origin == ClaimOrigin.USER_QUESTION
    assert result.risk.risk_level != RiskLevel.HIGH
    assert result.response.response_type == ResponseType.CLAIM_RESULT


def test_pipeline_5_ambiguous_product_ordinary_claim_asks_to_confirm_product():
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[FAMILY],
                    claims=[ad("TEST_ONLY 有助於維持測試機能甲")])
    result, llm = run(ex)
    bundle, payload = result.evidence, result.response
    assert bundle.product_match.is_ambiguous
    assert bundle.claim_assessment[0].policy.policy_result == ClaimPolicyResult.INSUFFICIENT_EVIDENCE
    assert result.risk.risk_level != RiskLevel.HIGH
    assert payload.response_type == ResponseType.MISSING_INFORMATION and payload.risk_level is None
    assert len(payload.product_choices) == 2 and CLAIMS_NEED_PRODUCT in payload.key_points
    # 產品待確認：不跑 planner / 官方網站 / 語意比對（一般宣稱在確認產品前無法比對）
    assert bundle.planner_mode == CLAIMS_PENDING_PRODUCT_MODE
    assert not (set(tools(result)) & (SLOW | {ASSESSMENT_STEP})) and not llm.called("plan_step")


def test_pipeline_6_ambiguous_product_explicit_treatment_is_claim_level_high():
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[FAMILY],
                    claims=[ad("TEST_ONLY 可以治療虛構病", MED, M.TREATS_OR_CURES_DISEASE, condition="虛構病")])
    result, _ = run(ex)
    bundle, payload = result.evidence, result.response
    assert bundle.product_match.status == ProductVerificationStatus.PARTIAL_MATCH  # 產品身份仍未確認
    assert bundle.product_match.best_match is None
    assert bundle.claim_assessment[0].policy.product_identity_unresolved
    assert result.risk.risk_level == RiskLevel.HIGH
    assert payload.response_type == ResponseType.RISK and payload.summary == CLAIM_LEVEL_HIGH_NOTE
    assert bundle.planner_mode == CLAIMS_PENDING_PRODUCT_MODE
    assert tools(result).count(ASSESSMENT_STEP) <= 1 and not (set(tools(result)) & SLOW)
    # 法規文件（RAG）通過 Relevance Gate 後作為該判定的來源
    assert bundle.claim_assessment[0].policy.sources and bundle.claim_assessment[0].policy.sources[0].is_official


def test_pipeline_7_advertisement_image_separates_product_claims_and_other_text():
    """廣告圖片：品名 / 品牌 / 標章 / 規格不是宣稱；每條宣稱各自一筆、各自查證。"""
    img = ClaimContentSource.IMAGE_CONTENT
    ex = extraction(
        intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT], brand_candidates=["虛構牌"],
        certification_marks=["TEST_ONLY 健康食品標章"], other_visible_text=["TEST_ONLY 100ml x 6 入"],
        claims=[
            ad(PRODUCT, origin=LABEL, source=img),  # LLM 誤把品名當宣稱
            ad("虛構牌", origin=LABEL, source=img),  # 品牌
            ad("TEST_ONLY 健康食品標章", origin=LABEL, source=img),  # 標章
            ad(APPROVED_TEXT, MED, M.PREVENTS_OR_IMPROVES_DISEASE, origin=LABEL, source=img),
            ad("TEST_ONLY 可以治療虛構病", MED, M.TREATS_OR_CURES_DISEASE, origin=LABEL, condition="虛構病", source=img),
        ],
    )
    result, _ = run(ex, image=b"TEST_ONLY ad image")
    req = result.normalized_request
    assert req.detected_product.product_names == [PRODUCT] and req.detected_product.brands == ["虛構牌"]
    assert req.detected_product.certification_marks == ["TEST_ONLY 健康食品標章"]
    assert PRODUCT in req.other_visible_text and "TEST_ONLY 100ml x 6 入" in req.other_visible_text
    all_claim_texts = [c.claim_text for c in req.claims + req.observed_package_claims]
    assert PRODUCT not in all_claim_texts and "虛構牌" not in all_claim_texts
    assert "TEST_ONLY 健康食品標章" not in all_claim_texts
    # 兩條疾病語意宣稱都自動查證，且各自得到不同的 policy 結果
    by_text = {c.claim_text: c.policy.policy_result for c in result.evidence.claim_assessment}
    assert by_text == {APPROVED_TEXT: ClaimPolicyResult.APPROVED_MATCH,
                       "TEST_ONLY 可以治療虛構病": ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM}
    assert result.evidence.planner_mode == PACKAGE_CLAIM_MODE
    assert result.risk.risk_level == RiskLevel.HIGH  # HIGH 來自療效宣稱，不是核准功效那一條


def test_pipeline_8_semantic_equivalent_with_medical_label_is_not_overridden():
    """核准功效的等價改寫：Agent A 標成疾病改善類 + 比對步驟判定等價 → APPROVED_MATCH，不 HIGH。"""
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT],
                    claims=[ad("TEST_ONLY 幫助改善胃腸功能", MED, M.PREVENTS_OR_IMPROVES_DISEASE)])
    result, _ = run(ex, ClaimEvidenceRelation.EQUIVALENT)
    assert result.evidence.claim_assessment[0].policy.policy_result == ClaimPolicyResult.APPROVED_MATCH
    assert result.risk.risk_level == RiskLevel.LOW
    assert tools(result).count(ASSESSMENT_STEP) == 1 and not (set(tools(result)) & SLOW)


def test_pipeline_8b_comparison_failure_is_conservative_not_high():
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT],
                    claims=[ad("TEST_ONLY 幫助改善胃腸功能", MED, M.PREVENTS_OR_IMPROVES_DISEASE)])
    from app.agents.verification_agent import _EvidenceAssessment
    from app.services.errors import ServiceError
    from app.schemas.enums import ErrorCode

    def boom(_):
        raise ServiceError(ErrorCode.TIMEOUT, "TEST_ONLY", "openai")

    llm = FakeLLM(structured={IntakeExtraction: lambda _: ex.model_copy(deep=True), _EvidenceAssessment: boom})
    pipe = AnalysisPipeline(intake=IntakeAgent(llm), verifier=VerificationAgent(
        InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm, enable_official_web=False),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    result = pipe.run(RawUserInput(input_type=InputType.TEXT, text=user_text(ex, "TEST_ONLY")))
    assert result.evidence.claim_assessment[0].policy.policy_result == ClaimPolicyResult.INSUFFICIENT_EVIDENCE
    assert result.risk.risk_level == RiskLevel.MEDIUM


def test_reporter_detail_why_uses_policy_explanation():
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT],
                    claims=[ad("TEST_ONLY 改善胃腸功能，還能讓測試機能乙變強")])
    result, llm = run(ex, ClaimEvidenceRelation.EXCEEDS, )
    summary_input = ReporterAgent(None)  # noqa: F841 - 確保無 LLM 也能組出 deterministic 說明
    from app.agents.reporter_agent import build_evidence_summary, detail_payload
    from app.schemas.enums import ResponseAction

    why = detail_payload(ResponseAction.WHY, result.normalized_request, result.evidence, result.risk)
    assert any(f"官方核准的是「{APPROVED_TEXT}」" in line for line in why.body)
    policy = build_evidence_summary(result.normalized_request, result.evidence, result.risk)["claim_assessment"][0]["claim_policy"]
    assert policy["policy_result"] == "EXCEEDS_APPROVED_SCOPE" and policy["matched_official_approved_claim"] == APPROVED_TEXT


def test_routing_regulation_contradicting_rule_decided_claim_is_not_evidence_conflict():
    """比對步驟把療效宣稱標成「與法規相反」：結論本來就由規則決定，不算需要官方網站的證據衝突。"""
    from app.agents.verification_agent import _EvidenceAssessment

    def contradicts(input_text):
        payload = json.loads(input_text)
        rag = [c["evidence_id"] for c in payload["candidates_to_assess"]]
        return _EvidenceAssessment.model_validate({
            "relevance": [{"evidence_id": e, "relevance_score": 0.9, "evidence_scope": "CLAIM_REGULATION",
                           "supports_claim": False, "rationale": "TEST_ONLY"} for e in rag],
            "claim_links": [{"claim_index": 0, "evidence_id": e, "relation": "contradicts", "rationale": "TEST_ONLY"}
                            for e in rag],
        })

    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT],
                    claims=[ad("TEST_ONLY 可以治療虛構病", MED, M.TREATS_OR_CURES_DISEASE, condition="虛構病")])
    llm = FakeLLM(structured={IntakeExtraction: lambda _: ex.model_copy(deep=True), _EvidenceAssessment: contradicts})
    pipe = AnalysisPipeline(intake=IntakeAgent(llm), verifier=VerificationAgent(
        InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    result = pipe.run(RawUserInput(input_type=InputType.TEXT, text=user_text(ex, "TEST_ONLY")))
    assert result.evidence.planner_mode == CLAIM_COMPARISON_MODE
    assert not (set(tools(result)) & SLOW) and tools(result).count(ASSESSMENT_STEP) == 1
    assert result.risk.risk_level == RiskLevel.HIGH
