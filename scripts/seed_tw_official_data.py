"""TW 官方資料 seed pipeline。

official data → normalization → Supabase → Neo4j → RAG

用法（在專案根目錄）：
    python -m scripts.seed_tw_official_data --dry-run          # 只抓資料與正規化，不寫入
    python -m scripts.seed_tw_official_data --limit 50         # 匯入 50 筆健康食品 + RAG 文件
    python -m scripts.seed_tw_official_data --all              # 匯入全部

安全防護：Supabase / Neo4j 必須已有呷安心 schema marker，否則拒絕寫入。
"""

import argparse
import json
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone

import httpx

from app.config.project import USER_AGENT
from app.config.settings import get_settings
from app.ingestion.ingredient_canonical import apply_canonical_names, build_ingredient_canonicalization
from app.ingestion.tfda_health_food import DATASET_URL, flatten_record, normalize_record

DEFAULT_RAG_DOCUMENTS = [
    {
        "url": "https://www.fda.gov.tw/tc/siteListContent.aspx?id=18087&sid=1758",
        "source_name": "衛生福利部食品藥物管理署 健康食品相關管理規定（健康食品管理法）",
        "document_type": "regulation",
    },
]


def fetch_dataset() -> list[dict]:
    resp = httpx.get(DATASET_URL, timeout=90, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return [flatten_record(r) for r in json.loads(resp.content.decode("utf-8-sig"))]


def main() -> int:
    parser = argparse.ArgumentParser(description="呷安心 TW 官方資料 seed")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-graph", action="store_true")
    parser.add_argument("--skip-rag", action="store_true")
    parser.add_argument("--graph-id-batch-size", type=int, default=None, help="graph sync 每次 Supabase in_ 查詢的 ID 數")
    parser.add_argument("--graph-write-batch-size", type=int, default=None, help="graph sync 每次 Neo4j 寫入列數")
    args = parser.parse_args()
    # 注意：資料庫需先依序執行 migrations/0001_initial_schema.sql、0002_brand_relationship_semantics.sql

    retrieved_at = datetime.now(timezone.utc)
    records = fetch_dataset()
    records.sort(key=lambda r: str(r.get("許可證字號")))
    selected = records if args.all else records[: args.limit]
    # 成分 canonical 名稱以完整資料集決定，避免 --limit 不同而得到不同顯示名稱
    all_bundles = [b for b in (normalize_record(r, retrieved_at) for r in records) if b]
    canon = build_ingredient_canonicalization(m for b in all_bundles for m in b.ingredient_mentions)
    apply_canonical_names(all_bundles, canon)
    selected_keys = {flatten_record(r).get("許可證字號") for r in selected}
    bundles = [b for b in all_bundles if b.raw_data.get("許可證字號") in selected_keys]
    print(f"[fetch] 官方資料 {len(records)} 筆，選取 {len(selected)} 筆，正規化成功 {len(bundles)} 筆")
    print(f"[normalize] 成分 {sum(len(b.ingredients) for b in bundles)}、核准功效 {sum(len(b.product_claims) for b in bundles)}、"
          f"警語/注意事項 {sum(len(b.warnings) for b in bundles)}；狀態 {dict(Counter(b.product['status'] for b in bundles))}")

    print(f"[ingredient] canonical：寫法 {canon.unique_surfaces} → canonical {canon.canonical_count}；alias {len(canon.aliases)}；"
          f"自動合併 {len(canon.auto_merges)}；inferred candidate {len(canon.inferred_candidates)}；"
          f"review candidate {len(canon.review_candidates)}")
    ing_status = Counter(b.ingredient_parse_status for b in bundles)
    print(f"[ingredient] 成分解析 {dict(ing_status)}；未解析片段 {sum(len(b.ingredient_unresolved_segments) for b in bundles)}")

    name_status = Counter(b.company_name_status for b in bundles)
    print(f"[company] 申請商正規化 {dict(name_status)}；unique 公司 原文 {len({b.company_name_raw for b in bundles})} → "
          f"正規化 {len({b.company['id'] for b in bundles})}")
    for b in bundles:
        if b.company_name_status != "unchanged":
            print(f"  [{b.company_name_status}] {b.company_name_raw} → {b.company['name_zh']}")

    if args.dry_run:
        from app.ingestion.alias_builder import build_aliases

        dry = build_aliases(all_bundles, "dry-run")  # 品牌以完整資料集推導；不寫入
        print(f"[brand] candidate {len(dry.brand_candidates)}；gate {dict(Counter(c['gate_status'] for c in dry.brand_candidates))}；"
              f"採用 brands {len(dry.brands)}、product_brands {len(dry.product_brands)}；"
              f"evidence {dict(Counter(b['evidence_status'] for b in dry.brands))}")
        print(json.dumps(bundles[0].model_dump(exclude={"raw_data"}), ensure_ascii=False, indent=2)[:3000])
        print("[dry-run] 未寫入任何資料庫")
        return 0

    settings = get_settings()
    from app.ingestion.supabase_loader import load_health_food
    from app.services.supabase_service import SupabaseService

    supabase = SupabaseService(settings)
    supabase.ensure_schema()  # 不是呷安心 DB 時直接拋錯
    batch_id = f"seed-{retrieved_at:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
    counts = load_health_food(supabase, bundles, retrieved_at, batch_id)
    print(f"[supabase] {counts}")

    # 別名以完整資料集推導（統計才有意義），只寫入本次已匯入實體的別名
    from app.ingestion.alias_builder import build_aliases
    from app.ingestion.supabase_loader import ensure_source, load_aliases
    from app.ingestion.tfda_health_food import SOURCE_REGISTRY_ROW

    built = build_aliases(all_bundles, ensure_source(supabase, SOURCE_REGISTRY_ROW))
    loaded_products = {b.product["id"] for b in bundles}
    loaded_companies = {b.company["id"] for b in bundles}
    loaded_ingredients = {i["id"] for b in bundles for i in b.ingredients}
    brands = [b for b in built.brands if b["company_id"] in loaded_companies]
    brand_ids = {b["id"] for b in brands}
    product_brands = [pb for pb in built.product_brands if pb["product_id"] in loaded_products and pb["brand_id"] in brand_ids]
    allowed = {"COMPANY": loaded_companies, "INGREDIENT": loaded_ingredients, "BRAND": brand_ids, "PRODUCT": loaded_products}
    aliases = [a for a in built.aliases if a["entity_id"] in allowed.get(a["entity_type"], set())]
    print(f"[aliases] {load_aliases(supabase, brands, product_brands, aliases)}")

    if not args.skip_graph:
        from app.ingestion.graph_sync import DEFAULT_ID_BATCH_SIZE, DEFAULT_WRITE_BATCH_SIZE, sync_graph_from_supabase
        from app.services.neo4j_service import Neo4jService

        neo4j = Neo4jService(settings)
        try:
            neo4j.ensure_schema()
            graph_counts = sync_graph_from_supabase(
                supabase, neo4j, [b.product["id"] for b in bundles],
                id_batch_size=args.graph_id_batch_size or DEFAULT_ID_BATCH_SIZE,
                write_batch_size=args.graph_write_batch_size or DEFAULT_WRITE_BATCH_SIZE,
            )
            print(f"[neo4j] {graph_counts}")
        finally:
            neo4j.close()

    if not args.skip_rag:
        from app.ingestion.rag_ingest import fetch_official_document, ingest_document
        from app.services.openai_service import OpenAIService

        llm = OpenAIService(settings)
        for doc_cfg in DEFAULT_RAG_DOCUMENTS:
            doc = fetch_official_document(doc_cfg["url"])
            result = ingest_document(
                supabase, llm, doc, source_name=doc_cfg["source_name"], document_type=doc_cfg["document_type"],
                embedding_model=settings.openai_embedding_model,
            )
            print(f"[rag] {doc.title[:30]}… → {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
