"""RequestStore / ClaimStore 行為契約：同一組測試跑 InMemory 與 Supabase 兩種實作。

Supabase 實作以 PsycopgStateBackend 對本機 scratch PostgreSQL（已套用 migration 0001–0008）跑真實 SQL；不連任何 Supabase project。
資料皆為 TEST_ONLY。
"""

import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg", reason="SKIPPED_EXTERNAL: 未安裝 psycopg")
from app.response.store import InMemoryRequestStore  # noqa: E402
from app.state.claims import InMemoryClaimStore, SupabaseClaimStore  # noqa: E402
from app.state.errors import StateSerializationError, StateUnavailableError  # noqa: E402
from app.state.owner_key import OwnerKeyHasher  # noqa: E402
from app.state.request_store import SupabaseRequestStore  # noqa: E402
from tests.db.conftest import SUPABASE_PRELUDE  # noqa: E402
from tests.db.pg_state_backend import PsycopgStateBackend  # noqa: E402
from tests.state_fixtures import rich_record  # noqa: E402

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
HASH = OwnerKeyHasher("TEST_ONLY_secret_0123456789")
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def backend(pg_uri):
    base = pg_uri.rsplit("/", 1)[0]
    name = f"state_contract_{uuid.uuid4().hex[:10]}"
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


@pytest.fixture(params=["memory", "supabase"])
def request_store(request, backend):
    if request.param == "memory":
        return InMemoryRequestStore()
    return SupabaseRequestStore(backend, HASH)


@pytest.fixture(params=["memory", "supabase"])
def claim_store(request, backend):
    clock = {"now": NOW}
    if request.param == "memory":
        store = InMemoryClaimStore(clock=lambda: clock["now"])
    else:
        store = SupabaseClaimStore(backend, clock=lambda: clock["now"])
    store.clock = clock
    return store


def new_record(**kw):
    kw.setdefault("created_at", datetime.now(timezone.utc))
    return rich_record(request_id=str(uuid.uuid4()), **kw)


# ------------------------------------------------------------------ RequestStore（兩種實作共用契約）


def test_round_trip_preserves_structured_content(request_store):
    rec = new_record(owner_id="U_TEST_ONLY_1")
    request_store.save(rec)
    got = request_store.get(rec.request_id)
    assert got is not None
    for field in ("response", "normalized_request", "evidence", "risk", "query_label", "received_at", "created_at"):
        assert getattr(got, field) == getattr(rec, field), field
    assert got.raw_input.text == rec.raw_input.text and got.raw_input.context == rec.raw_input.context
    assert got.has_evidence
    assert got.evidence.model_dump(mode="json") == rec.evidence.model_dump(mode="json")  # source provenance 保留


def test_first_write_wins_and_duplicate_does_not_overwrite(request_store):
    first = new_record(owner_id="U_TEST_ONLY_1", summary="第一次")
    second = first.model_copy(update={"response": first.response.model_copy(update={"summary": "晚到的結果"})})
    request_store.save(first)
    request_store.save(second)
    assert request_store.get(first.request_id).response.summary == "第一次"


def test_owner_information_preserved_and_validatable(request_store):
    rec = new_record(owner_id="U_TEST_ONLY_1")
    request_store.save(rec)
    got = request_store.get(rec.request_id)
    assert got.owner_id == request_store.owner_ref("U_TEST_ONLY_1")
    assert got.owner_id != request_store.owner_ref("U_TEST_ONLY_2")
    none_owner = new_record(owner_id=None)
    request_store.save(none_owner)
    assert request_store.get(none_owner.request_id).owner_id is None


def test_expired_record_is_missing_and_unknown_id_is_missing(request_store):
    old = new_record(owner_id="U_TEST_ONLY_1", created_at=datetime.now(timezone.utc) - timedelta(hours=25))
    request_store.save(old)
    assert request_store.get(old.request_id) is None
    assert request_store.get(str(uuid.uuid4())) is None
    assert request_store.get("not-a-uuid") is None


# ------------------------------------------------------------------ Supabase 專屬（持久化格式 / 失敗語意）


def test_media_bytes_are_not_persisted(backend):
    store = SupabaseRequestStore(backend, HASH)
    rec = new_record(owner_id="U_TEST_ONLY_1", with_media=b"SECRET_IMAGE_BYTES_\xff\xd8")
    store.save(rec)
    row = backend.select_one("line_request_records", {"request_id": rec.request_id})
    assert "SECRET_IMAGE_BYTES" not in str(row) and "U_TEST_ONLY_1" not in str(row)
    got = store.get(rec.request_id)
    assert got.raw_input.media == [] and got.media_omitted is True
    assert row["record"]["media_omitted"] == [
        {"kind": "image", "mime_type": "image/jpeg", "filename": None, "source_id": "TEST_ONLY_MSG"}]


def test_no_raw_owner_id_in_row(backend):
    store = SupabaseRequestStore(backend, HASH)
    rec = new_record(owner_id="U_TEST_ONLY_RAW")
    store.save(rec)
    row = backend.select_one("line_request_records", {"request_id": rec.request_id})
    assert row["owner_key"] == HASH("U_TEST_ONLY_RAW") and "U_TEST_ONLY_RAW" not in str(row)


@pytest.mark.parametrize("corrupt", [
    "update line_request_records set record = '{}'::jsonb where request_id = %s",
    "update line_request_records set record = jsonb_set(record, '{schema_version}', '99') where request_id = %s",
    "update line_request_records set record = jsonb_set(record, '{response}', '{\"x\":1}') where request_id = %s",
    "update line_request_records set record = '\"junk\"'::jsonb where request_id = %s",
])
def test_malformed_persisted_record_fails_closed(backend, corrupt):
    store = SupabaseRequestStore(backend, HASH)
    rec = new_record(owner_id="U_TEST_ONLY_1")
    store.save(rec)
    with psycopg.connect(backend._uri, autocommit=True) as conn:
        conn.execute(corrupt, (rec.request_id,))
    with pytest.raises(StateSerializationError):
        store.get(rec.request_id)


def test_non_uuid_request_id_is_rejected_on_save(backend):
    store = SupabaseRequestStore(backend, HASH)
    with pytest.raises(StateSerializationError):
        store.save(rich_record(request_id="not-a-uuid"))


def test_backend_outage_is_not_reported_as_missing(backend):
    flaky = PsycopgStateBackend(backend._uri)
    store = SupabaseRequestStore(flaky, HASH)
    rec = new_record(owner_id="U_TEST_ONLY_1")
    store.save(rec)
    flaky.broken = True
    with pytest.raises(StateUnavailableError):
        store.get(rec.request_id)
    with pytest.raises(StateUnavailableError):
        store.save(new_record())
    with pytest.raises(StateUnavailableError):
        SupabaseClaimStore(flaky).claim("webhook", "evt-outage")


def test_concurrent_saves_first_write_wins(backend):
    store = SupabaseRequestStore(backend, HASH)
    base = new_record(owner_id="U_TEST_ONLY_1")
    variants = [base.model_copy(update={"response": base.response.model_copy(update={"summary": f"v{i}"})}) for i in range(8)]
    ts = [threading.Thread(target=store.save, args=(v,)) for v in variants]
    [t.start() for t in ts]
    [t.join() for t in ts]
    winner = store.get(base.request_id).response.summary
    assert winner in {f"v{i}" for i in range(8)}
    store.save(variants[0])
    assert store.get(base.request_id).response.summary == winner


# ------------------------------------------------------------------ ClaimStore（兩種實作共用契約）


def test_first_claim_true_duplicate_false(claim_store):
    k = uuid.uuid4().hex
    assert claim_store.claim("webhook", k) is True
    assert claim_store.claim("webhook", k) is False


def test_different_kind_and_key_do_not_conflict(claim_store):
    k = uuid.uuid4().hex
    assert claim_store.claim("webhook", k) is True
    assert claim_store.claim("asr", k) is True
    assert claim_store.claim("webhook", k + "x") is True


def test_expired_claim_can_be_reclaimed(claim_store):
    k = uuid.uuid4().hex
    assert claim_store.claim("asr", k) is True
    claim_store.clock["now"] = NOW + timedelta(hours=23, minutes=59)
    assert claim_store.claim("asr", k) is False
    claim_store.clock["now"] = NOW + timedelta(hours=24, seconds=1)
    assert claim_store.claim("asr", k) is True
    assert claim_store.claim("asr", k) is False


@pytest.mark.parametrize("expired", [False, True])
def test_concurrent_claims_have_exactly_one_winner(claim_store, expired):
    k = uuid.uuid4().hex
    if expired:  # 過期後重新取得的競爭也只能有一個贏家
        claim_store.claim("webhook", k)
        claim_store.clock["now"] = NOW + timedelta(hours=25)
    results, barrier = [], threading.Barrier(12)

    def go():
        barrier.wait()
        results.append(claim_store.claim("webhook", k))

    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1 and results.count(False) == 11


def test_claim_table_holds_no_personal_text(backend):
    cols = {r[0] for r in psycopg.connect(backend._uri).execute(
        "select column_name from information_schema.columns where table_name='line_event_claims'").fetchall()}
    assert cols == {"kind", "key", "created_at", "expires_at"}
