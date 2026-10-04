"""TFDA 產品登記覆蓋擴充 importer（國產維生素錠狀膠囊 InfoId 24 / 輸入錠狀膠囊 InfoId 23）。

預設 dry-run：只讀 snapshot 檔案、驗證、計算 insert / update / unchanged / quarantine，不連線、不寫入。

用法（在專案根目錄）：
    python -m scripts.import_tfda_product_coverage --dry-run
    python -m scripts.import_tfda_product_coverage --dry-run --compare-supabase        # 唯讀比對既有資料
    python -m scripts.import_tfda_product_coverage --apply --confirm-project nutri_safe_ai   # 寫入（需使用者同意）

不建立 health_food、核准功效、警語、成分 canonical / Ingredient node / 交互作用；不以品名合併產品。
注意：新類別一旦寫入 products，現行 runtime（resolver / Risk Engine / reporter）就會查到它們；
runtime 尚未區分 product_category 前不要 --apply（見 docs/CURRENT_STATE.md）。
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from app.config.project import PROJECT_ID
from app.ingestion.tfda_product_coverage import (
    MIGRATION_0004_MARKER,
    apply_coverage_import,
    load_snapshot,
    plan_coverage_import,
)

DEFAULT_SNAPSHOT_DIR = Path("data/tfda_product_coverage/2026-10-04")


def _print_report(report: dict, mode: str) -> None:
    p = report["products"]
    ds = report["datasets"]
    print(f"[{mode}] snapshot {report['snapshot_version']}  batch_key {report['batch_key'][:16]}…  as_of {report['as_of']}")
    for label, info_id in (("Domestic", "24"), ("Imported", "23")):
        d = ds[info_id]
        print(f"  {label}: {d['rows']}（{d['category']}；importable {d['importable']}、quarantined {d['quarantined']}；"
              f"review {d['review_status']}；前導零字號 {d['leading_zero_numbers']}；"
              f"derived temporal（不寫入）{d['derived_temporal_status_not_persisted']}）")
    print(f"  Would insert: {p['would_insert']}")
    print(f"  Would update: {p['would_update']}")
    print(f"  Would remain unchanged: {p['would_remain_unchanged']}")
    print(f"  Would quarantine: {p['would_quarantine']}  {report['quarantine_reasons']}")
    print(f"  Key collisions: {report['key_collisions']}")
    print(f"  Missing required fields: {report['missing_required_fields']}")
    c = report["cross_category_candidates"]
    print(f"  Potential cross-category candidates: {c['file_pairs']} pairs {c['by_scope_and_classification']}；"
          f"同名不同字號列 {c['same_name_distinct_registration_rows']}；DB health_food 同名 {c['db_health_food_exact_name_overlap']}"
          f"（{c['action']}）")
    print(f"  READY / NEEDS_REVIEW: {report['review_status']}（成分解析品質，不阻擋產品登記）")
    for table, counts in report["tables"].items():
        print(f"  {table:20s} {counts}")
    print(f"  derived rows: {report['derived_rows_written']}")
    print(f"  graph plan: {json.dumps(report['graph_plan'], ensure_ascii=False)}")
    for note in report["notes"]:
        print(f"  note: {note}")
    for err in report["errors"]:
        print(f"  ERROR: {err}")


def main() -> int:
    parser = argparse.ArgumentParser(description="呷安心 TFDA 產品登記覆蓋擴充 importer")
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="預設；不寫入任何資料庫")
    mode.add_argument("--apply", action="store_true", help="寫入 Supabase（與 Neo4j，除非 --skip-graph）")
    parser.add_argument("--confirm-project", default=None, help=f"--apply 時必須為 {PROJECT_ID}")
    parser.add_argument("--compare-supabase", action="store_true", help="dry-run 時唯讀比對 Supabase 既有資料")
    parser.add_argument("--skip-graph", action="store_true")
    parser.add_argument("--allow-older-snapshot", action="store_true", help="允許匯入比資料庫既有 snapshot 舊的資料")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None, help="derived temporal 報告的基準日（預設今天）")
    parser.add_argument("--json-report", type=Path, default=None)
    args = parser.parse_args()

    if args.apply and args.confirm_project != PROJECT_ID:
        print(f"--apply 需要 --confirm-project {PROJECT_ID}")
        return 2

    snapshot = load_snapshot(args.snapshot_dir)
    store = service = settings = None
    migration_0004 = False
    if args.apply or args.compare_supabase:
        from app.config.settings import get_settings
        from app.ingestion.claim_policy_corpus import SupabaseCorpusStore
        from app.services.supabase_service import SupabaseService

        settings = get_settings()
        service = SupabaseService(settings)
        service.ensure_schema()  # 不是呷安心 DB 時直接拋錯
        store = SupabaseCorpusStore(service, read_only=not args.apply)
        marker = service.select(service.table("app_schema_meta").select("value").eq("key", MIGRATION_0004_MARKER).limit(1))
        migration_0004 = bool(marker)

    if args.apply and not migration_0004:
        # runtime 要靠 0004 的 product_category / expiry_date view 欄位區分類別；沒有 0004 就寫入新類別，
        # 現行 runtime 會把它們當成健康食品處理
        print(f"--apply 需要先套用 migrations/0004_tfda_product_coverage.sql（找不到 {MIGRATION_0004_MARKER} marker）")
        return 2

    plan = plan_coverage_import(snapshot, store, migration_0004=migration_0004, as_of=args.as_of,
                                allow_older_snapshot=args.allow_older_snapshot)
    if not args.apply:
        report = plan.report()
        _print_report(report, "dry-run")
        print("[dry-run] 未寫入 Supabase / Neo4j")
    else:
        neo4j = None
        if not args.skip_graph:
            from app.services.neo4j_service import Neo4jService

            neo4j = Neo4jService(settings)
            neo4j.ensure_schema()
        try:
            report = apply_coverage_import(plan, store, neo4j=neo4j)
        finally:
            if neo4j is not None:
                neo4j.close()
        _print_report(report, "apply")
    if args.json_report:
        args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
