"""Product Ingredient Mapping Bridge（虛構產品 TEST_ONLY；corpus 為測試用的小型合成 corpus，最後一組讀正式 corpus 檔）。"""

import copy
import csv
import json
import re
from pathlib import Path

import pytest

from app.ingestion import ingredient_entity_mapping as iem
from app.ingestion.ingredient_entity_mapping import (
    IngredientInput,
    IngredientSource,
    MappingStatus,
    MatchMethod,
    ProductIngredientSource,
    ReviewedAlias,
    ReviewStatus,
    graph_plan,
    health_food_source,
    load_interaction_corpus,
    map_product,
    registration_source,
)
from app.services.ingredient_bridge import (
    BridgeStatus,
    IntakeAmountAssessment,
    MissingInformation,
    detect_duplicate_ingredients,
    payload_dict,
    product_ingredient_payload,
)

ENTITY_COLS = ["entity_id", "entity_type", "canonical_name_en", "display_name_zh", "identity_level",
               "parent_nutrient_entity_id", "source_id", "source_url", "cas", "unii", "inchikey", "review_status", "notes"]
ALIAS_COLS = ["alias_id", "entity_id", "alias_text", "language", "alias_relation", "match_policy", "source_id",
              "review_status", "notes"]
RECORD_COLS = ["interaction_id", "entity_a_id", "entity_a_type", "entity_b_id", "entity_b_type", "interaction_type"]

ENTITIES = [
    # (id, en, zh, level, parent)
    ("N_CA", "Calcium", "鈣", "NUTRIENT_GROUP", None),
    ("N_ZN", "Zinc", "鋅", "NUTRIENT_GROUP", None),
    ("N_FE", "Iron", "鐵", "NUTRIENT_GROUP", None),
    ("N_CU", "Copper", "銅", "NUTRIENT_GROUP", None),
    ("N_K", "Vitamin K", "維生素K", "NUTRIENT_GROUP", None),
    ("N_D", "Vitamin D", "維生素D", "NUTRIENT_GROUP", None),
    ("N_B1", "Thiamin", "維生素B1", "NUTRIENT_GROUP", None),
    ("N_B12", "Vitamin B12", "維生素B12", "NUTRIENT_GROUP", None),
    ("N_B9", "Folate", "葉酸營養素群", "NUTRIENT_GROUP", None),
    ("N_OMEGA3", "Omega-3 fatty acids", "Omega-3脂肪酸群", "NUTRIENT_GROUP", None),
    ("F_FISH_OIL", "Fish oil", "魚油", "MIXTURE", "N_OMEGA3"),
    ("F_EPA", "Eicosapentaenoic acid", "EPA", "CHEMICAL_SUBSTANCE", "N_OMEGA3"),
    ("F_DHA", "Docosahexaenoic acid", "DHA", "CHEMICAL_SUBSTANCE", "N_OMEGA3"),
    ("F_FOLIC_ACID", "Folic acid", "葉酸（folic acid）", "CHEMICAL_SUBSTANCE", "N_B9"),
    ("F_D3", "Cholecalciferol", "維生素D3", "CHEMICAL_SUBSTANCE", "N_D"),
    ("F_CA_CARBONATE", "Calcium carbonate", "碳酸鈣", "SALT", "N_CA"),
    ("F_CA_CITRATE", "Calcium citrate", "檸檬酸鈣", "SALT", "N_CA"),
]
SYNONYMS = [("F_EPA", "EPA"), ("F_DHA", "DHA"), ("N_OMEGA3", "Omega-3"), ("N_B1", "Vitamin B1")]


def _write(path: Path, cols, rows):
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "NULL") for c in cols})


@pytest.fixture
def corpus_dir(tmp_path):
    ents = [{"entity_id": e, "entity_type": "INGREDIENT", "canonical_name_en": en, "display_name_zh": zh,
             "identity_level": lvl, "parent_nutrient_entity_id": parent or "NULL", "source_id": "TEST_ONLY_SRC",
             "source_url": "https://example.invalid/test-only", "review_status": "READY"}
            for e, en, zh, lvl, parent in ENTITIES]
    ents.append({"entity_id": "D_TEST", "entity_type": "DRUG_SUBSTANCE", "canonical_name_en": "Testdrugine",
                 "identity_level": "ACTIVE_MOIETY", "source_id": "TEST_ONLY_SRC", "review_status": "READY"})
    aliases, n = [], 0
    for e, en, zh, _, _ in ENTITIES:
        n += 1
        aliases.append({"alias_id": f"A{n:03d}", "entity_id": e, "alias_text": en, "language": "en",
                        "alias_relation": "CANONICAL_NAME", "match_policy": "EXACT_AT_DECLARED_IDENTITY_LEVEL",
                        "source_id": "TEST_ONLY_SRC", "review_status": "READY"})
        n += 1
        aliases.append({"alias_id": f"A{n:03d}", "entity_id": e, "alias_text": zh, "language": "zh-TW",
                        "alias_relation": "DISPLAY_TRANSLATION", "match_policy": "DISPLAY_ONLY_NOT_AUTOMERGE",
                        "source_id": "TEST_ONLY_SRC", "review_status": "READY"})
    for e, text in SYNONYMS:
        n += 1
        aliases.append({"alias_id": f"A{n:03d}", "entity_id": e, "alias_text": text, "language": "en",
                        "alias_relation": "SOURCED_SYNONYM_OR_ABBREVIATION",
                        "match_policy": "EXACT_AT_DECLARED_IDENTITY_LEVEL", "source_id": "TEST_ONLY_SRC",
                        "review_status": "READY"})
    # 未審核完成的 alias 不得產生正式 mapping
    aliases.append({"alias_id": "A999", "entity_id": "N_ZN", "alias_text": "Zincum", "language": "en",
                    "alias_relation": "SOURCED_SYNONYM_OR_ABBREVIATION", "match_policy": "EXACT_AT_DECLARED_IDENTITY_LEVEL",
                    "source_id": "TEST_ONLY_SRC", "review_status": "NEEDS_REVIEW"})
    _write(tmp_path / iem.ENTITIES_FILE, ENTITY_COLS, ents)
    _write(tmp_path / iem.ALIASES_FILE, ALIAS_COLS, aliases)
    _write(tmp_path / iem.RECORDS_FILE, RECORD_COLS, [
        {"interaction_id": f"DUP_{g}", "entity_a_id": f"N_{g}", "entity_a_type": "INGREDIENT", "entity_b_id": f"N_{g}",
         "entity_b_type": "INGREDIENT", "interaction_type": "DUPLICATE_INTAKE"} for g in ("CA", "ZN", "OMEGA3")
    ])
    (tmp_path / iem.VALIDATION_FILE).write_text(json.dumps({"corpus_version": "TEST_ONLY-0"}), encoding="utf-8")
    return tmp_path


@pytest.fixture
def corpus(corpus_dir):
    return load_interaction_corpus(corpus_dir)


def _product(pid: str, *tokens: str, parse_status: str = "READY") -> ProductIngredientSource:
    return ProductIngredientSource(
        product_id=pid, product_category="imported_tablet_capsule",
        ingredient_source=IngredientSource.TFDA_REGISTRATION_INGREDIENTS_RAW,
        ingredients=tuple(IngredientInput(raw_text=t, surfaces=(t,), token_index=i) for i, t in enumerate(tokens)),
        source_parse_status=parse_status, ingredients_raw=";;".join(tokens) + ";;",
    )


def _one(corpus, text):
    result = map_product(corpus, _product("TEST_ONLY_P", text))
    primary = [m for m in result.mappings if m.mapping_status != MappingStatus.FORM_TO_GROUP]
    assert len(primary) == 1
    return result, primary[0]


# ----------------------------------------------------------------------------- identity


def test_exact_nutrient_maps_to_group(corpus):
    result, m = _one(corpus, "ZINC")
    assert (m.mapping_status, m.entity_id, m.match_method) == (
        MappingStatus.VERIFIED_EXACT, "N_ZN", MatchMethod.CORPUS_CANONICAL_NAME_EXACT)
    assert m.review_status == ReviewStatus.AUTO_ACCEPTED_RULE and m.confidence == 1.0
    assert set(result.contains_targets()) == {"N_ZN"}
    assert result.nutrient_groups() == {"N_ZN": {"N_ZN"}}


def test_exact_sourced_alias_maps_to_form_and_group(corpus):
    result, m = _one(corpus, "EPA")
    assert (m.mapping_status, m.entity_id) == (MappingStatus.SUPPORTED_ALIAS, "F_EPA")
    group = [x for x in result.mappings if x.mapping_status == MappingStatus.FORM_TO_GROUP]
    assert [(g.entity_id, g.via_entity_id) for g in group] == [("N_OMEGA3", "F_EPA")]


def test_specific_form_contributes_to_group_without_direct_contains(corpus):
    result, m = _one(corpus, "Calcium Carbonate")
    assert (m.mapping_status, m.entity_id, m.identity_level) == (MappingStatus.VERIFIED_EXACT, "F_CA_CARBONATE", "SALT")
    assert set(result.contains_targets()) == {"F_CA_CARBONATE"}      # Product 不直接 CONTAINS Calcium
    assert result.nutrient_groups() == {"N_CA": {"F_CA_CARBONATE"}}
    plan = graph_plan(corpus, [result])
    assert {(c["form_entity_id"], c["nutrient_entity_id"]) for c in plan["contributes_to_nutrient"]} >= {
        ("F_CA_CARBONATE", "N_CA")}
    assert plan["relationship_counts"]["SAME_AS"] == 0
    assert [c["target_label"] for c in plan["contains"]] == ["IngredientForm"]


def test_fish_oil_epa_dha_are_distinct_identities(corpus):
    results = [map_product(corpus, _product(f"TEST_ONLY_P{i}", t)) for i, t in enumerate(("FISH OIL", "EPA", "DHA"))]
    targets = [set(r.contains_targets()) for r in results]
    assert targets == [{"F_FISH_OIL"}, {"F_EPA"}, {"F_DHA"}]
    assert all("N_OMEGA3" not in t for t in targets)                 # 不是 Omega-3 本身
    dup = detect_duplicate_ingredients(corpus, results)
    [cand] = dup.duplicate_candidates
    assert cand.nutrient.entity_id == "N_OMEGA3"
    assert cand.same_identity_level_across_products is False         # 魚油 / EPA / DHA 不可視為同一物質


def test_folate_is_not_folic_acid(corpus):
    _, folic = _one(corpus, "Folic acid")
    _, folate = _one(corpus, "Folate")
    assert folic.entity_id == "F_FOLIC_ACID" and folate.entity_id == "N_B9"
    assert corpus.parent("F_FOLIC_ACID") == "N_B9" and folic.entity_id != folate.entity_id


def test_calcium_carbonate_is_not_same_as_calcium(corpus):
    _, carb = _one(corpus, "CALCIUM CARBONATE")
    _, ca = _one(corpus, "CALCIUM")
    assert carb.entity_id == "F_CA_CARBONATE" and ca.entity_id == "N_CA"
    assert corpus.entities["F_CA_CARBONATE"].graph_label == "IngredientForm"
    assert corpus.entities["N_CA"].graph_label == "NutrientGroup"


def test_official_parenthetical_picks_most_specific_consistent_entity(corpus):
    _, m = _one(corpus, "VITAMIN D (CHOLECALCIFEROL)")
    assert (m.mapping_status, m.entity_id, m.match_method) == (
        MappingStatus.SUPPORTED_ALIAS, "F_D3", MatchMethod.OFFICIAL_PARENTHETICAL)
    # 括號是未知鹽類：只到營養素群，不推測 form
    _, m = _one(corpus, "VITAMIN B12 (CYANOCOBALAMIN)")
    assert (m.mapping_status, m.entity_id) == (MappingStatus.SUPPORTED_ALIAS, "N_B12")


# ----------------------------------------------------------------------------- 候選不建立 CONTAINS


@pytest.mark.parametrize("text", ["Calcium (Zinc)", "CALCIUM ZINC BLEND", "FISH OIL CONCENTRATE (EPA)"])
def test_ambiguous_does_not_create_contains(corpus, text):
    result, m = _one(corpus, text)
    assert m.mapping_status == MappingStatus.AMBIGUOUS and m.entity_id is None
    assert len(m.candidate_entity_ids) >= 2 and m.review_status == ReviewStatus.PENDING_REVIEW
    assert result.contains_targets() == {} and graph_plan(corpus, [result])["contains"] == []


@pytest.mark.parametrize("text,candidate,method", [
    ("碳酸鈣", "F_CA_CARBONATE", MatchMethod.DISPLAY_TRANSLATION_EXACT),     # 中文譯名完全相同也只是候選
    ("ZINC OXIDE", "N_ZN", MatchMethod.ALIAS_CONTAINED_IN_TEXT),            # 不做去鹽名合併
    ("葡萄糖酸鋅", "N_ZN", MatchMethod.ALIAS_CONTAINED_IN_TEXT),
    ("Zincum", "N_ZN", MatchMethod.CORPUS_ALIAS_NOT_READY),                  # 未 READY 的 alias 不能 exact
])
def test_needs_review_does_not_create_contains(corpus, text, candidate, method):
    result, m = _one(corpus, text)
    assert m.mapping_status == MappingStatus.NEEDS_REVIEW and m.entity_id is None
    assert candidate in m.candidate_entity_ids and m.match_method == method
    assert result.contains_targets() == {} and not result.formal()


def test_no_semantic_or_suffix_guessing(corpus):
    for text in ("膽鈣化醇", "VITAMIN B12 COMPLEX", "ENVIRONMENTAL IRONY", "CALCIFEROL"):
        _, m = _one(corpus, text)
        assert "N_CA" not in m.candidate_entity_ids and "N_B1" not in m.candidate_entity_ids
        assert "N_FE" not in m.candidate_entity_ids and not m.entity_id


def test_reviewed_chinese_alias_is_the_only_way_to_formalize_chinese(corpus_dir):
    reviewed = load_interaction_corpus(corpus_dir, reviewed_aliases=[
        ReviewedAlias(alias_text="碳酸鈣", entity_id="F_CA_CARBONATE", reviewer="TEST_ONLY", basis="TEST_ONLY")])
    result = map_product(reviewed, _product("TEST_ONLY_P", "碳酸鈣"))
    [m] = [x for x in result.mappings if x.mapping_status != MappingStatus.FORM_TO_GROUP]
    assert (m.mapping_status, m.entity_id, m.review_status) == (
        MappingStatus.SUPPORTED_ALIAS, "F_CA_CARBONATE", ReviewStatus.MANUAL_REVIEWED)


def test_parse_readiness_does_not_change_mapping(corpus):
    tokens = ("CALCIUM CARBONATE", "碳酸鈣", "ZINC OXIDE", "GELATIN")
    ready = map_product(corpus, _product("TEST_ONLY_P", *tokens, parse_status="READY"))
    review = map_product(corpus, _product("TEST_ONLY_P", *tokens, parse_status="NEEDS_REVIEW"))
    key = lambda r: [(m.raw_text, m.mapping_status, m.entity_id, m.candidate_entity_ids) for m in r.mappings]  # noqa: E731
    assert key(ready) == key(review)


# ----------------------------------------------------------------------------- 來源 / raw / amount


def test_raw_ingredient_unchanged(corpus):
    staging = {"ingredients_raw": "CALCIUM CARBONATE;;FD &amp; C RED #40;;魚油;;",
               "parser_draft": {"tokens": ["CALCIUM CARBONATE", "FD &amp; C RED #40", "魚油"]},
               "review_status": "NEEDS_REVIEW"}
    before = copy.deepcopy(staging)
    src = registration_source({"id": "TEST_ONLY_P", "product_category": "imported_tablet_capsule"}, staging)
    result = map_product(corpus, src)
    assert staging == before
    assert src.ingredients_raw == before["ingredients_raw"]
    assert [m.raw_text for m in result.mappings if m.mapping_status != MappingStatus.FORM_TO_GROUP] == \
        before["parser_draft"]["tokens"]


def test_health_food_uses_official_bilingual_names_only(corpus):
    product = {"id": "TEST_ONLY_HF", "product_category": "health_food"}
    pis = [{"product_id": "TEST_ONLY_HF", "ingredient_id": "I1", "raw_text": "TEST_ONLY 鈣 Calcium 225毫克",
            "amount_text": "225毫克"},
           {"product_id": "TEST_ONLY_HF", "ingredient_id": "I2", "raw_text": "TEST_ONLY 鋅", "amount_text": None}]
    ings = {"I1": {"name_zh": "鈣", "name_en": None}, "I2": {"name_zh": "鋅", "name_en": None}}
    aliases = {"I1": [{"alias": "Calcium", "evidence_status": "OFFICIAL_RECORD"}],
               "I2": [{"alias": "Zinc", "evidence_status": "INFERRED"}]}            # 推測別名不採用
    result = map_product(corpus, health_food_source(product, pis, ings, aliases))
    by_ing = {m.source_ingredient_id: m for m in result.mappings}
    assert (by_ing["I1"].mapping_status, by_ing["I1"].entity_id) == (MappingStatus.VERIFIED_EXACT, "N_CA")
    assert by_ing["I1"].raw_text == "TEST_ONLY 鈣 Calcium 225毫克"
    assert (by_ing["I1"].amount_raw, by_ing["I1"].unit, by_ing["I1"].amount_normalized) == ("225毫克", "mg", None)
    assert by_ing["I2"].mapping_status == MappingStatus.NEEDS_REVIEW


def test_health_food_conflicting_official_names_are_ambiguous(corpus):
    product = {"id": "TEST_ONLY_HF", "product_category": "health_food"}
    pis = [{"product_id": "TEST_ONLY_HF", "ingredient_id": "I1", "raw_text": "TEST_ONLY"}]
    ings = {"I1": {"name_zh": "TEST_ONLY成分", "name_en": "Calcium"}}
    aliases = {"I1": [{"alias": "Zinc", "evidence_status": "OFFICIAL_RECORD"}]}
    [m] = map_product(corpus, health_food_source(product, pis, ings, aliases)).mappings
    assert m.mapping_status == MappingStatus.AMBIGUOUS and set(m.candidate_entity_ids) == {"N_CA", "N_ZN"}


# ----------------------------------------------------------------------------- graph / duplicate / payload


def test_no_product_to_product_relationship(corpus):
    results = [map_product(corpus, _product("TEST_ONLY_A", "ZINC")),
               map_product(corpus, _product("TEST_ONLY_B", "ZINC"))]
    plan = graph_plan(corpus, results)
    assert plan["relationship_counts"]["INTERACTS_WITH"] == 0
    assert all(c["target_label"] in ("NutrientGroup", "IngredientForm") for c in plan["contains"])
    assert all(not c["entity_id"].startswith("TEST_ONLY") for c in plan["contains"])
    dumped = json.dumps(payload_dict(detect_duplicate_ingredients(corpus, results)))
    assert "INTERACTS_WITH" not in dumped


def test_duplicate_without_amount_is_not_overdose(corpus):
    a = _product("TEST_ONLY_A", "ZINC")
    b = ProductIngredientSource(**{**a.__dict__, "product_id": "TEST_ONLY_B", "ingredients": (
        IngredientInput(raw_text="ZINC", surfaces=("ZINC",), token_index=0, amount_raw="5000 毫克"),)})
    dup = detect_duplicate_ingredients(corpus, [map_product(corpus, a), map_product(corpus, b)])
    [cand] = dup.duplicate_candidates
    assert cand.finding.value == "DUPLICATE_INGREDIENT_DETECTED" and cand.duplicate_template_id == "DUP_ZN"
    assert cand.intake_amount_assessment == IntakeAmountAssessment.NOT_EVALUATED
    assert set(cand.missing_information) == {MissingInformation.AMOUNT_PER_SERVING, MissingInformation.SERVING_SIZE,
                                             MissingInformation.DAILY_SERVINGS, MissingInformation.AMOUNT_BASIS}
    text = json.dumps(payload_dict(dup), ensure_ascii=False).lower()
    for word in ("exceed", "overdose", "upper limit", "dangerous", "過量", "超過", "危險", "上限"):
        assert word not in text


def test_candidate_only_products_do_not_join_duplicates(corpus):
    dup = detect_duplicate_ingredients(corpus, [map_product(corpus, _product("TEST_ONLY_A", "ZINC")),
                                                map_product(corpus, _product("TEST_ONLY_B", "ZINC OXIDE"))])
    assert dup.duplicate_candidates == [] and dup.excluded_candidate_only_products == ["TEST_ONLY_B"]


FREE_TEXT_KEYS = {"raw_text", "raw_texts", "official_raw_ingredients", "unresolved_raw_texts", "canonical_name_en", "display_name_zh", "amount_raw", "unit",
                  "source_url", "product_category"}
ENUM_LIKE = re.compile(r"^[A-Za-z0-9_:.\-]+$")


def _strings(obj, key=None):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, key)
    elif isinstance(obj, str):
        yield key, obj


def test_structured_payload_contains_no_hard_coded_prose(corpus):
    results = [map_product(corpus, _product("TEST_ONLY_A", "CALCIUM CARBONATE", "碳酸鈣", "Calcium (Zinc)", "GELATIN")),
               map_product(corpus, _product("TEST_ONLY_B", "CALCIUM CITRATE"))]
    payloads = [payload_dict(product_ingredient_payload(corpus, r)) for r in results]
    payloads.append(payload_dict(detect_duplicate_ingredients(corpus, results)))
    for key, value in (kv for p in payloads for kv in _strings(p)):
        if key in FREE_TEXT_KEYS:
            continue
        assert ENUM_LIKE.match(value), f"{key}={value!r} 不是 enum / ID"
    first = payloads[0]
    assert first["bridge_status"] == BridgeStatus.INGREDIENTS_MAPPED
    assert {m["entity"]["entity_id"] for m in first["matched_ingredients"]} == {"F_CA_CARBONATE"}
    assert [c["entity_id"] for c in first["matched_ingredients"][0]["contributes_to"]] == ["N_CA"]
    assert {c["mapping_status"] for c in first["candidate_ingredients"]} == {"NEEDS_REVIEW", "AMBIGUOUS"}
    assert "INGREDIENT_MAPPING_REVIEW" in first["missing_information"]


def test_no_source_ingredients_payload(corpus):
    src = registration_source({"id": "TEST_ONLY_P", "product_category": "domestic_vitamin_tablet_capsule"}, None)
    payload = product_ingredient_payload(corpus, map_product(corpus, src))
    assert payload.bridge_status == BridgeStatus.NO_SOURCE_INGREDIENT
    assert payload.missing_information == [MissingInformation.SOURCE_INGREDIENT_LIST]


# ----------------------------------------------------------------------------- 正式 corpus 檔


def test_real_corpus_loads_with_identity_levels():
    if not (iem.CORPUS_DIR / iem.ENTITIES_FILE).is_file():
        pytest.skip("interaction corpus 不存在")
    corpus = load_interaction_corpus()
    assert corpus.version == "1.0"
    assert all(not e.startswith("D_") for e in corpus.entities)            # drug substance 不在 product mapping 範圍
    groups = [e for e in corpus.entities.values() if e.is_nutrient_group]
    assert len(groups) == 17 and len(corpus.duplicate_templates) == 17
    for a, b in (("F_FISH_OIL", "F_EPA"), ("F_EPA", "F_DHA"), ("F_FOLIC_ACID", "N_B9"), ("F_CA_CARBONATE", "N_CA")):
        assert a != b and corpus.entities[a].entity_id != corpus.entities[b].entity_id
    # 正式 corpus 的中文譯名全部是 display-only：中文完全相同只能是候選
    for zh in ("碳酸鈣", "鈣", "魚油", "維生素D3"):
        result = map_product(corpus, _product("TEST_ONLY_P", zh))
        assert not result.formal()
