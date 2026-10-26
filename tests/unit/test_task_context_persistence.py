"""C2d：TaskContext 持久化（memory 與 dict-backed Supabase 各跑同一組行為）、序列化、backend 選擇。"""

import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.dependencies import build_task_context_store
from app.services.context_store import TaskContext, TaskContextStore
from app.state.context_backend import InMemoryTaskContextBackend, SupabaseTaskContextBackend
from app.state.context_serialization import deserialize_context, serialize_context
from app.state.errors import StateSerializationError
from tests import context_flows as flows
from tests.state_fixtures import DictStateBackend
from tests.unit.test_pending_clarification import pending_data  # noqa: F401（autouse fixture）


@pytest.fixture(params=["memory", "supabase"])
def kind(request):
    return request.param


@pytest.fixture
def env_raw(kind):
    if kind == "memory":
        return flows.Env(InMemoryTaskContextBackend()), (lambda: {}), None
    dict_backend = DictStateBackend()
    return (flows.Env(SupabaseTaskContextBackend(dict_backend), owner_key=flows.HASH),
            (lambda: dict_backend.tables), dict_backend)


@pytest.mark.parametrize("flow", flows.ALL_FLOWS, ids=lambda f: f.__name__)
def test_task_context_flows(env_raw, flow):
    env, raw, _ = env_raw
    flow(env, raw)


@pytest.mark.parametrize("flow", flows.OUTAGE_FLOWS, ids=lambda f: f.__name__)
def test_task_context_outage_flows(env_raw, kind, flow):
    env, raw, flaky = env_raw
    if kind == "memory":
        pytest.skip("in-memory backend 不會有後端故障")
    flow(env, raw, flaky)


def test_serialization_is_versioned_and_fails_closed():
    ctx = TaskContext(user_id="U_X", updated_at=flows.T0)
    payload = serialize_context(ctx)
    assert payload["schema_version"] == 1 and "user_id" not in str(payload) and deserialize_context(payload, TaskContext, "U_X") == ctx
    for bad in ({**payload, "schema_version": 2}, {**payload, "extra": 1}, {"schema_version": 1},
                {"schema_version": 1, "context": {**payload["context"], "user_id": "U_X"}},
                {"schema_version": 1, "context": {**payload["context"], "turns": "oops"}}, "not a dict", None):
        with pytest.raises(StateSerializationError):
            deserialize_context(bad, TaskContext, "U_X")


def test_corrupt_persisted_context_fails_closed_on_snapshot():
    backend = DictStateBackend()
    env = flows.Env(SupabaseTaskContextBackend(backend), owner_key=flows.HASH)
    env.store.snapshot(flows.USER)
    row = next(iter(backend.tables["line_task_contexts"].values()))
    row["context"] = {"schema_version": 99, "context": {}}
    with pytest.raises(StateSerializationError):
        env.restart().snapshot(flows.USER)
    env.store.reset(flows.USER)  # 重設可以修復損毀的列
    assert env.restart().snapshot(flows.USER).turns == []


def test_backend_selection_uses_the_single_flag_and_owner_key():
    assert isinstance(build_task_context_store(Settings(_env_file=None)), TaskContextStore)
    s = Settings(_env_file=None, line_state_backend="supabase", line_state_hmac_secret=SecretStr("TEST_ONLY_secret_0123456789"),
                 supabase_url="https://abcdefghij.supabase.co", supabase_secret_key=SecretStr("sb_secret_TEST_ONLY"))
    store = build_task_context_store(s)
    assert isinstance(store._backend, SupabaseTaskContextBackend) and store._key(flows.USER) != flows.USER
