"""獨立成分 / 營養素 identity 解析（全部 TEST_ONLY 虛構資料）。

identity（Ingredient Bridge canonical entity / 已接受 alias，exact）、legacy 官方成分表、產品成分、功能證據是不同的事：
* canonical entity 或已接受 alias 完全相同 → identity found，成分概覽卡，不說「找不到成分」。
* identity 沒有收錄 → IDENTITY_NOT_IN_CORPUS（不代表不存在）；功能證據照一般規則查，與 NO_FUNCTION_EVIDENCE 分開。
* 不用相似度 / embedding 建立 identity；前文產品綁定規則不變。
"""

from datetime import datetime, timezone

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import STANDALONE_ENTITY_MODE, VerificationAgent
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    EntityType, FunctionEvidenceStatus, IngredientIdentityStatus, InputType, Intent, ProductConfirmationSource,
    ProductVerificationStatus, ResponseType,
)
from app.schemas.user_request import (
    ConfirmedProduct, ContextEntity, ConversationContext, ConversationTurn, IntakeExtraction, RawUserInput,
)
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import _product
from tests.unit.test_ingredient_retrieval_runtime import CORPUS, OVERLAY, _alias, _form, _group

NUT_EN, NUT_ZH = "TestNutrientX", "虛構營養素甲"
ALIAS_ZH = "虛構營養素甲別稱"
FORM_EN, FORM_ZH, FORM_ALIAS = "TestFormZ", "虛構型態丙", "虛構型態丙別稱"
ABSENT = "虛構未收錄營養素"  # 不在 identity corpus
PRODUCT_X = f"{NUT_ZH}膠囊"
SLOW = {"search_rag", "search_tw_official_web", "assess_evidence"}


@pytest.fixture(autouse=True)
def identity_data(monkeypatch):
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [_product("si-1", PRODUCT_X, "c-61", "甲測試公司", "Z00601")])
    entities = [_group("TN_X", NUT_EN, NUT_ZH), _form("TF_Z", FORM_EN, FORM_ZH, "TN_X")]
    aliases = [_alias("SA1", ALIAS_ZH, "TN_X"), _alias("SA2", FORM_ALIAS, "TF_Z"),
               _alias("SA3", "虛構候選別名", candidate="TN_X", status="AMBIGUOUS", policy="CANDIDATE_ONLY_NOT_AUTOMERGE")]
    monkeypatch.setattr(data, "INGREDIENT_BRIDGE", {"corpus_version": CORPUS, "overlay_version": OVERLAY,
                                                    "entities": entities, "aliases": aliases})


def run(ex, text, *, rag_rows=None, context=None):
    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipe = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(rag_rows), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    return pipe.run(RawUserInput(input_type=InputType.TEXT, text=text, context=context or ConversationContext())), llm


def rag_row():
    return {**data.RAG_CHUNKS[0], "similarity": 0.9}


def tools(result):
    return [t.tool_name for t in result.evidence.tools_called]


def ingredient_ex(name, intent=Intent.INGREDIENT_INQUIRY):
    return extraction(intent=intent, ingredient_candidates=[name], needs_graph_lookup=True)


# ------------------------------------------------------------------ 1–3 identity found（exact canonical / alias / form alias）


@pytest.mark.parametrize("name,entity_id,type_label", [
    (NUT_EN, "TN_X", "營養素"),          # 1. NutrientGroup 正式英文名
    (ALIAS_ZH, "TN_X", "營養素"),        # 2. 已接受的中文 alias
    (FORM_ALIAS, "TF_Z", "成分（型態）"),  # 3. IngredientForm 的 alias
])
def test_exact_canonical_name_or_accepted_alias_is_identity_found(name, entity_id, type_label):
    result, llm = run(ingredient_ex(name), f"{name}是什麼？", rag_rows=[])
    bundle, payload = result.evidence, result.response
    assert [e.entity_id for e in bundle.standalone_entities] == [entity_id]
    assert bundle.ingredient_identity_status == IngredientIdentityStatus.CANONICAL_ENTITY
    assert payload.response_type == ResponseType.INGREDIENT_LIST and type_label in payload.summary
    assert "沒有找到" not in payload.summary and "還沒有收錄" not in payload.summary
    assert bundle.planner_mode == STANDALONE_ENTITY_MODE and not llm.called("plan_step")
    assert not (set(tools(result)) & SLOW)  # 概覽只確認 identity：不查功能證據


def test_candidate_only_alias_does_not_establish_identity():
    result, _ = run(ingredient_ex("虛構候選別名"), "虛構候選別名是什麼？", rag_rows=[])
    assert not result.evidence.standalone_entities
    assert result.evidence.ingredient_identity_status == IngredientIdentityStatus.IDENTITY_NOT_IN_CORPUS


# ------------------------------------------------------------------ 4. identity 有、功能證據沒有


def test_identity_exists_but_no_function_evidence():
    result, llm = run(ingredient_ex(NUT_EN, Intent.EFFICACY_INQUIRY), f"{NUT_EN}有什麼功能？", rag_rows=[])
    bundle = result.evidence
    assert bundle.ingredient_identity_status == IngredientIdentityStatus.CANONICAL_ENTITY
    assert bundle.function_evidence_status == FunctionEvidenceStatus.NO_FUNCTION_EVIDENCE
    assert not bundle.rag_evidence and "沒有足夠的官方證據" in "".join(result.response.key_points)
    assert "已確認的營養素" in result.response.summary  # identity 仍然成立
    # 沒有 RAG 證據時才依一般規則查官方網站一次（只有這一次 web 查詢規劃，沒有多輪 planner）
    assert tools(result).count("search_rag") == 1 and tools(result).count("plan_tool_calls") <= 1
    assert bundle.planner_mode == STANDALONE_ENTITY_MODE


# ------------------------------------------------------------------ 5. 功能證據有、但 entity 不在 corpus


def test_function_evidence_exists_for_name_not_in_identity_corpus():
    result, llm = run(ingredient_ex(ABSENT, Intent.EFFICACY_INQUIRY), f"{ABSENT}有什麼功能？", rag_rows=[rag_row()])
    bundle, payload = result.evidence, result.response
    assert not bundle.standalone_entities
    assert bundle.ingredient_identity_status == IngredientIdentityStatus.IDENTITY_NOT_IN_CORPUS
    assert bundle.function_evidence_status == FunctionEvidenceStatus.SUPPORTED and bundle.rag_evidence
    assert "還沒有收錄" in payload.summary and "不代表它不存在" in payload.summary
    assert "官方資料" in "".join(payload.key_points)
    assert not llm.called("plan_step") and tools(result).count("search_rag") == 1  # 不跑 planner 多輪重複搜尋


# ------------------------------------------------------------------ 6. 真的完全沒有


def test_truly_unknown_ingredient_is_not_invented_and_not_called_nonexistent():
    result, _ = run(ingredient_ex(ABSENT), f"{ABSENT}是什麼？", rag_rows=[])
    bundle, payload = result.evidence, result.response
    assert not bundle.standalone_entities
    assert bundle.ingredient_identity_status == IngredientIdentityStatus.IDENTITY_NOT_IN_CORPUS
    assert bundle.function_evidence_status == FunctionEvidenceStatus.NO_FUNCTION_EVIDENCE
    text = payload.summary + "".join(payload.key_points)
    assert "還沒有收錄" in text and "沒有足夠的官方證據" in text and "沒有找到名稱完全相同的成分" not in text


# ------------------------------------------------------------------ 7–8. 前文產品綁定規則不變


def _context():
    return ConversationContext(
        turns=[ConversationTurn(user_text=PRODUCT_X, entities=[ContextEntity(entity_type=EntityType.PRODUCT, name=PRODUCT_X)])],
        confirmed_product=ConfirmedProduct(
            product_id="si-1", permit_number="測試健食字第Z00601號", official_product_name=PRODUCT_X,
            confirmation_source=ProductConfirmationSource.RESOLVER_VERIFIED, confirmed_at=datetime.now(timezone.utc),
            confirmed_request_id="prev"))


def test_bare_entity_after_confirmed_product_does_not_bind_previous_product():
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, refers_to_unspecified_product=True, context_resolved_names=[PRODUCT_X])
    result, _ = run(ex, NUT_ZH, rag_rows=[], context=_context())
    req = result.normalized_request
    assert req.standalone_entity and not req.context_entities_used
    assert result.evidence.product_match.status == ProductVerificationStatus.NOT_APPLICABLE
    assert result.evidence.ingredient_identity_status == IngredientIdentityStatus.CANONICAL_ENTITY


def test_pronoun_follow_up_still_binds_previous_product():
    ex = extraction(intent=Intent.INGREDIENT_INQUIRY, ingredient_candidates=[NUT_ZH], refers_to_unspecified_product=True,
                    context_resolved_names=[PRODUCT_X])
    result, _ = run(ex, f"它有含{NUT_ZH}嗎？", rag_rows=[], context=_context())
    req = result.normalized_request
    assert not req.standalone_entity and req.context_entities_used and req.context_entities_used[0].name == PRODUCT_X
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert result.evidence.ingredient_identity_status is None  # 產品成分問題，不走獨立成分路徑


# ------------------------------------------------------------------ 裸名稱：不在 corpus 的一般成分用語不可被產品候選搶走 / 完整分析按鈕

GENERIC = "維生素B6"  # 一般成分用語（沒有有辨識力的字樣）；synthetic bridge 中沒有這個 entity


@pytest.fixture
def generic_products(monkeypatch):
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [
        _product("si-2", f"虛構牌{GENERIC}錠", "c-62", "乙測試公司", "Z00602"),
        _product("si-3", f"虛構牌{GENERIC}膠囊", "c-63", "丙測試公司", "Z00603")])


def test_bare_generic_nutrient_not_in_corpus_is_standalone_not_product_candidates(generic_products):
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[GENERIC])  # Controller 偶發判成產品
    result, llm = run(ex, GENERIC, rag_rows=[rag_row()])
    req, bundle = result.normalized_request, result.evidence
    assert req.standalone_entity and not req.product_candidates and req.ingredient_candidates == [GENERIC]
    assert bundle.product_match.status == ProductVerificationStatus.NOT_APPLICABLE and not bundle.product_match.candidates
    assert bundle.ingredient_identity_status == IngredientIdentityStatus.IDENTITY_NOT_IN_CORPUS
    assert result.response.response_type == ResponseType.INGREDIENT_LIST and not result.response.product_choices
    assert "不代表它不存在" in result.response.summary and not llm.called("plan_step")


def test_bare_exact_official_product_name_that_is_generic_wording_is_collision_clarification(monkeypatch):
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [_product("si-4", GENERIC, "c-64", "丁測試公司", "Z00604")])
    result, llm = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[GENERIC]), GENERIC, rag_rows=[])
    assert result.normalized_request.identity_collision and not llm.called("plan_step")
    assert result.response.response_type == ResponseType.MISSING_INFORMATION


@pytest.mark.parametrize("text", [f"{GENERIC}錠", f"{GENERIC}膠囊"])
def test_generic_nutrient_with_dosage_form_stays_product(generic_products, text):
    result, _ = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[text]), text, rag_rows=[])
    assert not result.normalized_request.standalone_entity and result.normalized_request.product_candidates


def test_distinctive_typo_of_product_still_goes_to_product_candidates(generic_products):
    typo = "虛構牌維生素B6錠".replace("虛構", "虛搆")  # 有辨識力字樣的錯字：維持產品候選確認
    result, _ = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[typo]), typo, rag_rows=[])
    assert not result.normalized_request.standalone_entity


# ------------------------------------------------------------------ 查看完整分析


def _service(ex, rag_rows):
    from app.response.service import AdaptiveResponseService
    from app.response.store import InMemoryRequestStore

    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipe = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(rag_rows), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    return llm, AdaptiveResponseService(pipe, ReporterAgent(llm), InMemoryRequestStore(), timeout_seconds=10.0)


def _request_id_of(message):
    from app.response.actions import parse_postback_data
    from tests.unit.test_adaptive_response import buttons

    return [parse_postback_data(a["data"]) for a in buttons(message)]


def test_function_question_with_evidence_has_full_analysis_and_detail_reuses_request():
    import json

    from app.response.actions import ResponseAction, build_postback_data

    ex = ingredient_ex(NUT_EN, Intent.EFFICACY_INQUIRY)
    llm, svc = _service(ex, [rag_row()])
    msg = svc.respond(RawUserInput(input_type=InputType.TEXT, text=f"{NUT_EN}有什麼功能？"), owner_id="U1")
    acts = _request_id_of(msg)
    detail = [a for a in acts if a.action == ResponseAction.DETAIL]
    assert detail and all(a.request_id == detail[0].request_id for a in acts)  # request-scoped
    controller_calls = llm.calls.count(("parse_structured", "IntakeExtraction"))
    out = svc.handle_action(build_postback_data(ResponseAction.DETAIL, detail[0].request_id), owner_id="U1")
    assert llm.calls.count(("parse_structured", "IntakeExtraction")) == controller_calls  # 沒有重跑 Controller
    assert "完整分析" in json.dumps(out, ensure_ascii=False) and llm.called("generate_text")
    assert not llm.called("web_search_structured")  # 沒有新增證據查詢


def test_standalone_without_any_usable_evidence_has_no_full_analysis_button():
    from app.response.actions import ResponseAction

    ex = ingredient_ex(ABSENT, Intent.EFFICACY_INQUIRY)
    _, svc = _service(ex, [])
    msg = svc.respond(RawUserInput(input_type=InputType.TEXT, text=f"{ABSENT}有什麼功能？"), owner_id="U1")
    assert ResponseAction.DETAIL not in {a.action for a in _request_id_of(msg)}


def test_product_ingredient_card_buttons_unchanged():
    """產品的成分回覆：沒有新增「查看完整分析」。"""
    ex = extraction(intent=Intent.INGREDIENT_INQUIRY, product_candidates=[PRODUCT_X], needs_product_lookup=True)
    result, _ = run(ex, PRODUCT_X, rag_rows=[])
    from app.schemas.enums import ResponseAction as RA
    assert result.response.response_type == ResponseType.INGREDIENT_LIST and RA.DETAIL not in result.response.available_actions
