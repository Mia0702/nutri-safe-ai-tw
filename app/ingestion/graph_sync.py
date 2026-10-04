"""從 Supabase（canonical）同步知識關係到 Neo4j。

Neo4j 不自行維護產品主檔：節點只帶 canonical ID 與顯示需要的欄位，
每次同步都以 Supabase 為準 MERGE，可重複執行。
"""

import json
import logging
from typing import Any

logger = logging.getLogger("nutri_safe_ai.ingestion")
PAGE = 1000
# 每次 `in_` 查詢最多帶幾個 ID：UUID 36 字元，100 個約 4KB URL，遠低於常見 gateway 上限
DEFAULT_ID_BATCH_SIZE = 100
# 每次 Neo4j UNWIND 寫入的列數
DEFAULT_WRITE_BATCH_SIZE = 500


class GraphSyncBatchError(RuntimeError):
    """某一批次讀取或寫入失敗。每個批次都是 MERGE（idempotent），修正後可整個重跑。"""

    def __init__(self, stage: str, batch_index: int, batch_count: int, cause: Exception):
        self.stage, self.batch_index, self.batch_count = stage, batch_index, batch_count
        super().__init__(f"graph sync 批次失敗：{stage} 第 {batch_index + 1}/{batch_count} 批：{type(cause).__name__}: {cause}")


def _chunks(items: list, size: int) -> list[list]:
    if size < 1:
        raise ValueError("batch size 必須 >= 1")
    return [items[i : i + size] for i in range(0, len(items), size)]


def _fetch_all(
    service, table: str, columns: str, in_filter: tuple[str, list[str]] | None = None,
    id_batch_size: int = DEFAULT_ID_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """分頁讀取；有 ID 過濾時把 ID 切成小批，避免單一 URL 過長（414）。"""
    id_batches = _chunks(list(in_filter[1]), id_batch_size) if in_filter else [None]
    rows: list[dict[str, Any]] = []
    for index, ids in enumerate(id_batches):
        try:
            start = 0
            while True:
                query = service.table(table).select(columns)
                if ids is not None:
                    query = query.in_(in_filter[0], ids)
                page = service.select(query.range(start, start + PAGE - 1))
                rows.extend(page)
                if len(page) < PAGE:
                    break
                start += PAGE
        except Exception as exc:
            raise GraphSyncBatchError(f"read {table}", index, len(id_batches), exc) from exc
    return rows


def _run(neo4j, query: str, rows: list[dict[str, Any]], stage: str = "write",
         batch_size: int = DEFAULT_WRITE_BATCH_SIZE) -> int:
    batches = _chunks(rows, batch_size)
    for index, batch in enumerate(batches):
        try:
            neo4j.write(query, {"rows": batch})
        except Exception as exc:
            raise GraphSyncBatchError(f"write {stage}", index, len(batches), exc) from exc
    return len(rows)


def sync_graph_from_supabase(
    supabase, neo4j, product_ids: list[str] | None = None,
    id_batch_size: int = DEFAULT_ID_BATCH_SIZE, write_batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
) -> dict[str, int]:
    pf = ("id", product_ids) if product_ids else None
    lf = ("product_id", product_ids) if product_ids else None

    def fetch(table: str, columns: str, in_filter=None) -> list[dict[str, Any]]:
        return _fetch_all(supabase, table, columns, in_filter, id_batch_size)

    def run(stage: str, query: str, rows: list[dict[str, Any]]) -> int:
        return _run(neo4j, query, rows, stage, write_batch_size)

    products = fetch("products", "id,product_name_zh,status,country_code,product_category,company_id,permit_id,source_id,source_url,source_record_key", pf)
    company_ids = sorted({p["company_id"] for p in products if p["company_id"]})
    permit_ids = sorted({p["permit_id"] for p in products if p["permit_id"]})
    companies = fetch("companies", "id,name_zh", ("id", company_ids)) if company_ids else []
    permits = fetch("permits", "id,permit_number,status", ("id", permit_ids)) if permit_ids else []
    sources = fetch("source_registry", "id,source_name,is_official,base_url")
    p_ings = fetch("product_ingredients", "product_id,ingredient_id,raw_text,ingredients(name_zh,name_en)", lf)
    p_claims = fetch("product_claims", "product_id,approved_claim_id,claim_text,source_id,approved_claims(function_name,claim_kind)", lf)
    p_warns = fetch("product_warnings", "product_id,warning_id,warnings(warning_type,text)", lf)
    p_brands = fetch(
        "product_brands",
        "product_id,brand_id,confidence,evidence_status,is_inferred,inference_basis,"
        "brands(name,company_id,company_relation,confidence,evidence_status,is_inferred,inference_basis)",
        lf,
    )
    for row in p_brands:  # Neo4j 屬性不能是巢狀 map，推導依據以 JSON 字串保存
        row["basis_json"] = json.dumps(row.get("inference_basis") or {}, ensure_ascii=False)
        row["brand_basis_json"] = json.dumps((row.get("brands") or {}).get("inference_basis") or {}, ensure_ascii=False)

    counts = {}
    counts["Source"] = run("Source", """
        UNWIND $rows AS r MERGE (s:Source {source_id: r.id})
        SET s.source_name = r.source_name, s.is_official = r.is_official, s.base_url = r.base_url""", sources)
    counts["Product"] = run("Product", """
        UNWIND $rows AS r MERGE (p:Product {product_id: r.id})
        SET p.name_zh = r.product_name_zh, p.status = r.status, p.country_code = r.country_code,
            p.product_category = r.product_category
        WITH p, r MATCH (s:Source {source_id: r.source_id})
        MERGE (p)-[rel:SUPPORTED_BY]->(s)
        SET rel.source_url = r.source_url, rel.source_record_key = r.source_record_key""", products)
    counts["Company"] = run("Company", """
        UNWIND $rows AS r MERGE (c:Company {company_id: r.id}) SET c.name_zh = r.name_zh""", companies)
    counts["Permit"] = run("Permit", """
        UNWIND $rows AS r MERGE (pm:Permit {permit_id: r.id})
        SET pm.permit_number = r.permit_number, pm.status = r.status""", permits)
    counts["OWNS_OR_APPLIES_FOR/REGISTERED_AS"] = run("OWNS_OR_APPLIES_FOR/REGISTERED_AS", """
        UNWIND $rows AS r MATCH (p:Product {product_id: r.id})
        OPTIONAL MATCH (c:Company {company_id: r.company_id})
        OPTIONAL MATCH (pm:Permit {permit_id: r.permit_id})
        FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END | MERGE (c)-[:OWNS_OR_APPLIES_FOR]->(p))
        FOREACH (_ IN CASE WHEN pm IS NULL THEN [] ELSE [1] END | MERGE (p)-[:REGISTERED_AS]->(pm))""", products)
    counts["CONTAINS"] = run("CONTAINS", """
        UNWIND $rows AS r MATCH (p:Product {product_id: r.product_id})
        MERGE (i:Ingredient {ingredient_id: r.ingredient_id})
        SET i.name_zh = r.ingredients.name_zh, i.name_en = r.ingredients.name_en
        MERGE (p)-[rel:CONTAINS]->(i) SET rel.raw_text = r.raw_text""", p_ings)
    counts["HAS_APPROVED_CLAIM"] = run("HAS_APPROVED_CLAIM", """
        UNWIND $rows AS r MATCH (p:Product {product_id: r.product_id})
        MERGE (c:Claim {claim_id: r.approved_claim_id}) SET c.function_name = r.approved_claims.function_name, c.claim_kind = r.approved_claims.claim_kind
        MERGE (p)-[rel:HAS_APPROVED_CLAIM]->(c) SET rel.claim_text = r.claim_text
        WITH c, r MATCH (s:Source {source_id: r.source_id}) MERGE (c)-[:SUPPORTED_BY]->(s)""", p_claims)
    counts["HAS_WARNING"] = run("HAS_WARNING", """
        UNWIND $rows AS r MATCH (p:Product {product_id: r.product_id})
        MERGE (w:Warning {warning_id: r.warning_id})
        SET w.warning_type = r.warnings.warning_type, w.text = r.warnings.text
        MERGE (p)-[:HAS_WARNING]->(w)""", p_warns)
    for row in p_brands:  # 未知或缺少關係一律視為 ASSOCIATED_WITH_BRAND（不可預設為擁有）
        brand = row.get("brands") or {}
        row["company_relation"] = "OWNS_BRAND" if brand.get("company_relation") == "OWNS_BRAND" else "ASSOCIATED_WITH_BRAND"
    # Company → Brand：OWNS_BRAND 只用於有 ownership evidence 的官方 / 人工審核品牌；
    # 由產品名稱推導的品牌一律是 ASSOCIATED_WITH_BRAND（只代表有關聯，不代表擁有）
    counts["Brand/HAS_BRAND/COMPANY_BRAND"] = run("Brand/HAS_BRAND/COMPANY_BRAND", """
        UNWIND $rows AS r MATCH (p:Product {product_id: r.product_id})
        MERGE (b:Brand {brand_id: r.brand_id})
        SET b.name = r.brands.name, b.confidence = r.brands.confidence,
            b.evidence_status = r.brands.evidence_status, b.is_inferred = r.brands.is_inferred,
            b.inference_basis = r.brand_basis_json
        MERGE (p)-[rel:HAS_BRAND]->(b)
        SET rel.confidence = r.confidence, rel.evidence_status = r.evidence_status,
            rel.is_inferred = r.is_inferred, rel.inference_basis = r.basis_json
        WITH b, r MATCH (c:Company {company_id: r.brands.company_id})
        FOREACH (_ IN CASE WHEN r.company_relation = 'OWNS_BRAND' THEN [1] ELSE [] END |
            MERGE (c)-[own:OWNS_BRAND]->(b)
            SET own.confidence = r.brands.confidence, own.evidence_status = r.brands.evidence_status,
                own.is_inferred = r.brands.is_inferred, own.inference_basis = r.brand_basis_json)
        FOREACH (_ IN CASE WHEN r.company_relation = 'OWNS_BRAND' THEN [] ELSE [1] END |
            MERGE (c)-[assoc:ASSOCIATED_WITH_BRAND]->(b)
            SET assoc.confidence = r.brands.confidence, assoc.evidence_status = r.brands.evidence_status,
                assoc.is_inferred = r.brands.is_inferred, assoc.inference_basis = r.brand_basis_json)""", p_brands)
    # 關係語意改變時移除舊的另一種關係，避免推測品牌殘留 OWNS_BRAND
    run("cleanup stale company-brand relation", """
        UNWIND $rows AS r MATCH (c:Company)-[old:OWNS_BRAND|ASSOCIATED_WITH_BRAND]->(b:Brand {brand_id: r.brand_id})
        WHERE type(old) <> r.company_relation DELETE old""", p_brands)
    for k, v in counts.items():
        logger.info(f"graph sync {k}: {v}")
    return counts
