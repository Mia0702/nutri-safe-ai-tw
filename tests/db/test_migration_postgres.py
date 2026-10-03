"""在空白 PostgreSQL 上實際驗證 migrations/0001_initial_schema.sql。"""

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config.project import PROJECT_ID
from app.ingestion.alias_builder import build_aliases
from app.ingestion.supabase_loader import ensure_source, load_aliases, load_health_food
from app.ingestion.tfda_health_food import SOURCE_REGISTRY_ROW, normalize_record
from tests.db.conftest import apply_migration

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
    "app_schema_meta", "source_registry", "raw_import_records", "companies", "permits", "products",
    "ingredients", "product_ingredients", "approved_claims", "product_claims", "warnings", "product_warnings",
    "illegal_ad_cases", "brands", "product_brands", "entity_aliases", "rag_documents", "rag_chunks",
    "line_users", "analysis_requests", "evidence_logs", "observed_claims",
}


def scalar(conn, sql, params=None):
    return conn.execute(sql, params).fetchone()[0]


def expect_error(conn, sql, params=None):
    import psycopg

    with pytest.raises(psycopg.errors.IntegrityError):
        conn.execute(sql, params)


def new_source(conn) -> str:
    return scalar(conn, "insert into source_registry (source_key, source_name, source_type) values (%s, 'TEST_ONLY', 'open_data') returning id",
                  (f"test_{uuid.uuid4().hex}",))


# ---------------------------------------------------------------- 基本結構


def test_migration_is_rerunnable_on_same_db(conn):
    apply_migration(conn)  # 第二次執行不應出錯
    assert scalar(conn, "select value from app_schema_meta where key = 'project'") == PROJECT_ID


def test_all_tables_created_with_rls_and_no_anon_access(conn):
    rows = conn.execute(
        "select tablename, rowsecurity from pg_tables where schemaname = 'public'"
    ).fetchall()
    tables = {r[0]: r[1] for r in rows}
    assert set(tables) == EXPECTED_TABLES
    assert all(tables.values()), "所有表都必須開啟 RLS"
    for t in EXPECTED_TABLES:
        for role in ("anon", "authenticated"):
            assert not scalar(conn, "select has_table_privilege(%s, %s, 'select')", (role, f"public.{t}")), (role, t)
        assert scalar(conn, "select has_table_privilege('service_role', %s, 'insert')", (f"public.{t}",))
    assert not scalar(conn, "select has_function_privilege('anon', 'public.search_products(text[], integer)', 'execute')")
    assert scalar(conn, "select has_function_privilege('service_role', 'public.search_products(text[], integer)', 'execute')")


def test_every_updated_at_table_has_trigger_and_it_updates(conn):
    tables = [r[0] for r in conn.execute(
        "select table_name from information_schema.columns where table_schema = 'public' and column_name = 'updated_at'"
    ).fetchall() if r[0] in EXPECTED_TABLES]
    assert tables
    for t in tables:
        assert scalar(conn, "select count(*) from pg_trigger where tgname = %s and not tgisinternal", (f"trg_{t}_updated_at",)) == 1, t
    sid = new_source(conn)
    before = scalar(conn, "select updated_at from source_registry where id = %s", (sid,))
    time.sleep(0.05)
    conn.execute("update source_registry set description = 'changed' where id = %s", (sid,))
    after = scalar(conn, "select updated_at from source_registry where id = %s", (sid,))
    assert after > before


# ---------------------------------------------------------------- statement role


def _analysis_request(conn) -> str:
    rid = str(uuid.uuid4())
    conn.execute("insert into analysis_requests (id, channel, input_type) values (%s, 'debug_api', 'text')", (rid,))
    return rid


def test_observed_claims_origin_is_constrained_and_product_side_is_derived(conn):
    rid = _analysis_request(conn)
    rows = [("USER_QUESTION", False), ("ADVERTISEMENT_CLAIM", True), ("PRODUCT_LABEL_CLAIM", True), ("THIRD_PARTY_CLAIM", False)]
    for i, (origin, _) in enumerate(rows):
        conn.execute(
            "insert into observed_claims (request_id, claim_index, claim_text, origin, semantic_category) values (%s, %s, 'TEST_ONLY', %s, 'medical_efficacy')",
            (rid, i, origin),
        )
    got = dict(conn.execute("select origin, is_product_side_assertion from observed_claims where request_id = %s", (rid,)).fetchall())
    assert got == dict(rows)
    expect_error(conn, "insert into observed_claims (request_id, claim_index, claim_text, origin, semantic_category) values (%s, 9, 'x', 'free text', 'other')", (rid,))


def test_analysis_request_links_internal_line_user_record(conn):
    uid = scalar(conn, "insert into line_users (line_user_id) values (%s) returning id", (f"U{uuid.uuid4().hex}",))
    rid = str(uuid.uuid4())
    conn.execute("insert into analysis_requests (id, line_user_record_id, channel, input_type) values (%s, %s, 'line', 'text')", (rid, uid))
    expect_error(conn, "insert into analysis_requests (id, channel, input_type) values (%s, 'line', 'unknown')", (str(uuid.uuid4()),))


# ---------------------------------------------------------------- alias / brand


def test_entity_alias_must_point_to_exactly_one_existing_entity(conn):
    cid = str(uuid.uuid4())
    conn.execute("insert into companies (id, name_zh, name_normalized) values (%s, 'TEST_ONLY 公司', %s)", (cid, f"test{cid}"))
    alias_id = str(uuid.uuid4())
    conn.execute(
        "insert into entity_aliases (id, entity_type, company_id, alias, alias_normalized, alias_type, confidence, evidence_status, inference_basis) "
        "values (%s, 'COMPANY', %s, 'TEST', 'test', 'SHORT_NAME', 0.9, 'INFERRED', '{\"rule\": \"t\"}')", (alias_id, cid))
    assert str(scalar(conn, "select entity_id from entity_aliases where id = %s", (alias_id,))) == cid
    # 型別與 FK 欄位不符
    expect_error(conn, "insert into entity_aliases (id, entity_type, company_id, alias, alias_normalized, alias_type, confidence, evidence_status) "
                       "values (%s, 'BRAND', %s, 'x', 'x', 'BRAND_NAME', 0.5, 'OFFICIAL_RECORD')", (str(uuid.uuid4()), cid))
    # 指向不存在的實體
    expect_error(conn, "insert into entity_aliases (id, entity_type, ingredient_id, alias, alias_normalized, alias_type, confidence, evidence_status) "
                       "values (%s, 'INGREDIENT', %s, 'x', 'x', 'ENGLISH_NAME', 1, 'OFFICIAL_RECORD')", (str(uuid.uuid4()), str(uuid.uuid4())))
    # confidence 範圍
    expect_error(conn, "insert into entity_aliases (id, entity_type, company_id, alias, alias_normalized, alias_type, confidence, evidence_status) "
                       "values (%s, 'COMPANY', %s, 'y', 'y', 'SHORT_NAME', 1.5, 'OFFICIAL_RECORD')", (str(uuid.uuid4()), cid))


def test_brand_without_company_is_still_unique(conn):
    sql = ("insert into brands (id, name, name_normalized, derivation, evidence_status, confidence) "
           "values (%s, 'B', 'testbrand-null', 'MANUAL_REVIEWED', 'MANUAL_REVIEWED', 0.7)")
    conn.execute(sql, (str(uuid.uuid4()),))
    expect_error(conn, sql, (str(uuid.uuid4()),))


def test_inferred_brand_and_alias_constraints(conn):
    brand = ("insert into brands (id, name, name_normalized, derivation, evidence_status, confidence, inference_basis) "
             "values (%s, 'B', %s, %s, %s, %s, %s)")
    # 由產品名稱推導的品牌不能標成官方紀錄
    expect_error(conn, brand, (str(uuid.uuid4()), f"b-{uuid.uuid4()}", "DERIVED_BRAND_PREFIX", "OFFICIAL_RECORD", 0.8, '{"rule": "x"}'))
    # 推測必須有 inference_basis
    expect_error(conn, brand, (str(uuid.uuid4()), f"b-{uuid.uuid4()}", "DERIVED_BRAND_PREFIX", "INFERRED", 0.8, "{}"))
    # 推測的 confidence 不可為 1
    expect_error(conn, brand, (str(uuid.uuid4()), f"b-{uuid.uuid4()}", "DERIVED_BRAND_PREFIX", "INFERRED", 1.0, '{"rule": "x"}'))
    bid = str(uuid.uuid4())
    conn.execute(brand, (bid, f"b-{bid}", "DERIVED_BRAND_PREFIX", "INFERRED", 0.8, '{"rule": "shared_product_name_prefix"}'))
    assert scalar(conn, "select is_inferred from brands where id = %s", (bid,)) is True
    alias = ("insert into entity_aliases (id, entity_type, brand_id, alias, alias_normalized, alias_type, confidence, evidence_status, inference_basis) "
             "values (%s, 'BRAND', %s, 'B', %s, 'DERIVED_BRAND_PREFIX', 0.8, %s, %s)")
    expect_error(conn, alias, (str(uuid.uuid4()), bid, f"a-{bid}", "OFFICIAL_RECORD", '{"rule": "x"}'))
    aid = str(uuid.uuid4())
    conn.execute(alias, (aid, bid, f"a-{bid}", "INFERRED", '{"rule": "x"}'))
    row = conn.execute(
        "select (alias_row->>'is_inferred')::boolean, (alias_row->>'brand_is_inferred')::boolean, alias_row ? 'source_is_official' "
        "from search_entity_aliases(%s, array['BRAND'], 5) where alias_row->>'id' = %s", ([f"a-{bid}"], aid)).fetchone()
    assert row == (True, True, True)


def test_brand_owns_relation_requires_ownership_evidence(conn):
    sql = ("insert into brands (id, name, name_normalized, company_id, company_relation, derivation, evidence_status, "
           "confidence, inference_basis) values (%s, 'B', %s, %s, %s, %s, %s, 0.8, %s)")
    cid = str(uuid.uuid4())
    conn.execute("insert into companies (id, name_zh, name_normalized) values (%s, 'TEST_ONLY 公司', %s)", (cid, f"c-{cid}"))
    # 推測品牌不可標為擁有
    expect_error(conn, sql, (str(uuid.uuid4()), f"b-{uuid.uuid4()}", cid, "OWNS_BRAND", "DERIVED_BRAND_PREFIX", "INFERRED", '{"rule": "x", "ownership_evidence": {"note": "x"}}'))
    # 人工審核但沒有 ownership evidence 也不可標為擁有
    expect_error(conn, sql, (str(uuid.uuid4()), f"b-{uuid.uuid4()}", cid, "OWNS_BRAND", "MANUAL_REVIEWED", "MANUAL_REVIEWED", '{"rule": "x"}'))
    expect_error(conn, sql, (str(uuid.uuid4()), f"b-{uuid.uuid4()}", cid, "OWNED", "MANUAL_REVIEWED", "MANUAL_REVIEWED", '{"rule": "x"}'))
    conn.execute(sql, (str(uuid.uuid4()), f"b-{uuid.uuid4()}", cid, "OWNS_BRAND", "MANUAL_REVIEWED", "MANUAL_REVIEWED", '{"ownership_evidence": {"note": "TEST_ONLY"}}'))
    # 同一家公司可以有多個品牌；預設關係是 ASSOCIATED_WITH_BRAND
    bid = str(uuid.uuid4())
    conn.execute("insert into brands (id, name, name_normalized, company_id, derivation, evidence_status, confidence, inference_basis) "
                 "values (%s, 'B2', %s, %s, 'DERIVED_BRAND_PREFIX', 'INFERRED', 0.7, '{\"rule\": \"x\"}')", (bid, f"b-{bid}", cid))
    assert scalar(conn, "select company_relation from brands where id = %s", (bid,)) == "ASSOCIATED_WITH_BRAND"
    assert scalar(conn, "select count(*) from brands where company_id = %s", (cid,)) == 2


# ---------------------------------------------------------------- RAG


def test_rag_document_dedupe_with_null_url_and_single_current_version(conn):
    sid = new_source(conn)
    insert = ("insert into rag_documents (title, source_url, source_url_normalized, source_name, source_id, document_type, "
              "retrieved_at, content_hash, is_current) values ('T', %s, %s, 'S', %s, 'regulation', now(), %s, %s)")
    conn.execute(insert, (None, None, sid, "hash-null-url", True))
    expect_error(conn, insert, (None, None, sid, "hash-null-url", True))  # 相同內容、網址為 null → 仍不可重複
    url = "https://www.fda.gov.tw/test-only"
    conn.execute(insert, (url, url, sid, "hash-v1", True))
    expect_error(conn, insert, (url, url, sid, "hash-v2", True))  # 同網址不能有兩個 current 版本
    conn.execute("update rag_documents set is_current = false where source_url_normalized = %s", (url,))
    conn.execute(insert, (url, url, sid, "hash-v2", True))
    # 沒有網址也沒有來源 → 不允許
    expect_error(conn, insert, (None, None, None, "hash-orphan", True))


def test_match_rag_chunks_with_vector_1536(conn):
    sid = new_source(conn)
    doc = scalar(conn, "insert into rag_documents (title, source_name, source_id, document_type, retrieved_at, content_hash) "
                       "values ('T', 'S', %s, 'regulation', now(), %s) returning id", (sid, f"h-{uuid.uuid4()}"))
    vec = "[" + ",".join(["0.01"] * 1536) + "]"
    conn.execute("insert into rag_chunks (document_id, chunk_index, content, embedding, embedding_model) values (%s, 0, 'c', %s, 'm')", (doc, vec))
    rows = conn.execute("select chunk_id, similarity from match_rag_chunks(%s::extensions.vector, 5, 0.3, 'TW')", (vec,)).fetchall()
    assert any(str(r[0]) for r in rows) and rows[0][1] > 0.99


# ---------------------------------------------------------------- idempotent seed import


class PostgresUpsertService:
    """TEST_ONLY：以 SQL 重現 PostgREST upsert 語意（insert ... on conflict do update），讓真正的 loader 直接對 DB 執行。"""

    def __init__(self, conn):
        self.conn = conn

    def upsert(self, table, rows, on_conflict=None):
        from psycopg.types.json import Jsonb

        out = []
        for row in rows:
            cols = list(row)
            values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in row.values()]
            conflict = on_conflict or "id"
            keys = {c.strip() for c in conflict.split(",")}
            updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in keys) or f"{cols[0]} = excluded.{cols[0]}"
            sql = (f"insert into {table} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
                   f"on conflict ({conflict}) do update set {updates} returning to_jsonb({table}.*)")
            out.append(self.conn.execute(sql, values).fetchone()[0])
        return out


def _counts(conn):
    tables = ["raw_import_records", "companies", "permits", "products", "ingredients", "product_ingredients",
              "approved_claims", "product_claims", "warnings", "product_warnings", "brands", "product_brands", "entity_aliases"]
    return {t: scalar(conn, f"select count(*) from {t}") for t in tables}


def test_seed_loader_is_idempotent_on_real_postgres(conn):
    records = json.loads(Path("tests/fixtures/tfda_health_food_sample.json").read_text(encoding="utf-8"))["records"]
    service = PostgresUpsertService(conn)

    def run(retrieved_at):
        bundles = [normalize_record(r, retrieved_at) for r in records]
        load_health_food(service, bundles, retrieved_at, f"batch-{retrieved_at.timestamp()}")
        built = build_aliases(bundles, ensure_source(service, SOURCE_REGISTRY_ROW))
        load_aliases(service, built.brands, built.product_brands, built.aliases)
        return bundles

    first_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    bundles = run(first_at)
    first = _counts(conn)
    created = dict(conn.execute("select id::text, created_at from products").fetchall())
    run(first_at + timedelta(days=1))  # 同一批官方資料、不同抓取時間
    second = _counts(conn)

    assert first == second, "重複匯入相同官方資料不可產生新列"
    assert first["products"] >= len(bundles)
    for b in bundles:
        pid = b.product["id"]
        row = conn.execute("select created_at, updated_at, retrieved_at from products where id = %s", (pid,)).fetchone()
        assert row[0] == created[pid]  # 同一列被更新，不是重建
        assert row[1] > row[0]  # updated_at 由 trigger 更新
        assert row[2] == first_at + timedelta(days=1)
    assert scalar(conn, "select count(*) from entity_aliases where entity_type = 'INGREDIENT'") >= 1


def test_same_permit_cannot_create_second_product(conn):
    sid = new_source(conn)
    permit = str(uuid.uuid4())
    conn.execute("insert into permits (id, permit_number, permit_code) values (%s, %s, 'Z99999')", (permit, f"TEST_ONLY-{permit}"))
    conn.execute("insert into products (id, product_name_zh, product_name_normalized, permit_id, source_id, source_record_key) "
                 "values (%s, '同名', '同名', %s, %s, 'k1')", (str(uuid.uuid4()), permit, sid))
    expect_error(conn, "insert into products (id, product_name_zh, product_name_normalized, permit_id, source_id, source_record_key) "
                       "values (%s, '另一名稱', '另一名稱', %s, %s, 'k2')", (str(uuid.uuid4()), permit, sid))
    # 不同許可證但同名 → 允許（品名不是唯一鍵）
    other = str(uuid.uuid4())
    conn.execute("insert into permits (id, permit_number, permit_code) values (%s, %s, 'Z99998')", (other, f"TEST_ONLY-{other}"))
    conn.execute("insert into products (id, product_name_zh, product_name_normalized, permit_id, source_id, source_record_key) "
                 "values (%s, '同名', '同名', %s, %s, 'k3')", (str(uuid.uuid4()), other, sid))
