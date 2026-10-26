"""C2c-1：RequestStore 的 runtime 接線（LINE_STATE_BACKEND）、owner 比對一律經 store.owner_ref、
store 故障與「過期 / 找不到」分開、media_omitted 的重新查詢保護。全部使用 TEST_ONLY 資料。"""

import ast
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config.settings import Settings
from app.dependencies import build_request_store
from app.response.actions import build_postback_data
from app.response.service import EXPIRED_ACTION_TEXT, MEDIA_UNAVAILABLE_TEXT, STATE_UNAVAILABLE_TEXT
from app.response.store import InMemoryRequestStore
from app.schemas.enums import ResponseAction
from app.state.owner_key import OwnerKeyHasher
from app.state.request_store import SupabaseRequestStore
from tests.builders import extraction
from tests.state_fixtures import PRODUCT, DictStateBackend, build_service, rich_record
from tests.unit.test_adaptive_response import _rid, _text

APP = Path(__file__).resolve().parents[2] / "app"
HASH = OwnerKeyHasher("TEST_ONLY_secret_0123456789")
URL = "https://abcdefghij.supabase.co"


def texts(message) -> str:
    return json.dumps(message, ensure_ascii=False)


# ---------------------------------------------------------------- backend 選擇

def _settings(**kw):
    return Settings(_env_file=None, **kw)


def test_default_backend_is_memory_even_when_supabase_is_configured():
    s = _settings(supabase_url=URL, supabase_secret_key=SecretStr("sb_secret_TEST_ONLY"))
    assert s.line_state_backend == "memory"
    assert isinstance(build_request_store(s), InMemoryRequestStore)


def test_supabase_backend_is_opt_in_and_needs_hmac_secret_and_credentials():
    with pytest.raises(ValidationError):
        _settings(line_state_backend="supabase", supabase_url=URL, supabase_secret_key=SecretStr("sb_secret_TEST_ONLY"))
    with pytest.raises(ValidationError):
        _settings(line_state_backend="supabase", line_state_hmac_secret=SecretStr("TEST_ONLY_secret_0123456789"))
    s = _settings(line_state_backend="supabase", line_state_hmac_secret=SecretStr("TEST_ONLY_secret_0123456789"),
                  supabase_url=URL, supabase_secret_key=SecretStr("sb_secret_TEST_ONLY"))
    assert isinstance(build_request_store(s), SupabaseRequestStore)


# ---------------------------------------------------------------- owner 比對掃描

def _owner_comparisons():
    found = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel.startswith("state/") or rel == "response/store.py":  # store 實作自己負責 owner 表示
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(fn):
                if isinstance(node, ast.Compare) and any(
                        isinstance(x, ast.Attribute) and x.attr == "owner_id" for x in [node.left, *node.comparators]):
                    found.append((rel, fn.name))
    return found


def test_record_owner_is_only_compared_through_owner_ref_helper():
    """RequestRecord.owner_id 在 runtime 只能經 _is_owner（store.owner_ref）比對；不得出現 record.owner_id == user_id。"""
    assert _owner_comparisons() == [("response/service.py", "_is_owner")]


@pytest.mark.parametrize("store", [InMemoryRequestStore(), SupabaseRequestStore(DictStateBackend(), HASH)],
                         ids=["memory", "supabase"])
def test_is_owner_uses_store_owner_ref(store):
    svc = build_service([], store)
    rec = rich_record(owner_id="U_A")
    store.save(rec)
    loaded = store.get(rec.request_id)
    assert svc._is_owner(loaded, "U_A") and not svc._is_owner(loaded, "U_B")


# ---------------------------------------------------------------- 兩種 backend 的 postback 行為一致

@pytest.fixture(params=["memory", "supabase"])
def make_store(request):
    backend = DictStateBackend()
    shared = InMemoryRequestStore()
    if request.param == "memory":
        return lambda **kw: shared
    return lambda **kw: SupabaseRequestStore(backend, HASH, **kw)


def test_same_user_postback_works_and_other_or_unknown_is_rejected(make_store):
    svc = build_service([extraction(product_candidates=[PRODUCT])], make_store())
    rid = _rid(svc.respond(_text(f"TEST_ONLY {PRODUCT}"), owner_id="U1"))
    ok = svc.handle_action(build_postback_data(ResponseAction.OFFICIAL_INFO, rid), owner_id="U1")
    assert PRODUCT in texts(ok) and EXPIRED_ACTION_TEXT not in texts(ok)
    for data, owner in [(build_postback_data(ResponseAction.WHY, rid), "U2"),
                        (build_postback_data(ResponseAction.WHY, str(uuid.uuid4())), "U1")]:
        assert EXPIRED_ACTION_TEXT in texts(svc.handle_action(data, owner_id=owner))


def test_text_retry_still_reruns(make_store):
    svc = build_service([extraction(product_candidates=[PRODUCT]), extraction(product_candidates=[PRODUCT])], make_store())
    rid = _rid(svc.respond(_text(f"TEST_ONLY {PRODUCT}"), owner_id="U1"))
    again = svc.handle_action(build_postback_data(ResponseAction.RETRY, rid), owner_id="U1")
    assert again["type"] == "flex" and _rid(again) != rid


def test_expired_memory_record_uses_expired_text():
    store = InMemoryRequestStore()
    svc = build_service([extraction(product_candidates=[PRODUCT])], store)
    rid = _rid(svc.respond(_text(f"TEST_ONLY {PRODUCT}"), owner_id="U1"))
    store._data[rid] = store._data[rid].model_copy(update={"created_at": datetime.now(timezone.utc) - timedelta(hours=25)})
    out = texts(svc.handle_action(build_postback_data(ResponseAction.WHY, rid), owner_id="U1"))
    assert EXPIRED_ACTION_TEXT in out and STATE_UNAVAILABLE_TEXT not in out


def test_expired_persisted_record_uses_expired_text():
    clock = {"now": datetime.now(timezone.utc)}
    store = SupabaseRequestStore(DictStateBackend(), HASH, clock=lambda: clock["now"])
    svc = build_service([extraction(product_candidates=[PRODUCT])], store)
    rid = _rid(svc.respond(_text(f"TEST_ONLY {PRODUCT}"), owner_id="U1"))
    clock["now"] += timedelta(hours=25)
    out = texts(svc.handle_action(build_postback_data(ResponseAction.WHY, rid), owner_id="U1"))
    assert EXPIRED_ACTION_TEXT in out and STATE_UNAVAILABLE_TEXT not in out


# ---------------------------------------------------------------- 只有 persistent backend 的情境

def test_restart_with_new_store_and_service_keeps_request_scoped_postbacks():
    backend = DictStateBackend()
    svc1 = build_service([extraction(product_candidates=[PRODUCT])], SupabaseRequestStore(backend, HASH))
    rid = _rid(svc1.respond(_text(f"TEST_ONLY {PRODUCT}"), owner_id="U1"))
    del svc1  # 模擬重啟：全新的 store 與 service，只共用資料庫
    svc2 = build_service([], SupabaseRequestStore(backend, HASH))
    ok = svc2.handle_action(build_postback_data(ResponseAction.OFFICIAL_INFO, rid), owner_id="U1")
    assert PRODUCT in texts(ok)
    assert EXPIRED_ACTION_TEXT in texts(svc2.handle_action(build_postback_data(ResponseAction.WHY, rid), owner_id="U2"))


def test_storage_failure_is_neutral_and_distinct_from_expired():
    backend = DictStateBackend()
    svc = build_service([extraction(product_candidates=[PRODUCT])], SupabaseRequestStore(backend, HASH))
    rid = _rid(svc.respond(_text(f"TEST_ONLY {PRODUCT}"), owner_id="U1"))
    backend.broken = True
    out = texts(svc.handle_action(build_postback_data(ResponseAction.OFFICIAL_INFO, rid), owner_id="U1"))
    assert STATE_UNAVAILABLE_TEXT in out and EXPIRED_ACTION_TEXT not in out
    assert "outage" not in out and "StateUnavailable" not in out
    backend.broken = False  # 恢復後同一張卡仍可用
    assert PRODUCT in texts(svc.handle_action(build_postback_data(ResponseAction.OFFICIAL_INFO, rid), owner_id="U1"))


def test_save_failure_does_not_lose_the_completed_answer():
    backend = DictStateBackend()
    backend.broken = True
    svc = build_service([extraction(product_candidates=[PRODUCT])], SupabaseRequestStore(backend, HASH))
    out = svc.respond(_text(f"TEST_ONLY {PRODUCT}"), owner_id="U1")
    assert out["type"] == "flex" and PRODUCT in texts(out)


class _MustNotRun:
    def run(self, *args, **kwargs):
        raise AssertionError("media_omitted 的重新查詢不得重跑 pipeline")


def _persisted_media_record(backend):
    rec = rich_record(owner_id="U1", with_media=b"TEST_ONLY_IMAGE_BYTES")
    SupabaseRequestStore(backend, HASH).save(rec)
    return rec


def test_retry_of_persisted_media_request_asks_to_resend_instead_of_running_empty():
    backend = DictStateBackend()
    rec = _persisted_media_record(backend)
    svc = build_service([], SupabaseRequestStore(backend, HASH), pipeline=_MustNotRun())
    out = svc.handle_action(build_postback_data(ResponseAction.RETRY, rec.request_id), owner_id="U1")
    assert MEDIA_UNAVAILABLE_TEXT in texts(out)


def test_media_omitted_does_not_block_non_retry_actions():
    backend = DictStateBackend()
    rec = _persisted_media_record(backend)
    svc = build_service([], SupabaseRequestStore(backend, HASH), pipeline=_MustNotRun())
    out = texts(svc.handle_action(build_postback_data(ResponseAction.OFFICIAL_INFO, rec.request_id), owner_id="U1"))
    assert MEDIA_UNAVAILABLE_TEXT not in out and STATE_UNAVAILABLE_TEXT not in out and EXPIRED_ACTION_TEXT not in out
