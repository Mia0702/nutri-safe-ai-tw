"""Approved-function 提問的 deterministic fast path（保健功效目錄 → 產品核准功效）回歸測試。

條件：產品 VERIFIED、USER_QUESTION、問句（去掉產品名稱後）逐字出現唯一一個官方功效名稱、該功效就是產品核准功效類別、
Agent A 的 target 沒有指向另一個官方功效 → APPROVED_SCOPE_CONSISTENT，不跑 RAG / assess / web / planner。
部分重疊、多個功效、對應不一致、產品沒有該功效 → 維持保守的 evidence path。

全部使用虛構產品（synthetic_data p-1：虛構牌晶亮測試膠囊，核准功效「測試功效甲」）與虛構功效目錄。
"""

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
from app.schemas.user_request import IntakeExtraction, RawUserInput
from tests.builders import claim, extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.unit.test_claim_policy_runtime import FUNCTIONS, FakeClaimPolicyRepo

PRODUCT = "虛構牌晶亮測試膠囊"
FUNCTION = "測試功效甲"  # p-1 的核准功效類別
OTHER = "測試功效乙"  # 目錄中有、但 p-1 沒有核准的功效
SLOW = {"search_rag", "search_tw_official_web", "plan_tool_calls", ASSESSMENT_STEP}
R = AdaptiveLineRenderer()


def _fn(fid, name):
    return dict(FUNCTIONS[0], function_id=fid, official_function_name=name, official_definition=f"TEST_ONLY {name} 定義")


class CatalogRepo(FakeClaimPolicyRepo):
    """虛構保健功效目錄：含 p-1 的核准功效、另一個未核准功效，以及一個剛好是品名片段的功效名稱。"""

    def list_health_functions(self):
        return [_fn("HF_TA", FUNCTION), _fn("HF_TB", OTHER), _fn("HF_TC", "晶亮測試")]


def ask(text, target=None, repo=None, intent=Intent.CLAIM_VERIFICATION, medical=MedicalAssertionType.NONE):
    category = ClaimSemanticCategory.HEALTH_FUNCTION if medical == MedicalAssertionType.NONE \
        else ClaimSemanticCategory.MEDICAL_EFFICACY
    q = claim(text, category, ClaimOrigin.USER_QUESTION, function=target,
              condition=None if medical == MedicalAssertionType.NONE else "虛構病")
    q.medical_assertion = medical
    ex = extraction(intent=intent, product_candidates=[PRODUCT], claims=[q])
    llm = FakeLLM(structured={IntakeExtraction: lambda _: ex.model_copy(deep=True)})
    pipe = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm,
                                   claim_policy_repo=CatalogRepo() if repo is None else repo),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    return pipe.run(RawUserInput(input_type=InputType.TEXT, text=text)), llm


def tools(result):
    return [t.tool_name for t in result.evidence.tools_called]


# ================================================================ fast path


@pytest.mark.parametrize("target", [None, FUNCTION, "TEST_ONLY 虛構機能保健"])  # target 可為空、相同或 LLM 改寫
def test_single_catalog_function_matching_product_approved_function_is_deterministic(target):
    result, _ = ask(f"{PRODUCT}可以{FUNCTION}嗎？", target)
    bundle = result.evidence
    ca = bundle.claim_assessment[0]
    assert ca.origin == ClaimOrigin.USER_QUESTION
    assert ca.policy.policy_result == ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT
    assert ca.policy.matched_function_name == FUNCTION
    assert bundle.planner_mode == CLAIM_COMPARISON_MODE
    assert bundle.fast_path_reason == "claim_comparisons_assessed:structured_policy_deterministic"
    assert not (set(tools(result)) & SLOW)  # no RAG / assessment / web / planner
    assert result.response.claim_risk_level == RiskLevel.LOW


def test_catalog_match_uses_function_named_in_text_not_the_product_name():
    """品名「虛構牌晶亮測試膠囊」含目錄功效名稱「晶亮測試」：去掉品名後只剩「測試功效甲」，仍是唯一功效。"""
    result, _ = ask(f"{PRODUCT}可以{FUNCTION}嗎？", "TEST_ONLY 改寫")
    assert result.evidence.claim_assessment[0].policy.match_method == "health_function_catalog"
    assert not (set(tools(result)) & SLOW)


# ================================================================ 保守的 evidence path


@pytest.mark.parametrize("text, target", [
    (f"{PRODUCT}可以測試功效嗎？", "測試功效"),  # partial overlap：問句沒有完整官方功效名稱
    (f"{PRODUCT}可以{FUNCTION}和{OTHER}嗎？", None),  # multiple functions
    (f"{PRODUCT}可以{FUNCTION}嗎？", OTHER),  # ambiguous mapping：target 指向另一個官方功效
    (f"{PRODUCT}可以{OTHER}嗎？", OTHER),  # 產品沒有這個核准功效（沒有 product→HealthFunction 對應）
])
def test_non_deterministic_function_questions_keep_evidence_path(text, target):
    result, _ = ask(text, target)
    assert ASSESSMENT_STEP in tools(result)
    assert result.evidence.fast_path_reason != "claim_comparisons_assessed:structured_policy_deterministic"
    ca = result.evidence.claim_assessment[0]
    assert not (ca.policy and ca.policy.match_method == "health_function_catalog")


def test_without_corpus_paraphrased_target_keeps_evidence_path():
    class NoCorpus(FakeClaimPolicyRepo):
        def corpus_available(self):
            return False
    result, _ = ask(f"{PRODUCT}可以{FUNCTION}嗎？", "TEST_ONLY 改寫", repo=NoCorpus())
    assert ASSESSMENT_STEP in tools(result)


# ================================================================ Primary Flex


def test_primary_card_lists_the_approved_function_once():
    result, llm = ask(f"{PRODUCT}可以{FUNCTION}嗎？", "TEST_ONLY 改寫")
    p = result.response
    assert p.response_type == ResponseType.CLAIM_RESULT
    assert "官方核准的保健功效範圍內" in p.summary
    assert p.key_points == [f"官方核准功效：{FUNCTION}"]
    card_texts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                card_texts.append(node["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(R.render_primary(p))
    assert sum(FUNCTION in t for t in card_texts) == 1
    assert not any("官方核准：" in t or "（官方核准功效" in t for t in card_texts)
    # 逐條判定仍在「為什麼」（同一份 ClaimAssessment）
    why = ReporterAgent(llm).detail(ResponseAction.WHY, result.normalized_request, result.evidence, result.risk)
    assert any(FUNCTION in line and "你問的" in line for line in why.body)


# ================================================================ 其他 fast path 不退化


def test_treatment_question_fast_path_not_regressed():
    result, _ = ask(f"{PRODUCT}可以治療虛構病嗎？", medical=MedicalAssertionType.TREATS_OR_CURES_DISEASE)
    ca = result.evidence.claim_assessment[0]
    assert ca.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM and ca.claim_risk == RiskLevel.HIGH
    assert not (set(tools(result)) & SLOW)


def test_open_efficacy_inquiry_fast_path_not_regressed():
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT])
    llm = FakeLLM(structured={IntakeExtraction: lambda _: ex.model_copy(deep=True)})
    pipe = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm,
                                   claim_policy_repo=CatalogRepo()),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    result = pipe.run(RawUserInput(input_type=InputType.TEXT, text=f"{PRODUCT}有什麼核准功效？"))
    assert result.evidence.planner_mode == "structured_fast_path" and not (set(tools(result)) & SLOW)
    assert result.response.response_type == ResponseType.PRODUCT_INFO and result.response.risk_level is None
