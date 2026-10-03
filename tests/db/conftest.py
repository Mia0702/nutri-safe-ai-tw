"""在本機「完全空白」的 PostgreSQL（pgserver + pgvector）上實際套用 migration。

模擬 Supabase 新 project 的前置環境：
* extensions schema
* anon / authenticated / service_role 三個角色
不連線到任何 Supabase project。缺少 pgserver / psycopg 時整組 skip。
"""

import tempfile
from pathlib import Path

import pytest

MIGRATION = Path("migrations/0001_initial_schema.sql")

SUPABASE_PRELUDE = """
create schema if not exists extensions;
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
    if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
end $$;
grant usage on schema public, extensions to anon, authenticated, service_role;
-- Supabase 預設會把 public 新表授權給 anon / authenticated；這裡模擬同樣的預設，驗證 migration 會撤銷
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
"""


def apply_migration(conn) -> None:
    conn.execute(MIGRATION.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def pg_uri():
    pgserver = pytest.importorskip("pgserver", reason="SKIPPED_EXTERNAL: 未安裝 pgserver（requirements-dev.txt）")
    psycopg = pytest.importorskip("psycopg", reason="SKIPPED_EXTERNAL: 未安裝 psycopg（requirements-dev.txt）")
    server = pgserver.get_server(tempfile.mkdtemp(prefix="nutri_safe_ai_pg_"), cleanup_mode="delete")
    uri = server.get_uri()
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute("drop database if exists migration_test")
        conn.execute("create database migration_test")
    test_uri = uri.rsplit("/", 1)[0] + "/migration_test"
    with psycopg.connect(test_uri, autocommit=True) as conn:
        conn.execute(SUPABASE_PRELUDE)
        apply_migration(conn)
    yield test_uri
    server.cleanup()


@pytest.fixture
def conn(pg_uri):
    import psycopg

    with psycopg.connect(pg_uri, autocommit=True) as c:
        yield c
