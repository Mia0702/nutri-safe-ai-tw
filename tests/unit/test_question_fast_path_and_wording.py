"""產品資訊用語 / USER_QUESTION 共用結構化 claim-policy fast path / 警語列表編號 回歸測試。

* 查有官方紀錄 ≠ 產品本身低風險：單純登記 / 安全性詢問沒有命題、沒有風險發現時是產品資訊，不顯示風險燈。
* 使用者提問的命題與廣告宣稱共用同一條 evidence path：核准功效 → 結構化規則 → 例示 → RAG → web；
  規則已足以決定時不跑 RAG / 官方網站 / 語意比對。claim_role 不變、產品風險不因提問升級。
* 警語原文自帶的列表編號只在顯示時移除；EvidenceBundle 原文不變。

全部使用虛構產品（synthetic_data p-1）、虛構規則與虛構警語。
"""

import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import ASSESSMENT_STEP, CLAIM_COMPARISON_MODE, VerificationAgent
from app.pipeline import AnalysisPipeline
from app.response.line_renderer import AdaptiveLineRenderer
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimOrigin,
    ClaimPolicyResult,
    ClaimSemanticCategory,
    InputType,
    Intent,
    MedicalAssertionType,
    ResponseAction,
    ResponseType,
    RiskLevel,
)
from app.schemas.response import DetailPayload
from app.schemas.user_request import IntakeExtraction, RawUserInput
from tests.builders import claim, extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.unit.test_claim_policy_runtime import FakeClaimPolicyRepo

PRODUCT = "虛構牌晶亮測試膠囊"
FUNCTION = "測試功效甲"
R = AdaptiveLineRenderer()
SLOW = {"search_rag", "search_tw_official_web", "plan_tool_calls", ASSESSMENT_STEP}
MARKS = ("🟢", "🟠", "🔴")


def c(text, origin=ClaimOrigin.USER_QUESTION, medical=MedicalAssertionType.NONE, function=None):
    category = ClaimSemanticCategory.HEALTH_FUNCTION if medical == MedicalAssertionType.NONE \
        else ClaimSemanticCategory.MEDICAL_EFFICACY
    out = claim(text, category, origin, function=function, condition=None if medical == MedicalAssertionType.NONE else "虛構病")
    out.medical_assertion = medical
    return out


def run(*claims, intent=Intent.CLAIM_VERIFICATION, repo=None, llm_text="TEST_ONLY 白話回覆正文。"):
    ex = extraction(intent=intent, product_candidates=[PRODUCT], claims=list(claims))
    llm = FakeLLM(structured={IntakeExtraction: lambda _: ex.model_copy(deep=True)}, text=llm_text)
    pipe = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm,
                                   claim_policy_repo=repo),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    return pipe.run(RawUserInput(input_type=InputType.TEXT, text="TEST_ONLY")), llm


def tools(result):
    return [t.tool_name for t in result.evidence.tools_called]


def full_analysis(result, llm):
    return ReporterAgent(llm).detail(ResponseAction.DETAIL, result.normalized_request, result.evidence, result.risk)


# ================================================================ 1. 產品資訊用語


def test_product_registration_does_not_claim_product_is_low_risk():
    result, llm = run(intent=Intent.PRODUCT_VERIFICATION)
    assert result.risk.risk_level == RiskLevel.LOW  # Risk Engine 不變
    p = result.response
    assert p.response_type == ResponseType.PRODUCT_INFO and p.risk_level is None
    card = json.dumps(R.render_primary(p), ensure_ascii=False)
    full = "\n".join(full_analysis(result, llm).body)
    for text in (card, p.plain_text(), full):
        assert "低風險" not in text and not any(m in text for m in MARKS)
    assert "官方紀錄" in p.summary


def test_full_analysis_llm_saying_product_is_low_risk_falls_back():
    result, llm = run(intent=Intent.PRODUCT_VERIFICATION, llm_text="TEST_ONLY 這項產品屬於低風險，可以放心。")
    detail = full_analysis(result, llm)
    assert detail.used_fallback_template and "低風險" not in "\n".join(detail.body)


@pytest.mark.parametrize("intent", [Intent.SAFETY_INQUIRY, Intent.EFFICACY_INQUIRY, Intent.PRODUCT_VERIFICATION])
def test_product_info_has_no_risk_light(intent):
    result, _ = run(intent=intent)
    p = result.response
    assert p.response_type == ResponseType.PRODUCT_INFO and p.risk_level is None and p.claim_risk_level is None
    assert not any(m in json.dumps(R.render_primary(p), ensure_ascii=False) for m in MARKS)
    if intent == Intent.SAFETY_INQUIRY:
        assert any("不代表對每個人都安全" in k for k in p.key_points)


def test_real_risk_findings_still_use_risk_card(monkeypatch):
    """查證到實際風險（例如許可證失效）時仍是風險卡。"""
    monkeypatch.setattr(data, "PRODUCTS", [dict(p, status="EXPIRED") if p["product_id"] == "p-1" else p
                                           for p in data.PRODUCTS])
    result, _ = run(intent=Intent.PRODUCT_VERIFICATION)
    assert result.response.response_type == ResponseType.RISK and result.response.risk_level == RiskLevel.MEDIUM


# ================================================================ 2. USER_QUESTION 共用 claim-policy path


def test_approved_function_question_routes_through_claim_verification_deterministically():
    q = c(f"{PRODUCT}可以{FUNCTION}嗎", function=FUNCTION)
    result, _ = run(q, intent=Intent.EFFICACY_INQUIRY)  # 指定了具體功效：不是開放式功效詢問，保留為命題
    bundle = result.evidence
    assert [x.origin for x in result.normalized_request.claims] == [ClaimOrigin.USER_QUESTION]
    assert bundle.planner_mode == CLAIM_COMPARISON_MODE
    assert bundle.fast_path_reason == "claim_comparisons_assessed:structured_policy_deterministic"
    assert bundle.claim_assessment[0].policy.policy_result == ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT
    assert result.response.response_type == ResponseType.CLAIM_RESULT
    assert result.response.claim_risk_level == RiskLevel.LOW
    assert not (set(tools(result)) & SLOW)


def test_medical_user_question_with_sufficient_structured_policy_skips_rag_web_and_assessment():
    q = c("TEST_ONLY 可以治療虛構病嗎", medical=MedicalAssertionType.TREATS_OR_CURES_DISEASE)
    result, _ = run(q, repo=FakeClaimPolicyRepo())
    ca = result.evidence.claim_assessment[0]
    assert ca.origin == ClaimOrigin.USER_QUESTION and not ca.is_product_side_assertion
    assert ca.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM and ca.claim_risk == RiskLevel.HIGH
    assert "RULE_T2" in ca.policy.policy_rule_ids  # 法規來源來自 DECISION_ELIGIBLE 結構化規則
    assert result.risk.risk_level != RiskLevel.HIGH and result.response.risk_level is None
    used = set(tools(result))
    assert {"search_claim_policy_rules"} <= used and not (used & SLOW)
    assert result.evidence.fast_path_reason.endswith("structured_policy_deterministic")


def test_medical_user_question_without_corpus_keeps_previous_evidence_path():
    q = c("TEST_ONLY 可以治療虛構病嗎", medical=MedicalAssertionType.TREATS_OR_CURES_DISEASE)
    result, _ = run(q, repo=None)
    assert "search_rag" in tools(result)  # 沒有結構化規則時才往下查 RAG
    assert result.evidence.claim_assessment[0].claim_risk == RiskLevel.HIGH


def test_medical_ad_claim_uses_the_same_fast_path():
    ad = c("TEST_ONLY 可以治療虛構病", origin=ClaimOrigin.ADVERTISEMENT_CLAIM,
           medical=MedicalAssertionType.TREATS_OR_CURES_DISEASE)
    result, _ = run(ad, repo=FakeClaimPolicyRepo())
    ca = result.evidence.claim_assessment[0]
    assert ca.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM and result.risk.risk_level == RiskLevel.HIGH
    assert not (set(tools(result)) & SLOW)
    assert result.evidence.fast_path_reason.endswith("structured_policy_deterministic")


def test_disease_improvement_question_with_approved_claims_still_compares_once_but_no_rag_or_web():
    """「改善某疾病」類命題要先和核准功效語意比對（可能等價）：最多一次比對，不查 RAG / 官方網站。"""
    q = c("TEST_ONLY 可以改善虛構病嗎", medical=MedicalAssertionType.PREVENTS_OR_IMPROVES_DISEASE)
    result, _ = run(q, repo=FakeClaimPolicyRepo())
    used = tools(result)
    assert used.count(ASSESSMENT_STEP) == 1
    assert not (set(used) & {"search_rag", "search_tw_official_web", "plan_tool_calls"})


# ================================================================ 3. 警語列表編號


NUMBERED = "1.TEST_ONLY 虛構警語甲。2、TEST_ONLY 虛構警語乙。(3)TEST_ONLY 虛構警語丙。（4）每日 1.5 公克以下。"


def test_warning_numbering_is_removed_only_when_rendering(monkeypatch):
    monkeypatch.setattr(data, "WARNINGS", [dict(data.WARNINGS[0], text=NUMBERED)])
    result, llm = run(intent=Intent.PRODUCT_VERIFICATION)
    detail = ReporterAgent(llm).detail(ResponseAction.WARNINGS, result.normalized_request, result.evidence, result.risk)
    msg = R.render_detail(detail)
    shown = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "box" and node.get("layout") == "horizontal":
                shown.append([t["text"] for t in node["contents"]])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(msg)
    assert shown == [["・", "TEST_ONLY 虛構警語甲。"], ["・", "TEST_ONLY 虛構警語乙。"], ["・", "TEST_ONLY 虛構警語丙。"],
                     ["・", "每日 1.5 公克以下。"]]
    # 原始 warning evidence 不變
    assert result.evidence.warnings[0].text == NUMBERED


@pytest.mark.parametrize("line, expected", [
    ("・1.TEST_ONLY 甲", "TEST_ONLY 甲"), ("・12. TEST_ONLY 乙", "TEST_ONLY 乙"), ("・(3)TEST_ONLY 丙", "TEST_ONLY 丙"),
    ("・（4）TEST_ONLY 丁", "TEST_ONLY 丁"), ("・5、TEST_ONLY 戊", "TEST_ONLY 戊"),
    ("・1.5 公克以下", "1.5 公克以下"), ("・第1條 TEST_ONLY", "第1條 TEST_ONLY"),
])
def test_list_number_normalization_is_general_and_keeps_real_numbers(line, expected):
    msg = R.render_detail(DetailPayload(request_id="00000000-0000-4000-8000-0000000000bb",
                                        action=ResponseAction.WARNINGS, title="警語與注意事項", body=[line]))
    texts = json.dumps(msg, ensure_ascii=False)
    assert f'"text": "{expected}"' in texts
