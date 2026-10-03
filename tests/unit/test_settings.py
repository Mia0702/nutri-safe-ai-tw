"""Supabase 設定驗證：只接受專案根網址與 sb_secret_ 後端金鑰，錯誤訊息不得洩漏 key。"""

import pytest
from pydantic import ValidationError

from app.config.settings import Settings

VALID_URL = "https://abcdefghijklmnopqrst.supabase.co"
FAKE_SECRET = "sb_secret_TEST_ONLY_not_a_real_key_0000"


def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_valid_supabase_configuration():
    s = make(supabase_url=VALID_URL, supabase_secret_key=FAKE_SECRET)
    assert s.supabase_configured
    assert s.supabase_secret_key.get_secret_value() == FAKE_SECRET
    assert FAKE_SECRET not in repr(s)


@pytest.mark.parametrize("url", [
    f"{VALID_URL}/rest/v1",
    f"{VALID_URL}/rest/v1/",
    f"{VALID_URL}/",
    "http://abcdefghijklmnopqrst.supabase.co",
    "https://abcdefghijklmnopqrst.example.com",
    "https://ABCDEF.supabase.co",
    "abcdefghijklmnopqrst.supabase.co",
])
def test_supabase_url_rejects_anything_but_project_root(url):
    with pytest.raises(ValidationError) as exc:
        make(supabase_url=url)
    assert "https://<project-ref>.supabase.co" in str(exc.value)


@pytest.mark.parametrize("bad_key", [
    "sb_publishable_TEST_ONLY_publishable_key",
    "eyJhbGciOiJIUzI1NiJ9.TEST_ONLY.legacy",
    "random-TEST_ONLY-value",
])
def test_supabase_secret_key_format_is_enforced_without_leaking_value(bad_key):
    with pytest.raises(ValidationError) as exc:
        make(supabase_url=VALID_URL, supabase_secret_key=bad_key)
    message = str(exc.value)
    assert "sb_secret_" in message
    assert bad_key not in message


def test_legacy_service_role_key_is_detected_but_never_used():
    s = make(supabase_url=VALID_URL, supabase_service_role_key="eyJ.TEST_ONLY.legacy")
    assert s.legacy_supabase_key_detected
    assert not s.supabase_configured
    assert s.supabase_service_role_key is None  # 值已丟棄
    assert "TEST_ONLY.legacy" not in repr(s) and "TEST_ONLY.legacy" not in s.model_dump_json()


def test_supabase_service_reports_missing_secret_key_with_legacy_hint():
    from app.services.errors import ServiceError
    from app.services.supabase_service import SupabaseService

    with pytest.raises(ServiceError) as exc:
        SupabaseService(make(supabase_url=VALID_URL, supabase_service_role_key="eyJ.TEST_ONLY.legacy"))
    assert exc.value.code == "NOT_CONFIGURED"
    assert "SUPABASE_SECRET_KEY" in exc.value.message and "TEST_ONLY" not in exc.value.message


def test_secret_key_is_redacted_from_logs():
    import logging

    from app.observability import JsonFormatter, configure_logging

    configure_logging(make(supabase_url=VALID_URL, supabase_secret_key=FAKE_SECRET))
    formatter = logging.getLogger("nutri_safe_ai").handlers[0].formatter
    assert isinstance(formatter, JsonFormatter)
    record = logging.LogRecord("nutri_safe_ai", logging.INFO, __file__, 1, f"key={FAKE_SECRET}", None, None)
    assert FAKE_SECRET not in formatter.format(record)


def test_web_search_budget_fits_inside_line_response_timeout():
    """官方網站 fallback 單次逾時（不重試）必須遠小於 LINE 一題時限，留時間給 Agent A / 其他 tools / assess。"""
    s = Settings(_env_file=None)
    assert s.openai_web_search_timeout_seconds * 2 <= s.response_timeout_seconds


def test_web_search_call_has_no_retry_and_uses_its_own_timeout():
    from types import SimpleNamespace

    from app.services.openai_service import OpenAIService

    calls = {}

    class _Responses:
        def parse(self, **kw):
            calls["timeout"] = kw["timeout"]
            return SimpleNamespace(output_parsed=None, output=[])

    class _Client:
        def with_options(self, **kw):
            calls["options"] = kw
            return SimpleNamespace(responses=_Responses())

    svc = OpenAIService(Settings(_env_file=None, openai_api_key="TEST_ONLY_key"))
    svc._client = _Client()
    svc.web_search_structured(instructions="", input_text="", output_model=Settings, allowed_domains=["fda.gov.tw"])
    assert calls == {"options": {"max_retries": 0}, "timeout": 40.0}
