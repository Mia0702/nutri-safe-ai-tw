"""在本機空白 PostgreSQL（pgserver）驗證 migration 0006 與 Interaction Corpus importer（不連 Supabase / Neo4j）。

0001 → 0006 → 以正式 corpus 的 entity / source 列模擬 Ingredient Bridge 已匯入 → importer apply → 重跑 idempotent →
CHECK / trigger 拒絕不合語意的列（duplicate 有 severity、conflict 變 eligible、藥品中文名 EXACT、取得失敗來源…）→
integrity view 為空 → 0001–0005 表不被修改。
"""

import uuid
from pathlib import Path

import psycopg
import pytest

from app.ingestion import ingredient_bridge_import as bi
from app.ingestion import interaction_corpus_import as ic
from app.ingestion.ingredient_entity_mapping import load_interaction_corpus, load_reviewed_overlay
from tests.db.conftest import SUPABASE_PRELUDE
from tests.db.test_tfda_product_coverage_db import BulkPsycopgStore as _BaseStore

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = [ROOT / "migrations" / name for name in (
    "0001_initial_schema.sql", "0002_brand_relationship_semantics.sql", "0003_claim_policy_corpus.sql",
    "0004_tfda_product_coverage.sql", "0005_product_ingredient_entity_mapping.sql", "0006_interaction_corpus.sql")]
NEW_TABLES = ("drug_substances", "drug_substance_aliases", "interactions", "interaction_participants", "interaction_evidence")
BRIDGE_TABLES = ("interaction_corpus_entities", "interaction_corpus_sources", "interaction_corpus_entity_aliases",
                 "interaction_form_nutrient_links", "product_ingredient_entity_mappings", "products", "ingredients")


class BulkPsycopgStore(_BaseStore):
    """0006 的 jsonb 欄位（publication_types）是 list；基底 helper 只包 dict。"""

    def upsert(self, table, rows, on_conflict):
        super().upsert(table, [{k: (v if not isinstance(v, list) else __import__("psycopg").types.json.Jsonb(v))
                                for k, v in r.items()} for r in rows], on_conflict)


class FakeNeo4j:
    def __init__(self, labels):
        self.calls = []
        self.labels = labels

    def write(self, query, params=None):
        self.calls.append((query, params))
        return []

    def read(self, query, params=None):
        if "n.entity_id IN $ids" in query:
            return [{"entity_id": i, "labels": [self.labels[i]]} for i in params["ids"] if i in self.labels]
        if query.startswith("MATCH (n:"):
            label = query.split("(n:")[1].split(")")[0]
            return [{"n": sum(v == label for v in self.labels.values())}]
        token = "PARTICIPATES_IN" if "PARTICIPATES_IN" in query else ("HAS_EVIDENCE" if "HAS_EVIDENCE" in query else "FROM_SOURCE")
        return [{"n": sum(len(p["rows"]) for q, p in self.calls if p and token in q)}]


@pytest.fixture(scope="module")
def corpus():
    return load_interaction_corpus(overlay=load_reviewed_overlay())


@pytest.fixture(scope="module")
def data():
    return ic.load_interaction_corpus_data()


@pytest.fixture(scope="module")
def db(pg_uri, corpus, data):
    base = pg_uri.rsplit("/", 1)[0]
    name = f"interaction_imp_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"create database {name}")
    conn = psycopg.connect(f"{base}/{name}", autocommit=True)
    conn.execute(SUPABASE_PRELUDE)
    for path in MIGRATIONS:
        conn.execute(path.read_text(encoding="utf-8"))
    # 模擬 Ingredient Bridge 已匯入：entity 與 source 用 Bridge importer 自己的 row builder
    store = BulkPsycopgStore(conn)
    store.upsert("interaction_corpus_entities", bi.entity_rows(corpus), "entity_id")
    sources, _ = bi.source_rows(corpus)
    store.upsert("interaction_corpus_sources", sources, "source_id")
    before = {t: sorted(map(str, conn.execute(f"select * from {t}").fetchall())) for t in BRIDGE_TABLES}
    plan = ic.plan_interaction_import(data, store, bridge_entities=ic.bridge_prerequisites(store, data)[0],
                                      bridge_source_ids=ic.bridge_prerequisites(store, data)[1])
    assert plan.errors == []
    neo = FakeNeo4j(data.ingredient_graph_labels)   # 以 0005 identity level 模擬 production 既有 Bridge 節點
    first = ic.apply_interaction_import(plan, store, neo4j=neo)
    yield conn, store, plan, first, before, neo
    conn.close()
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")


def _q(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


def _fails(conn, sql, *args):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(sql, args)


def test_import_counts_and_marker(db):
    conn, _, _, first, _, _ = db
    assert first["errors"] == [] and first["graph_problems"] == []
    counts = {t: _q(conn, f"select count(*) from {t}")[0][0] for t in NEW_TABLES}
    assert counts == {"drug_substances": 15, "drug_substance_aliases": 15, "interactions": 42,
                      "interaction_participants": 67, "interaction_evidence": 60}
    assert _q(conn, "select value from app_schema_meta where key = 'migration_0006_interaction_corpus'") == [("applied",)]
    assert _q(conn, "select count(*) from app_schema_meta where key like 'interaction_corpus_import:%%'")[0][0] == 1


def test_participants_fk_to_bridge_entities_and_integrity_view_empty(db):
    conn = db[0]
    assert _q(conn, "select count(*) from interaction_participants p join interaction_corpus_entities e "
                    "on e.entity_id = p.ingredient_entity_id where e.graph_label = p.participant_kind")[0][0] == 47
    assert _q(conn, "select count(*) from interaction_participants where participant_kind = 'DrugSubstance'")[0][0] == 20
    assert _q(conn, "select * from interaction_integrity_violations") == []


def test_deterministic_eligibility_is_generated_and_view_excludes_duplicates(db):
    conn = db[0]
    assert _q(conn, "select count(*) from interactions where deterministic_conclusion_allowed "
                    "and record_kind = 'PAIRWISE_INTERACTION'")[0][0] == 12
    assert _q(conn, "select count(*) from interaction_conclusion_eligible")[0][0] == 12
    assert _q(conn, "select deterministic_conclusion_allowed, review_status, certainty from interactions "
                    "where interaction_id = 'INT_FISH_WAR'") == [(False, "EVIDENCE_CONFLICT", "CONFLICTING")]
    assert _q(conn, "select count(*) from interactions where review_status <> 'READY' "
                    "and deterministic_conclusion_allowed")[0][0] == 0
    # duplicate rule 為 eligible 的「重複存在」模板，但不是 pairwise 結論，也沒有劑量結論
    assert _q(conn, "select count(*), min(dose_evaluation_default) from interactions "
                    "where record_kind = 'DUPLICATE_INTAKE_RULE'") == [(17, "NOT_EVALUATED")]


def test_rerun_is_idempotent(db, data, corpus):
    conn, store, plan, _, _, _ = db
    before = {t: sorted(map(str, conn.execute(f"select * from {t}").fetchall())) for t in NEW_TABLES}
    entities, sources = ic.bridge_prerequisites(store, data)
    again = ic.plan_interaction_import(data, store, bridge_entities=entities, bridge_source_ids=sources)
    assert again.errors == []
    for stage, diff in again.diffs.items():
        assert diff.counts() == {"insert": 0, "update": 0, "unchanged": len(plan.stages[stage]), "stale_would_delete": 0}
    report = ic.apply_interaction_import(again, store)
    assert sum(report["written"].values()) == 0
    assert {t: sorted(map(str, conn.execute(f"select * from {t}").fetchall())) for t in NEW_TABLES} == before


def test_bridge_and_existing_tables_untouched(db):
    conn, _, _, _, before, _ = db
    assert {t: sorted(map(str, conn.execute(f"select * from {t}").fetchall())) for t in BRIDGE_TABLES} == before


def test_db_rejects_duplicate_with_severity_or_pairwise_self(db):
    conn = db[0]
    base = ("insert into interactions (interaction_id, record_kind, interaction_type, directionality, severity, certainty, "
            "evidence_level, evidence_use, decision_mode, review_status, applicability_conditions, dose_evaluation_default, "
            "corpus_version) values (%s, %s, %s, %s, %s, 'X', 'X', 'SUPPORTING', 'X', 'READY', 'c', %s, '1.0')")
    _fails(conn, base, "T1", "DUPLICATE_INTAKE_RULE", "DUPLICATE_INTAKE", "SELF_ACCUMULATION", "MAJOR", "NOT_EVALUATED")
    _fails(conn, base, "T2", "DUPLICATE_INTAKE_RULE", "DUPLICATE_INTAKE", "SELF_ACCUMULATION", "DOSE_DEPENDENT_NOT_ASSIGNED", "NOT_APPLICABLE")
    _fails(conn, base, "T3", "PAIRWISE_INTERACTION", "ABSORPTION", "SELF_ACCUMULATION", "MODERATE", "NOT_APPLICABLE")
    _fails(conn, base, "T4", "PAIRWISE_INTERACTION", "DUPLICATE_INTAKE", "A_AFFECTS_B", "MODERATE", "NOT_APPLICABLE")


def test_db_rejects_conflict_that_looks_ready(db):
    conn = db[0]
    sql = ("insert into interactions (interaction_id, record_kind, interaction_type, directionality, severity, certainty, "
           "evidence_level, evidence_use, decision_mode, review_status, applicability_conditions, corpus_version) "
           "values (%s, 'PAIRWISE_INTERACTION', 'ABSORPTION', 'A_AFFECTS_B', 'MODERATE', %s, 'X', %s, 'CONDITIONAL_MESSAGE_ONLY', %s, 'c', '1.0')")
    _fails(conn, sql, "T5", "CONFLICTING", "DECISION_ELIGIBLE", "READY")
    _fails(conn, sql, "T6", "CONFLICTING", "NEEDS_REVIEW", "NEEDS_REVIEW")
    _fails(conn, sql, "T7", "MODERATE", "NEEDS_REVIEW", "EVIDENCE_CONFLICT")


def test_db_rejects_bad_participants(db):
    conn = db[0]
    ins = ("insert into interaction_participants (interaction_id, role, participant_kind, ingredient_entity_id, "
           "drug_substance_id, corpus_version) values (%s, %s, %s, %s, %s, '1.0')")
    _fails(conn, ins, "INT_K_WAR", "SELF", "NutrientGroup", "N_K", None)            # SELF 只屬於 duplicate rule
    _fails(conn, ins, "DUP_K", "SELF", "DrugSubstance", None, "D_WAR")              # duplicate 不可有藥品
    _fails(conn, ins, "DUP_C", "SELF", "IngredientForm", "N_C", None)               # 類型與 entity 不符
    with pytest.raises((psycopg.errors.ForeignKeyViolation, psycopg.errors.CheckViolation)):
        conn.execute(ins, ("DUP_C", "SELF", "NutrientGroup", "N_NOPE", None))        # 只能用 Bridge 已有的 identity


def test_db_rejects_drug_translation_merge_and_failed_source(db):
    conn = db[0]
    _fails(conn, "insert into drug_substances (drug_substance_id, canonical_name_en, identity_level, "
                 "taiwan_code_mapping_status, source_id, review_status, corpus_version) "
                 "values ('D_T', 'T', 'ACTIVE_MOIETY', 'MAPPED_REVIEWED', 's', 'READY', '1.0')")      # 無官方代碼 / 中文名不得標已對應
    _fails(conn, "insert into drug_substance_aliases (alias_id, drug_substance_id, alias_text, alias_normalized, language, "
                 "match_policy, review_status, corpus_version) values ('A_T', 'D_WAR', '華法林', '華法林', 'zh-Hant', "
                 "'EXACT_AT_DECLARED_IDENTITY_LEVEL', 'READY', '1.0')")                               # 中文譯名不可 EXACT
    _fails(conn, "update interaction_evidence set source_retrieval_status = 'FETCH_FAILED_502_TIMEOUT' "
                 "where evidence_id = 'EV_0001'")


def test_neo4j_sync_after_marker(db):
    _, _, _, first, _, neo = db
    assert first["graph_written"]["duplicate_intake_rules"] == 17
    assert all("MERGE" in q or q.startswith("CREATE CONSTRAINT") for q, _ in neo.calls)
