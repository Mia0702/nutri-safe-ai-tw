"""Integration test 共用 fixture。

需要外部服務的測試在 credentials 缺失、或連到的不是本專案資料庫時，
一律以「SKIPPED_EXTERNAL: 原因」skip，不假裝成功。
"""

import pytest

from app.config.settings import Settings


def skip_external(reason: str):
    pytest.skip(f"SKIPPED_EXTERNAL: {reason}")


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def openai_service(live_settings):
    if not live_settings.openai_configured:
        skip_external("未設定 OPENAI_API_KEY")
    from app.services.openai_service import OpenAIService

    return OpenAIService(live_settings)


@pytest.fixture(scope="session")
def supabase_service(live_settings):
    if not live_settings.supabase_configured:
        hint = "（偵測到 legacy SUPABASE_SERVICE_ROLE_KEY，請改設定 SUPABASE_SECRET_KEY）" if live_settings.legacy_supabase_key_detected else ""
        skip_external(f"未設定 SUPABASE_URL / SUPABASE_SECRET_KEY{hint}")
    from app.services.errors import ServiceError
    from app.services.supabase_service import SupabaseService

    service = SupabaseService(live_settings)
    try:
        if not service.is_project_database():
            skip_external("目前的 Supabase 不是 nutri_safe_ai DB（缺 app_schema_meta marker），請建立新 project 並執行 migration")
    except ServiceError as exc:
        skip_external(f"Supabase 無法連線：{exc.code.value}")
    return service


@pytest.fixture(scope="session")
def neo4j_service(live_settings):
    if not live_settings.neo4j_configured:
        skip_external("未設定 NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD")
    from app.services.errors import ServiceError
    from app.services.neo4j_service import Neo4jService

    service = Neo4jService(live_settings)
    try:
        if not service.is_project_database():
            skip_external("目前的 Neo4j 不是 nutri_safe_ai DB（缺 AppSchemaMeta marker），請換新 instance 並執行 scripts/setup_neo4j.py")
    except ServiceError as exc:
        skip_external(f"Neo4j 無法連線：{exc.code.value}")
    yield service
    service.close()
