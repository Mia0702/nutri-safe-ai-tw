"""在本機空白 PostgreSQL（pgserver + pgvector）驗證 0003 與完整 corpus 匯入（不連 Supabase / Neo4j）。

0001 → 0002 → 0003 →（重跑 0003）→ 完整 corpus 以 production importer 寫入本機 DB（fake embedding）→
FK / CHECK / RLS / current retrieval / 重跑 idempotent。
"""

import hashlib
import math
import uuid
from pathlib import Path

import pytest

from app.ingestion import claim_policy_corpus as cpc
from tests.db.conftest import SUPABASE_PRELUDE

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = [ROOT / "migrations" / name for name in
              ("0001_initial_schema.sql", "0002_brand_relationship_semantics.sql", "0003_claim_policy_corpus.sql")]
CORPUS_DIR = ROOT / "data" / "tfda_claim_policy"
NEW_TABLES = ("claim_policy_import_batches", "claim_policy_sources", "claim_policy_rules", "claim_policy_examples",
              "health_function_catalog", "health_function_historical_sources")
DIM = 1536


def fake_vector(text: str) -> list[float]:
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [((seed[i % 32] + i * 31) % 251) / 251 - 0.5 for i in range(DIM)]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


class FakeEmbedder:
    def embed(self, texts):
        return [fake_vector(t) for t in texts]


class PsycopgStore:
    """CorpusStore 的本機 PostgreSQL 實作（只給測試用）。"""

    def __init__(self, conn):
        self.conn = conn
        self._types: dict[str, dict[str, str]] = {}

    def _coltypes(self, table):
        if table not in self._types:
            rows = self.conn.execute(
                "select column_name, data_type, udt_name from information_schema.columns "
                "where table_schema = 'public' and table_name = %s", (table,)).fetchall()
            self._types[table] = {r[0]: (r[2] if r[1] == "USER-DEFINED" else r[1]) for r in rows}
        return self._types[table]

    def _param(self, table, column, value):
        from psycopg.types.json import Jsonb

        kind = self._coltypes(table)[column]
        if kind == "jsonb":
            return Jsonb(value), "%s"
        if kind == "vector":
            return "[" + ",".join(f"{v:.6f}" for v in value) + "]", "%s::extensions.vector"
        return value, "%s"

    @staticmethod
    def _where(eq):
        clauses, params = [], []
        for key, value in (eq or {}).items():
            if "->>" in key:
                col, sub = key.split("->>")
                clauses.append(f"{col}->>'{sub}' = %s")
            else:
                clauses.append(f"{key} = %s")
            params.append(value)
        return (" where " + " and ".join(clauses)) if clauses else "", params

    def fetch(self, table, columns, eq=None):
        where, params = self._where(eq)
        cur = self.conn.execute(f"select {', '.join(columns)} from {table}{where}", params)
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def upsert(self, table, rows, on_conflict):
        keys = on_conflict.split(",")
        for row in rows:
            cols = list(row)
            params, marks = zip(*(self._param(table, c, row[c]) for c in cols))
            updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in keys) or f"{keys[0]} = excluded.{keys[0]}"
            self.conn.execute(
                f"insert into {table} ({', '.join(cols)}) values ({', '.join(marks)}) "
                f"on conflict ({on_conflict}) do update set {updates}", params)

    def update(self, table, values, match):
        sets, params = [], []
        for c, v in values.items():
            p, mark = self._param(table, c, v)
            sets.append(f"{c} = {mark}")
            params.append(p)
        where, wparams = self._where(match)
        self.conn.execute(f"update {table} set {', '.join(sets)}{where}", [*params, *wparams])


@pytest.fixture(scope="module")
def db(pg_uri):
    import psycopg

    if not (CORPUS_DIR / cpc.FILES["validation"]).is_file():
        pytest.skip("corpus 檔案不存在")
    base = pg_uri.rsplit("/", 1)[0]
    name = f"claim_policy_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"create database {name}")
    uri = f"{base}/{name}"
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(SUPABASE_PRELUDE)
        for path in MIGRATIONS:
            conn.execute(path.read_text(encoding="utf-8"))
        corpus = cpc.load_corpus(CORPUS_DIR)
        store = PsycopgStore(conn)
        plan = cpc.plan_import(corpus, store)
        assert plan.errors == []
        report = cpc.apply_import(plan, store, embedder=FakeEmbedder(), embedding_model="test-embedding")
        yield conn, store, corpus, report
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")


def scalar(conn, sql, params=None):
    return conn.execute(sql, params).fetchone()[0]


def test_migration_0003_is_rerunnable_and_secured(db):
    conn, *_ = db
    conn.execute(MIGRATIONS[2].read_text(encoding="utf-8"))  # 重跑不報錯
    assert scalar(conn, "select value from app_schema_meta where key = 'migration_0003_claim_policy_corpus'") == "applied"
    for t in NEW_TABLES:
        assert scalar(conn, "select rowsecurity from pg_tables where schemaname = 'public' and tablename = %s", (t,))
        for role in ("anon", "authenticated"):
            assert not scalar(conn, "select has_table_privilege(%s, %s, 'select')", (role, f"public.{t}")), (role, t)
        assert scalar(conn, "select has_table_privilege('service_role', %s, 'insert')", (f"public.{t}",))
    # 既有 0001 表與 RAG 函式未被改動：仍是同一組 22 張表 + 新增 6 張
    assert scalar(conn, "select count(*) from pg_tables where schemaname = 'public'") == 22 + len(NEW_TABLES)


def test_migration_0003_refuses_db_without_project_marker(pg_uri):
    import psycopg

    base = pg_uri.rsplit("/", 1)[0]
    name = f"no_marker_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"create database {name}")
    try:
        with psycopg.connect(f"{base}/{name}", autocommit=True) as conn:
            with pytest.raises(psycopg.errors.RaiseException):
                conn.execute(MIGRATIONS[2].read_text(encoding="utf-8"))
    finally:
        with psycopg.connect(pg_uri, autocommit=True) as admin:
            admin.execute(f"drop database if exists {name} with (force)")


def test_full_corpus_rows_and_foreign_keys(db):
    conn, _, corpus, report = db
    counts = {t: scalar(conn, f"select count(*) from {t}") for t in NEW_TABLES}
    assert counts == {"claim_policy_import_batches": 1, "claim_policy_sources": 71, "claim_policy_rules": 163,
                      "claim_policy_examples": 267, "health_function_catalog": 14, "health_function_historical_sources": 4}
    assert scalar(conn, "select count(*) from source_registry where source_key like 'tfda_claim_policy:%'") == 71
    assert scalar(conn, "select count(*) from illegal_ad_cases") == 0
    # FK 實際由 DB 強制；再確認每列都連得回來
    assert scalar(conn, "select count(*) from claim_policy_rules r left join claim_policy_sources s "
                        "on s.id = r.claim_policy_source_id where s.id is null") == 0
    assert scalar(conn, "select count(*) from claim_policy_examples e join claim_policy_rules r on r.id = e.claim_policy_rule_id "
                        "join claim_policy_sources s on s.id = e.claim_policy_source_id") == 267
    assert scalar(conn, "select count(*) from health_function_catalog f join claim_policy_sources c on c.id = f.catalog_source_id "
                        "join claim_policy_sources e on e.id = f.evaluation_source_id") == 14
    # corpus 原始 ID / 狀態完整保留
    assert {r[0] for r in conn.execute("select rule_id from claim_policy_rules")} == {r["rule_id"] for r in corpus.rules}
    assert dict(conn.execute("select status, count(*) from claim_policy_sources group by 1").fetchall()) == \
        {"CURRENT": 61, "SUPERSEDED": 9, "NEEDS_REVIEW": 1}
    assert scalar(conn, "select count(*) from claim_policy_rules where review_status = 'NEEDS_REVIEW'") == 29
    assert scalar(conn, "select count(*) from claim_policy_rules where policy_effect = 'NEEDS_REVIEW'") == 11


def test_rag_uses_existing_schema_and_superseded_is_not_retrieved(db):
    conn, *_ = db
    sections = scalar(conn, "select count(distinct metadata->>'corpus_document_id') from rag_chunks "
                            "where metadata->>'corpus' = 'tfda_claim_policy'")
    assert sections == 976
    assert scalar(conn, "select count(*) from rag_documents where metadata->>'corpus' = 'tfda_claim_policy'") == 124
    assert scalar(conn, "select count(*) from rag_documents where metadata->>'status' = 'SUPERSEDED' and is_current") == 0
    # 用被取代區段自己的向量查詢：既有 match_rag_chunks 也不會回傳它
    vec, doc_id = conn.execute(
        "select c.embedding::text, c.document_id from rag_chunks c join rag_documents d on d.id = c.document_id "
        "where not d.is_current limit 1").fetchone()
    hits = conn.execute("select document_id from match_rag_chunks(%s::extensions.vector, 20, 0.0, 'TW')", (vec,)).fetchall()
    assert hits and doc_id not in {h[0] for h in hits}
    # NEEDS_REVIEW / OCR 區段可檢索，但 metadata 標示不可作 deterministic 結論
    vec = scalar(conn, "select embedding::text from rag_chunks where metadata->>'extraction_method' = 'OCR_UNVERIFIED' limit 1")
    top = conn.execute("select chunk_metadata from match_rag_chunks(%s::extensions.vector, 1, 0.99, 'TW')", (vec,)).fetchone()[0]
    assert top["evidence_use"] == "CONTEXT_ONLY" and "UNVERIFIED_EXTRACTION" in top["quarantine_reasons"]


def test_check_constraints_block_contradictory_quarantine(db):
    import psycopg

    conn, *_ = db
    rid = scalar(conn, "select id from claim_policy_rules where review_status = 'NEEDS_REVIEW' limit 1")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("update claim_policy_rules set evidence_use = 'DECISION_ELIGIBLE' where id = %s", (rid,))
    sid = scalar(conn, "select id from claim_policy_sources where status = 'SUPERSEDED' limit 1")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("update claim_policy_sources set evidence_use = 'DECISION_ELIGIBLE' where id = %s", (sid,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("update claim_policy_rules set policy_effect = 'MAYBE' where id = %s", (rid,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute("update claim_policy_rules set claim_policy_source_id = %s where id = %s", (str(uuid.uuid4()), rid))


def test_rerun_on_local_db_is_all_unchanged(db):
    conn, store, corpus, _ = db
    before = {t: scalar(conn, f"select count(*) from {t}") for t in (*NEW_TABLES, "rag_documents", "rag_chunks")}
    plan = cpc.plan_import(corpus, store)
    report = plan.report()
    assert all(c["insert"] == 0 and c["update"] == 0 for c in report["tables"].values()), report["tables"]
    assert report["rag"]["unchanged"] == 124 and report["rag"]["chunks_to_embed"] == 0
    cpc.apply_import(plan, store, embedder=FakeEmbedder(), embedding_model="test-embedding")
    after = {t: scalar(conn, f"select count(*) from {t}") for t in before}
    assert after == before
    assert conn.execute("select status, run_count from claim_policy_import_batches").fetchone() == ("COMPLETED", 2)


def test_graph_sync_from_local_db_matches_plan(db):
    _, store, corpus, _ = db

    class FakeNeo4j:
        def __init__(self):
            self.rows = {}

        def write(self, query, params=None):
            for stage, q in cpc.GRAPH_QUERIES.items():
                if q == query:
                    self.rows.setdefault(stage, []).extend((params or {}).get("rows", []))

    neo = FakeNeo4j()
    counts = cpc.sync_claim_policy_graph(store, neo, lambda: [])
    assert counts["ClaimPolicyRule"] == 163 and counts["HealthFunction"] == 14 and counts["Source"] == 71
    assert counts["DEFINED_BY(HealthFunction→Source)"] == 28 and counts["HAS_HISTORICAL_METHOD"] == 4
    source_ids = {r["id"] for r in neo.rows["Source"]}
    assert all(r["source_id"] in source_ids for r in neo.rows["ClaimPolicyRule"])
