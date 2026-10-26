"""C2c-2：webhook 去重 / ASR 一次性在真實 SQL（本機 scratch PostgreSQL，migration 0001–0008）上的重啟與並行行為。TEST_ONLY。"""

import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg", reason="SKIPPED_EXTERNAL: 未安裝 psycopg")
from app.state.claims import SupabaseClaimStore  # noqa: E402
from tests import claim_flows  # noqa: E402
from tests.db.conftest import SUPABASE_PRELUDE  # noqa: E402
from tests.db.pg_state_backend import PsycopgStateBackend  # noqa: E402
from tests.unit.test_ingredient_retrieval_runtime import HF_ZINC, ingredient_data  # noqa: E402,F401（autouse fixture）

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def backend(pg_uri):
    base = pg_uri.rsplit("/", 1)[0]
    name = f"claim_rt_{uuid.uuid4().hex[:10]}"
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


@pytest.mark.parametrize("flow", claim_flows.ALL_FLOWS, ids=lambda f: f.__name__)
def test_claim_flows_on_real_sql(backend, flow):
    flow(SupabaseClaimStore(backend))


@pytest.mark.parametrize("flow", claim_flows.OUTAGE_FLOWS, ids=lambda f: f.__name__)
def test_outage_flows_on_real_sql(backend, flow):
    flow(SupabaseClaimStore(backend), backend)
