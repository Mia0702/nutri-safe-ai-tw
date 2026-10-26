"""C2c-2：webhook 去重與 ASR 一次性已改用 ClaimStore（memory 與 dict-backed Supabase 實作各跑一次同一組行為）。"""

import ast
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.dependencies import build_claim_store
from app.state.claims import InMemoryClaimStore, SupabaseClaimStore
from tests import claim_flows
from tests.state_fixtures import DictStateBackend
from tests.unit.test_ingredient_retrieval_runtime import HF_ZINC, ingredient_data  # noqa: F401（autouse fixture）

APP = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture(params=["memory", "supabase"])
def stores(request):
    backend = DictStateBackend()
    return (InMemoryClaimStore() if request.param == "memory" else SupabaseClaimStore(backend)), backend, request.param


@pytest.mark.parametrize("flow", claim_flows.ALL_FLOWS, ids=lambda f: f.__name__)
def test_claim_flows(stores, flow):
    flow(stores[0])


@pytest.mark.parametrize("flow", claim_flows.OUTAGE_FLOWS, ids=lambda f: f.__name__)
def test_outage_flows(stores, flow):
    claims, backend, kind = stores
    if kind == "memory":
        pytest.skip("in-memory store 不會有後端故障")
    flow(claims, backend)


def test_backend_selection_shares_the_single_state_flag():
    assert isinstance(build_claim_store(Settings(_env_file=None)), InMemoryClaimStore)
    s = Settings(_env_file=None, line_state_backend="supabase", line_state_hmac_secret=SecretStr("TEST_ONLY_secret_0123456789"),
                 supabase_url="https://abcdefghij.supabase.co", supabase_secret_key=SecretStr("sb_secret_TEST_ONLY"))
    assert isinstance(build_claim_store(s), SupabaseClaimStore)


def test_no_process_local_once_only_sets_remain():
    for path in (APP / "line" / "handler.py", APP / "response" / "service.py"):
        attrs = {n.attr for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
        assert not {"_seen", "_asr_used"} & attrs, path.name
