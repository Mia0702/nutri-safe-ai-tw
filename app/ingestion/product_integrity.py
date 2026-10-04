"""產品資料 integrity 規則（唯讀、deterministic；由 scripts/check_data_integrity.py 使用）。

不再固定「Product = 某個數字」。改為依來源 / 批次 / 類別驗證：
  1. 每個產品的 product_category 必須是已知類別，且產品來源（source_registry.source_key）與類別一致。
  2. 每個來源的產品數 = 該來源 raw_import_records 的不同 source_record_key 數（批次內每列官方紀錄恰好一個產品）。
  3. 類別預期筆數（選填）：使用者以 CLI 指定，或由已匯入的 coverage snapshot validation.json 取得。
  4. Canonical ID 不變：產品 id 必須等於由官方字號推得的 uuid5
     （health_food：TW:<許可證字號>；查驗登記／核備類別：TW:TFDA:<InfoId>:<原始字號>）。
這裡不放任何特定產品、字號或筆數。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ingestion import tfda_product_coverage as coverage
from app.ingestion.tfda_health_food import SOURCE_KEY as HEALTH_FOOD_SOURCE_KEY
from app.normalization import stable_id
from app.schemas.enums import ProductCategory

CATEGORY_SOURCE_KEYS: dict[ProductCategory, str] = {
    ProductCategory.HEALTH_FOOD: HEALTH_FOOD_SOURCE_KEY,
    **{spec.category: spec.source_key for spec in coverage.DATASETS.values()},
}
_CATEGORY_INFO_ID = {spec.category: spec.info_id for spec in coverage.DATASETS.values()}


@dataclass(frozen=True)
class IntegrityCheck:
    ok: bool
    label: str


def expected_product_id(category: ProductCategory, permit_number: str) -> str:
    """產品 canonical id 的推導規則（與 ingestion 相同）。"""
    if category == ProductCategory.HEALTH_FOOD:
        return stable_id("product", f"TW:{permit_number}")
    return coverage.product_id_for(coverage.canonical_key(_CATEGORY_INFO_ID[category], permit_number))


def parse_category_expectations(values: list[str]) -> dict[ProductCategory, int]:
    """CLI「category=count」→ {ProductCategory: count}；未知類別 / 非整數 → ValueError。"""
    out: dict[ProductCategory, int] = {}
    for value in values:
        name, _, count = value.partition("=")
        out[ProductCategory(name.strip())] = int(count)
    return out


def snapshot_expectations(snapshot_dir: str | Path) -> dict[ProductCategory, int]:
    """coverage snapshot validation.json 中各資料集的 normalized_rows（只含查驗登記／核備類別）。"""
    path = Path(snapshot_dir) / coverage.FILES["validation"]
    datasets = json.loads(path.read_text(encoding="utf-8")).get("datasets") or {}
    return {spec.category: int(datasets[info_id]["normalized_rows"])
            for info_id, spec in coverage.DATASETS.items() if info_id in datasets}


def product_count_checks(
    products: list[dict[str, Any]],
    permits: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    raw_records: list[dict[str, Any]],
    expected: dict[ProductCategory, int] | None = None,
    expected_total: int | None = None,
) -> list[IntegrityCheck]:
    """products（id, product_category, permit_id, source_id）、permits（id, permit_number）、
    sources（id, source_key）、raw_records（source_id, source_record_key）。"""
    checks: list[IntegrityCheck] = []
    source_key = {str(s["id"]): s["source_key"] for s in sources}
    permit_number = {str(p["id"]): p["permit_number"] for p in permits}

    by_category: Counter[str] = Counter()
    unknown, wrong_source, wrong_id = [], [], []
    for p in products:
        raw_cat = p.get("product_category")
        try:
            category = ProductCategory(raw_cat)
        except ValueError:
            unknown.append(p["id"])
            continue
        by_category[category.value] += 1
        if source_key.get(str(p.get("source_id"))) != CATEGORY_SOURCE_KEYS[category]:
            wrong_source.append(p["id"])
        number = permit_number.get(str(p.get("permit_id")))
        if not number or str(p["id"]) != expected_product_id(category, number):
            wrong_id.append(p["id"])
    checks.append(IntegrityCheck(not unknown, f"product_category 皆為已知類別（未知 {len(unknown)}）；分布 {dict(by_category)}"))
    checks.append(IntegrityCheck(not wrong_source, f"產品來源與類別一致（不一致 {len(wrong_source)}）"))
    checks.append(IntegrityCheck(not wrong_id, f"產品 canonical ID = 官方字號推導（不一致 {len(wrong_id)}）"))

    # 每個來源：產品數 = raw 官方紀錄（不同 source_record_key）數
    per_source_products = Counter(str(p.get("source_id")) for p in products)
    raw_keys: dict[str, set[str]] = {}
    for r in raw_records:
        raw_keys.setdefault(str(r["source_id"]), set()).add(r["source_record_key"])
    for sid in sorted(set(per_source_products) | {s for s in raw_keys if source_key.get(s) in CATEGORY_SOURCE_KEYS.values()}):
        n_products, n_raw = per_source_products.get(sid, 0), len(raw_keys.get(sid, ()))
        checks.append(IntegrityCheck(n_products == n_raw,
                                     f"來源 {source_key.get(sid, sid)}：products {n_products} / raw records {n_raw}"))

    for category, count in (expected or {}).items():
        actual = by_category.get(category.value, 0)
        checks.append(IntegrityCheck(actual == count, f"{category.value}：{actual}（預期 {count}）"))
    if expected_total is not None:
        checks.append(IntegrityCheck(len(products) == expected_total, f"Product 總數 {len(products)}（預期 {expected_total}）"))
    return checks


def graph_category_checks(
    products: list[dict[str, Any]], graph_rows: list[dict[str, Any]], *, required: bool
) -> list[IntegrityCheck]:
    """Neo4j Product.product_category 必須與 Supabase 相同。required=False（尚未套用 0004）時只回報、不當 blocker。"""
    sb = {str(p["id"]): p.get("product_category") for p in products}
    graph = {str(r["id"]): r.get("product_category") for r in graph_rows}
    missing = sum(1 for pid in sb if graph.get(pid) is None)
    mismatch = sum(1 for pid, cat in sb.items() if graph.get(pid) is not None and graph[pid] != cat)
    ok = mismatch == 0 and (missing == 0 or not required)
    suffix = "" if required else "（0004 未套用：缺值只回報）"
    return [IntegrityCheck(ok, f"Neo4j Product.product_category：缺 {missing}、與 Supabase 不同 {mismatch}{suffix}")]
