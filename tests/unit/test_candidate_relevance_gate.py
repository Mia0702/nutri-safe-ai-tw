"""Candidate Relevance / Product-Core Relevance Gate 回歸測試。

* 品牌 / 公司相同只代表可能同品牌；使用者寫了產品核心描述時，候選也必須有該核心（同品牌不同核心 → 不列）。
* 未知辨識名稱＋一般營養素 → 不列只靠營養素字樣相同的產品，NOT_FOUND（NO_PRODUCT_IDENTITY_SUPPORT），不跑 planner / RAG / web。
* 一般查詢照常多候選；核心單一錯字仍可候選確認；exact / alias / 字號 / 自動確認 / postback 不變。

全部 TEST_ONLY 虛構產品、品牌、公司（品牌 NUTRIX / ZENITH 為虛構），不呼叫外部服務。
"""

import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import IDENTITY_NOT_FOUND_MODE, VerificationAgent
from app.line.handler import LineWebhookHandler
from app.pipeline import AnalysisPipeline
from app.response.actions import build_select_product_data
from app.response.service import AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.risk.assessment_rules import apply_candidate_relevance_gate, resolve_product_match
from app.risk.candidate_relevance import distinctive_segments
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    InputType,
    Intent,
    ProductAmbiguityReason,
    ProductMatchType,
    ProductNotFoundReason,
    ProductVerificationStatus,
    ResponseType,
)
from app.schemas.evidence import OFFICIAL_ALIAS_MATCHED_BY, ProductCandidateMatch, ProductMatch
from app.schemas.user_request import IntakeExtraction, RawUserInput
from app.services.context_store import TaskContextStore
from app.tools.product_tools import _product_from_row
from tests.builders import extraction, request, user_text
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import _product
from tests.unit.test_line_webhook import FakeLine, event, send

UNKNOWN = "星河"  # TEST_ONLY：資料中沒有任何品名 / 品牌 / 公司含這兩個字
BRAND_A, BRAND_Z = "NUTRIX", "ZENITH"  # TEST_ONLY：虛構品牌
VITA_A = [("ga-1", "虛構甲牌維生素A軟膠囊", "c-31", "虛構甲測試生技有限公司", "Z00931"),
          ("ga-2", "範例乙維生素A膠囊", "c-32", "範例乙測試食品有限公司", "Z00932"),
          ("ga-3", "測試丙維生素A錠", "c-33", "測試丙測試股份有限公司", "Z00933")]
FISH = [("gf-1", "虛構甲深海魚油", "c-31", "虛構甲測試生技有限公司", "Z00941"),
        ("gf-2", "範例乙深海魚油軟膠囊", "c-32", "範例乙測試食品有限公司", "Z00942")]
# BRAND-A：只有「B群＋虛構植物」與另一項產品，沒有任何「螯晶」核心的產品
BRAND_A_ROWS = [("gn-1", f"{BRAND_A} B群+瑪測卡錠", "c-35", "虛構紐測試有限公司", "Z00951"),
                ("gn-2", f"{BRAND_A}晚安測試錠", "c-35", "虛構紐測試有限公司", "Z00952")]
# BRAND-Z：兩個鋅產品變體（核心不同）；另有他牌同核心產品
ZINC = [("gz-1", f"{BRAND_Z}螯晶鋅錠", "c-37", "虛構澤測試有限公司", "Z00971"),
        ("gz-2", f"{BRAND_Z}甘胺鋅膠囊", "c-37", "虛構澤測試有限公司", "Z00972"),
        ("gz-3", "範例乙螯晶鋅錠", "c-32", "範例乙測試食品有限公司", "Z00973")]


@pytest.fixture(autouse=True)
def gate_data(monkeypatch):
    rows = [_product(pid, name, cid, company, code) for pid, name, cid, company, code in VITA_A + FISH + BRAND_A_ROWS + ZINC]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + rows)


def run(**ex_kw):
    ex = extraction(intent=ex_kw.pop("intent", Intent.PRODUCT_VERIFICATION), needs_product_lookup=True, **ex_kw)
    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    return pipeline.run(RawUserInput(input_type=InputType.TEXT, text=user_text(ex))), llm


def candidate_ids(match: ProductMatch) -> set[str]:
    return {c.product.product_id for c in match.candidates}


def _cm(pid: str, score: float = 0.6, matched_by: str = "product_name", query: str = "") -> ProductCandidateMatch:
    row = next(p for p in data.PRODUCTS if p["product_id"] == pid)
    return ProductCandidateMatch(product=_product_from_row(row), match_score=score, matched_by=matched_by, query=query)


def _match(pids, match_type=ProductMatchType.NAME_SIMILARITY, ambiguity=ProductAmbiguityReason.MULTIPLE_SIMILAR_NAMES):
    best = _cm(pids[0]) if len(pids) == 1 else None
    return ProductMatch(status=ProductVerificationStatus.PARTIAL_MATCH, best_match=best, candidates=[_cm(p) for p in pids],
                        match_type=match_type, ambiguity_reason=ambiguity, candidate_count=len(pids))


def assert_no_candidates(result):
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.NOT_FOUND and match.candidates == []
    assert match.not_found_reason == ProductNotFoundReason.NO_PRODUCT_IDENTITY_SUPPORT and match.filtered_candidate_count > 0
    p = result.response
    assert p.response_type == ResponseType.MISSING_INFORMATION and p.product_choices == [] and p.risk_level is None
    assert result.evidence.planner_mode == IDENTITY_NOT_FOUND_MODE


# ============================================================ 1. 同品牌、不同產品核心 → 0 候選


@pytest.mark.parametrize("ex_kw", [
    {"product_candidates": [f"{BRAND_A}螯晶鋅錠"]},
    {"product_candidates": ["螯晶鋅錠"], "brand_candidates": [BRAND_A]},  # Agent A 把品牌拆開
], ids=["brand_in_name", "brand_separate"])
def test_same_brand_different_product_core_has_no_candidates(ex_kw):
    result, llm = run(**ex_kw)
    assert_no_candidates(result)
    assert "gn-1" not in candidate_ids(result.evidence.product_match)
    summary = result.response.summary  # 說明最接近的候選缺少哪一段（核心或品牌）
    assert ("螯晶" in summary or BRAND_A.lower() in summary) and "品牌相同" in summary
    assert not llm.called("plan_step") and not llm.called("web_search_structured")


def test_brand_corroborated_single_candidate_with_conflicting_core_is_removed():
    """名稱＋品牌佐證（只因品牌相同成為唯一候選）也要通過產品核心檢查；不影響 VERIFIED。"""
    req = request(product_candidates=["螯晶鋅錠"], brand_candidates=[BRAND_A])
    corroborated = _match(["gn-1"], ProductMatchType.NAME_COMPANY_CORROBORATED, ProductAmbiguityReason.LOW_MATCH_CONFIDENCE)
    assert apply_candidate_relevance_gate(req, corroborated, [], None).status == ProductVerificationStatus.NOT_FOUND
    verified = corroborated.model_copy(update={"status": ProductVerificationStatus.VERIFIED, "ambiguity_reason": None})
    assert apply_candidate_relevance_gate(req, verified, [], None) is verified


# ============================================================ 2. 同品牌、同產品核心 → 可列


@pytest.mark.parametrize("ex_kw", [
    {"product_candidates": [f"{BRAND_A} B群瑪測卡"]},
    {"product_candidates": ["B群瑪測卡"], "brand_candidates": [BRAND_A]},
], ids=["brand_in_name", "brand_separate"])
def test_same_brand_same_product_core_candidate_allowed(ex_kw):
    result, _ = run(**ex_kw)
    match = result.evidence.product_match
    if match.status == ProductVerificationStatus.VERIFIED:  # 只有省略字：既有自動確認規則（不變）
        assert match.best_match.product.product_id == "gn-1"
    else:
        assert "gn-1" in candidate_ids(match) and "gn-2" not in candidate_ids(match)
    assert match.not_found_reason is None


# ============================================================ 3. 同品牌、同核心多個變體 → 候選確認


def test_same_brand_zinc_variants_need_clarification():
    result, _ = run(product_candidates=[f"{BRAND_Z}鋅"])
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.PARTIAL_MATCH and match.is_ambiguous
    assert candidate_ids(match) == {"gz-1", "gz-2"}  # 他牌同成分產品、同公司以外的產品不列
    assert {c.product_id for c in result.response.product_choices} == {"gz-1", "gz-2"}


# ============================================================ 4. 未知品牌 + 一般營養素


@pytest.mark.parametrize("ex_kw", [
    {"product_candidates": [f"{UNKNOWN}維生素A"]},
    {"product_candidates": ["維生素A"], "brand_candidates": [UNKNOWN]},
    {"product_candidates": [f"{UNKNOWN}深海魚油"]},
], ids=["vitamin", "brand_split", "fish_oil"])
def test_unknown_brand_with_generic_nutrient_has_no_candidates(ex_kw):
    result, _ = run(**ex_kw)
    assert_no_candidates(result)
    assert f"「{UNKNOWN}」" in result.response.summary and "不代表產品是假貨" in " ".join(result.response.key_points)


# ============================================================ 5. 一般查詢


def test_generic_query_without_identifier_still_lists_candidates():
    result, _ = run(product_candidates=["維生素A膠囊"])
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.PARTIAL_MATCH and match.filtered_candidate_count == 0
    assert len(candidate_ids(match) & {"ga-1", "ga-2", "ga-3"}) >= 2 and len(result.response.product_choices) >= 2
    assert distinctive_segments(["維生素A膠囊"]) == []


def test_generic_core_query_lists_all_brands_with_that_core():
    gated = apply_candidate_relevance_gate(request(product_candidates=["螯晶鋅錠"]), _match(["gz-1", "gz-3", "gn-1"]), [], None)
    assert candidate_ids(gated) == {"gz-1", "gz-3"}  # 沒有品牌：同核心的各品牌可列；核心不同的移除


# ============================================================ 6. 核心單一錯字


def test_one_character_typo_in_product_core_keeps_intended_candidate():
    result, _ = run(product_candidates=[f"{BRAND_Z}螯精鋅錠"])  # 晶 → 精
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.PARTIAL_MATCH and match.needs_confirmation
    assert candidate_ids(match) == {"gz-1"}  # 同品牌但核心不同的變體不列
    typo = run(product_candidates=["虛構牌晶亮測式膠囊"])[0].evidence.product_match
    assert "p-1" in candidate_ids(typo) and typo.not_found_reason is None


def test_typo_keeps_target_when_product_word_is_shared_by_other_applicants(monkeypatch):
    rows = [_product("ty-1", "虛構花保健測試錠", "c-71", "虛構花甲測試公司", "Z00981"),
            _product("ty-2", "虛構花健康測試錠", "c-72", "虛構花乙測試公司", "Z00982")]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + rows)
    gated = apply_candidate_relevance_gate(request(product_candidates=["虛構花甲健測試錠"]), _match(["ty-1", "ty-2"]), [], None)
    assert "ty-1" in candidate_ids(gated) and gated.status == ProductVerificationStatus.PARTIAL_MATCH


# ============================================================ 7–9. exact / alias / 字號不變


def test_exact_name_alias_and_permit_are_unchanged_by_gate():
    terms = ["維生素A"]
    exact = resolve_product_match(request(product_candidates=[VITA_A[0][1]]), [_cm("ga-1", 1.0), _cm("ga-2")],
                                  [], [], [], True, generic_terms=terms)
    assert exact.status == ProductVerificationStatus.VERIFIED and exact.match_type == ProductMatchType.CANONICAL_EXACT
    alias = resolve_product_match(
        request(product_candidates=[f"{UNKNOWN}A"]),
        [_cm("ga-2", 1.0, OFFICIAL_ALIAS_MATCHED_BY, f"{UNKNOWN}A"), _cm("ga-1")], [], [], [], True, generic_terms=terms)
    assert alias.status == ProductVerificationStatus.VERIFIED and alias.match_type == ProductMatchType.OFFICIAL_ALIAS_EXACT
    permit = resolve_product_match(
        request(product_candidates=[f"{BRAND_A}螯晶鋅錠"], permit_number_candidates=["Z00951"]),
        [_cm("gz-1")], [_cm("gn-1", 1.0, "permit_number")], [], [], True, generic_terms=terms)
    assert permit.match_type == ProductMatchType.PERMIT_NUMBER and permit.best_match.product.product_id == "gn-1"
    result, _ = run(product_candidates=[BRAND_A_ROWS[1][1]], intent=Intent.EFFICACY_INQUIRY)
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED


def test_gate_never_confirms_and_ignores_other_match_types():
    req = request(product_candidates=[f"{UNKNOWN}維生素A"])
    multi = _match(["ga-1", "gn-1"])
    assert apply_candidate_relevance_gate(req, multi, [], None).status == ProductVerificationStatus.NOT_FOUND
    one_left = apply_candidate_relevance_gate(request(product_candidates=[f"{BRAND_A}晚安"]), _match(["gn-2", "ga-1"]), [], None)
    assert one_left.status == ProductVerificationStatus.PARTIAL_MATCH and one_left.needs_confirmation  # 不升級為 VERIFIED
    assert one_left.best_match.product.product_id == "gn-2" and one_left.filtered_candidate_count == 1
    for match_type in (ProductMatchType.BASE_NAME_EXACT, ProductMatchType.CANONICAL_EXACT, ProductMatchType.PERMIT_NUMBER,
                       ProductMatchType.OFFICIAL_ALIAS_EXACT):
        other = multi.model_copy(update={"match_type": match_type})
        assert apply_candidate_relevance_gate(req, other, [], None) is other
    company = apply_candidate_relevance_gate(req, multi, ["虛構甲測試生技有限公司"], None)
    assert company.match_type == ProductMatchType.COMPANY_ONLY and company.candidates == []


def test_wording_shared_by_several_products_is_not_identity(monkeypatch):
    rows = [_product("lu-1", "葉測黃素", "c-91", "測試一公司", "Z00991"),
            _product("lu-2", "葉測黃素藍莓膠囊", "c-92", "測試二公司", "Z00992")]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + rows)
    gated = apply_candidate_relevance_gate(request(product_candidates=[f"{UNKNOWN}葉測黃素"]), _match(["lu-1", "lu-2"]), [], None)
    assert gated.status == ProductVerificationStatus.NOT_FOUND and gated.candidates == []  # 詞表外的字樣也一樣


# ============================================================ 10. 候選 postback 不變


def test_candidate_postback_selects_eligible_candidate_and_rejects_others():
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[f"{BRAND_Z}鋅"], needs_product_lookup=True)
    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    contexts, store = TaskContextStore(), InMemoryRequestStore()
    svc = AdaptiveResponseService(pipeline, ReporterAgent(llm), store, timeout_seconds=5.0, context_store=contexts)
    line = FakeLine()
    handler = LineWebhookHandler(svc, line, contexts)
    [rid] = send(handler, event(text=f"{BRAND_Z}鋅的功效？"))
    choices = [c.product_id for c in store.get(rid).response.product_choices]
    assert set(choices) == {"gz-1", "gz-2"}
    before = len(store._data)
    send(handler, event(kind="postback", data=build_select_product_data(rid, "gz-3")))  # 不在候選中的產品不能被選
    assert len(store._data) == before
    send(handler, event(kind="postback", data=build_select_product_data(rid, "gz-1")))
    rec = list(store._data.values())[-1]
    assert rec.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert rec.evidence.product_match.best_match.product.product_id == "gz-1"
    assert json.dumps(line.sent[-1][2], ensure_ascii=False)
