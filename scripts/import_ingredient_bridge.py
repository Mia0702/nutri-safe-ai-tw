"""Ingredient Bridge importer（corpus＋reviewed overlay → 0005 表＋Neo4j CONTAINS / CONTRIBUTES_TO_NUTRIENT）。

預設 dry-run：計算 A → E 各階段 insert / update / unchanged / stale、collision、invalid mapping，不寫入。

用法（在專案根目錄）：
    python -m scripts.import_ingredient_bridge --dry-run                                  # 唯讀讀 production 輸入
    python -m scripts.import_ingredient_bridge --dry-run --compare-supabase               # ＋唯讀比對 0005 表既有列
    python -m scripts.import_ingredient_bridge --dry-run --input-json <唯讀快照>          # 不連線
    python -m scripts.import_ingredient_bridge --apply --confirm-project nutri_safe_ai    # 寫入（需 0005 marker、需使用者同意）

只寫 0005 的表與完成標記；不寫 products / ingredients / product_ingredients；不刪除；
不建立 SAME_AS / INTERACTS_WITH / Interaction；不判斷風險或劑量。
"""

import argparse
import json
import sys
from pathlib import Path

from app.config.project import PROJECT_ID
from app.ingestion.ingredient_bridge_import import (
    MIGRATION_0005_MARKER,
    STAGES,
    apply_bridge_import,
    plan_bridge_import,
)
from app.ingestion.ingredient_entity_mapping import CORPUS_DIR, OVERLAY_DIR, load_interaction_corpus, load_reviewed_overlay
from scripts.dry_run_ingredient_entity_mapping import load_production_inputs, map_all


ORDER_KEYS = {table: pk for _, table, pk in STAGES} | {"app_schema_meta": "key"}


def ordered_store(service, *, read_only: bool):
    """SupabaseCorpusStore＋分頁穩定排序（主鍵 order by；否則大表 range 分頁可能漏列 / 重複）。"""
    from app.ingestion.claim_policy_corpus import SupabaseCorpusStore
    from app.schemas.enums import ErrorCode
    from app.services.errors import ServiceError

    class OrderedStore(SupabaseCorpusStore):
        def fetch(self, table, columns, eq=None):
            rows, start = [], 0
            try:
                while True:
                    query = self._db.table(table).select(",".join(columns))
                    for column, value in (eq or {}).items():
                        query = query.eq(column, value)
                    query = query.order(ORDER_KEYS.get(table, "id"))
                    page = self._db.select(query.range(start, start + self.PAGE - 1))
                    rows.extend(page)
                    if len(page) < self.PAGE:
                        return rows
                    start += self.PAGE
            except ServiceError as exc:
                if self.read_only and exc.code == ErrorCode.SCHEMA_NOT_INITIALIZED:
                    self.missing_tables.add(table)
                    return []
                raise

    return OrderedStore(service, read_only=read_only)


def _print_report(report: dict, mode: str) -> None:
    print(f"[{mode}] corpus {report['corpus_version']}  overlay {report['overlay_version']}")
    for stage, counts in report["stages"].items():
        print(f"  {stage:22s} {counts}")
    print(f"  Would update: {report['would_update']}")
    print(f"  Would delete: {report['would_delete']}")
    print(f"  Collisions: {report['collisions']}")
    print(f"  Invalid formal mappings: {report['invalid_formal_mappings']}")
    print(f"  Invalid candidate mappings: {report['invalid_candidate_mappings']}")
    print(f"  Mapping units: {report['mapping_status_units']}")
    for cat, counts in report["by_category"].items():
        print(f"  {cat:34s} {counts}")
    print(f"  Neo4j preview: {json.dumps(report['neo4j_preview'], ensure_ascii=False)}")
    for note in report["notes"]:
        print(f"  note: {note}")
    for err in report["errors"]:
        print(f"  ERROR: {err}")


def main() -> int:
    parser = argparse.ArgumentParser(description="呷安心 Ingredient Bridge importer")
    parser.add_argument("--corpus-dir", type=Path, default=CORPUS_DIR)
    parser.add_argument("--overlay-dir", type=Path, default=OVERLAY_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="預設；不寫入任何資料庫")
    mode.add_argument("--apply", action="store_true", help="寫入 Supabase（與 Neo4j，除非 --skip-graph）")
    parser.add_argument("--confirm-project", default=None, help=f"--apply 時必須為 {PROJECT_ID}")
    parser.add_argument("--compare-supabase", action="store_true", help="dry-run 時唯讀比對 0005 表既有列")
    parser.add_argument("--input-json", type=Path, default=None, help="唯讀輸入快照（dry-run 用）")
    parser.add_argument("--skip-graph", action="store_true")
    parser.add_argument("--json-report", type=Path, default=None)
    args = parser.parse_args()

    if args.apply and args.confirm_project != PROJECT_ID:
        print(f"--apply 需要 --confirm-project {PROJECT_ID}")
        return 2
    if args.apply and args.input_json:
        print("--apply 必須讀取目前 production 輸入，不接受 --input-json")
        return 2

    corpus = load_interaction_corpus(args.corpus_dir, overlay=load_reviewed_overlay(args.overlay_dir))
    data = (json.loads(args.input_json.read_text(encoding="utf-8")) if args.input_json else load_production_inputs())
    results = map_all(corpus, data)

    store = service = settings = None
    if args.apply or args.compare_supabase:
        from app.config.settings import get_settings
        from app.services.supabase_service import SupabaseService

        settings = get_settings()
        service = SupabaseService(settings)
        service.ensure_schema()  # 不是呷安心 DB 時直接拋錯
        store = ordered_store(service, read_only=not args.apply)
        marker = service.select(service.table("app_schema_meta").select("value").eq("key", MIGRATION_0005_MARKER).limit(1))
        if args.apply and not marker:
            print(f"--apply 需要先套用 migrations/0005_product_ingredient_entity_mapping.sql（找不到 {MIGRATION_0005_MARKER}）")
            return 2

    plan = plan_bridge_import(corpus, results, store,
                              product_ids={p["id"] for p in data["products"]},
                              ingredient_ids={i["id"] for i in data["ingredients"]})
    if store is not None and getattr(store, "missing_tables", None):
        plan.notes.append(f"0005 表尚未存在（視為空表）：{sorted(store.missing_tables)}")
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
            report = apply_bridge_import(plan, store, neo4j=neo4j)
        finally:
            if neo4j is not None:
                neo4j.close()
        _print_report(report, "apply")
    if args.json_report:
        args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
