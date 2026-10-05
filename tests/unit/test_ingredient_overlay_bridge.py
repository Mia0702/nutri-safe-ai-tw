"""Reviewed overlay × Product Ingredient Bridge（虛構產品 TEST_ONLY；合成 corpus＋合成 overlay，最後一組讀正式 overlay 檔）。"""

import copy
import csv
import hashlib
import json
import re

import pytest

from app.ingestion import ingredient_entity_mapping as iem
from app.ingestion.ingredient_entity_mapping import (
    IngredientSource,
    MappingStatus,
    MatchMethod,
    ReviewStatus,
    graph_plan,
    health_food_source,
    load_interaction_corpus,
    load_reviewed_overlay,
    map_product,
    registration_source,
)
from app.services.ingredient_bridge import (
    BridgeStatus,
    detect_duplicate_ingredients,
    payload_dict,
    product_ingredient_payload,
)
from scripts.dry_run_ingredient_entity_mapping import _digest, health_food_protection, map_all
from tests.unit.test_ingredient_entity_mapping import _product, corpus_dir  # noqa: F401（fixture）

ALIAS_COLS = ["alias_id", "alias_text", "language", "entity_id", "entity_type", "identity_level", "mapping_status",
              "source_id", "source_url", "source_locator", "review_status", "retrieved_at", "candidate_entity_id",
              "match_policy", "mapping_context", "precedence", "notes", "overlay_version"]
FORM_COLS = ["form_entity_id", "canonical_name_en", "official_name_zh", "parent_nutrient_entity_id", "relationship_type",
             "source_id", "source_url", "source_locator", "identity_level", "review_status", "existing_corpus_entity",
             "overlay_version"]
LINK_COLS = ["mapping_id", "form_entity_id", "parent_nutrient_entity_id", "relationship_type", "source_id", "source_url",
             "source_locator", "evidence_text", "review_status", "retrieved_at", "amount_conversion_allowed",
             "automatic_interaction_inheritance", "notes"]
SOURCE_COLS = ["source_id", "authority", "source_tier", "title", "source_url", "source_type", "sha256"]
EXACT = "EXACT_NFKC_CASEFOLD_WHITESPACE_AT_DECLARED_LEVEL"
CANDIDATE = "CANDIDATE_ONLY_NOT_AUTOMERGE"

# (alias_text, entity_id, status, candidate, precedence)
ALIASES = [
    ("碳酸鈣", "F_CA_CARBONATE", "VERIFIED_OFFICIAL", None, "EXACT_OVERLAY"),
    ("Calcium carbonate", "F_CA_CARBONATE", "VERIFIED_OFFICIAL", None, "EXACT_OVERLAY"),   # 與 corpus 正式名稱一致
    ("鋅", "N_ZN", "SUPPORTED_AUTHORITATIVE", None, "EXACT_OVERLAY"),
    ("TEST_ONLY氧化鋅", "F_ZN_TEST_OXIDE", "VERIFIED_OFFICIAL", None, "EXACT_OVERLAY"),
    ("Thiamin", "F_TEST_THIAMIN", "VERIFIED_OFFICIAL", None, "PRODUCT_FORM_CONTEXT_OVER_BASE_NUTRIENT_ALIAS"),
    ("魚油", "F_FISH_OIL", "VERIFIED_OFFICIAL", None, "EXACT_OVERLAY"),
    ("葉酸", "F_FOLIC_ACID", "VERIFIED_OFFICIAL", None, "EXACT_OVERLAY"),
    ("TEST_ONLY硬脂酸鎂", None, "AMBIGUOUS", "N_ZN", "EXACT_OVERLAY"),
    ("TEST_ONLY複方鈣", None, "NEEDS_REVIEW", "N_CA", "EXACT_OVERLAY"),
    ("TEST_ONLY未定成分", None, "NEEDS_REVIEW", None, "EXACT_OVERLAY"),
    ("Zinc", None, "NEEDS_REVIEW", "N_ZN", "EXACT_OVERLAY"),            # 審核結論是候選 → 即使 corpus exact 也不轉正
]
FORMS = [("F_ZN_TEST_OXIDE", "Zinc test oxide", "TEST_ONLY氧化鋅", "N_ZN", "SALT_FAMILY_UNSPECIFIED_HYDRATION", False),
         ("F_TEST_THIAMIN", "Thiamin test form", "TEST_ONLY硫胺", "N_B1", "CHEMICAL_FORM", False),
         ("F_CA_CARBONATE", "Calcium carbonate", "碳酸鈣", "N_CA", "SALT_FAMILY_UNSPECIFIED_HYDRATION", True)]


def _write(path, cols, rows):
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "NULL") for c in cols})


def _sums(directory):
    lines = [f"{hashlib.sha256((directory / n).read_bytes()).hexdigest()}  {n}" for n in iem.OVERLAY_FILES]
    (directory / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_overlay(directory, aliases=ALIASES, forms=FORMS, link_override=None):
    directory.mkdir(exist_ok=True)
    rows = [{"alias_id": f"TEST_ONLY_OV_{i:03d}", "alias_text": t, "language": "zh-Hant", "entity_id": e or "NULL",
             "mapping_status": s, "source_id": "TEST_ONLY_SRC", "source_url": "https://example.invalid/test-only",
             "source_locator": f"TEST_ONLY row {i}", "review_status": "READY" if e else s,
             "candidate_entity_id": c or "NULL", "match_policy": EXACT if e else CANDIDATE,
             "mapping_context": "OFFICIAL_PRODUCT_INGREDIENT_TOKEN", "precedence": p, "overlay_version": "TEST_ONLY-OV"}
            for i, (t, e, s, c, p) in enumerate(aliases)]
    _write(directory / iem.OVERLAY_ALIASES_FILE, ALIAS_COLS, rows)
    _write(directory / iem.OVERLAY_FORMS_FILE, FORM_COLS, [
        {"form_entity_id": f, "canonical_name_en": en, "official_name_zh": zh, "parent_nutrient_entity_id": parent,
         "relationship_type": "CONTRIBUTES_TO_NUTRIENT", "source_id": "TEST_ONLY_SRC", "identity_level": lvl,
         "review_status": "VERIFIED_OFFICIAL", "existing_corpus_entity": str(existing)}
        for f, en, zh, parent, lvl, existing in forms])
    links = [{"mapping_id": f"TEST_ONLY_REL_{f}", "form_entity_id": f, "parent_nutrient_entity_id": parent,
              "relationship_type": "CONTRIBUTES_TO_NUTRIENT", "source_id": "TEST_ONLY_SRC",
              "source_locator": f"TEST_ONLY rel {f}", "review_status": "VERIFIED_OFFICIAL",
              "amount_conversion_allowed": "false", "automatic_interaction_inheritance": "false"}
             for f, _, _, parent, _, _ in forms]
    if link_override:
        links[0].update(link_override)
    _write(directory / iem.OVERLAY_LINKS_FILE, LINK_COLS, links)
    _write(directory / iem.OVERLAY_SOURCES_FILE, SOURCE_COLS, [
        {"source_id": "TEST_ONLY_SRC", "authority": "TEST_ONLY", "source_tier": "A", "title": "TEST_ONLY"}])
    (directory / iem.OVERLAY_VALIDATION_FILE).write_text(json.dumps({"overlay_version": "TEST_ONLY-OV"}),
                                                         encoding="utf-8")
    _sums(directory)
    return directory


@pytest.fixture
def ov_corpus(corpus_dir, tmp_path):  # noqa: F811
    return load_interaction_corpus(corpus_dir, overlay=load_reviewed_overlay(build_overlay(tmp_path / "overlay")))


def _primary(result):
    return [m for m in result.mappings if m.mapping_status != MappingStatus.FORM_TO_GROUP]


def _one(corpus, text):
    result = map_product(corpus, _product("TEST_ONLY_P", text))
    [m] = _primary(result)
    return result, m


# ----------------------------------------------------------------------------- 正式 / 候選


def test_reviewed_official_alias_is_formal_mapping(ov_corpus):
    result, m = _one(ov_corpus, "碳酸鈣")      # base corpus 中文譯名只能候選；overlay 審核後才正式
    assert (m.mapping_status, m.entity_id, m.match_method) == (
        MappingStatus.SUPPORTED_ALIAS, "F_CA_CARBONATE", MatchMethod.REVIEWED_OVERLAY_ALIAS_EXACT)
    assert m.review_status == ReviewStatus.SOURCE_REVIEWED and m.alias_evidence_status == "VERIFIED_OFFICIAL"
    assert m.entity_type == "IngredientForm" and m.overlay_version == "TEST_ONLY-OV"
    assert m.matched_alias_ids and m.matched_alias_ids[0].startswith("TEST_ONLY_OV_")
    assert set(result.contains_targets()) == {"F_CA_CARBONATE"}


def test_supported_authoritative_alias_is_formal_mapping(ov_corpus):
    result, m = _one(ov_corpus, "鋅")
    assert (m.mapping_status, m.entity_id, m.alias_evidence_status) == (
        MappingStatus.SUPPORTED_ALIAS, "N_ZN", "SUPPORTED_AUTHORITATIVE")
    assert m.confidence < 0.95 and m.entity_type == "NutrientGroup"
    assert result.nutrient_groups() == {"N_ZN": {"N_ZN"}}


@pytest.mark.parametrize("text,status,candidates", [
    ("TEST_ONLY硬脂酸鎂", MappingStatus.AMBIGUOUS, ("N_ZN",)),
    ("TEST_ONLY複方鈣", MappingStatus.NEEDS_REVIEW, ("N_CA",)),
    ("TEST_ONLY未定成分", MappingStatus.NEEDS_REVIEW, ()),     # 審核為候選但未指定 entity
    ("Zinc", MappingStatus.NEEDS_REVIEW, ("N_ZN",)),          # overlay 候選優先於 corpus exact
])
def test_ambiguous_and_needs_review_are_candidate_only(ov_corpus, text, status, candidates):
    result, m = _one(ov_corpus, text)
    assert m.mapping_status == status and m.entity_id is None and m.entity_type is None
    assert m.candidate_entity_ids == candidates and m.review_status == ReviewStatus.PENDING_REVIEW
    assert m.match_method == MatchMethod.REVIEWED_OVERLAY_CANDIDATE and m.matched_alias_ids
    assert not result.formal() and result.contains_targets() == {} and result.nutrient_groups() == {}
    assert graph_plan(ov_corpus, [result])["contains"] == []


def test_specific_overlay_form_maps_to_form_entity_with_reviewed_relationship(ov_corpus):
    result, m = _one(ov_corpus, "TEST_ONLY氧化鋅")
    assert (m.entity_id, m.entity_type) == ("F_ZN_TEST_OXIDE", "IngredientForm")
    entity = ov_corpus.entities["F_ZN_TEST_OXIDE"]
    assert entity.origin == iem.OVERLAY_ORIGIN and entity.parent_nutrient_entity_id == "N_ZN"
    [group] = [x for x in result.mappings if x.mapping_status == MappingStatus.FORM_TO_GROUP]
    assert (group.entity_id, group.via_entity_id, group.match_method) == (
        "N_ZN", "F_ZN_TEST_OXIDE", MatchMethod.REVIEWED_FORM_TO_NUTRIENT)
    assert group.relationship_id == "TEST_ONLY_REL_F_ZN_TEST_OXIDE" and group.entity_type == "NutrientGroup"
    assert set(result.contains_targets()) == {"F_ZN_TEST_OXIDE"}           # Product 不直接 CONTAINS Zinc


def test_form_to_nutrient_relationship_has_provenance_and_no_same_as(ov_corpus):
    result, _ = _one(ov_corpus, "TEST_ONLY氧化鋅")
    plan = graph_plan(ov_corpus, [result])
    [link] = [c for c in plan["contributes_to_nutrient"] if c["form_entity_id"] == "F_ZN_TEST_OXIDE"]
    assert (link["nutrient_entity_id"], link["relationship_type"], link["basis"]) == (
        "N_ZN", "CONTRIBUTES_TO_NUTRIENT", "REVIEWED_OVERLAY")
    assert link["relationship_id"] and link["source_id"] == "TEST_ONLY_SRC" and link["source_locator"]
    assert link["amount_conversion_allowed"] is False and link["automatic_interaction_inheritance"] is False
    assert {c["relationship_type"] for c in plan["contributes_to_nutrient"]} == {"CONTRIBUTES_TO_NUTRIENT"}
    assert plan["relationship_counts"]["SAME_AS"] == 0
    assert all(c["form_entity_id"] != c["nutrient_entity_id"] for c in plan["contributes_to_nutrient"])
    assert "SAME_AS" not in json.dumps([plan["contains"], plan["contributes_to_nutrient"]])


def test_overlay_product_form_context_beats_generic_nutrient_alias(corpus_dir, ov_corpus):  # noqa: F811
    base = load_interaction_corpus(corpus_dir)
    _, before = _one(base, "Thiamin")
    assert before.entity_id == "N_B1"                                       # base：營養素群名稱
    result, after = _one(ov_corpus, "Thiamin")
    assert after.entity_id == "F_TEST_THIAMIN"                              # 產品原料語境：較具體 form
    assert after.overlay_precedence == "PRODUCT_FORM_CONTEXT_OVER_BASE_NUTRIENT_ALIAS"
    assert result.nutrient_groups() == {"N_B1": {"F_TEST_THIAMIN"}}         # 營養素群 coverage 不變


def test_overlay_agreeing_with_corpus_canonical_keeps_verified_exact(ov_corpus):
    _, m = _one(ov_corpus, "CALCIUM CARBONATE")
    assert (m.mapping_status, m.entity_id, m.match_method) == (
        MappingStatus.VERIFIED_EXACT, "F_CA_CARBONATE", MatchMethod.CORPUS_CANONICAL_NAME_EXACT)
    assert m.alias_evidence_status == "VERIFIED_OFFICIAL" and any(a.startswith("TEST_ONLY_OV_") for a in m.matched_alias_ids)


def test_tokens_without_overlay_row_use_unchanged_base_rules(corpus_dir, ov_corpus):  # noqa: F811
    base = load_interaction_corpus(corpus_dir)
    for text in ("ZINC OXIDE", "VITAMIN D (CHOLECALCIFEROL)", "GELATIN", "Calcium (Zinc)"):
        _, b = _one(base, text)
        _, o = _one(ov_corpus, text)
        assert (b.mapping_status, b.entity_id, b.candidate_entity_ids, b.match_method) == (
            o.mapping_status, o.entity_id, o.candidate_entity_ids, o.match_method)


@pytest.mark.parametrize("text", ["碳酸鈣 99%", "TEST_ONLY(鋅)", "複方碳酸鈣", "鋅 TEST_ONLY", "TEST_ONLY氧化鋅(鋅)"])
def test_overlay_is_whole_token_exact_only(ov_corpus, text):
    # 不去括號、不去含量 / 字首、不部分包含：overlay alias 只做整段 exact
    _, m = _one(ov_corpus, text)
    assert m.match_method not in (MatchMethod.REVIEWED_OVERLAY_ALIAS_EXACT, MatchMethod.REVIEWED_OVERLAY_CANDIDATE)
    assert m.alias_evidence_status is None


def test_overlay_key_is_format_only(ov_corpus):
    _, m = _one(ov_corpus, "  ｃａｌｃｉｕｍ   CARBONATE ")              # 全形 / 大小寫 / 空白
    assert m.entity_id == "F_CA_CARBONATE"
    _, m = _one(ov_corpus, "Calcium-carbonate")                          # 標點不是 overlay 格式差異
    assert m.match_method != MatchMethod.REVIEWED_OVERLAY_ALIAS_EXACT


# ----------------------------------------------------------------------------- identity separation


def test_fish_oil_omega3_epa_dha_stay_distinct_with_overlay(ov_corpus):
    targets = {t: set(map_product(ov_corpus, _product("TEST_ONLY_P", t)).contains_targets())
               for t in ("魚油", "FISH OIL", "EPA", "DHA", "Omega-3")}
    assert targets == {"魚油": {"F_FISH_OIL"}, "FISH OIL": {"F_FISH_OIL"}, "EPA": {"F_EPA"}, "DHA": {"F_DHA"},
                       "Omega-3": {"N_OMEGA3"}}
    assert len({frozenset(v) for v in targets.values()}) == 4


def test_folate_and_folic_acid_stay_distinct_with_overlay(ov_corpus):
    _, folic_zh = _one(ov_corpus, "葉酸")
    _, folate = _one(ov_corpus, "Folate")
    assert folic_zh.entity_id == "F_FOLIC_ACID" and folate.entity_id == "N_B9"
    assert ov_corpus.parent("F_FOLIC_ACID") == "N_B9"


# ----------------------------------------------------------------------------- loader fail closed


def _broken(tmp_path, **kw):
    return load_reviewed_overlay(build_overlay(tmp_path / "broken", **kw))


def test_overlay_sha256_mismatch_fails_closed(tmp_path):
    directory = build_overlay(tmp_path / "ov")
    with (directory / iem.OVERLAY_ALIASES_FILE).open("a", encoding="utf-8") as fh:
        fh.write("tampered\n")
    with pytest.raises(ValueError, match="sha256"):
        load_reviewed_overlay(directory)


@pytest.mark.parametrize("kw,match", [
    ({"aliases": [("TEST_ONLY", "N_ZN", "AMBIGUOUS", None, "EXACT_OVERLAY")]}, "候選"),          # 候選不得有正式 entity
    ({"aliases": [("TEST_ONLY", None, "VERIFIED_OFFICIAL", None, "EXACT_OVERLAY")]}, "正式"),     # 正式必須有 entity
    ({"aliases": [("Omega-3", "F_FISH_OIL", "VERIFIED_OFFICIAL", None, "EXACT_OVERLAY"),
                  ("omega-3", "F_EPA", "VERIFIED_OFFICIAL", None, "EXACT_OVERLAY")]}, "多個"),   # 同一文字兩個 entity
    ({"link_override": {"relationship_type": "SAME_AS"}}, "CONTRIBUTES_TO_NUTRIENT"),
    ({"link_override": {"amount_conversion_allowed": "true"}}, "換算"),
])
def test_overlay_structural_violations_fail_closed(tmp_path, kw, match):
    with pytest.raises(ValueError, match=match):
        _broken(tmp_path, **kw)


def test_overlay_parent_conflicting_with_corpus_fails_closed(corpus_dir, tmp_path):  # noqa: F811
    overlay = _broken(tmp_path, forms=[("F_CA_CARBONATE", "Calcium carbonate", "碳酸鈣", "N_ZN", "SALT", True)])
    with pytest.raises(ValueError, match="parent"):
        load_interaction_corpus(corpus_dir, overlay=overlay)


# ----------------------------------------------------------------------------- health_food / raw / interaction / payload


def _hf_data():
    return {
        "products": [{"id": "TEST_ONLY_HF", "product_category": "health_food"},
                     {"id": "TEST_ONLY_REG", "product_category": "imported_tablet_capsule", "raw_record_id": "R1"}],
        "ingredients": [{"id": "I1", "name_zh": "碳酸鈣", "name_en": None}, {"id": "I2", "name_zh": "TEST_ONLY成分",
                                                                          "name_en": None}],
        "product_ingredients": [
            {"product_id": "TEST_ONLY_HF", "ingredient_id": "I1", "raw_text": "TEST_ONLY 碳酸鈣 500毫克", "amount_text": "500毫克"},
            {"product_id": "TEST_ONLY_HF", "ingredient_id": "I2", "raw_text": "TEST_ONLY 成分", "amount_text": None}],
        "aliases": [],
        "raw_staging": [{"id": "R1", "staging": {"ingredients_raw": "碳酸鈣;;TEST_ONLY硬脂酸鎂;;",
                                                 "parser_draft": {"tokens": ["碳酸鈣", "TEST_ONLY硬脂酸鎂"]}}}],
    }


def test_existing_health_food_product_ingredients_unchanged(ov_corpus):
    data = _hf_data()
    before = copy.deepcopy(data)
    digest = _digest(data)
    results = map_all(ov_corpus, data)
    assert data == before                                                  # 不重建 / 覆蓋 / 刪除既有成分
    protection = health_food_protection(data, results, digest)
    assert protection["links_carried_exactly_once"] and protection["raw_text_unchanged"]
    assert protection["input_snapshot_unchanged"] and protection["result_product_ids_equal_input"]
    assert protection["existing_product_ingredient_links"] == 2
    [hf] = [r for r in results if r.product_id == "TEST_ONLY_HF"]
    by_ing = {m.source_ingredient_id: m for m in _primary(hf)}
    assert by_ing["I1"].entity_id == "F_CA_CARBONATE"                      # overlay 只補 entity mapping
    assert by_ing["I1"].raw_text == "TEST_ONLY 碳酸鈣 500毫克" and by_ing["I1"].amount_raw == "500毫克"
    assert by_ing["I2"].mapping_status == MappingStatus.NO_MATCH          # 沒有對應的既有成分仍保留、不刪除
    assert hf.source.ingredient_source == IngredientSource.HEALTH_FOOD_CANONICAL_INGREDIENT


def test_health_food_source_does_not_mutate_inputs(ov_corpus):
    data = _hf_data()
    product, pis = data["products"][0], data["product_ingredients"]
    ings = {i["id"]: i for i in data["ingredients"]}
    before = copy.deepcopy((product, pis, ings))
    map_product(ov_corpus, health_food_source(product, pis, ings, {}))
    assert (product, pis, ings) == before


def test_raw_ingredient_unchanged_with_overlay(ov_corpus):
    staging = {"ingredients_raw": "碳酸鈣;;TEST_ONLY硬脂酸鎂;;TEST_ONLY氧化鋅;;",
               "parser_draft": {"tokens": ["碳酸鈣", "TEST_ONLY硬脂酸鎂", "TEST_ONLY氧化鋅"]},
               "review_status": "NEEDS_REVIEW"}
    before = copy.deepcopy(staging)
    src = registration_source({"id": "TEST_ONLY_P", "product_category": "domestic_vitamin_tablet_capsule"}, staging)
    result = map_product(ov_corpus, src)
    assert staging == before and src.ingredients_raw == before["ingredients_raw"]
    assert [m.raw_text for m in _primary(result)] == before["parser_draft"]["tokens"]


def test_no_interaction_generated(ov_corpus):
    results = [map_product(ov_corpus, _product("TEST_ONLY_A", "碳酸鈣", "TEST_ONLY氧化鋅")),
               map_product(ov_corpus, _product("TEST_ONLY_B", "Calcium carbonate", "鋅"))]
    plan = graph_plan(ov_corpus, results)
    assert plan["interaction_nodes"] == 0
    assert plan["relationship_counts"]["INTERACTS_WITH"] == 0 and plan["relationship_counts"]["PRODUCT_TO_INTERACTION"] == 0
    assert {c["target_label"] for c in plan["contains"]} <= {"IngredientForm", "NutrientGroup"}
    assert all(not c["entity_id"].startswith("TEST_ONLY_") for c in plan["contains"])   # 沒有 Product↔Product
    dup = payload_dict(detect_duplicate_ingredients(ov_corpus, results))
    assert "INTERACTS_WITH" not in json.dumps(dup) and "interaction_id" not in json.dumps(dup)


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


def test_structured_payload_has_no_fixed_final_prose(ov_corpus):
    result = map_product(ov_corpus, _product("TEST_ONLY_A", "碳酸鈣", "TEST_ONLY氧化鋅", "TEST_ONLY硬脂酸鎂",
                                             "TEST_ONLY未定成分", "GELATIN"))
    payload = payload_dict(product_ingredient_payload(ov_corpus, result))
    for key, value in _strings(payload):
        if key not in FREE_TEXT_KEYS:
            assert ENUM_LIKE.match(value), f"{key}={value!r} 不是 enum / ID"
    assert payload["bridge_status"] == BridgeStatus.INGREDIENTS_MAPPED and payload["overlay_version"] == "TEST_ONLY-OV"
    matched = {m["entity"]["entity_id"]: m for m in payload["matched_ingredients"]}
    assert set(matched) == {"F_CA_CARBONATE", "F_ZN_TEST_OXIDE"}
    zn = matched["F_ZN_TEST_OXIDE"]
    assert zn["alias_evidence_status"] == "VERIFIED_OFFICIAL" and zn["alias_ids"]
    assert [c["entity_id"] for c in zn["contributes_to"]] == ["N_ZN"] and zn["relationship_ids"]
    assert zn["entity"]["origin"] == "REVIEWED_OVERLAY" and zn["entity"]["graph_label"] == "IngredientForm"
    cands = {c["raw_text"]: c for c in payload["candidate_ingredients"]}
    assert cands["TEST_ONLY硬脂酸鎂"]["mapping_status"] == "AMBIGUOUS"
    assert cands["TEST_ONLY未定成分"]["candidate_entities"] == [] and cands["TEST_ONLY未定成分"]["alias_ids"]
    assert "INGREDIENT_MAPPING_REVIEW" in payload["missing_information"]


# ----------------------------------------------------------------------------- 正式 overlay 檔


def test_real_overlay_counts_and_identity_rules():
    if not (iem.OVERLAY_DIR / iem.OVERLAY_ALIASES_FILE).is_file():
        pytest.skip("reviewed overlay 不存在")
    overlay = load_reviewed_overlay()
    statuses = [a.alias_status for a in overlay.aliases]
    assert len(statuses) == 250
    assert {s: statuses.count(s) for s in set(statuses)} == {
        "VERIFIED_OFFICIAL": 167, "SUPPORTED_AUTHORITATIVE": 24, "AMBIGUOUS": 14, "NEEDS_REVIEW": 45}
    assert sum(a.is_formal for a in overlay.aliases) == 191 and len(overlay.forms) == 61 and len(overlay.links) == 61
    assert all(a.entity_id is None for a in overlay.aliases if not a.is_formal)
    corpus = load_interaction_corpus(overlay=overlay)
    for a, b in (("F_FISH_OIL", "N_OMEGA3"), ("F_EPA", "F_DHA"), ("F_FOLIC_ACID", "N_B9"), ("F_CA_CARBONATE", "N_CA")):
        assert a != b and corpus.entities[a].entity_id != corpus.entities[b].entity_id
    assert corpus.parent("F_CA_CARBONATE") == "N_CA" and corpus.entities["F_CA_CARBONATE"].graph_label == "IngredientForm"
    for zh, expected in (("碳酸鈣", "F_CA_CARBONATE"), ("魚油", "F_FISH_OIL"), ("葉酸", "F_FOLIC_ACID")):
        assert set(map_product(corpus, _product("TEST_ONLY_P", zh)).contains_targets()) == {expected}
    assert not map_product(corpus, _product("TEST_ONLY_P", "硬脂酸鎂")).formal()


# ----------------------------------------------------------------------------- 已存 bridge 列 → payload（runtime retrieval）


def _stored_rows(corpus, result):
    from app.ingestion.ingredient_bridge_import import mapping_rows

    return mapping_rows([result])


def test_stored_rows_rebuild_same_payload_and_unresolved_does_not_block(ov_corpus):
    from app.services.ingredient_bridge import result_from_stored_rows

    src = _product("TEST_ONLY_A", "碳酸鈣", "TEST_ONLY氧化鋅", "TEST_ONLY硬脂酸鎂", "GELATIN", "TEST_ONLY 未知")
    fresh = map_product(ov_corpus, src)
    stored = result_from_stored_rows(ov_corpus, src, _stored_rows(ov_corpus, fresh))   # NO_MATCH 不存，重建時補回
    a = payload_dict(product_ingredient_payload(ov_corpus, stored))
    b = payload_dict(product_ingredient_payload(ov_corpus, fresh))
    assert a["matched_ingredients"] == b["matched_ingredients"] and a["candidate_ingredients"] == b["candidate_ingredients"]
    assert a["official_raw_ingredients"] == list(src.ingredients and [i.raw_text for i in src.ingredients])
    assert sorted(a["unresolved_raw_texts"]) == ["GELATIN", "TEST_ONLY 未知"]
    assert a["bridge_status"] == BridgeStatus.INGREDIENTS_MAPPED                 # 未解析成分不阻擋已確認成分
    assert {m["entity"]["entity_id"] for m in a["matched_ingredients"]} == {"F_CA_CARBONATE", "F_ZN_TEST_OXIDE"}


def test_stored_rows_with_other_version_are_rejected(ov_corpus):
    from app.services.ingredient_bridge import StoredBridgeVersionMismatch, result_from_stored_rows

    src = _product("TEST_ONLY_A", "碳酸鈣")
    rows = _stored_rows(ov_corpus, map_product(ov_corpus, src))
    rows[0]["overlay_version"] = "TEST_ONLY-OTHER"
    with pytest.raises(StoredBridgeVersionMismatch):
        result_from_stored_rows(ov_corpus, src, rows)
