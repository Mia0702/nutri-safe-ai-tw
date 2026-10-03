"""把正規化後的 TFDA 健康食品資料寫入 Supabase（idempotent upsert）。"""

import logging
from datetime import datetime
from typing import Any, Iterable

from app.ingestion.tfda_health_food import SOURCE_REGISTRY_ROW, HealthFoodBundle

logger = logging.getLogger("nutri_safe_ai.ingestion")
BATCH = 200


def _dedupe(rows: Iterable[dict[str, Any]], key: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        seen[tuple(row[k] for k in key)] = row
    return list(seen.values())


def _upsert(service, table: str, rows: list[dict[str, Any]], on_conflict: str) -> list[dict[str, Any]]:
    out = []
    for i in range(0, len(rows), BATCH):
        out.extend(service.upsert(table, rows[i : i + BATCH], on_conflict=on_conflict))
    return out


def ensure_source(service, row: dict[str, Any]) -> str:
    [saved] = service.upsert("source_registry", [row], on_conflict="source_key")
    return saved["id"]


def load_health_food(service, bundles: list[HealthFoodBundle], retrieved_at: datetime, batch_id: str) -> dict[str, int]:
    source_id = ensure_source(service, SOURCE_REGISTRY_ROW)
    raw_rows = [
        {
            "source_id": source_id,
            "source_record_key": b.source_record_key,
            "source_url": b.source_url,
            "retrieved_at": retrieved_at.isoformat(),
            "raw_data": b.raw_data,
            "raw_hash": b.raw_hash,
            "import_batch_id": batch_id,
        }
        for b in bundles
    ]
    saved_raw = _upsert(service, "raw_import_records", raw_rows, "source_id,source_record_key,raw_hash")
    raw_id = {r["source_record_key"]: r["id"] for r in saved_raw}

    def with_source(rows: Iterable[dict[str, Any]], raw_link: bool = False) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            row = {**row, "source_id": source_id}
            if raw_link:
                row["raw_record_id"] = raw_id.get(row["source_record_key"])
            out.append(row)
        return out

    counts: dict[str, int] = {"raw_import_records": len(saved_raw)}
    plan = [
        ("companies", _dedupe(with_source((b.company for b in bundles), True), ("id",)), "id"),
        ("permits", _dedupe(with_source((b.permit for b in bundles), True), ("id",)), "id"),
        ("products", _dedupe(with_source((b.product for b in bundles), True), ("id",)), "id"),
        ("ingredients", _dedupe(with_source(i for b in bundles for i in b.ingredients), ("id",)), "id"),
        ("product_ingredients", _dedupe(with_source(pi for b in bundles for pi in b.product_ingredients), ("product_id", "ingredient_id")), "product_id,ingredient_id"),
        ("approved_claims", _dedupe(with_source(c for b in bundles for c in b.approved_claims), ("id",)), "id"),
        ("product_claims", _dedupe(with_source(pc for b in bundles for pc in b.product_claims), ("id",)), "id"),
        ("warnings", _dedupe(with_source(w for b in bundles for w in b.warnings), ("id",)), "id"),
        ("product_warnings", _dedupe(with_source(pw for b in bundles for pw in b.product_warnings), ("product_id", "warning_id")), "product_id,warning_id"),
    ]
    for table, rows, conflict in plan:
        counts[table] = len(_upsert(service, table, rows, conflict))
        logger.info(f"upsert {table}: {counts[table]}")
    return counts


ALIAS_FK_COLUMN = {
    "PRODUCT": "product_id",
    "BRAND": "brand_id",
    "COMPANY": "company_id",
    "INGREDIENT": "ingredient_id",
}


def alias_row_for_db(alias: dict[str, Any]) -> dict[str, Any]:
    """entity_aliases.entity_id 是 generated column：寫入時改填對應實體的 FK 欄位。"""
    row = {k: v for k, v in alias.items() if k != "entity_id"}
    for column in ALIAS_FK_COLUMN.values():
        row[column] = None
    row[ALIAS_FK_COLUMN[alias["entity_type"]]] = alias["entity_id"]
    return row


def load_aliases(
    service, brands: list[dict[str, Any]], product_brands: list[dict[str, Any]], aliases: list[dict[str, Any]]
) -> dict[str, int]:
    """brands / product_brands / entity_aliases upsert（只應傳入已匯入實體的資料）。"""
    return {
        "brands": len(_upsert(service, "brands", _dedupe(brands, ("id",)), "id")),
        "product_brands": len(_upsert(
            service, "product_brands", _dedupe(product_brands, ("product_id", "brand_id")), "product_id,brand_id"
        )),
        "entity_aliases": len(_upsert(
            service, "entity_aliases", [alias_row_for_db(a) for a in _dedupe(aliases, ("id",))], "id"
        )),
    }
