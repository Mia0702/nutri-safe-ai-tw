"""持久化狀態：owner_key HMAC、設定防護、SupabaseStateBackend 呼叫形狀 / 失敗轉換、序列化（不需資料庫）。"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.services.errors import ServiceError
from app.state.backend import SupabaseStateBackend
from app.state.errors import StateUnavailableError
from app.state.owner_key import OwnerKeyHasher, build_owner_key_hasher
from app.state.serialization import deserialize_record, serialize_record
from tests.state_fixtures import rich_record

SECRET = "TEST_ONLY_hmac_secret_0123456789"
SUPABASE_ENV = {"supabase_url": "https://abcdefgh.supabase.co", "supabase_secret_key": "sb_secret_TEST_ONLY"}


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# ------------------------------------------------------------------ HMAC


def test_hmac_deterministic_and_distinct_per_user_and_secret():
    h = OwnerKeyHasher(SECRET)
    assert h("U_TEST_ONLY_1") == h("U_TEST_ONLY_1") and len(h("U_TEST_ONLY_1")) == 64
    assert h("U_TEST_ONLY_1") != h("U_TEST_ONLY_2")
    assert h("U_TEST_ONLY_1") != OwnerKeyHasher(SECRET + "x")("U_TEST_ONLY_1")


def test_hmac_output_does_not_contain_raw_id_and_repr_hides_secret():
    h = OwnerKeyHasher(SECRET)
    assert "U_TEST_ONLY_1" not in h("U_TEST_ONLY_1")
    assert SECRET not in repr(h)


def test_hmac_rejects_short_secret_and_empty_id():
    with pytest.raises(ValueError):
        OwnerKeyHasher("short")
    with pytest.raises(ValueError):
        OwnerKeyHasher(SECRET)("")


def test_missing_secret_fails_when_persistent_backend_requested():
    with pytest.raises(ValidationError):
        settings(line_state_backend="supabase", **SUPABASE_ENV)
    with pytest.raises(ServiceError):
        build_owner_key_hasher(settings())
    assert build_owner_key_hasher(settings(line_state_hmac_secret=SECRET))("U_TEST_ONLY_1")


def test_default_backend_is_memory_and_supabase_url_alone_does_not_enable_persistence():
    s = settings(**SUPABASE_ENV)
    assert s.line_state_backend == "memory" and s.line_state_hmac_secret is None
    ok = settings(line_state_backend="supabase", line_state_hmac_secret=SECRET, **SUPABASE_ENV)
    assert ok.line_state_backend == "supabase"
    with pytest.raises(ValidationError):  # 啟用 persistent state 也需要 Supabase 設定
        settings(line_state_backend="supabase", line_state_hmac_secret=SECRET)


# ------------------------------------------------------------------ SupabaseStateBackend（fake PostgREST client）


class FakeQuery:
    def __init__(self, log, data=None, boom=False):
        self.log, self.data, self.boom = log, data, boom

    def __getattr__(self, name):
        def call(*a, **kw):
            self.log.append((name, a, kw))
            return self
        return call

    def execute(self):
        if self.boom:
            raise RuntimeError("network down SECRET_TOKEN_VALUE")
        return type("R", (), {"data": self.data})()


class FakeClient:
    def __init__(self, data=None, boom=False):
        self.log, self.data, self.boom = [], data, boom

    def table(self, name):
        self.log.append(("table", (name,), {}))
        return FakeQuery(self.log, self.data, self.boom)


def backend_with(client) -> SupabaseStateBackend:
    b = SupabaseStateBackend.__new__(SupabaseStateBackend)
    b._client = client
    return b


def test_insert_if_absent_uses_ignore_duplicates_upsert_and_reports_winner():
    won = FakeClient(data=[{"kind": "a"}])
    assert backend_with(won).insert_if_absent("line_event_claims", {"kind": "a", "key": "k"}, ("kind", "key")) is True
    assert ("upsert", ({"kind": "a", "key": "k"},), {"on_conflict": "kind,key", "ignore_duplicates": True}) in won.log
    lost = FakeClient(data=[])
    assert backend_with(lost).insert_if_absent("line_event_claims", {"kind": "a", "key": "k"}, ("kind", "key")) is False


def test_backend_failure_becomes_unavailable_without_leaking_message():
    b = backend_with(FakeClient(boom=True))
    for call in (lambda: b.insert_if_absent("t", {"a": 1}, ("a",)), lambda: b.select_one("t", {"a": 1}),
                 lambda: b.delete_expired("t", {"a": 1}, datetime.now(timezone.utc))):
        with pytest.raises(StateUnavailableError) as exc:
            call()
        assert "SECRET_TOKEN_VALUE" not in str(exc.value)


def test_select_one_returns_none_when_no_rows():
    assert backend_with(FakeClient(data=[])).select_one("t", {"a": 1}) is None
    assert backend_with(FakeClient(data=[{"a": 1}])).select_one("t", {"a": 1}) == {"a": 1}


# ------------------------------------------------------------------ 序列化


def test_serialization_is_json_safe_and_excludes_media_bytes():
    import json

    rec = rich_record(with_media=b"RAW_BYTES_TEST_ONLY")
    payload = serialize_record(rec)
    text = json.dumps(payload, ensure_ascii=False)
    assert "RAW_BYTES_TEST_ONLY" not in text and payload["raw_input"]["media"] == []
    back = deserialize_record(json.loads(text), request_id=rec.request_id, owner_key="k", query_label=rec.query_label,
                              received_at=rec.received_at, created_at=rec.created_at)
    assert back.evidence == rec.evidence and back.risk == rec.risk and back.media_omitted is True
