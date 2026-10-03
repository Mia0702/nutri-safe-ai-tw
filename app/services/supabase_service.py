"""Supabase（PostgreSQL + pgvector）存取。

* 第一次使用時檢查 app_schema_meta.project = PROJECT_ID（nutri_safe_ai），
  確認連到的是本專案 DB，否則所有查詢回傳 SCHEMA_NOT_INITIALIZED。
* SUPABASE_SECRET_KEY（sb_secret_）只在後端使用，不得回傳給 client 或寫進 log。
"""

import logging
from typing import Any

from app.config.project import PROJECT_ID
from app.config.settings import Settings
from app.schemas.enums import ErrorCode
from app.services.errors import ServiceError

logger = logging.getLogger(__name__)

PROJECT_MARKER = PROJECT_ID


class SupabaseService:
    def __init__(self, settings: Settings):
        if not settings.supabase_configured:
            message = "未設定 SUPABASE_URL / SUPABASE_SECRET_KEY"
            if settings.legacy_supabase_key_detected:
                message += "（偵測到 legacy SUPABASE_SERVICE_ROLE_KEY，已不再使用，請改設定 SUPABASE_SECRET_KEY）"
            raise ServiceError(ErrorCode.NOT_CONFIGURED, message, "supabase")
        from supabase import create_client

        self._client = create_client(settings.supabase_url, settings.supabase_secret_key.get_secret_value())
        self._schema_checked = False
        self._source_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 基礎

    def _execute(self, builder) -> list[dict[str, Any]]:
        try:
            return builder.execute().data or []
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            if code in ("PGRST205", "PGRST202", "42P01", "42883"):
                raise ServiceError(
                    ErrorCode.SCHEMA_NOT_INITIALIZED, "Supabase 缺少本專案資料表或函式，請先執行 migration", "supabase"
                ) from exc
            raise ServiceError(ErrorCode.UNAVAILABLE, f"Supabase 查詢失敗：{type(exc).__name__}", "supabase") from exc

    def is_project_database(self) -> bool:
        try:
            rows = self._execute(
                self._client.table("app_schema_meta").select("value").eq("key", "project").limit(1)
            )
        except ServiceError as exc:
            if exc.code == ErrorCode.SCHEMA_NOT_INITIALIZED:
                return False
            raise
        return bool(rows) and rows[0].get("value") == PROJECT_MARKER

    def ensure_schema(self) -> None:
        if self._schema_checked:
            return
        if not self.is_project_database():
            raise ServiceError(
                ErrorCode.SCHEMA_NOT_INITIALIZED,
                "Supabase 不是 nutri_safe_ai 的資料庫（找不到 schema marker），請確認 SUPABASE_URL 並執行 migration",
                "supabase",
            )
        self._schema_checked = True

    def table(self, name: str):
        self.ensure_schema()
        return self._client.table(name)

    def select(self, builder) -> list[dict[str, Any]]:
        return self._execute(builder)

    def rpc(self, fn: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.ensure_schema()
        return self._execute(self._client.rpc(fn, params))

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str | None = None) -> list[dict[str, Any]]:
        if not rows:
            return []
        builder = self.table(table).upsert(rows, on_conflict=on_conflict) if on_conflict else self.table(table).upsert(rows)
        return self._execute(builder)

    def update(self, table: str, values: dict[str, Any], match: dict[str, Any]) -> list[dict[str, Any]]:
        builder = self.table(table).update(values)
        for column, value in match.items():
            builder = builder.eq(column, value)
        return self._execute(builder)

    def insert(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        return self._execute(self.table(table).insert(rows))

    def source_info(self, source_id: str | None) -> dict[str, Any]:
        if not source_id:
            return {}
        if source_id not in self._source_cache:
            rows = self._execute(
                self.table("source_registry").select("id,source_name,is_official,base_url").eq("id", source_id).limit(1)
            )
            self._source_cache[source_id] = rows[0] if rows else {}
        return self._source_cache[source_id]


class SupabaseOfficialDataRepository:
    """OfficialDataRepository 的 Supabase 實作。"""

    def __init__(self, service: SupabaseService):
        self._db = service

    def _with_source(self, row: dict[str, Any]) -> dict[str, Any]:
        info = self._db.source_info(row.get("source_id"))
        row.setdefault("source_name", info.get("source_name"))
        row.setdefault("is_official", info.get("is_official", False))
        return row

    def search_products(self, terms: list[str], limit: int) -> list[dict[str, Any]]:
        rows = self._db.rpc("search_products", {"p_terms": terms, "p_limit": limit})
        return [r["product"] for r in rows]

    def get_products_by_permit_code(self, permit_code: str) -> list[dict[str, Any]]:
        return self._db.select(self._db.table("product_details").select("*").eq("permit_code", permit_code))

    def search_ingredients(self, terms: list[str], limit: int) -> list[dict[str, Any]]:
        rows = self._db.rpc("search_ingredients", {"p_terms": terms, "p_limit": limit})
        return [self._with_source(r["ingredient"]) for r in rows]

    def get_approved_claims(self, product_ids: list[str]) -> list[dict[str, Any]]:
        rows = self._db.select(
            self._db.table("product_claims")
            .select("id,product_id,approved_claim_id,claim_text,source_id,source_url,source_record_key,retrieved_at,approved_claims(function_name,claim_kind)")
            .in_("product_id", product_ids)
        )
        out = []
        for r in rows:
            r = self._with_source(r)
            r["product_claim_id"] = r.pop("id")
            approved = r.pop("approved_claims", None) or {}
            r["function_name"] = approved.get("function_name", "")
            r["claim_kind"] = approved.get("claim_kind")
            out.append(r)
        return out

    def get_warnings(self, product_ids: list[str]) -> list[dict[str, Any]]:
        rows = self._db.select(
            self._db.table("product_warnings")
            .select("product_id,warning_id,source_id,source_url,source_record_key,retrieved_at,warnings(warning_type,text)")
            .in_("product_id", product_ids)
        )
        out = []
        for r in rows:
            r = self._with_source(r)
            r.update(r.pop("warnings", None) or {})
            out.append(r)
        return out

    def get_ingredients_for_products(self, product_ids: list[str]) -> list[dict[str, Any]]:
        rows = self._db.select(
            self._db.table("product_ingredients")
            .select("product_id,ingredient_id,raw_text,source_id,source_url,source_record_key,retrieved_at,ingredients(name_zh,name_en)")
            .in_("product_id", product_ids)
        )
        out = []
        for r in rows:
            r = self._with_source(r)
            r.update(r.pop("ingredients", None) or {})
            out.append(r)
        return out

    def search_illegal_ad_cases(self, terms: list[str], limit: int) -> list[dict[str, Any]]:
        rows = self._db.rpc("search_illegal_ad_cases", {"p_terms": terms, "p_limit": limit})
        return [self._with_source(r["ad_case"]) for r in rows]

    def search_entity_aliases(self, terms: list[str], entity_types: list[str], limit: int) -> list[dict[str, Any]]:
        rows = self._db.rpc(
            "search_entity_aliases", {"p_terms": terms, "p_entity_types": entity_types, "p_limit": limit}
        )
        return [r["alias_row"] for r in rows]

    def get_products_by_company_ids(self, company_ids: list[str], limit: int) -> list[dict[str, Any]]:
        return self._db.select(
            self._db.table("product_details").select("*").in_("company_id", company_ids).limit(limit)
        )


class SupabaseRagStore:
    """RagStore 的 Supabase pgvector 實作。"""

    def __init__(self, service: SupabaseService):
        self._db = service

    def match_chunks(
        self, embedding: list[float], top_k: int, min_similarity: float, country_code: str = "TW"
    ) -> list[dict[str, Any]]:
        return self._db.rpc(
            "match_rag_chunks",
            {
                "query_embedding": embedding,
                "match_count": top_k,
                "min_similarity": min_similarity,
                "p_country_code": country_code,
            },
        )
