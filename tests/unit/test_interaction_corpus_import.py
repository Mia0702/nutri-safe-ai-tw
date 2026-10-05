"""Interaction Corpus importer（不連 DB；讀 repo 內正式 corpus 檔做唯讀稽核，寫入一律用 in-memory store / fake Neo4j）。"""

import copy
import dataclasses
import json

import pytest

from app.ingestion import interaction_corpus_import as ic
from app.ingestion.ingredient_entity_mapping import load_interaction_corpus, load_reviewed_overlay


class MemoryStore:
    def __init__(self, bridge=True):
        self.tables: dict[str, dict[str, dict]] = {}
        self.writes: list[str] = []
        if bridge:
            self.seed_bridge()

    def seed_bridge(self):
        corpus = load_interaction_corpus(overlay=load_reviewed_overlay())
        data = ic.load_interaction_corpus_data()
        self.tables["interaction_corpus_entities"] = {
            eid: {"entity_id": eid, "graph_label": e.graph_label} for eid, e in corpus.entities.items()}
        self.tables["interaction_corpus_sources"] = {s: {"source_id": s} for s in data.sources}

    def fetch(self, table, columns, eq=None):
        return [copy.deepcopy(r) for r in self.tables.get(table, {}).values()]

    def upsert(self, table, rows, on_conflict):
        self.writes.append(table)
        keys = on_conflict.split(",")
        for row in rows:
            self.tables.setdefault(table, {})["|".join(str(row[k]) for k in keys)] = copy.deepcopy(row)


def bridge_graph():
    """production 既有 Bridge 節點的替身：entity_id → [labels]（每個 ID 一個節點）。"""
    d = ic.load_interaction_corpus_data()
    return {i: [label] for i, label in d.ingredient_graph_labels.items()}


class FakeNeo4j:
    """記錄寫入；read 回答 preflight（既有 Bridge 節點）與已寫入關係數（模擬 MERGE 成功建立的關係數）。"""

    def __init__(self, drop_participants=False, graph=None, extra_nodes=()):
        self.calls: list[tuple[str, dict | None]] = []
        self.drop = drop_participants
        self.graph = bridge_graph() if graph is None else graph
        self.extra = list(extra_nodes)

    def write(self, query, params=None):
        self.calls.append((query, params))
        return []

    def _count(self, token):
        return sum(len(p["rows"]) for q, p in self.calls if p and token in q)

    def read(self, query, params=None):
        if "n.entity_id IN $ids" in query:
            rows = [{"entity_id": i, "labels": l} for i, l in self.graph.items() if i in params["ids"]]
            return rows + self.extra
        if query.startswith("MATCH (n:"):
            label = query.split("(n:")[1].split(")")[0]
            return [{"n": sum(label in l for l in self.graph.values())}]
        if "PARTICIPATES_IN" in query:
            return [{"n": self._count("PARTICIPATES_IN") - (1 if self.drop else 0)}]
        return [{"n": self._count("HAS_EVIDENCE" if "HAS_EVIDENCE" in query else "FROM_SOURCE")}]


@pytest.fixture(scope="module")
def data():
    return ic.load_interaction_corpus_data()


@pytest.fixture(scope="module")
def bridge():
    return load_interaction_corpus(overlay=load_reviewed_overlay())


def _plan(data, bridge, store=None):
    return ic.plan_interaction_import(data, store, bridge_entities={k: e.graph_label for k, e in bridge.entities.items()},
                                      bridge_source_ids=set(data.sources))


def test_corpus_counts_match_audit(data):
    c = data.counts()
    assert (c["entities"], c["aliases"], c["interactions"], c["evidence"], c["sources"]) == (45, 86, 42, 60, 39)
    assert (c["pairwise_interactions"], c["duplicate_intake_rules"], c["drug_substances"]) == (25, 17, 15)
    assert data.validation["all_checks_passed"] is True


def test_dry_plan_has_no_errors_and_expected_rows(data, bridge):
    plan = _plan(data, bridge)
    assert plan.errors == []
    assert {s: len(r) for s, r in plan.stages.items()} == {
        "A_drug_substances": 15, "B_drug_aliases": 15, "C_interactions": 42, "D_participants": 67, "E_evidence": 60}


def test_participants_use_ingredient_bridge_entity_ids(data, bridge):
    ids = {p["ingredient_entity_id"] for p in ic.participant_rows(data) if p["ingredient_entity_id"]}
    assert ids and ids <= set(bridge.entities)                     # 同一套 N_* / F_* ID，沒有第二套 identity
    for p in ic.participant_rows(data):
        if p["ingredient_entity_id"]:
            assert bridge.entities[p["ingredient_entity_id"]].graph_label == p["participant_kind"]
    assert not any(e.startswith("D_") for e in ids)


def test_duplicate_intake_is_independent_semantics(data, bridge):
    rows = {r["interaction_id"]: r for r in ic.interaction_rows(data)}
    dups = [r for r in rows.values() if r["record_kind"] == ic.DUPLICATE_RULE]
    assert len(dups) == 17
    assert all(r["severity"] == "DOSE_DEPENDENT_NOT_ASSIGNED" and r["dose_evaluation_default"] == "NOT_EVALUATED" for r in dups)
    parts = ic.participant_rows(data)
    for r in dups:
        mine = [p for p in parts if p["interaction_id"] == r["interaction_id"]]
        assert [p["role"] for p in mine] == ["SELF"] and mine[0]["participant_kind"] != "DrugSubstance"
    graph = ic.graph_payloads(_plan(data, bridge))
    assert len(graph["duplicate_intake_rules"]) == 17 and len(graph["interactions"]) == 25
    assert not any(r["interaction_type"] == "DUPLICATE_INTAKE" for r in graph["interactions"])


def test_conflict_and_review_never_become_deterministic(data):
    rows = {r["interaction_id"]: r for r in ic.interaction_rows(data)}
    fish = rows["INT_FISH_WAR"]
    assert fish["review_status"] == "EVIDENCE_CONFLICT" and fish["certainty"] == "CONFLICTING"
    assert not ic.eligibility(fish)
    pairwise = [r for r in rows.values() if r["record_kind"] == ic.PAIRWISE]
    assert sum(ic.eligibility(r) for r in pairwise) == 12
    assert not any(ic.eligibility(r) for r in pairwise if r["review_status"] != "READY" or r["evidence_use"] != "DECISION_ELIGIBLE")
    ev = [e for e in ic.evidence_rows(data) if e["interaction_id"] == "INT_FISH_WAR"]
    assert {e["supports_direction"] for e in ev} >= {"SUPPORTS", "NO_SIGNIFICANT_EFFECT", "MIXED"}   # 保留衝突語意


def test_severity_certainty_evidence_are_separate_fields(data):
    row = {r["interaction_id"]: r for r in ic.interaction_rows(data)}["INT_K_WAR"]
    assert (row["severity"], row["certainty"], row["evidence_use"]) == ("MAJOR", "HIGH", "DECISION_ELIGIBLE")
    assert row["applicability_conditions"] and row["decision_mode"] == "CONDITIONAL_MESSAGE_ONLY"


def test_drug_identity_not_translated_or_merged(data):
    drugs = ic.drug_rows(data)
    assert len(drugs) == 15 and all(d["taiwan_code_mapping_status"] == "NEEDS_REVIEW" for d in drugs)
    assert all(d["official_name_zh"] is None and d["tfda_ingredient_code"] is None for d in drugs)
    assert {d["drug_substance_id"]: d["parent_active_moiety_id"] for d in drugs}["D_DTG_SODIUM"] == "D_DTG"


def test_gates_reject_unsafe_data(data, bridge):
    def mutated(fn):
        d = copy.deepcopy(data)
        fn(d)
        return _plan(d, bridge).errors

    def failed_source(d):
        d.sources[d.evidence[0]["source_id"]]["retrieval_status"] = "FETCH_FAILED_502_TIMEOUT"

    def conflict_eligible(d):
        r = next(r for r in d.records if r["interaction_id"] == "INT_FISH_WAR")
        r.update(review_status="READY", evidence_use="DECISION_ELIGIBLE", decision_mode="CONDITIONAL_MESSAGE_ONLY")

    def dup_with_severity(d):
        next(r for r in d.records if r["interaction_type"] == "DUPLICATE_INTAKE")["severity"] = "MAJOR"

    def unknown_participant(d):
        d.records[0]["entity_a_id"] = "N_NOT_IN_BRIDGE"
        d.entities["N_NOT_IN_BRIDGE"] = {"entity_type": "INGREDIENT", "identity_level": "NUTRIENT_GROUP"}
        d.ingredient_graph_labels["N_NOT_IN_BRIDGE"] = "NutrientGroup"

    assert any("EVIDENCE_FROM_FAILED_SOURCE" in e for e in mutated(failed_source))
    assert any("CONFLICT_NOT_PRESERVED" in e for e in mutated(conflict_eligible))
    assert any("DUPLICATE_HAS_SEVERITY_SEMANTICS" in e for e in mutated(dup_with_severity))
    assert any("BRIDGE_MISSING_ENTITY" in e for e in mutated(unknown_participant))


def test_apply_is_idempotent_and_only_writes_0006_tables(data, bridge):
    store = MemoryStore()
    first = ic.apply_interaction_import(_plan(data, bridge, store), store)
    assert first["written"] == {"A_drug_substances": 15, "B_drug_aliases": 15, "C_interactions": 42, "D_participants": 67,
                                "E_evidence": 60}
    assert set(store.writes) <= ic.WRITABLE_TABLES and not (set(store.writes) & ic.PROTECTED_TABLES)
    again = _plan(data, bridge, store)
    assert again.errors == [] and all(d.counts()["insert"] == 0 and d.counts()["update"] == 0 for d in again.diffs.values())
    before = len(store.writes)
    second = ic.apply_interaction_import(again, store)
    assert sum(second["written"].values()) == 0 and store.writes[before:] == ["app_schema_meta"]
    assert len(store.tables["interactions"]) == 42 and len(store.tables["interaction_participants"]) == 67


def test_apply_refuses_without_bridge_or_with_stale_rows(data, bridge):
    store = MemoryStore(bridge=False)
    with pytest.raises(RuntimeError, match="Bridge 前置資料"):
        ic.apply_interaction_import(_plan(data, bridge, store), store)
    assert store.writes == []
    store = MemoryStore()
    store.tables["interactions"] = {"INT_OLD": {"interaction_id": "INT_OLD"}}
    plan = _plan(data, bridge, store)
    assert any("不在本次計畫" in e for e in plan.errors)
    with pytest.raises(RuntimeError, match="拒絕寫入"):
        ic.apply_interaction_import(plan, store)


def test_neo4j_sync_merge_only_and_verified(data, bridge):
    store, neo = MemoryStore(), FakeNeo4j()
    report = ic.apply_interaction_import(_plan(data, bridge, store), store, neo4j=neo)
    assert report["graph_problems"] == [] and report["errors"] == []
    queries = [q for q, _ in neo.calls]
    assert not any(t in q for q in queries for t in ic.FORBIDDEN_GRAPH_TOKENS)
    assert all("MERGE" in q or q.startswith("CREATE CONSTRAINT") for q in queries)
    assert report["graph_written"]["duplicate_intake_rules"] == 17 and report["graph_written"]["interactions"] == 25


def test_neo4j_not_touched_without_completion_marker(data, bridge):
    class NoMarker(MemoryStore):
        def fetch(self, table, columns, eq=None):
            return [] if table == "app_schema_meta" else super().fetch(table, columns, eq)

    store, neo = NoMarker(), FakeNeo4j()
    with pytest.raises(RuntimeError, match="完成標記"):
        ic.apply_interaction_import(_plan(data, bridge, store), store, neo4j=neo)
    assert neo.calls == []


def test_graph_verification_detects_missing_bridge_nodes(data, bridge):
    store, neo = MemoryStore(), FakeNeo4j(drop_participants=True)
    report = ic.apply_interaction_import(_plan(data, bridge, store), store, neo4j=neo)
    assert any("GRAPH_COUNT_MISMATCH:participants" in e for e in report["errors"])


def test_plan_report_is_serializable(data, bridge):
    plan = _plan(data, bridge)
    assert dataclasses.is_dataclass(plan)
    json.dumps(plan.report(), default=str)


def test_evidence_relationship_is_neutral_has_evidence(data, bridge):
    store, neo = MemoryStore(), FakeNeo4j()
    ic.apply_interaction_import(_plan(data, bridge, store), store, neo4j=neo)
    queries = " ".join(q for q, _ in neo.calls)
    assert "HAS_EVIDENCE" in queries and "SUPPORTED_BY" not in queries
    assert ic.NEO4J_QUERIES["has_evidence_interaction"] and ic.NEO4J_QUERIES["has_evidence_duplicate_rule"]
    payload = ic.graph_payloads(_plan(data, bridge))
    assert len(payload["has_evidence_interaction"]) + len(payload["has_evidence_duplicate_rule"]) == 60
    assert {"SUPPORTS", "NO_SIGNIFICANT_EFFECT", "MIXED"} <= {e["supports_direction"] for e in payload["has_evidence_interaction"]}
    assert "supports_direction" in ic.NEO4J_QUERIES["has_evidence_interaction"]      # 方向是結構化欄位，不拆關係類型


def test_neo4j_preflight_passes_for_existing_bridge_nodes(data):
    result = ic.neo4j_bridge_preflight(FakeNeo4j(), ic.required_ingredient_participants(data))
    assert result["reusable"] and result["found"] == result["required"] == 30
    assert result["missing"] == result["label_mismatches"] == result["duplicates"] == []
    assert result["node_counts"] == {"NutrientGroup": 17, "IngredientForm": 13}


@pytest.mark.parametrize("mutate,token", [
    (lambda g: g.pop("N_CA"), "NEO4J_MISSING_PARTICIPANT:N_CA"),
    (lambda g: g.__setitem__("F_FISH_OIL", ["NutrientGroup"]), "NEO4J_LABEL_MISMATCH:F_FISH_OIL"),
    (lambda g: g.__setitem__("N_ZN", ["IngredientForm"]), "NEO4J_LABEL_MISMATCH:N_ZN"),
])
def test_preflight_missing_or_mismatch_stops_before_any_write(data, bridge, mutate, token):
    graph = bridge_graph()
    mutate(graph)
    store, neo = MemoryStore(), FakeNeo4j(graph=graph)
    with pytest.raises(RuntimeError, match="preflight 失敗"):
        ic.apply_interaction_import(_plan(data, bridge, store), store, neo4j=neo)
    assert store.writes == [] and neo.calls == []                   # Supabase / Neo4j 都沒寫；不自動建立節點
    errors = ic.neo4j_bridge_preflight(FakeNeo4j(graph=graph), ic.required_ingredient_participants(data))["errors"]
    assert any(e.startswith(token) for e in errors)


def test_preflight_detects_duplicate_identity(data):
    extra = [{"entity_id": "N_CA", "labels": ["NutrientGroup"]}]
    result = ic.neo4j_bridge_preflight(FakeNeo4j(extra_nodes=extra), ic.required_ingredient_participants(data))
    assert result["duplicates"] == ["N_CA"] and not result["reusable"]


def test_importer_never_creates_ingredient_nodes(data, bridge):
    assert not any(q.lstrip().upper().startswith("UNWIND $ROWS AS R MERGE (N:NUTRIENTGROUP")
                   or "MERGE (n:NutrientGroup" in q or "MERGE (n:IngredientForm" in q for q in ic.NEO4J_QUERIES.values())
