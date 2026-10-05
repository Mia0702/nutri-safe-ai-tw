"""Ingredient Retrieval Runtime Integration 回歸測試（Agent B lookup_product_ingredients → Agent C）。

* 三層分開：官方原始成分 / 已確認 canonical identity（VERIFIED_EXACT / SUPPORTED_ALIAS / FORM_TO_GROUP）/ 未確認候選。
* health_food 沒有 bridge mapping 仍列既有官方成分；國產 / 輸入回官方原文＋已確認部分。
* 成分是非題：form → nutrient 只經 CONTRIBUTES_TO_NUTRIENT；候選（賦形劑鹽類）不會變成「有」。
* 結構化 fast path 不跑 planner / RAG / web；Agent C 依問題語意組合事實，不顯示內部 ID。

全部 TEST_ONLY 虛構產品與虛構 entity ID（T* 前綴），不呼叫外部服務。
"""

import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent, detail_payload
from app.agents.verification_agent import FAST_PATH_MODE, VerificationAgent
from app.line.handler import LineWebhookHandler
from app.pipeline import AnalysisPipeline
from app.response.actions import reachable_actions
from app.response.line_renderer import AdaptiveLineRenderer
from app.response.service import AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    IngredientPresence,
    InputType,
    Intent,
    ProductCategory,
    ProductReferenceStatus,
    ProductVerificationStatus,
    ResponseAction,
    ResponseType,
    SemanticTone,
)
from app.schemas.user_request import IntakeExtraction, RawUserInput
from app.services.context_store import TaskContextStore
from app.tools import ingredient_tools
from tests.builders import extraction, user_text
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product
from tests.unit.test_line_webhook import USER, FakeLine, event, send

R = AdaptiveLineRenderer()
HF_ZINC = "虛構牌補給測試錠"            # TEST_ONLY：health_food，既有成分中一項有 bridge mapping
HF_PLAIN = "虛構牌晶亮測試膠囊"          # synthetic_data 既有 health_food：只有既有成分、沒有任何 bridge mapping
DOM_NAME = "虛構牌綜合測試嚼錠"          # TEST_ONLY：國產維生素類
IMP_NAME = "虛構牌海洋測試軟膠囊"        # TEST_ONLY：輸入錠狀膠囊
FOLATE_NAME = "虛構牌孕補測試錠"         # TEST_ONLY：輸入，只有 Folate 營養素群（沒有 folic acid）
EPA_NAME = "虛構牌精純測試膠囊"          # TEST_ONLY：輸入，只有 EPA
CORPUS, OVERLAY = "T1.0", "T1.0"


def _entity(eid, label, level, en, zh, parent=None):
    return {"entity_id": eid, "graph_label": label, "identity_level": level, "canonical_name_en": en,
            "display_name_zh": zh, "parent_nutrient_entity_id": parent, "source_id": "TEST_ONLY_SRC",
            "source_url": "https://example.invalid/identity"}


def _group(eid, en, zh):
    return _entity(eid, "NutrientGroup", "NUTRIENT_GROUP", en, zh)


def _form(eid, en, zh, parent):
    return _entity(eid, "IngredientForm", "CHEMICAL_FORM", en, zh, parent)


ENTITIES = [
    _group("TN_ZN", "Zinc", "鋅"), _group("TN_CU", "Copper", "銅"), _group("TN_MG", "Magnesium", "鎂"),
    _group("TN_OMEGA3", "Omega-3 fatty acids", "Omega-3脂肪酸群"), _group("TN_B9", "Folate", "葉酸營養素群"),
    _form("TF_ZN_GLUCONATE", "Zinc gluconate", "葡萄糖酸鋅", "TN_ZN"),
    _form("TF_ZN_OXIDE", "Zinc oxide", "氧化鋅", "TN_ZN"),
    _form("TF_CU_GLUCONATE", "Copper gluconate", "葡萄糖酸銅", "TN_CU"),
    _form("TF_FISH_OIL", "Fish oil", "魚油", "TN_OMEGA3"),
    _form("TF_EPA", "Eicosapentaenoic acid", "EPA", "TN_OMEGA3"),
    _form("TF_DHA", "Docosahexaenoic acid", "DHA", "TN_OMEGA3"),
    _form("TF_FOLIC_ACID", "Folic acid", "葉酸（folic acid）", "TN_B9"),
]


def _alias(aid, text, entity=None, candidate=None, origin="REVIEWED_OVERLAY", status="VERIFIED_OFFICIAL",
           policy="EXACT_NFKC_CASEFOLD_WHITESPACE_AT_DECLARED_LEVEL"):
    return {"alias_id": aid, "entity_id": entity, "candidate_entity_id": candidate, "alias_text": text,
            "alias_origin": origin, "alias_status": status, "match_policy": policy}


ALIASES = [
    _alias("TA1", "葡萄糖酸鋅", "TF_ZN_GLUCONATE"), _alias("TA2", "鋅", "TN_ZN", status="SUPPORTED_AUTHORITATIVE"),
    _alias("TA3", "葡萄糖酸銅", "TF_CU_GLUCONATE"), _alias("TA4", "鎂", "TN_MG", status="SUPPORTED_AUTHORITATIVE"),
    _alias("TA5", "魚油", "TF_FISH_OIL"), _alias("TA6", "EPA", "TF_EPA"), _alias("TA7", "DHA", "TF_DHA"),
    _alias("TA8", "葉酸", "TF_FOLIC_ACID"), _alias("TA9", "氧化鋅", "TF_ZN_OXIDE"),
    _alias("TA10", "硬脂酸鎂", candidate="TN_MG", status="AMBIGUOUS", policy="CANDIDATE_ONLY_NOT_AUTOMERGE"),
    _alias("TB1", "Omega-3", "TN_OMEGA3", origin="BASE_CORPUS", status=None, policy="EXACT_AT_DECLARED_IDENTITY_LEVEL"),
    _alias("TB2", "銅", "TN_CU", origin="BASE_CORPUS", status=None, policy="DISPLAY_ONLY_NOT_AUTOMERGE"),
    _alias("TB3", "維生素D", None, origin="BASE_CORPUS", status=None, policy="DISPLAY_ONLY_NOT_AUTOMERGE"),
]


def _mapping(pid, raw, status, entity=None, via=None, candidates=(), token=None, ingredient=None):
    method = {"VERIFIED_EXACT": "CORPUS_CANONICAL_NAME_EXACT", "SUPPORTED_ALIAS": "REVIEWED_OVERLAY_ALIAS_EXACT",
              "FORM_TO_GROUP": "REVIEWED_FORM_TO_NUTRIENT", "AMBIGUOUS": "REVIEWED_OVERLAY_CANDIDATE",
              "NEEDS_REVIEW": "REVIEWED_OVERLAY_CANDIDATE"}[status]
    formal = status in ("VERIFIED_EXACT", "SUPPORTED_ALIAS", "FORM_TO_GROUP")
    return {"product_id": pid, "ingredient_source": "HEALTH_FOOD_CANONICAL_INGREDIENT" if ingredient
            else "TFDA_REGISTRATION_INGREDIENTS_RAW", "source_ingredient_id": ingredient, "token_index": token,
            "raw_text": raw, "mapping_status": status, "entity_id": entity, "via_entity_id": via,
            "entity_type": None, "relationship_id": f"TREL:{via}" if via else None,
            "candidate_entity_ids": list(candidates), "match_method": method, "matched_alias_ids": [],
            "alias_evidence_status": None, "confidence": 0.95 if formal else 0.4,
            "review_status": "SOURCE_REVIEWED" if formal else "PENDING_REVIEW",
            "corpus_version": CORPUS, "overlay_version": OVERLAY}


def _form_mapping(pid, raw, form, group, **unit):
    return [_mapping(pid, raw, "SUPPORTED_ALIAS", form, **unit), _mapping(pid, raw, "FORM_TO_GROUP", group, via=form, **unit)]


def _registration(pid, name, code, category):
    return {**_product(pid, name, "c-9", "虛構輸入測試有限公司", code), "product_category": category.value,
            "status": "UNKNOWN", "permit_number": code, "permit_type": "查驗登記證", "expiry_date": "2099-01-01"}


@pytest.fixture(autouse=True)
def ingredient_data(monkeypatch):
    dom, imp = ProductCategory.DOMESTIC_VITAMIN_TABLET_CAPSULE, ProductCategory.IMPORTED_TABLET_CAPSULE
    products = [
        _product("ing-hf", HF_ZINC, "c-1", "虛構測試生技股份有限公司", "Z00900"),
        _registration("ing-dom", DOM_NAME, "9990000001", dom),
        _registration("ing-imp", IMP_NAME, "9990000002", imp),
        _registration("ing-fol", FOLATE_NAME, "9990000003", imp),
        _registration("ing-epa", EPA_NAME, "9990000004", imp),
    ]
    ingredients = [
        {"id": "i-zn", "name_zh": "葡萄糖酸鋅", "name_en": None, "name_normalized": "葡萄糖酸鋅", **SOURCE},
        {"id": "i-x", "name_zh": "虛構萃取物", "name_en": "fictional extract", "name_normalized": "虛構萃取物", **SOURCE},
    ]
    product_ingredients = [{"product_id": "ing-hf", "ingredient_id": "i-zn", "raw_text": "葡萄糖酸鋅"},
                           {"product_id": "ing-hf", "ingredient_id": "i-x", "raw_text": "虛構萃取物 50 mg"}]
    staging = {
        "ing-dom": {"ingredients_raw": "葡萄糖酸鋅、原料X、原料Y、硬脂酸鎂", "review_status": "READY",
                    "parser_draft": {"tokens": ["葡萄糖酸鋅", "原料X", "原料Y", "硬脂酸鎂"]}},
        "ing-imp": {"ingredients_raw": "葡萄糖酸銅,FISH OIL,原料Z", "review_status": "NEEDS_REVIEW",
                    "parser_draft": {"tokens": ["葡萄糖酸銅", "FISH OIL", "原料Z"]}},
        "ing-fol": {"ingredients_raw": "FOLATE,原料W", "parser_draft": {"tokens": ["FOLATE", "原料W"]}},
        "ing-epa": {"ingredients_raw": "EPA", "parser_draft": {"tokens": ["EPA"]}},
    }
    mappings = (
        _form_mapping("ing-hf", "葡萄糖酸鋅", "TF_ZN_GLUCONATE", "TN_ZN", ingredient="i-zn")
        + _form_mapping("ing-dom", "葡萄糖酸鋅", "TF_ZN_GLUCONATE", "TN_ZN", token=0)
        + [_mapping("ing-dom", "硬脂酸鎂", "AMBIGUOUS", candidates=["TN_MG"], token=3)]
        + _form_mapping("ing-imp", "葡萄糖酸銅", "TF_CU_GLUCONATE", "TN_CU", token=0)
        + [_mapping("ing-imp", "FISH OIL", "VERIFIED_EXACT", "TF_FISH_OIL", token=1),
           _mapping("ing-imp", "FISH OIL", "FORM_TO_GROUP", "TN_OMEGA3", via="TF_FISH_OIL", token=1)]
        + [_mapping("ing-fol", "FOLATE", "VERIFIED_EXACT", "TN_B9", token=0)]
        + [_mapping("ing-epa", "EPA", "VERIFIED_EXACT", "TF_EPA", token=0),
           _mapping("ing-epa", "EPA", "FORM_TO_GROUP", "TN_OMEGA3", via="TF_EPA", token=0)]
        # 其他版本的列不得混用
        + [{**_mapping("ing-dom", "原料X", "VERIFIED_EXACT", "TN_CU", token=1), "corpus_version": "T0.9"}]
    )
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + products)
    monkeypatch.setattr(data, "INGREDIENTS", data.INGREDIENTS + ingredients)
    monkeypatch.setattr(data, "PRODUCT_INGREDIENTS", data.PRODUCT_INGREDIENTS + product_ingredients)
    monkeypatch.setattr(data, "INGREDIENT_STAGING", staging)
    monkeypatch.setattr(data, "INGREDIENT_ENTITY_MAPPINGS", mappings)
    monkeypatch.setattr(data, "INGREDIENT_BRIDGE", {"corpus_version": CORPUS, "overlay_version": OVERLAY,
                                                    "entities": ENTITIES, "aliases": ALIASES})


def run(product: str, ingredients: list[str] | None = None, llm_out: dict | None = None):
    ex = extraction(intent=Intent.INGREDIENT_INQUIRY, product_candidates=[product], ingredient_candidates=ingredients or [],
                    needs_product_lookup=True, needs_graph_lookup=True)
    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    result = pipeline.run(RawUserInput(input_type=InputType.TEXT, text=user_text(ex)))
    if llm_out is not None:
        llm_out["llm"] = llm
    return result


def lookup(pid, queries=()):
    return ingredient_tools.lookup_product_ingredient_evidence(InMemoryOfficialRepo(), pid, list(queries))


def card_text(payload) -> str:
    return json.dumps(R.render_primary(payload), ensure_ascii=False)


def assert_no_internal_ids(text: str):
    assert "TN_" not in text and "TF_" not in text and "TREL" not in text


# ============================================================ 1–2. health_food


def test_health_food_existing_ingredients_with_bridge_identity():
    ev = lookup("ing-hf")
    assert [o.display_text for o in ev.official_ingredients] == ["葡萄糖酸鋅", "虛構萃取物（fictional extract）"]
    assert {(c.entity.entity_id, c.mapping_status) for c in ev.confirmed_ingredients} == {
        ("TF_ZN_GLUCONATE", "SUPPORTED_ALIAS"), ("TN_ZN", "FORM_TO_GROUP")}
    assert [u.display_text for u in ev.unresolved_ingredients] == ["虛構萃取物（fictional extract）"]
    assert ev.coverage.model_dump() == {"official_count": 2, "confirmed_count": 1, "unresolved_count": 1}
    p = run(HF_ZINC).response
    assert p.response_type == ResponseType.INGREDIENT_LIST and p.risk_level is None
    assert p.key_points[0] == "葡萄糖酸鋅（鋅的來源形式）" and "虛構萃取物（fictional extract）" in p.key_points
    assert "保健功效相關成分共 2 項" in p.summary and "1 項已確認成分身分" in p.summary


def test_health_food_without_bridge_mapping_still_lists_official_ingredients():
    ev = lookup("p-1")
    assert ev.coverage.official_count == 1 and ev.confirmed_ingredients == []
    assert ev.official_ingredients[0].display_text == "虛構素甲（fictionin-a）"
    result = run(HF_PLAIN)
    p = result.response
    assert p.response_type == ResponseType.INGREDIENT_LIST
    assert p.key_points == ["虛構素甲（fictionin-a）"] and "1 項" in p.summary
    assert "尚未" not in p.summary  # 沒有 bridge mapping 不讓健康食品看起來「沒有成分」或缺資料
    assert (p.status_line, p.status_tone) == ("官方登記成分", SemanticTone.GREEN)
    detail = detail_payload(ResponseAction.INGREDIENTS, result.normalized_request, result.evidence, result.risk)
    assert detail.body == ["官方登記的保健功效相關成分（1 項）：\n・虛構素甲（fictionin-a）"]


# ============================================================ 3–4. 國產 / 輸入


def test_domestic_raw_and_confirmed_ingredients():
    ev = lookup("ing-dom")
    assert [o.raw_text for o in ev.official_ingredients] == ["葡萄糖酸鋅", "原料X", "原料Y", "硬脂酸鎂"]
    assert ev.official_ingredients_raw == "葡萄糖酸鋅、原料X、原料Y、硬脂酸鎂"
    # T0.9 版本的列（原料X → 銅）不混用
    assert {c.entity.entity_id for c in ev.confirmed_ingredients} == {"TF_ZN_GLUCONATE", "TN_ZN"}
    assert [(u.raw_text, u.mapping_status) for u in ev.unresolved_ingredients] == [
        ("原料X", "NO_MATCH"), ("原料Y", "NO_MATCH"), ("硬脂酸鎂", "AMBIGUOUS")]
    p = run(DOM_NAME).response
    assert p.response_type == ResponseType.INGREDIENT_LIST
    assert "官方登記的成分共 4 項" in p.summary and "其餘 3 項尚未完成識別" in p.summary
    assert p.key_points == ["葡萄糖酸鋅（鋅的來源形式）", "原料X", "原料Y", "硬脂酸鎂"]


def test_imported_raw_and_confirmed_ingredients():
    ev = lookup("ing-imp")
    assert ev.product_category == ProductCategory.IMPORTED_TABLET_CAPSULE
    direct = {c.entity.entity_id: c for c in ev.confirmed_ingredients if c.is_direct}
    assert set(direct) == {"TF_CU_GLUCONATE", "TF_FISH_OIL"}
    assert direct["TF_FISH_OIL"].mapping_status == "VERIFIED_EXACT" and direct["TF_FISH_OIL"].parent_nutrient.entity_id == "TN_OMEGA3"
    assert [u.raw_text for u in ev.unresolved_ingredients] == ["原料Z"]
    p = run(IMP_NAME).response
    assert p.key_points[:2] == ["葡萄糖酸銅（銅的來源形式）", "FISH OIL（Omega-3脂肪酸群的來源形式）"]
    assert p.key_points[2] == "原料Z"


# ============================================================ 5–9. 是非題


def test_zinc_form_to_nutrient_yes():
    result = run(DOM_NAME, ["鋅"])
    answer = result.evidence.product_ingredients.query_answers[0]
    assert answer.presence == IngredientPresence.CONFIRMED and answer.query_entity.entity_id == "TN_ZN"
    assert answer.matched[0].via_entity.entity_id == "TF_ZN_GLUCONATE"  # 經 CONTRIBUTES_TO_NUTRIENT，不是 SAME_AS
    p = result.response
    assert p.summary.startswith("有。") and "葡萄糖酸鋅" in p.summary and "來源形式" in p.summary
    assert (p.status_line, p.status_tone) == ("已確認含有", SemanticTone.GREEN)


def test_copper_form_to_nutrient_yes():
    p = run(IMP_NAME, ["銅"]).response  # 「銅」只是顯示譯名：用來理解使用者問句，不是產品成分 identity
    assert p.summary.startswith("有。") and "葡萄糖酸銅" in p.summary


def test_unresolved_ingredients_do_not_block_confirmed():
    ev = lookup("ing-dom", ["葡萄糖酸鋅"])
    assert ev.coverage.unresolved_count == 3
    answer = ev.query_answers[0]
    assert answer.presence == IngredientPresence.CONFIRMED and answer.matched[0].is_direct
    p = run(DOM_NAME, ["葡萄糖酸鋅"]).response
    assert p.summary.startswith("有。") and "葡萄糖酸鋅" in p.summary


def test_ambiguous_candidate_is_not_confirmed():
    ev = lookup("ing-dom")
    assert all(c.entity.entity_id != "TN_MG" for c in ev.confirmed_ingredients)
    stearate = next(u for u in ev.unresolved_ingredients if u.raw_text == "硬脂酸鎂")
    assert stearate.mapping_status == "AMBIGUOUS" and stearate.candidate_entity_ids == ["TN_MG"]
    assert stearate.review_status == "PENDING_REVIEW"


def test_magnesium_stearate_is_not_nutritional_magnesium():
    result = run(DOM_NAME, ["鎂"])
    answer = result.evidence.product_ingredients.query_answers[0]
    assert answer.presence == IngredientPresence.CANDIDATE_ONLY and answer.matched == []
    p = result.response
    assert not p.summary.startswith("有") and "無法確認" in p.summary
    assert any("硬脂酸鎂" in k and "不能視為含有鎂" in k for k in p.key_points)
    assert (p.status_line, p.status_tone) == ("目前未能確認", SemanticTone.ORANGE)
    # 問「硬脂酸鎂」本身：只能說官方列有這個名稱，不說成營養用鎂
    listed = run(DOM_NAME, ["硬脂酸鎂"]).response
    assert "列有「硬脂酸鎂」" in listed.summary and "鎂的來源" not in card_text(listed)


# ============================================================ 10–11. identity distinctions


def test_fish_oil_omega3_epa_dha_stay_distinct():
    answers = {a.query_text: a for a in lookup("ing-imp", ["魚油", "Omega-3", "EPA", "DHA"]).query_answers}
    assert answers["魚油"].presence == IngredientPresence.CONFIRMED and answers["魚油"].matched[0].is_direct
    omega = answers["Omega-3"]
    assert omega.presence == IngredientPresence.CONFIRMED and omega.matched[0].via_entity.entity_id == "TF_FISH_OIL"
    for q in ("EPA", "DHA"):  # 魚油不等於 EPA / DHA
        assert answers[q].presence == IngredientPresence.NOT_CONFIRMED
        assert [r.entity.entity_id for r in answers[q].related_confirmed] == ["TF_FISH_OIL"]
    p = run(IMP_NAME, ["EPA"]).response
    assert "沒有找到EPA" in p.summary and any("FISH OIL" in k and "不是同一種成分" in k for k in p.key_points)
    p = run(IMP_NAME, ["Omega-3"]).response
    assert p.summary.startswith("有。") and "「FISH OIL」（魚油）是Omega-3脂肪酸群的來源形式" in p.summary
    # 反方向：只有 EPA 的產品問魚油 → 未確認（EPA 不是魚油）
    fish = lookup("ing-epa", ["魚油"]).query_answers[0]
    assert fish.presence == IngredientPresence.NOT_CONFIRMED and fish.related_confirmed[0].entity.entity_id == "TF_EPA"


def test_folate_and_folic_acid_stay_distinct():
    answers = {a.query_text: a for a in lookup("ing-fol", ["葉酸", "Folate"]).query_answers}
    assert answers["Folate"].presence == IngredientPresence.CONFIRMED  # 營養素群直接 mapping
    folic = answers["葉酸"]
    assert folic.query_entity.entity_id == "TF_FOLIC_ACID" and folic.presence == IngredientPresence.NOT_CONFIRMED
    assert [r.entity.entity_id for r in folic.related_confirmed] == ["TN_B9"]
    p = run(FOLATE_NAME, ["葉酸"]).response
    assert not p.summary.startswith("有") and any("FOLATE" in k and "不是同一種成分" in k for k in p.key_points)
    assert any("另有 1 項官方成分尚未完成識別" in k for k in p.key_points)  # 未確認 ≠ 確定沒有


# ============================================================ 12. follow-up context


def _follow_up_intake(input_text: str) -> IntakeExtraction:
    content = json.loads(input_text)["content"]
    if DOM_NAME in content:
        return extraction(intent=Intent.INGREDIENT_INQUIRY, product_candidates=[DOM_NAME], normalized_text=content)
    return extraction(intent=Intent.INGREDIENT_INQUIRY, ingredient_candidates=["鋅"], refers_to_unspecified_product=True,
                      normalized_text=content)


def test_follow_up_it_has_zinc_uses_confirmed_product_context():
    llm = FakeLLM(structured={IntakeExtraction: _follow_up_intake})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    contexts, store = TaskContextStore(), InMemoryRequestStore()
    svc = AdaptiveResponseService(pipeline, ReporterAgent(llm), store, timeout_seconds=5.0, context_store=contexts)
    handler = LineWebhookHandler(svc, FakeLine(), contexts)
    send(handler, event(text=f"{DOM_NAME}有哪些成分？"))
    assert contexts.snapshot(USER).confirmed_product.product_id == "ing-dom"
    send(handler, event(text="它有鋅嗎？"))
    rec = list(store._data.values())[-1]
    assert rec.normalized_request.product_reference_status == ProductReferenceStatus.RESOLVED_FROM_CONTEXT
    assert rec.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert rec.evidence.product_match.best_match.product.product_id == "ing-dom"
    assert rec.response.response_type == ResponseType.INGREDIENT_LIST and rec.response.summary.startswith("有。")


# ============================================================ 13. structured fast path


@pytest.mark.parametrize("product,queries", [(DOM_NAME, []), (IMP_NAME, ["EPA"]), (HF_PLAIN, []), (DOM_NAME, ["鎂"])])
def test_structured_fast_path_does_not_start_planner_rag_or_web(product, queries):
    out: dict = {}
    result = run(product, queries, llm_out=out)
    bundle = result.evidence
    assert bundle.planner_mode == FAST_PATH_MODE
    called = {t.tool_name for t in bundle.tools_called}
    assert "lookup_product_ingredients" in called
    assert not called & {"search_rag", "search_tw_official_web", "plan_tool_calls", "assess_evidence"}
    assert not out["llm"].called("plan_step") and not out["llm"].called("web_search_structured")
    assert not out["llm"].called("embed")


def test_registration_product_without_official_ingredients_keeps_existing_flow():
    """官方完全沒有成分資料才算缺 evidence：照既有 Agent B policy（不走 fast path）。"""
    data.INGREDIENT_STAGING.pop("ing-epa")
    bundle = run(EPA_NAME).evidence
    assert bundle.planner_mode != FAST_PATH_MODE and bundle.fast_path_reason == "requested_data_missing"


# ============================================================ 14. Agent C 不是固定完整模板


def test_agent_c_composes_from_evidence_not_fixed_template():
    cases = {"list": run(DOM_NAME), "yes": run(DOM_NAME, ["鋅"]), "candidate": run(DOM_NAME, ["鎂"]),
             "no": run(DOM_NAME, ["銅"]), "other_product": run(IMP_NAME)}
    summaries = {k: r.response.summary for k, r in cases.items()}
    assert len(set(summaries.values())) == len(summaries)  # 依問題語意與 evidence 而不同
    assert "4 項" in summaries["list"] and "3 項" in summaries["other_product"]
    assert "另有 3 項官方成分尚未完成識別" in cases["no"].response.key_points[-1]
    for r in cases.values():
        assert_no_internal_ids(card_text(r.response))
        assert DOM_NAME in r.response.summary or IMP_NAME in r.response.summary
    # 完整分析（LLM）只收到結構化成分資料（無內部 ID）＋規則；正文由 LLM 依問題產生
    out: dict = {}
    result = run(DOM_NAME, ["鋅"], llm_out=out)
    ReporterAgent(out["llm"]).detail(ResponseAction.DETAIL, result.normalized_request, result.evidence, result.risk)
    sent = next(c for c in out["llm"].calls if c[0] == "generate_text")[1]
    payload = json.loads(sent)["product_ingredients"]
    assert payload["ingredient_answers"][0]["presence"] == "CONFIRMED"
    assert payload["unresolved_ingredients"] == ["原料X", "原料Y", "硬脂酸鎂"]
    assert_no_internal_ids(json.dumps(payload, ensure_ascii=False))


# ============================================================ 15. detail / provenance


def test_full_ingredient_detail_and_source_provenance():
    result = run(DOM_NAME)
    p = result.response
    assert ResponseAction.INGREDIENTS in p.available_actions
    detail = detail_payload(ResponseAction.INGREDIENTS, result.normalized_request, result.evidence, result.risk)
    lines = detail.body[0].split("\n")
    assert lines[0] == "官方登記的成分（4 項）："
    assert "・葡萄糖酸鋅：已確認成分身分（鋅的來源形式）" in lines
    assert "・硬脂酸鎂：尚未完成成分識別" in lines
    assert "官方成分欄位原文：葡萄糖酸鋅、原料X、原料Y、硬脂酸鎂" in detail.body  # 研究 token 之外保留官方原文
    assert detail.available_actions == [ResponseAction.SOURCES]
    assert ResponseAction.SOURCES in reachable_actions(p.available_actions)
    sources = detail_payload(ResponseAction.SOURCES, result.normalized_request, result.evidence, result.risk)
    assert any(SOURCE["source_name"] in line for line in sources.body)
    assert result.evidence.product_ingredients.source.source_url == SOURCE["source_url"]
    assert_no_internal_ids(json.dumps(R.render_detail(detail), ensure_ascii=False))


def test_ingredient_words_inside_product_name_are_not_yes_no_questions_unless_asked_again():
    terms = ingredient_tools.ingredient_query_terms
    assert terms(["維他命C"], ["虛構牌測試維他命C錠"], "虛構牌測試維他命C錠有哪些成分？") == []
    assert terms(["鋅"], ["虛構牌蠔鋅測試膠囊"], "虛構牌蠔鋅測試膠囊有鋅嗎？") == ["鋅"]
    assert terms(["鋅", "鋅"], ["虛構牌蠔鋅測試膠囊"], "它有鋅嗎？") == ["鋅"]  # follow-up：品名來自 context
    assert terms(["維生素C"], ["虛構牌測試錠"], "有沒有維他命C") == ["維生素C"]


def test_long_ingredient_list_detail_stays_within_card_limits():
    tokens = [f"原料{i:02d}" for i in range(60)] + ["葡萄糖酸鋅"]
    data.INGREDIENT_STAGING["ing-dom"] = {"ingredients_raw": "、".join(tokens), "parser_draft": {"tokens": tokens}}
    data.INGREDIENT_ENTITY_MAPPINGS[:] = _form_mapping("ing-dom", "葡萄糖酸鋅", "TF_ZN_GLUCONATE", "TN_ZN", token=60)
    result = run(DOM_NAME)
    assert result.response.key_points[0] == "葡萄糖酸鋅（鋅的來源形式）"  # 已確認的先列，不被長清單淹沒
    detail = detail_payload(ResponseAction.INGREDIENTS, result.normalized_request, result.evidence, result.risk)
    assert len(detail.body) <= 8
    assert "另有 21 項，可參考下方官方成分欄位原文。" in detail.body
    assert any(p.startswith("官方成分欄位原文：原料00") for p in detail.body)
    card = json.dumps(R.render_detail(detail), ensure_ascii=False)
    assert "未顯示" not in card and len(card.encode("utf-8")) < 30_000
