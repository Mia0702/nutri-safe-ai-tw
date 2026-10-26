"""C2d：TaskContext 持久化在真實 SQL（本機 scratch PostgreSQL，migration 0001–0008）上的重啟 / TTL / CAS / 並行行為。TEST_ONLY。"""

import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg", reason="SKIPPED_EXTERNAL: 未安裝 psycopg")
from app.state.context_backend import SupabaseTaskContextBackend  # noqa: E402
from tests import context_flows as flows  # noqa: E402
from tests.db.conftest import SUPABASE_PRELUDE  # noqa: E402
from tests.db.pg_state_backend import PsycopgStateBackend  # noqa: E402
from tests.unit.test_pending_clarification import pending_data  # noqa: E402,F401（autouse fixture）

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def pg(pg_uri):
    base = pg_uri.rsplit("/", 1)[0]
    name = f"ctx_rt_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"create database {name}")
    uri = f"{base}/{name}"
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(SUPABASE_PRELUDE)
        for p in sorted((ROOT / "migrations").glob("000[1-8]_*.sql")):
            conn.execute(p.read_text(encoding="utf-8"))
    yield PsycopgStateBackend(uri)
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")


@pytest.fixture
def env_raw(pg):
    pg.broken = False
    with pg._connect() as conn:
        conn.execute("delete from line_task_contexts")
    env = flows.Env(SupabaseTaskContextBackend(pg), owner_key=flows.HASH)

    def raw():
        with pg._connect() as conn:
            return conn.execute("select owner_key, session_id, context::text, version from line_task_contexts").fetchall()

    return env, raw, pg


@pytest.mark.parametrize("flow", flows.ALL_FLOWS, ids=lambda f: f.__name__)
def test_task_context_flows_on_real_sql(env_raw, flow):
    env, raw, _ = env_raw
    flow(env, raw)


@pytest.mark.parametrize("flow", flows.OUTAGE_FLOWS, ids=lambda f: f.__name__)
def test_task_context_outage_flows_on_real_sql(env_raw, flow):
    env, raw, pg = env_raw
    flow(env, raw, pg)


def test_version_increments_and_expiry_is_enforced_by_the_reader(env_raw):
    env, raw, pg = env_raw
    s = env.store.snapshot(flows.USER)
    flows._record(env.store, s.session_id, rid="a")
    flows._record(env.store, s.session_id, rid="b")
    [(owner_key, session_id, _, version)] = raw()
    assert version == 3 and session_id == s.session_id and owner_key == flows.HASH(flows.USER) != flows.USER
    with pg._connect() as conn:  # purge 尚未執行：過期的列仍在表中，reader 也必須視為不存在
        conn.execute("update line_task_contexts set expires_at = now() - interval '1 second'")
    env.clock.now = flows.T0  # 時鐘仍在 context 的有效期內，但資料庫端 expires_at 已過
    from datetime import datetime, timedelta, timezone
    env.clock.now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert env.restart().snapshot(flows.USER).session_id != s.session_id
