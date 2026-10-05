"""Bare nutrient routing / clarification-before-research / Taiwan imported product scope（全部 TEST_ONLY 虛構資料）。

1. 沒有產品訊號的成分名稱 → 獨立成分詢問，不沿用前一題的已確認產品；代名詞 follow-up 仍沿用。
2. 成分名稱同時是官方產品名稱 → collision，請使用者選擇。
3. 產品只有候選（待確認）→ 不跑 planner / RAG / web / assess；選定後以原題重跑（不重跑 Controller Agent）。
4. resolver 已確認的台灣官方紀錄（含 imported_tablet_capsule）不是外國產品；沒有台灣紀錄的外國產品維持原行為。
"""

from datetime import datetime, timezone

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import CLARIFICATION_MODE, VerificationAgent
from app.pipeline import AnalysisPipeline, request_for_selected_product
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    EntityType, FunctionEvidenceStatus, InputType, Intent, ProductConfirmationSource, ProductMatchType, ProductReferenceStatus,
    ProductVerificationStatus, ResponseType,
)
from app.schemas.user_request import (
    ConfirmedProduct, ContextEntity, ConversationContext, ConversationTurn, IntakeExtraction, RawUserInput,
)
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import _product
from tests.unit.test_ingredient_retrieval_runtime import CORPUS, OVERLAY, _form, _group

NUT_X, NUT_Y = "虛構營養素甲", "虛構營養素乙"
PRODUCT_X = f"{NUT_X}膠囊"
IMPORTED = "虛構進口牌美國測試錠"
SLOW = {"search_rag", "search_tw_official_web", "assess_evidence"}


@pytest.fixture(autouse=True)
def routing_data(monkeypatch):
    rows = [_product("bn-1", PRODUCT_X, "c-51", "甲測試公司", "Z00501"),
            _product("bn-2", NUT_Y, "c-52", "乙測試公司", "Z00502"),  # 官方產品名稱與成分名稱相同（collision）
            _product("bn-3", IMPORTED, "c-53", "丙測試公司", "Z00503")]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + rows)
    entities = [_group("TN_X", "TestNutrientX", NUT_X), _group("TN_Y", "TestNutrientY", NUT_Y),
                _form("TF_Z", "TestFormZ", "虛構型態丙", "TN_X")]
    monkeypatch.setattr(data, "INGREDIENT_BRIDGE", {"corpus_version": CORPUS, "overlay_version": OVERLAY,
                                                    "entities": entities, "aliases": []})


def pipeline(llm):
    return AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))


def run(ex, text, confirmed: str | None = None):
    llm = FakeLLM(structured={IntakeExtraction: ex})
    ctx = ConversationContext()
    if confirmed:
        ctx = ConversationContext(confirmed_product=ConfirmedProduct(
            product_id="bn-1", permit_number="測試健食字第Z00501號", official_product_name=confirmed,
            confirmation_source=ProductConfirmationSource.RESOLVER_VERIFIED, confirmed_at=datetime.now(timezone.utc),
            confirmed_request_id="prev"))
    return pipeline(llm).run(RawUserInput(input_type=InputType.TEXT, text=text, context=ctx)), llm


# ---------------------------------------------------------------- 1. bare nutrient vs context


def test_exact_product_phrase_is_product_exact():
    result, _ = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT_X]), PRODUCT_X)
    m = result.evidence.product_match
    assert m.status == ProductVerificationStatus.VERIFIED and m.match_type == ProductMatchType.CANONICAL_EXACT


def test_bare_nutrient_after_confirmed_product_is_standalone_not_context_bound():
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[NUT_X])
    result, _ = run(ex, NUT_X, confirmed=PRODUCT_X)
    req = result.normalized_request
    assert req.intent == Intent.INGREDIENT_INQUIRY and req.ingredient_candidates == [NUT_X]
    assert req.product_candidates == [] and not req.context_entities_used and req.user_confirmed_target is None
    assert result.evidence.product_match.status == ProductVerificationStatus.NOT_APPLICABLE
    assert not req.needs_product_lookup and not req.permit_number_candidates


def test_bare_nutrient_without_context_is_also_standalone():
    result, _ = run(extraction(intent=Intent.UNCLEAR, product_candidates=[NUT_X]), NUT_X)
    assert result.normalized_request.intent == Intent.INGREDIENT_INQUIRY
    assert result.evidence.product_match.status == ProductVerificationStatus.NOT_APPLICABLE


def test_explicit_function_question_about_nutrient_is_standalone():
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[NUT_X])
    result, _ = run(ex, f"{NUT_X}有什麼功能？", confirmed=PRODUCT_X)
    req = result.normalized_request
    assert req.ingredient_candidates == [NUT_X] and req.product_candidates == [] and not req.context_entities_used


def test_pronoun_follow_up_still_uses_previous_product():
    ex = extraction(intent=Intent.INGREDIENT_INQUIRY, ingredient_candidates=[NUT_Y], refers_to_unspecified_product=True,
                    product_reference_status=ProductReferenceStatus.MISSING_CONTEXT)
    result, _ = run(ex, f"它有含{NUT_Y}嗎？", confirmed=PRODUCT_X)
    req = result.normalized_request
    assert req.context_entities_used and req.context_entities_used[0].name == PRODUCT_X
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED


@pytest.mark.parametrize("text", [f"{NUT_X}膠囊", f"虛構牌 {NUT_X}", f"{NUT_X} 產品", f"{NUT_X}錠"])
def test_nutrient_with_product_signal_stays_on_product_path(text):
    brand = ["虛構牌"] if "虛構牌" in text else []
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[text.replace(" ", "")],
                    brand_candidates=brand)
    result, _ = run(ex, text)
    req = result.normalized_request
    assert req.product_candidates and req.intent == Intent.PRODUCT_VERIFICATION and not req.identity_collision


def test_name_that_is_both_canonical_nutrient_and_official_product_asks_user():
    result, llm = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[NUT_Y]), NUT_Y, confirmed=PRODUCT_X)
    req, bundle = result.normalized_request, result.evidence
    assert req.identity_collision and not req.context_entities_used  # 不綁定前一題的產品
    assert bundle.planner_mode == CLARIFICATION_MODE and not llm.called("plan_step")
    assert not ({t.tool_name for t in bundle.tools_called} & SLOW)
    assert result.response.response_type == ResponseType.MISSING_INFORMATION and result.response.risk_level is None


# ---------------------------------------------------------------- 3. clarification before research


@pytest.mark.parametrize("intent", [Intent.SAFETY_INQUIRY, Intent.EFFICACY_INQUIRY, Intent.PRODUCT_VERIFICATION])
def test_unconfirmed_typo_candidate_stops_before_any_research(intent):
    typo = PRODUCT_X.replace("營", "榮")  # 單字錯字
    result, llm = run(extraction(intent=intent, product_candidates=[typo]), typo)
    bundle = result.evidence
    assert bundle.product_match.status == ProductVerificationStatus.PARTIAL_MATCH and bundle.product_match.candidates
    assert bundle.planner_mode == CLARIFICATION_MODE and not llm.called("plan_step")
    assert not ({t.tool_name for t in bundle.tools_called} & SLOW) and not bundle.official_web_evidence
    assert result.response.response_type == ResponseType.MISSING_INFORMATION and result.response.product_choices


def test_selected_candidate_resumes_original_intent_without_controller_rerun():
    typo = PRODUCT_X.replace("營", "榮")
    llm = FakeLLM(structured={IntakeExtraction: extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[typo])})
    pipe = pipeline(llm)
    first = pipe.run(RawUserInput(input_type=InputType.TEXT, text=typo))
    controller_calls = llm.calls.count(("parse_structured", "IntakeExtraction"))
    chosen = first.evidence.product_match.candidates[0].product
    resumed = pipe.resume_with_request(request_for_selected_product(first.normalized_request, chosen, "r2"))
    assert resumed.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert resumed.normalized_request.intent == Intent.EFFICACY_INQUIRY
    assert llm.calls.count(("parse_structured", "IntakeExtraction")) == controller_calls


# ---------------------------------------------------------------- 4. Taiwan imported vs foreign


def test_taiwan_registered_product_with_country_word_is_not_foreign_scope():
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[IMPORTED], mentioned_country_codes=["US"])
    result, _ = run(ex, IMPORTED)
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert result.evidence.fast_path_reason != "foreign_product"
    assert not result.normalized_request.foreign_country_codes
    assert not any("無法比對外國產品" in m for m in result.evidence.missing_information)


def test_unregistered_overseas_product_keeps_foreign_behavior():
    name = "完全沒有台灣紀錄的海外測試品"
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[name], mentioned_country_codes=["US"])
    result, _ = run(ex, name)
    assert result.evidence.product_match.status != ProductVerificationStatus.VERIFIED
    assert result.normalized_request.foreign_country_codes == ["US"]
    assert result.evidence.fast_path_reason == "foreign_product"


# ---------------------------------------------------------------- live-shaped Controller outputs（不依賴 Controller 的 intent / 產品欄位）


def run_with_turn(ex, text, confirmed=PRODUCT_X):
    """前一題確認了產品（task context + 對話前文實體），這一句才是測試輸入。"""
    llm = FakeLLM(structured={IntakeExtraction: ex})
    ctx = ConversationContext(
        turns=[ConversationTurn(user_text=PRODUCT_X, entities=[ContextEntity(entity_type=EntityType.PRODUCT, name=confirmed)])],
        confirmed_product=ConfirmedProduct(
            product_id="bn-1", permit_number="測試健食字第Z00501號", official_product_name=confirmed,
            confirmation_source=ProductConfirmationSource.RESOLVER_VERIFIED, confirmed_at=datetime.now(timezone.utc),
            confirmed_request_id="prev"))
    return pipeline(llm).run(RawUserInput(input_type=InputType.TEXT, text=text, context=ctx)), llm


def test_controller_resolving_bare_name_to_previous_product_is_overridden_by_raw_text():
    """live bug：Controller 把裸成分名稱當成前文代名詞，輸出 product_verification＋前一題產品。原文才是依據。"""
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, refers_to_unspecified_product=True,
                    context_resolved_names=[PRODUCT_X], needs_product_lookup=True)
    result, _ = run_with_turn(ex, NUT_X)
    req = result.normalized_request
    assert req.standalone_entity and req.intent == Intent.INGREDIENT_INQUIRY
    assert not req.context_entities_used and not req.product_candidates and req.user_confirmed_target is None
    assert req.product_reference_status == ProductReferenceStatus.NOT_NEEDED
    assert result.evidence.product_match.status == ProductVerificationStatus.NOT_APPLICABLE


def test_controller_product_candidate_for_bare_name_with_stray_brand_is_overridden():
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT_X], brand_candidates=["甲測試公司"])
    result, _ = run_with_turn(ex, f"{NUT_X}？")
    assert result.normalized_request.standalone_entity and not result.normalized_request.brand_candidates


def test_pronoun_question_with_same_nutrient_still_binds_previous_product():
    ex = extraction(intent=Intent.INGREDIENT_INQUIRY, ingredient_candidates=[NUT_X], refers_to_unspecified_product=True,
                    context_resolved_names=[PRODUCT_X])
    result, _ = run_with_turn(ex, f"它有含{NUT_X}嗎？")
    req = result.normalized_request
    assert not req.standalone_entity and req.context_entities_used and req.context_entities_used[0].name == PRODUCT_X


def test_bare_name_collision_overrides_controller_context_resolution():
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, refers_to_unspecified_product=True, context_resolved_names=[PRODUCT_X])
    result, llm = run_with_turn(ex, NUT_Y)
    assert result.normalized_request.identity_collision and not result.normalized_request.context_entities_used
    assert result.evidence.planner_mode == CLARIFICATION_MODE and not llm.called("plan_step")


def test_base_name_alone_does_not_turn_entity_exact_into_collision(monkeypatch):
    """官方產品「X(規格)」的基本品名等於 canonical entity：entity exact 優先，不是 collision。"""
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [_product("bn-9", f"{NUT_X}(原味)", "c-59", "己測試公司", "Z00509")])
    result, _ = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[NUT_X]), NUT_X)
    assert result.normalized_request.standalone_entity and not result.normalized_request.identity_collision


# ---------------------------------------------------------------- canonical entity identity / 沒有 legacy 成分列也不能說「找不到」


def test_canonical_entity_without_legacy_ingredient_row_is_identity_found():
    result, llm = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[NUT_X]), NUT_X)
    bundle, payload = result.evidence, result.response
    assert [e.entity_id for e in bundle.standalone_entities] == ["TN_X"]
    assert not any(i.name_zh in payload.summary + "".join(payload.key_points) for i in bundle.standalone_ingredients)  # 相似但不同的 legacy 成分不列
    assert payload.response_type == ResponseType.INGREDIENT_LIST
    text = payload.title + payload.summary + "".join(payload.key_points)
    assert "沒有找到名稱完全相同的成分" not in text and "已確認的營養素" in payload.summary
    assert bundle.function_evidence_status == FunctionEvidenceStatus.NOT_ASSESSED
    assert bundle.planner_mode == "standalone_entity" and not llm.called("plan_step")
    assert not ({t.tool_name for t in bundle.tools_called} & SLOW)  # 概覽不查功能證據


def test_unknown_name_without_entity_is_not_given_an_entity_card():
    result, _ = run(extraction(intent=Intent.INGREDIENT_INQUIRY, ingredient_candidates=["完全未知的測試名稱"]), "完全未知的測試名稱是什麼")
    assert not result.evidence.standalone_entities and not result.normalized_request.standalone_entity


def test_function_question_runs_each_evidence_tool_once_and_reports_insufficient_without_evidence():
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[NUT_X], needs_rag=True)
    result, llm = run(ex, f"{NUT_X}有什麼功能？", confirmed=PRODUCT_X)
    bundle = result.evidence
    names = [t.tool_name for t in bundle.tools_called]
    assert result.normalized_request.standalone_entity and result.evidence.product_match.status == ProductVerificationStatus.NOT_APPLICABLE
    assert bundle.planner_mode == "standalone_entity" and not llm.called("plan_step")
    assert names.count("lookup_ingredient") == 1 and names.count("search_rag") == 1
    assert names.count("assess_evidence") <= 1
    assert bundle.function_evidence_status in (FunctionEvidenceStatus.NO_FUNCTION_EVIDENCE, FunctionEvidenceStatus.SUPPORTED)
    if bundle.function_evidence_status == FunctionEvidenceStatus.NO_FUNCTION_EVIDENCE:
        assert not bundle.rag_evidence and "沒有足夠的官方證據" in "".join(result.response.key_points)


def test_existing_product_ingredient_follow_up_is_unchanged():
    ex = extraction(intent=Intent.INGREDIENT_INQUIRY, refers_to_unspecified_product=True, context_resolved_names=[PRODUCT_X])
    result, _ = run_with_turn(ex, "它有哪些成分？")
    req = result.normalized_request
    assert not req.standalone_entity and req.context_entities_used and not result.evidence.standalone_entities
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
