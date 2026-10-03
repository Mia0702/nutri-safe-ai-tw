"""Supabase connectivity：用 SUPABASE_SECRET_KEY 實際連線，確認 schema marker。

只有在未設定 credentials 時才 skip；一旦有設定，連不上或 marker 不符就是失敗，不會 skip。
"""

import pytest

from app.config.project import PROJECT_ID

pytestmark = pytest.mark.integration


def test_supabase_connectivity_and_schema_marker(live_settings):
    if not live_settings.supabase_configured:
        hint = "（偵測到 legacy SUPABASE_SERVICE_ROLE_KEY，請改設定 SUPABASE_SECRET_KEY）" if live_settings.legacy_supabase_key_detected else ""
        pytest.skip(f"SKIPPED_EXTERNAL: 未設定 SUPABASE_URL / SUPABASE_SECRET_KEY{hint}")
    from app.services.supabase_service import SupabaseService

    service = SupabaseService(live_settings)
    rows = service.select(service._client.table("app_schema_meta").select("key,value"))
    meta = {r["key"]: r["value"] for r in rows}
    print(f"\napp_schema_meta: project = {meta.get('project')}, schema_version = {meta.get('schema_version')}")
    assert meta.get("project") == PROJECT_ID == "nutri_safe_ai"
    assert meta.get("schema_version") == "1"
    assert service.is_project_database()
