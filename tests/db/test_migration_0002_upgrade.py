"""驗證 migrations/0002_brand_relationship_semantics.sql 的升級路徑（本機 PostgreSQL，不連 Supabase）。

* 舊 DB：套用「舊版 0001 等價 schema」（目前 0001 去掉 company_relation 區塊與
  search_entity_aliases 的 brand_company_relation 欄位）→ 放入既有資料 → 0002 → 再跑一次 0002。
* 新 DB：目前 0001 → 0002（應為 no-op）。
* 兩者最終 schema（brands / product_brands / entity_aliases 欄位、constraint、index、函式）必須一致。
"""

import uuid
from pathlib import Path

import pytest

from tests.db.conftest import SUPABASE_PRELUDE

MIGRATION_0001 = Path("migrations/0001_initial_schema.sql").read_text(encoding="utf-8")
MIGRATION_0002 = Path("migrations/0002_brand_relationship_semantics.sql").read_text(encoding="utf-8")

RELATION_BLOCK_START = "-- 公司與品牌的關係語意"
RELATION_BLOCK_END = "-- 產品屬於哪個品牌"
RELATION_FUNCTION_LINE = "               'brand_company_relation', b.company_relation,\n"
BRAND_TABLES = ("brands", "product_brands", "entity_aliases")


def old_0001_sql() -> str:
    """重建已套用到新 Supabase 的舊版 0001（brand gate 之前）。"""
    start = MIGRATION_0001.index(RELATION_BLOCK_START)
    end = MIGRATION_0001.index(RELATION_BLOCK_END)
    assert MIGRATION_0001.count(RELATION_FUNCTION_LINE) == 1
    old = (MIGRATION_0001[:start] + MIGRATION_0001[end:]).replace(RELATION_FUNCTION_LINE, "")
    assert not any("company_relation" in line.split("--", 1)[0] for line in old.splitlines())
    return old


def snapshot(conn) -> dict:
    tables = list(BRAND_TABLES)
    columns = conn.execute(
        "select table_name, column_name, data_type, column_default, is_nullable, is_generated, generation_expression "
        "from information_schema.columns where table_schema = 'public' and table_name = any(%s) "
        "order by table_name, column_name", (tables,)).fetchall()
    constraints = conn.execute(
        "select conrelid::regclass::text, conname, pg_get_constraintdef(oid) from pg_constraint "
        "where conrelid::regclass::text = any(%s) order by 1, 2", (tables,)).fetchall()
    indexes = conn.execute(
        "select tablename, indexname, indexdef from pg_indexes where schemaname = 'public' and tablename = any(%s) "
        "order by 1, 2", (tables,)).fetchall()
    function = conn.execute(
        "select pg_get_functiondef(p.oid), "
        "has_function_privilege('anon', p.oid, 'execute'), has_function_privilege('service_role', p.oid, 'execute') "
        "from pg_proc p where p.proname = 'search_entity_aliases'").fetchall()
    return {"columns": columns, "constraints": constraints, "indexes": indexes, "function": function}


@pytest.fixture
def make_db(pg_uri):
    import psycopg

    base = pg_uri.rsplit("/", 1)[0]
    created = []

    def _make() -> str:
        name = f"upgrade_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(pg_uri, autocommit=True) as admin:
            admin.execute(f"create database {name}")
        created.append(name)
        uri = f"{base}/{name}"
        with psycopg.connect(uri, autocommit=True) as c:
            c.execute(SUPABASE_PRELUDE)
        return uri

    yield _make
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        for name in created:
            admin.execute(f"drop database if exists {name}")


def seed_old_brand(conn) -> tuple[str, str]:
    cid, bid = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute("insert into companies (id, name_zh, name_normalized) values (%s, 'TEST_ONLY 公司', %s)", (cid, f"c-{cid}"))
    conn.execute(
        "insert into brands (id, name, name_normalized, company_id, derivation, evidence_status, confidence, inference_basis) "
        "values (%s, 'TEST_ONLY', %s, %s, 'DERIVED_BRAND_PREFIX', 'INFERRED', 0.8, '{\"rule\": \"x\"}')",
        (bid, f"b-{bid}", cid))
    conn.execute(
        "insert into entity_aliases (id, entity_type, brand_id, alias, alias_normalized, alias_type, confidence, "
        "evidence_status, inference_basis) values (%s, 'BRAND', %s, 'TEST_ONLY', %s, 'DERIVED_BRAND_PREFIX', 0.8, "
        "'INFERRED', '{\"rule\": \"x\"}')", (str(uuid.uuid4()), bid, f"a-{bid}"))
    return cid, bid


def test_old_schema_upgrades_and_matches_fresh_install(make_db):
    import psycopg

    old_uri, fresh_uri = make_db(), make_db()
    with psycopg.connect(old_uri, autocommit=True) as old:
        old.execute(old_0001_sql())
        assert old.execute(
            "select count(*) from information_schema.columns where table_name = 'brands' and column_name = 'company_relation'"
        ).fetchone()[0] == 0
        cid, bid = seed_old_brand(old)

        old.execute(MIGRATION_0002)
        upgraded = snapshot(old)
        old.execute(MIGRATION_0002)  # 重跑不報錯、不重複建立
        assert snapshot(old) == upgraded

        # 既有資料補為 ASSOCIATED_WITH_BRAND，且別名查詢回傳關係
        assert old.execute("select company_relation from brands where id = %s", (bid,)).fetchone()[0] == "ASSOCIATED_WITH_BRAND"
        row = old.execute("select alias_row->>'brand_company_relation' from search_entity_aliases(%s, array['BRAND'], 5)",
                          ([f"a-{bid}"],)).fetchone()
        assert row == ("ASSOCIATED_WITH_BRAND",)
        # 推測品牌不可標為 OWNS_BRAND；非法值被拒
        for value in ("OWNS_BRAND", "OWNED"):
            with pytest.raises(psycopg.errors.CheckViolation):
                old.execute("update brands set company_relation = %s where id = %s", (value, bid))
        names = [c[1] for c in upgraded["constraints"] if c[0] == "brands"]
        assert names.count("brands_company_relation_valid") == 1
        assert names.count("brands_owns_requires_evidence") == 1
        assert old.execute("select value from app_schema_meta where key = 'migration_0002_brand_relationship_semantics'"
                           ).fetchone() == ("applied",)

    with psycopg.connect(fresh_uri, autocommit=True) as fresh:
        fresh.execute(MIGRATION_0001)
        fresh.execute(MIGRATION_0002)
        assert snapshot(fresh) == upgraded


def test_0002_refuses_db_without_project_marker(make_db):
    import psycopg

    with psycopg.connect(make_db(), autocommit=True) as empty:
        with pytest.raises(psycopg.errors.RaiseException):
            empty.execute(MIGRATION_0002)
