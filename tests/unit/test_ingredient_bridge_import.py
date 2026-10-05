"""Ingredient Bridge importer（不連 DB；合成 corpus＋合成 overlay、虛構產品 TEST_ONLY、in-memory store）。"""

import copy
import dataclasses
import json

import pytest

from app.ingestion import ingredient_bridge_import as bi
from app.ingestion.ingredient_entity_mapping import (
    MappingStatus,
    ReviewStatus,
    load_interaction_corpus,
    load_reviewed_overlay,
    map_product,
)
from tests.unit.test_ingredient_entity_mapping import _product, corpus_dir  # noqa: F401（fixture）
from tests.unit.test_ingredient_overlay_bridge import build_overlay


class MemoryStore:
    def __init__(self):
        self.tables: dict[str, dict[str, dict]] = {}
        self.writes: list[str] = []

    def fetch(self, table, columns, eq=None):
        return [copy.deepcopy(r) for r in self.tables.get(table, {}).values()]

    def upsert(self, table, rows, on_conflict):
        self.writes.append(table)
        for row in rows:
            self.tables.setdefault(table, {})[str(row[on_conflict])] = copy.deepcopy(row)


TOKENS = ("碳酸鈣", "Thiamin", "TEST_ONLY氧化鋅", "TEST_ONLY硬脂酸鎂", "TEST_ONLY未定成分", "CALCIUM CARBONATE", "GELATIN")


@pytest.fixture
def corpus(corpus_dir, tmp_path):  # noqa: F811
    return load_interaction_corpus(corpus_dir, overlay=load_reviewed_overlay(build_overlay(tmp_path / "overlay")))


def _results(corpus):
    return [map_product(corpus, _product("TEST_ONLY_A", *TOKENS)), map_product(corpus, _product("TEST_ONLY_B", "鋅"))]


def _plan(corpus, store=None, results=None):
    return bi.plan_bridge_import(corpus, results or _results(corpus), store,
                                 product_ids={"TEST_ONLY_A", "TEST_ONLY_B"}, ingredient_ids=set())


def test_dry_run_plan_is_clean_and_store_refuses_writes(corpus):
    plan = _plan(corpus)
    report = plan.report()
    assert plan.errors == [] and report["would_delete"] == 0 and report["would_update"] == 0
    assert report["invalid_formal_mappings"] == {} and report["invalid_candidate_mappings"] == {}
    assert report["protected_tables_written"] == 0
    with pytest.raises(RuntimeError, match="dry-run"):
        bi.apply_bridge_import(plan, bi.EmptyStore())


def test_stage_order_and_formal_vs_candidate_split(corpus):
    plan = _plan(corpus)
    assert [s for s, _, _ in bi.STAGES] == ["A_entities", "B_sources", "B_aliases", "C_form_to_nutrient",
                                            "D_formal_mappings", "E_candidate_mappings"]
    formal = {m["mapping_status"] for m in plan.stages["D_formal_mappings"]}
    cand = {m["mapping_status"] for m in plan.stages["E_candidate_mappings"]}
    assert formal <= {"VERIFIED_EXACT", "SUPPORTED_ALIAS", "FORM_TO_GROUP"} and cand <= {"AMBIGUOUS", "NEEDS_REVIEW"}
    assert all(m["entity_id"] is None for m in plan.stages["E_candidate_mappings"])
    assert "NO_MATCH" not in {m["mapping_status"] for s in plan.stages.values() for m in s if "mapping_status" in m}
    by_text = {(m["raw_text"], m["mapping_status"]): m for m in plan.stages["D_formal_mappings"]}
    assert by_text[("碳酸鈣", "SUPPORTED_ALIAS")]["entity_id"] == "F_CA_CARBONATE"        # accepted alias
    assert by_text[("Thiamin", "SUPPORTED_ALIAS")]["entity_id"] == "F_TEST_THIAMIN"       # overlay precedence
    assert by_text[("Thiamin", "FORM_TO_GROUP")]["entity_id"] == "N_B1"
    assert by_text[("CALCIUM CARBONATE", "VERIFIED_EXACT")]["alias_evidence_status"] == "VERIFIED_OFFICIAL"
    assert {m["raw_text"] for m in plan.stages["E_candidate_mappings"]} == {"TEST_ONLY硬脂酸鎂", "TEST_ONLY未定成分"}


def test_idempotent_apply_with_memory_store(corpus):
    store = MemoryStore()
    first = bi.apply_bridge_import(_plan(corpus, store), store)
    assert first["written"]["D_formal_mappings"] > 0
    again = _plan(corpus, store)
    assert again.errors == [] and all(d.counts()["insert"] == d.counts()["update"] == 0 for d in again.diffs.values())
    second = bi.apply_bridge_import(again, store)
    assert sum(second["written"].values()) == 0
    assert set(store.writes) <= bi.WRITABLE_TABLES and not set(store.writes) & bi.PROTECTED_TABLES


def test_form_to_nutrient_rows_are_contributes_only(corpus):
    links = _plan(corpus).stages["C_form_to_nutrient"]
    assert {link["relationship_type"] for link in links} == {"CONTRIBUTES_TO_NUTRIENT"}
    assert all(link["form_entity_id"] != link["nutrient_entity_id"] and link["source_id"] for link in links)
    assert all(link["amount_conversion_allowed"] is False and link["automatic_interaction_inheritance"] is False
               for link in links)
    overlay = {link["form_entity_id"]: link for link in links if link["basis"] == "REVIEWED_OVERLAY"}
    assert overlay["F_ZN_TEST_OXIDE"]["relationship_id"] == "TEST_ONLY_REL_F_ZN_TEST_OXIDE"
    assert overlay["F_ZN_TEST_OXIDE"]["source_locator"]


@pytest.mark.parametrize("change,reason", [
    (dict(alias_evidence_status="AMBIGUOUS"), "CANDIDATE_ALIAS_AS_FORMAL"),
    (dict(review_status=ReviewStatus.PENDING_REVIEW), "FORMAL_PENDING_REVIEW"),
    (dict(entity_id="N_TEST_ONLY_UNKNOWN"), "FORMAL_WITHOUT_KNOWN_ENTITY"),
    (dict(candidate_entity_ids=["N_ZN"]), "FORMAL_WITH_CANDIDATES"),
])
def test_invalid_formal_mapping_stops_plan(corpus, change, reason):
    results = _results(corpus)
    m = next(x for x in results[0].mappings if x.raw_text == "碳酸鈣" and x.mapping_status == MappingStatus.SUPPORTED_ALIAS)
    tampered = dataclasses.replace(m, **change)
    results[0].mappings[results[0].mappings.index(m)] = tampered
    plan = _plan(corpus, results=results)
    assert plan.report()["invalid_formal_mappings"].get(reason) == 1
    with pytest.raises(RuntimeError, match="拒絕寫入"):
        bi.apply_bridge_import(plan, MemoryStore())


def test_row_gate_rejects_normalized_amount_without_basis(corpus):
    plan = _plan(corpus)
    row = dict(plan.stages["D_formal_mappings"][0], amount_normalized=10.0)
    entities = {e["entity_id"]: e for e in plan.stages["A_entities"]}
    links = {r["relationship_id"]: r for r in plan.stages["C_form_to_nutrient"]}
    assert "AMOUNT_NORMALIZED_WITHOUT_BASIS" in bi.invalid_mapping_reasons(row, entities, links)


def test_candidate_with_formal_entity_stops_plan(corpus):
    results = _results(corpus)
    m = next(x for x in results[0].mappings if x.raw_text == "TEST_ONLY硬脂酸鎂")
    results[0].mappings[results[0].mappings.index(m)] = dataclasses.replace(m, entity_id="N_ZN", entity_type="NutrientGroup")
    plan = _plan(corpus, results=results)
    assert plan.report()["invalid_candidate_mappings"] == {"CANDIDATE_WITH_FORMAL_ENTITY": 1} and plan.errors


def test_unknown_product_or_ingredient_stops_plan(corpus):
    plan = bi.plan_bridge_import(corpus, _results(corpus), None, product_ids={"TEST_ONLY_A"}, ingredient_ids=set())
    assert any("product" in e for e in plan.errors)


def test_stale_rows_and_version_replay_refuse_without_delete(corpus):
    store = MemoryStore()
    bi.apply_bridge_import(_plan(corpus, store), store)
    snapshot = copy.deepcopy(store.tables)
    newer = dataclasses.replace(corpus, overlay=dataclasses.replace(corpus.overlay, version="TEST_ONLY-OV-2"))
    plan = _plan(newer, store)
    assert plan.report()["would_delete"] > 0 and any("其他版本" in e for e in plan.errors)
    with pytest.raises(RuntimeError):
        bi.apply_bridge_import(plan, store)
    assert store.tables == snapshot


def test_protected_tables_cannot_be_written(corpus):
    for table in ("products", "ingredients", "product_ingredients", "interaction_records"):
        with pytest.raises(RuntimeError, match="不允許寫入"):
            bi._batched(MemoryStore(), table, [{"id": "TEST_ONLY"}], "id")


class FakeNeo4j:
    def __init__(self):
        self.queries = []

    def write(self, query, params=None):
        self.queries.append((query, params or {}))


class NoMarkerStore(MemoryStore):
    def upsert(self, table, rows, on_conflict):
        if table != "app_schema_meta":
            super().upsert(table, rows, on_conflict)


def test_neo4j_sync_requires_marker_and_has_no_forbidden_relationships(corpus):
    neo = FakeNeo4j()
    store = NoMarkerStore()
    with pytest.raises(RuntimeError, match="完成標記"):
        bi.apply_bridge_import(_plan(corpus, store), store, neo4j=neo)
    assert neo.queries == []
    store = MemoryStore()
    report = bi.apply_bridge_import(_plan(corpus, store), store, neo4j=neo)
    text = "\n".join(q for q, _ in neo.queries)
    assert all(tok not in text for tok in bi.FORBIDDEN_GRAPH_TOKENS) and "MERGE" in text
    assert report["graph_written"]["contributes"] == len(corpus.overlay.links) + sum(
        1 for e in corpus.entities.values() if e.parent_nutrient_entity_id and e.entity_id not in corpus.overlay.links)
    contains = [r for q, p in neo.queries if "CONTAINS" in q for r in p.get("rows", [])]
    assert {r["entity_id"] for r in contains} >= {"F_CA_CARBONATE", "F_TEST_THIAMIN", "F_ZN_TEST_OXIDE", "N_ZN"}
    assert all(r["mapping_status"] in ("VERIFIED_EXACT", "SUPPORTED_ALIAS") for r in contains)


def test_no_risk_dose_or_interaction_output(corpus):
    plan = _plan(corpus)
    text = json.dumps({"report": plan.report(), "stages": plan.stages}, ensure_ascii=False, default=str).lower()
    for word in ("overdose", "exceed", "upper limit", "dangerous", "risk_level", "過量", "超過", "危險", "上限",
                 "interacts_with\": 1", "interaction_id"):
        assert word not in text
    preview = plan.report()["neo4j_preview"]
    assert preview["interaction_nodes"] == 0 and preview["relationships"]["INTERACTS_WITH"] == 0
    assert preview["relationships"]["SAME_AS"] == 0 and preview["relationships"]["PRODUCT_TO_INTERACTION"] == 0
    assert all(m["amount_normalized"] is None and m["amount_basis"] == "UNKNOWN"
               for s in ("D_formal_mappings", "E_candidate_mappings") for m in plan.stages[s])
