"""在本機空白 PostgreSQL（pgserver）驗證 migration 0008（LINE 持久化狀態 schema）：additive、RLS、anon 無權限、可重複執行。

不連線到任何 Supabase project。資料皆為 TEST_ONLY。
"""

import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg", reason="SKIPPED_EXTERNAL: 未安裝 psycopg")
from tests.db.conftest import SUPABASE_PRELUDE  # noqa: E402

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
PRIOR = sorted(p.name for p in (ROOT / "migrations").glob("000[1-7]_*.sql"))
M8 = ROOT / "migrations" / "0008_line_persistent_state.sql"
NEW = ("line_request_records", "line_task_contexts", "line_event_claims")


def columns_snapshot(conn):
    return set(conn.execute(
        "select table_name, column_name, data_type, is_nullable, column_default from information_schema.columns "
        "where table_schema='public'").fetchall())


@pytest.fixture(scope="module")
def db(pg_uri):
    base = pg_uri.rsplit("/", 1)[0]
    name = f"line_state_mig_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"create database {name}")
    conn = psycopg.connect(f"{base}/{name}", autocommit=True)
    conn.execute(SUPABASE_PRELUDE)
    for n in PRIOR:
        conn.execute((ROOT / "migrations" / n).read_text(encoding="utf-8"))
    conn.columns_before = columns_snapshot(conn)
    conn.execute(M8.read_text(encoding="utf-8"))
    yield conn
    conn.close()
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")


def q(conn, sql, *a):
    return conn.execute(sql, a).fetchall()


def test_sql_is_additive_only():
    text = M8.read_text(encoding="utf-8").lower()
    for forbidden in ("drop table", "drop column", "truncate", "delete from", "alter column", "cron.schedule"):
        assert forbidden not in text


def test_prior_migrations_found():
    assert len(PRIOR) == 7


def test_three_tables_exist_and_only_those_added(db):
    before_tables = {t for t, *_ in db.columns_before}
    after_tables = {t for t, *_ in columns_snapshot(db)}
    assert after_tables - before_tables == set(NEW)


def test_primary_keys(db):
    sql = ("select a.attname from pg_index i join pg_attribute a on a.attrelid=i.indrelid and a.attnum = any(i.indkey) "
           "where i.indisprimary and i.indrelid = %s::regclass order by a.attname")
    assert [r[0] for r in q(db, sql, "line_request_records")] == ["request_id"]
    assert [r[0] for r in q(db, sql, "line_task_contexts")] == ["owner_key"]
    assert [r[0] for r in q(db, sql, "line_event_claims")] == ["key", "kind"]


def test_expires_at_version_and_expiry_indexes(db):
    for t in NEW:
        assert q(db, "select 1 from information_schema.columns where table_name=%s and column_name='expires_at' and is_nullable='NO'", t)
        assert q(db, "select 1 from pg_indexes where tablename=%s and indexdef like %s", t, "%(expires_at)%")
    assert q(db, "select column_default from information_schema.columns where table_name='line_task_contexts' and column_name='version'") == [("1",)]
    for t in NEW:  # 不存原始 LINE user id
        assert q(db, "select 1 from information_schema.columns where table_name=%s and column_name in ('user_id','line_user_id')", t) == []


def test_rls_enabled_and_privileges(db):
    assert set(q(db, "select relname, relrowsecurity from pg_class where relname = any(%s)", list(NEW))) == {(n, True) for n in NEW}
    for t in NEW:
        assert q(db, "select count(*) from pg_policies where tablename=%s", t) == [(0,)]
        for priv in ("select", "insert", "update", "delete"):
            assert q(db, "select has_table_privilege('anon', %s, %s), has_table_privilege('authenticated', %s, %s)", t, priv, t, priv) == [(False, False)]
        assert q(db, "select has_table_privilege('service_role', %s, 'select,insert,update,delete')", t) == [(True,)]


def test_anon_denied_and_service_role_works(db):
    for role in ("anon", "authenticated"):
        db.execute(f"set role {role}")
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute("select * from line_event_claims")
        finally:
            db.execute("reset role")
    db.execute("set role service_role")
    try:
        db.execute("insert into line_event_claims (kind, key) values ('TEST_ONLY', 'k1')")
        ttl = db.execute("select expires_at - created_at from line_event_claims where key='k1'").fetchone()[0]
        assert ttl.total_seconds() == 24 * 3600
        db.execute("delete from line_event_claims where key='k1'")
    finally:
        db.execute("reset role")


def test_atomic_claim_first_write_wins_and_cas_defaults(db):
    ins = "insert into line_event_claims (kind, key) values ('TEST_ONLY', 'c1') on conflict (kind, key) do nothing"
    assert db.execute(ins).rowcount == 1
    assert db.execute(ins).rowcount == 0
    rid = uuid.uuid4()
    rec = "insert into line_request_records (request_id, owner_key, record) values (%s, 'ok', %s::jsonb) on conflict do nothing"
    assert db.execute(rec, (rid, '{"a":1}')).rowcount == 1
    assert db.execute(rec, (rid, '{"a":2}')).rowcount == 0
    assert q(db, "select record->>'a' from line_request_records where request_id=%s", rid) == [("1",)]
    ttl = db.execute("select expires_at - created_at from line_request_records where request_id=%s", (rid,)).fetchone()[0]
    assert ttl.total_seconds() == 24 * 3600
    db.execute("insert into line_task_contexts (owner_key, session_id, context) values ('o1', 's1', '{}')")
    ver, ttl = db.execute("select version, expires_at - updated_at from line_task_contexts where owner_key='o1'").fetchone()
    assert ver == 1 and ttl.total_seconds() == 30 * 60
    cas = "update line_task_contexts set version = version + 1 where owner_key='o1' and version = 1"
    assert db.execute(cas).rowcount == 1
    assert db.execute(cas).rowcount == 0


def test_existing_columns_untouched_marker_and_idempotent(db):
    db.execute(M8.read_text(encoding="utf-8"))  # 第二次
    assert q(db, "select value from app_schema_meta where key='migration_0008_line_persistent_state'") == [("applied",)]
    assert db.columns_before <= columns_snapshot(db)
