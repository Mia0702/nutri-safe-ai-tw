"""C2c-1：重啟後的 request-scoped postback（真實 SQL：本機 scratch PostgreSQL，已套用 migration 0001–0008）。

「重啟」= 丟掉 service / store 物件，用同一個資料庫建立全新的 store 與 service。不連任何 Supabase project；資料皆為 TEST_ONLY。
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg", reason="SKIPPED_EXTERNAL: 未安裝 psycopg")
from app.response.actions import build_postback_data  # noqa: E402
from app.response.service import EXPIRED_ACTION_TEXT, MEDIA_UNAVAILABLE_TEXT, STATE_UNAVAILABLE_TEXT  # noqa: E402
from app.schemas.enums import ResponseAction  # noqa: E402
from app.state.owner_key import OwnerKeyHasher  # noqa: E402
from app.state.request_store import SupabaseRequestStore  # noqa: E402
from tests.builders import extraction  # noqa: E402
from tests.db.conftest import SUPABASE_PRELUDE  # noqa: E402
from tests.db.pg_state_backend import PsycopgStateBackend  # noqa: E402
from tests.state_fixtures import PRODUCT, build_service, rich_record  # noqa: E402
from tests.unit.test_adaptive_response import _rid, _text  # noqa: E402

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
HASH = OwnerKeyHasher("TEST_ONLY_secret_0123456789")


@pytest.fixture(scope="module")
def backend(pg_uri):
    base = pg_uri.rsplit("/", 1)[0]
    name = f"state_restart_{uuid.uuid4().hex[:10]}"
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


def texts(message) -> str:
    return json.dumps(message, ensure_ascii=False)


def test_old_request_survives_restart_for_same_user_and_rejects_other_user(backend):
    first = build_service([extraction(product_candidates=[PRODUCT])], SupabaseRequestStore(backend, HASH))
    rid = _rid(first.respond(_text(f"TEST_ONLY {PRODUCT}"), owner_id="U_RESTART_1"))
    del first
    second = build_service([], SupabaseRequestStore(backend, HASH))
    ok = second.handle_action(build_postback_data(ResponseAction.OFFICIAL_INFO, rid), owner_id="U_RESTART_1")
    assert PRODUCT in texts(ok) and EXPIRED_ACTION_TEXT not in texts(ok)
    other = second.handle_action(build_postback_data(ResponseAction.OFFICIAL_INFO, rid), owner_id="U_RESTART_2")
    assert EXPIRED_ACTION_TEXT in texts(other) and PRODUCT not in texts(other)
    row = backend.select_one("line_request_records", {"request_id": rid})
    assert "U_RESTART_1" not in json.dumps(row, ensure_ascii=False)  # 資料庫裡沒有原始 LINE user id


def test_expired_request_after_restart_behaves_expired(backend):
    first = build_service([extraction(product_candidates=[PRODUCT])], SupabaseRequestStore(backend, HASH))
    rid = _rid(first.respond(_text(f"TEST_ONLY {PRODUCT}"), owner_id="U_RESTART_3"))
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    second = build_service([], SupabaseRequestStore(backend, HASH, clock=lambda: later))
    out = texts(second.handle_action(build_postback_data(ResponseAction.WHY, rid), owner_id="U_RESTART_3"))
    assert EXPIRED_ACTION_TEXT in out and STATE_UNAVAILABLE_TEXT not in out


def test_media_request_retry_after_restart_asks_to_resend(backend):
    rec = rich_record(owner_id="U_RESTART_4", with_media=b"TEST_ONLY_IMAGE_BYTES")
    SupabaseRequestStore(backend, HASH).save(rec)

    class MustNotRun:
        def run(self, *args, **kwargs):
            raise AssertionError("不得以空媒體重跑")

    svc = build_service([], SupabaseRequestStore(backend, HASH), pipeline=MustNotRun())
    out = svc.handle_action(build_postback_data(ResponseAction.RETRY, rec.request_id), owner_id="U_RESTART_4")
    assert MEDIA_UNAVAILABLE_TEXT in texts(out)


def test_storage_outage_after_restart_is_not_reported_as_expired(backend):
    first = build_service([extraction(product_candidates=[PRODUCT])], SupabaseRequestStore(backend, HASH))
    rid = _rid(first.respond(_text(f"TEST_ONLY {PRODUCT}"), owner_id="U_RESTART_5"))
    backend.broken = True
    try:
        second = build_service([], SupabaseRequestStore(backend, HASH))
        out = texts(second.handle_action(build_postback_data(ResponseAction.WHY, rid), owner_id="U_RESTART_5"))
    finally:
        backend.broken = False
    assert STATE_UNAVAILABLE_TEXT in out and EXPIRED_ACTION_TEXT not in out
