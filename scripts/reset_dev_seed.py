"""Development seed reset：清除呷安心的 seed 資料（不刪 schema）。

只在 Supabase `app_schema_meta.project` 與 Neo4j `(:AppSchemaMeta {project})` 都等於 nutri_safe_ai，
且命令列明確帶 `--confirm-project nutri_safe_ai` 時才執行。

清除範圍（Supabase / Neo4j / RAG 一致）：
* Supabase：官方 seed 資料表（raw_import_records、companies、permits、products、ingredients、
  approved_claims、warnings、brands 與所有 join / alias 表）與 RAG（rag_documents / rag_chunks）。
* Neo4j：本專案 domain label 的節點與關係（AppSchemaMeta marker 與 constraints 保留）。

保留：app_schema_meta、source_registry、line_users、analysis_requests / evidence_logs / observed_claims、
illegal_ad_cases（若 illegal_ad_cases 仍引用 raw_import_records，直接拒絕執行）。

用法：
    python -m scripts.reset_dev_seed --confirm-project nutri_safe_ai --dry-run
    python -m scripts.reset_dev_seed --confirm-project nutri_safe_ai
"""

import argparse
import sys

from app.config.project import PROJECT_ID
from app.config.settings import get_settings

# 依 FK 順序（子表先刪）；值 = 一定非 null 的欄位，用來組合「全部列」的 delete filter
SUPABASE_TABLES: list[tuple[str, str]] = [
    ("entity_aliases", "id"),
    ("product_brands", "product_id"),
    ("product_claims", "id"),
    ("product_ingredients", "product_id"),
    ("product_warnings", "product_id"),
    ("brands", "id"),
    ("products", "id"),
    ("permits", "id"),
    ("companies", "id"),
    ("ingredients", "id"),
    ("approved_claims", "id"),
    ("warnings", "id"),
    ("raw_import_records", "id"),
    ("rag_chunks", "id"),
    ("rag_documents", "id"),
]
NEO4J_LABELS = ["Product", "Company", "Permit", "Ingredient", "Claim", "Warning", "Brand", "Source"]
NEO4J_DELETE_BATCH = 2000


def _count(client, table: str) -> int:
    return client.table(table).select("*", count="exact").limit(1).execute().count or 0


def main() -> int:
    parser = argparse.ArgumentParser(description="呷安心 development seed reset")
    parser.add_argument("--confirm-project", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.confirm_project != PROJECT_ID:
        print(f"[abort] --confirm-project 必須為 {PROJECT_ID}")
        return 2

    settings = get_settings()
    from app.services.neo4j_service import Neo4jService
    from app.services.supabase_service import SupabaseService

    supabase = SupabaseService(settings)
    if not supabase.is_project_database():
        print(f"[abort] Supabase project marker 不是 {PROJECT_ID}，拒絕清除")
        return 2
    neo4j = Neo4jService(settings)
    try:
        if not neo4j.is_project_database():
            print(f"[abort] Neo4j project marker 不是 {PROJECT_ID}，拒絕清除")
            return 2
        foreign = neo4j.foreign_labels()
        if foreign:
            print(f"[abort] Neo4j 有非本專案 label：{foreign}，拒絕清除")
            return 2
        client = supabase._client
        referencing = client.table("illegal_ad_cases").select("id").not_.is_("raw_record_id", "null").limit(1).execute().data
        if referencing:
            print("[abort] illegal_ad_cases 仍引用 raw_import_records，請先處理")
            return 2

        before = {t: _count(client, t) for t, _ in SUPABASE_TABLES}
        graph_before = {
            label: neo4j.read(f"MATCH (n:{label}) RETURN count(n) AS c")[0]["c"] for label in NEO4J_LABELS
        }
        print(f"[supabase] before {before}")
        print(f"[neo4j] before {graph_before}")
        if args.dry_run:
            print("[dry-run] 未刪除任何資料")
            return 0

        for table, col in SUPABASE_TABLES:
            client.table(table).delete().not_.is_(col, "null").execute()
        for label in NEO4J_LABELS:
            while True:
                rows = neo4j.write(
                    f"MATCH (n:{label}) WITH n LIMIT $n DETACH DELETE n RETURN count(*) AS c", {"n": NEO4J_DELETE_BATCH}
                )
                if not rows or rows[0]["c"] == 0:
                    break

        after = {t: _count(client, t) for t, _ in SUPABASE_TABLES}
        graph_after = {
            label: neo4j.read(f"MATCH (n:{label}) RETURN count(n) AS c")[0]["c"] for label in NEO4J_LABELS
        }
        print(f"[supabase] after {after}")
        print(f"[neo4j] after {graph_after}")
        ok = not any(after.values()) and not any(graph_after.values()) and neo4j.is_project_database()
        print("[reset] OK" if ok else "[reset] 尚有殘留資料")
        return 0 if ok else 1
    finally:
        neo4j.close()


if __name__ == "__main__":
    sys.exit(main())
