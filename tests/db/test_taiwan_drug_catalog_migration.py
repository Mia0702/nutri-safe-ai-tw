"""在本機空白 PostgreSQL（pgserver）驗證 migration 0007（Taiwan Drug Ingredient Catalog）：additive、RLS、anon 無權限、可重複執行。

不連線到任何 Supabase project。名稱全為 TEST_ONLY。
"""

import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg", reason="SKIPPED_EXTERNAL: 未安裝 psycopg")
from tests.db.conftest import SUPABASE_PRELUDE  # noqa: E402

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
NAMES = ("0001_initial_schema.sql", "0002_brand_relationship_semantics.sql", "0003_claim_policy_corpus.sql",
         "0004_tfda_product_coverage.sql", "0005_product_ingredient_entity_mapping.sql", "0006_interaction_corpus.sql")
M7 = ROOT / "migrations" / "0007_taiwan_drug_ingredient_catalog.sql"
NEW = ("taiwan_drug_ingredients", "taiwan_drug_ingredient_permits")


@pytest.fixture(scope="module")
def db(pg_uri):
    base = pg_uri.rsplit("/", 1)[0]
    name = f"catalog_mig_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"create database {name}")
    conn = psycopg.connect(f"{base}/{name}", autocommit=True)
    conn.execute(SUPABASE_PRELUDE)
    for n in NAMES:
        conn.execute((ROOT / "migrations" / n).read_text(encoding="utf-8"))
    conn.before = {t: conn.execute(f"select count(*) from {t}").fetchone()[0] for t in ("drug_substances", "interactions", "app_schema_meta")}
    conn.execute(M7.read_text(encoding="utf-8"))
    yield conn
    conn.close()
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")


def q(conn, sql, *a):
    return conn.execute(sql, a).fetchall()


def test_sql_is_additive_only():
    text = M7.read_text(encoding="utf-8").lower()
    for forbidden in ("drop table", "drop column", "truncate", "delete from", "alter table drug", "alter table interaction"):
        assert forbidden not in text


def test_tables_exist_rls_enabled_and_no_anon_authenticated_privileges(db):
    assert {r[0] for r in q(db, "select tablename from pg_tables where schemaname='public' and tablename like 'taiwan_drug_%%'")} == set(NEW)
    assert set(q(db, "select relname, relrowsecurity from pg_class where relname = any(%s)", list(NEW))) == {(n, True) for n in NEW}
    for t in NEW:
        assert q(db, "select has_table_privilege('anon', %s, 'select'), has_table_privilege('authenticated', %s, 'select')", t, t) == [(False, False)]
        assert q(db, "select has_table_privilege('service_role', %s, 'select,insert,update,delete')", t) == [(True,)]


def test_existing_data_untouched_marker_written_and_rerun_is_idempotent(db):
    db.execute(M7.read_text(encoding="utf-8"))  # 第二次
    assert q(db, "select value from app_schema_meta where key='migration_0007_taiwan_drug_ingredient_catalog'") == [("applied",)]
    after = {t: db.execute(f"select count(*) from {t}").fetchone()[0] for t in ("drug_substances", "interactions")}
    assert after == {k: v for k, v in db.before.items() if k != "app_schema_meta"}
    assert db.execute("select count(*) from app_schema_meta").fetchone()[0] == db.before["app_schema_meta"] + 1


def test_constraints_unique_code_mapping_consistency_and_cascade(db):
    ins = ("insert into taiwan_drug_ingredients (ingredient_code, official_name, normalized_exact_name, active_permit_count, rank, "
           "source_id, source_dataset, retrieved_at, interaction_mapping_status, mapped_drug_substance_id) "
           "values (%s, 'Testing-A', 'testing-a', 3, 1, 's', '9123', now(), %s, %s) returning id")
    rid = db.execute(ins, ("K1", "NOT_MAPPED", None)).fetchone()[0]
    for bad in (("K1", "NOT_MAPPED", None), ("K2", "REVIEWED_EXACT_MAP", None), ("K3", "NOT_MAPPED", "D_NONEXISTENT")):
        with pytest.raises((psycopg.errors.UniqueViolation, psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation)):
            db.execute(ins, bad)
    db.execute("insert into taiwan_drug_ingredient_permits values (%s, 'P1')", (rid,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("insert into taiwan_drug_ingredient_permits values (%s, 'P1')", (rid,))
    db.execute("delete from taiwan_drug_ingredients where id = %s", (rid,))
    assert q(db, "select count(*) from taiwan_drug_ingredient_permits") == [(0,)]
