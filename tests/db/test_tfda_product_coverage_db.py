"""在本機空白 PostgreSQL（pgserver）驗證 TFDA 產品登記覆蓋 importer 與 0004 proposal（不連 Supabase / Neo4j）。

0001 → 0002 → 0003 → 一筆虛構 health_food 產品 → 完整研究 snapshot（9112 筆）以 production importer 寫入 →
FK / CHECK / unique 通過、health_food 列未被改動、重跑全部 unchanged → 套用 0004 proposal（重跑一次）→
importer 補 typed 欄位、product_details 可看到 product_category。
"""

import uuid
from datetime import date
from pathlib import Path

import pytest

from app.ingestion import tfda_product_coverage as cov
from app.normalization import stable_id
from tests.db.conftest import SUPABASE_PRELUDE

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = [ROOT / "migrations" / name for name in
              ("0001_initial_schema.sql", "0002_brand_relationship_semantics.sql", "0003_claim_policy_corpus.sql")]
MIGRATION_0004 = ROOT / "migrations" / "0004_tfda_product_coverage.sql"
SNAPSHOT = ROOT / "data" / "tfda_product_coverage" / "2026-10-04"
AS_OF = date(2026, 10, 4)


class BulkPsycopgStore:
    """CoverageStore 的本機 PostgreSQL 實作（executemany；只給測試用）。"""

    def __init__(self, conn):
        self.conn = conn
        self.writes = 0

    def fetch(self, table, columns, eq=None):
        cur = self.conn.execute(f"select {', '.join(columns)} from {table}")
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def upsert(self, table, rows, on_conflict):
        from psycopg.types.json import Jsonb

        if not rows:
            return
        cols = list(rows[0])
        keys = on_conflict.split(",")
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in keys) or f"{keys[0]} = excluded.{keys[0]}"
        sql = (f"insert into {table} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
               f"on conflict ({on_conflict}) do update set {updates}")
        params = [[Jsonb(r[c]) if isinstance(r[c], dict) else r[c] for c in cols] for r in rows]
        with self.conn.cursor() as cur:
            cur.executemany(sql, params)
        self.writes += len(rows)


HF_PERMIT = "TEST_ONLY健食字第Z99999號"


def _seed_health_food(conn) -> dict:
    sid = conn.execute("insert into source_registry (source_key, source_name, source_type) "
                       "values ('test_only_health_food', 'TEST_ONLY 健康食品', 'open_data') returning id").fetchone()[0]
    pid, mid = stable_id("product", f"TW:{HF_PERMIT}"), stable_id("permit", f"TW:{HF_PERMIT}")
    conn.execute("insert into permits (id, permit_number, permit_code, status, source_id) values (%s, %s, 'Z99999', 'ACTIVE', %s)",
                 (mid, HF_PERMIT, sid))
    conn.execute("insert into products (id, product_name_zh, product_name_normalized, permit_id, status, source_id, "
                 "source_record_key) values (%s, 'TEST_ONLY 健康食品', 'testonly健康食品', %s, 'ACTIVE', %s, %s)",
                 (pid, mid, sid, HF_PERMIT))
    return {"product_id": pid, "row": conn.execute("select * from products where id = %s", (pid,)).fetchone()}


@pytest.fixture(scope="module")
def db(pg_uri):
    import psycopg

    if not (SNAPSHOT / cov.FILES["validation"]).is_file():
        pytest.skip("研究 snapshot 不存在")
    base = pg_uri.rsplit("/", 1)[0]
    name = f"coverage_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"create database {name}")
    with psycopg.connect(f"{base}/{name}", autocommit=True) as conn:
        conn.execute(SUPABASE_PRELUDE)
        for path in MIGRATIONS:
            conn.execute(path.read_text(encoding="utf-8"))
        hf = _seed_health_food(conn)
        store = BulkPsycopgStore(conn)
        snapshot = cov.load_snapshot(SNAPSHOT)
        plan = cov.plan_coverage_import(snapshot, store, as_of=AS_OF)
        assert plan.errors == []
        report = cov.apply_coverage_import(plan, store)
        yield conn, store, snapshot, report, hf
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")


def scalar(conn, sql, params=None):
    return conn.execute(sql, params).fetchone()[0]


def test_full_snapshot_written_with_constraints(db):
    conn, _, _, report, _ = db
    assert report["written"]["products"] == 9112
    assert scalar(conn, "select count(*) from products where product_category = 'domestic_vitamin_tablet_capsule'") == 1705
    assert scalar(conn, "select count(*) from products where product_category = 'imported_tablet_capsule'") == 7407
    assert scalar(conn, "select count(*) from products") == 9113            # 9112 + 1 虛構 health_food
    assert scalar(conn, "select count(*) from permits where status_raw is not null and source_record_key like 'TFDA:%'") == 0
    assert scalar(conn, "select count(*) from permits where permit_number like '0%' and source_record_key like 'TFDA:23:%'") > 0
    assert scalar(conn, "select count(*) from raw_import_records where raw_data->'registration'->>'official_status' is not null "
                        "and source_record_key like 'TFDA:%'") == 0
    for table in ("product_claims", "approved_claims", "warnings", "ingredients", "product_ingredients", "product_brands"):
        assert scalar(conn, f"select count(*) from {table}") == 0, table
    # 每個新產品都可追溯到 raw 原始列與官方來源
    assert scalar(conn, "select count(*) from products p join raw_import_records r on r.id = p.raw_record_id "
                        "join source_registry s on s.id = p.source_id where p.source_record_key like 'TFDA:%' "
                        "and s.is_official and r.raw_data ? 'raw_source_fields'") == 9112


def test_health_food_row_untouched(db):
    conn, _, _, _, hf = db
    assert conn.execute("select * from products where id = %s", (hf["product_id"],)).fetchone() == hf["row"]


def test_rerun_is_unchanged(db):
    conn, store, snapshot, _, _ = db
    plan = cov.plan_coverage_import(snapshot, store, as_of=AS_OF)
    report = plan.report()
    assert report["products"] == {"would_insert": 0, "would_update": 0, "would_remain_unchanged": 9112, "would_quarantine": 0}
    before = store.writes
    cov.apply_coverage_import(plan, store)
    assert store.writes == before


def test_migration_0004_is_additive_and_importer_fills_typed_columns(db):
    conn, store, snapshot, _, hf = db
    for _ in range(2):  # 可重跑
        conn.execute(MIGRATION_0004.read_text(encoding="utf-8"))
    assert scalar(conn, "select value from app_schema_meta where key = %s", (cov.MIGRATION_0004_MARKER,)) == "applied"
    with pytest.raises(Exception):
        conn.execute("update products set product_category = 'not_a_category' where id = %s", (hf["product_id"],))
    plan = cov.plan_coverage_import(snapshot, store, migration_0004=True, as_of=AS_OF)
    assert plan.report()["tables"]["permits"]["update"] == 9112
    assert plan.report()["products"]["would_remain_unchanged"] == 9112
    cov.apply_coverage_import(plan, store)
    assert scalar(conn, "select count(*) from permits where expiry_date is not null and source_record_key like 'TFDA:%'") == 9112
    assert scalar(conn, "select count(*) from permits where signing_document_no is not null") == 7407
    assert scalar(conn, "select count(*) from product_details where product_category = 'health_food'") == 1
    assert not scalar(conn, "select has_table_privilege('anon', 'public.product_details', 'select')")
    assert conn.execute("select * from products where id = %s", (hf["product_id"],)).fetchone() == hf["row"]


def test_runtime_reads_coverage_rows_after_0004(db):
    """0004 之後（上一個測試已套用）：runtime 字號解析與 product_details 對應都能正確處理 9112 筆官方字號。"""
    from app.ingestion.product_integrity import product_count_checks
    from app.normalization import extract_permit_code
    from app.schemas.enums import ProductCategory, ProductStatus
    from app.tools.product_tools import _product_from_row

    conn = db[0]
    rows = conn.execute("select permit_number, permit_code from permits where source_record_key like 'TFDA:%'").fetchall()
    assert len(rows) == 9112
    assert all(extract_permit_code(number) == code == number for number, code in rows)  # 10 碼、前導零、不截斷

    cur = conn.execute("select * from product_details where product_category <> 'health_food'")
    cols = [c.name for c in cur.description]
    records = [_product_from_row(dict(zip(cols, r))) for r in cur.fetchall()]
    assert len(records) == 9112
    assert {r.product_category for r in records} == {ProductCategory.DOMESTIC_VITAMIN_TABLET_CAPSULE,
                                                    ProductCategory.IMPORTED_TABLET_CAPSULE}
    assert all(r.status == ProductStatus.UNKNOWN and r.expiry_date for r in records)

    products = [dict(zip(("id", "product_category", "permit_id", "source_id"), r)) for r in conn.execute(
        "select id::text, product_category, permit_id::text, source_id::text from products where source_record_key like 'TFDA:%'")]
    permits = [dict(zip(("id", "permit_number"), r)) for r in conn.execute("select id::text, permit_number from permits")]
    sources = [dict(zip(("id", "source_key"), r)) for r in conn.execute("select id::text, source_key from source_registry")]
    raw = [dict(zip(("source_id", "source_record_key"), r)) for r in conn.execute(
        "select source_id::text, source_record_key from raw_import_records")]
    checks = product_count_checks(products, permits, sources, raw, expected={
        ProductCategory.DOMESTIC_VITAMIN_TABLET_CAPSULE: 1705, ProductCategory.IMPORTED_TABLET_CAPSULE: 7407})
    assert [c.label for c in checks if not c.ok] == []
