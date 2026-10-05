"""手機 smoke 三類問題的一般化回歸測試（全部 TEST_ONLY 虛構產品 / 成分）。

1. Exact（官方品名 / 官方別名 / 字號 / 基本品名）解析不經過 Candidate Relevance Gate；複製官方品名問成分 → VERIFIED＋成分 fast path。
2. 產品 vs 成分路由：「成分＋劑型」片語、只有一個名稱的訊息 → 產品查詢；明確的成分問題 → 成分查詢；
   成分名稱查詢的結果不會被當成任何產品的成分，也不列名稱相似但不同的成分。
3. 短品名單字錯字：保留為候選（只請使用者確認，不自動確認）；營養素字樣不同、同品牌不同核心、未知前綴仍不列。
"""

import pytest

from app.agents.intake_agent import BARE_NAME_NOTE, PRODUCT_PHRASE_NOTE, IntakeAgent
from app.agents.reporter_agent import ReporterAgent, compose_payload, detail_payload
from app.agents.verification_agent import FAST_PATH_MODE, VerificationAgent
from app.pipeline import AnalysisPipeline
from app.risk import assessment_rules
from app.risk.assessment_rules import apply_candidate_relevance_gate
from app.risk.candidate_relevance import missed_core_terms
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    InputType,
    Intent,
    ProductAmbiguityReason,
    ProductMatchType,
    ProductVerificationStatus,
    ResponseAction,
    ResponseType,
)
from app.schemas.evidence import (
    OFFICIAL_ALIAS_MATCHED_BY,
    EvidenceBundle,
    IngredientRecord,
    ProductCandidateMatch,
    ProductMatch,
    SourceRef,
)
from app.schemas.enums import SourceType
from app.schemas.user_request import IntakeExtraction, RawUserInput
from app.tools.product_tools import _product_from_row
from tests.builders import extraction, request
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore, plan
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product

HF_NAME = "虛構牌晶亮測試膠囊"  # synthetic_data 既有產品（有既有成分「虛構素甲」）
NUTRIENT = "虛構素乙"           # TEST_ONLY 虛構成分（官方成分表中有這個名稱）
SHORT = "虛測茶"                # TEST_ONLY 3 字官方品名


@pytest.fixture(autouse=True)
def routing_data(monkeypatch):
    rows = [_product("rt-1", f"甲廠{NUTRIENT}膠囊", "c-41", "甲廠測試公司", "Z00401"),
            _product("rt-2", f"乙廠{NUTRIENT}膠囊", "c-42", "乙廠測試公司", "Z00402"),
            _product("rt-3", SHORT, "c-43", "丙廠測試公司", "Z00403"),
            _product("rt-4", "QXZ", "c-44", "丁廠測試公司", "Z00404"),
            _product("rt-5", "虛構甲牌維生素A軟膠囊", "c-45", "戊廠測試公司", "Z00405")]
    ingredients = [{"id": "i-b", "name_zh": NUTRIENT, "name_en": None, "name_normalized": NUTRIENT, **SOURCE}]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + rows)
    monkeypatch.setattr(data, "INGREDIENTS", data.INGREDIENTS + ingredients)


def run(text: str, ex, plans=None):
    llm = FakeLLM(structured={IntakeExtraction: ex}, plans=plans)
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    return pipeline.run(RawUserInput(input_type=InputType.TEXT, text=text)), llm


def tools(result) -> set[str]:
    return {t.tool_name for t in result.evidence.tools_called}


@pytest.fixture
def gate_spy(monkeypatch):
    calls = []
    real = assessment_rules.supports_product_core
    monkeypatch.setattr(assessment_rules, "supports_product_core", lambda *a, **k: calls.append(a) or real(*a, **k))
    return calls


def _cm(pid, score=0.6, matched_by="product_name", query=""):
    row = next(p for p in data.PRODUCTS if p["product_id"] == pid)
    return ProductCandidateMatch(product=_product_from_row(row), match_score=score, matched_by=matched_by, query=query)


# ============================================================ 1. exact 不經過 gate


def test_exact_canonical_name_copied_from_db_with_ingredient_question_is_verified(gate_spy):
    result, llm = run(f"{HF_NAME}有哪些成分？", extraction(
        intent=Intent.INGREDIENT_INQUIRY, product_candidates=[HF_NAME], brand_candidates=["虛構牌"], needs_product_lookup=True))
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.VERIFIED and match.match_type == ProductMatchType.CANONICAL_EXACT
    assert match.filtered_candidate_count == 0 and gate_spy == []  # gate 沒有參與 eligibility
    assert result.evidence.planner_mode == FAST_PATH_MODE and result.evidence.product_ingredients is not None
    assert result.response.response_type == ResponseType.INGREDIENT_LIST and "虛構素甲" in " ".join(result.response.key_points)
    assert not llm.called("plan_step")


@pytest.mark.parametrize("kind", ["alias", "permit", "base_name"])
def test_alias_permit_and_base_name_resolution_bypass_the_gate(kind, gate_spy, monkeypatch):
    if kind == "alias":
        req = request(product_candidates=["星河別名"])
        matches, permits = [_cm("rt-1", 1.0, OFFICIAL_ALIAS_MATCHED_BY, "星河別名"), _cm("rt-2")], []
        expected = ProductMatchType.OFFICIAL_ALIAS_EXACT
    elif kind == "permit":
        req = request(permit_number_candidates=["Z00402"])
        matches, permits = [_cm("rt-1")], [_cm("rt-2", 1.0, "permit_number")]
        expected = ProductMatchType.PERMIT_NUMBER
    else:
        monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [_product("rt-9", f"{SHORT}(檸檬)", "c-43", "丙廠測試公司", "Z00409")])
        req = request(product_candidates=[f"{SHORT}"])
        matches, permits = [_cm("rt-9")], []
        expected = ProductMatchType.BASE_NAME_EXACT
    match = assessment_rules.resolve_product_match(req, matches, permits, [], [], True, generic_terms=[NUTRIENT])
    assert match.status == ProductVerificationStatus.VERIFIED and match.match_type == expected and gate_spy == []


# ============================================================ 2. 產品 vs 成分路由


def test_nutrient_with_dosage_form_is_routed_to_product_candidates_not_ingredient_card():
    result, llm = run(f"{NUTRIENT}膠囊", extraction(intent=Intent.UNCLEAR, ingredient_candidates=[NUTRIENT]))
    req = result.normalized_request
    assert req.product_candidates == [f"{NUTRIENT}膠囊"] and req.ingredient_candidates == []
    assert req.intent == Intent.PRODUCT_VERIFICATION and PRODUCT_PHRASE_NOTE in req.adapter_notes
    assert "lookup_ingredient" not in tools(result) and not llm.called("plan_step")
    p = result.response
    assert p.response_type == ResponseType.MISSING_INFORMATION  # 多個產品：請使用者確認是哪一項
    assert {c.product_id for c in p.product_choices} == {"rt-1", "rt-2"}


def test_exact_catalog_product_as_bare_text_is_resolved_as_product():
    result, _ = run(HF_NAME, extraction(intent=Intent.UNCLEAR))
    req = result.normalized_request
    assert req.product_candidates == [HF_NAME] and BARE_NAME_NOTE in req.adapter_notes
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert result.response.response_type == ResponseType.PRODUCT_INFO


def test_what_is_nutrient_stays_ingredient_inquiry_and_lists_only_exact_ingredient():
    result, _ = run("虛構素甲是什麼？", extraction(intent=Intent.INGREDIENT_INQUIRY, ingredient_candidates=["虛構素甲"]),
                    plans=[plan(("lookup_ingredient", {"names": ["虛構素甲"]}))])
    assert result.normalized_request.product_candidates == []
    assert {i.name_zh for i in result.evidence.standalone_ingredients} >= {"虛構素甲"}
    assert result.evidence.ingredients == []  # 成分名稱查詢不是產品成分
    p = result.response
    assert p.response_type == ResponseType.INGREDIENT_LIST
    assert "不是指任何一項產品的成分" in p.summary and p.key_points == ["虛構素甲（fictionin-a）"]


def test_ingredient_effects_question_without_product_stays_ingredient_inquiry():
    result, _ = run("虛構素甲有什麼功效？", extraction(intent=Intent.EFFICACY_INQUIRY, ingredient_candidates=["虛構素甲"]))
    req = result.normalized_request
    assert req.product_candidates == [] and req.intent == Intent.EFFICACY_INQUIRY
    assert "lookup_product" not in tools(result)


def test_product_contains_nutrient_is_product_ingredient_inquiry():
    result, _ = run(f"{HF_NAME}有虛構素甲嗎？", extraction(
        intent=Intent.INGREDIENT_INQUIRY, product_candidates=[HF_NAME], ingredient_candidates=["虛構素甲"],
        needs_product_lookup=True))
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    answer = result.evidence.product_ingredients.query_answers[0]
    assert answer.query_text == "虛構素甲" and result.response.response_type == ResponseType.INGREDIENT_LIST


def test_similar_but_different_ingredient_names_are_never_listed():
    req = request(intent=Intent.INGREDIENT_INQUIRY, ingredient_candidates=["虛構素丙"])
    bundle = EvidenceBundle(request_id="r", product_match=ProductMatch(status=ProductVerificationStatus.NOT_APPLICABLE),
                            standalone_ingredients=[_ingredient("虛構素甲"), _ingredient(NUTRIENT)])
    p = compose_payload(req, bundle, RiskEngine().assess(req, bundle))
    assert "虛構素甲" not in p.plain_text() and NUTRIENT not in p.plain_text()


def test_standalone_lookup_results_are_never_rendered_as_a_products_ingredients():
    result, _ = run(f"{SHORT}有哪些成分？", extraction(
        intent=Intent.INGREDIENT_INQUIRY, product_candidates=[SHORT], needs_product_lookup=True))
    bundle = result.evidence.model_copy(update={"standalone_ingredients": [_ingredient(NUTRIENT)]})
    p = compose_payload(result.normalized_request, bundle, result.risk)
    assert bundle.product_match.status == ProductVerificationStatus.VERIFIED
    assert NUTRIENT not in p.plain_text()  # 這項產品沒有登記成分；成分名稱查詢結果不是它的成分
    detail = detail_payload(ResponseAction.INGREDIENTS, result.normalized_request, bundle, result.risk)
    assert NUTRIENT not in " ".join(detail.body)


def _ingredient(name: str) -> IngredientRecord:
    return IngredientRecord(ingredient_id=f"id-{name}", name_zh=name,
                            source=SourceRef(source_name="TEST_ONLY", source_type=SourceType.STRUCTURED_DB, is_official=True))


# ============================================================ 3. 短品名單字錯字


@pytest.mark.parametrize("query,target", [("虛測查", "rt-3"), ("QXY", "rt-4")], ids=["3_chars", "abc_abd"])
def test_short_name_one_character_typo_keeps_intended_candidate_as_clarification(query, target):
    result, _ = run(query, extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[query], needs_product_lookup=True))
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.PARTIAL_MATCH and match.needs_confirmation  # 不自動確認
    assert target in {c.product.product_id for c in match.candidates}
    assert result.response.product_choices and result.response.product_choices[0].product_id == target


def test_short_typo_from_bare_text_reaches_resolver():
    result, _ = run("虛測查", extraction(intent=Intent.UNCLEAR))
    assert result.normalized_request.product_candidates == ["虛測查"]
    assert result.evidence.product_match.needs_confirmation


def test_one_character_typo_with_competing_products_lists_eligible_candidates(monkeypatch):
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [_product("rt-6", "虛測菜", "c-46", "己廠測試公司", "Z00406")])
    gated = apply_candidate_relevance_gate(
        request(product_candidates=["虛測查"]),
        ProductMatch(status=ProductVerificationStatus.PARTIAL_MATCH, candidates=[_cm("rt-3"), _cm("rt-6")],
                     match_type=ProductMatchType.NAME_SIMILARITY,
                     ambiguity_reason=ProductAmbiguityReason.MULTIPLE_SIMILAR_NAMES, candidate_count=2), [], None)
    assert {c.product.product_id for c in gated.candidates} == {"rt-3", "rt-6"} and gated.best_match is None


def test_single_edit_rule_does_not_override_core_conflicts():
    assert missed_core_terms("虛構甲牌維生素C", "虛構甲牌維生素A軟膠囊", None)[0] is False  # 營養素不同
    assert missed_core_terms("NUTRIX螯晶鋅錠", "NUTRIX B群+瑪測卡錠", None)[0] is False  # 同品牌不同核心
    assert missed_core_terms("星河維生素A", "虛構甲牌維生素A軟膠囊", None)[0] is False  # 未知前綴＋一般營養素
    ok, terms = missed_core_terms("BRAND B群馬卡", "NUTRIX B群+瑪測卡錠", None)
    assert ok is False and terms[0] == "brand"  # 顯示整段缺少的片段，不是對齊造成的半個詞
