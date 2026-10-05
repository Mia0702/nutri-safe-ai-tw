"""Interaction Corpus importer（drug substance / interaction / participants / evidence → 0006 表＋Neo4j）。

預設 dry-run：計算 A → E 各階段 insert / update / unchanged / stale、semantic gate、Ingredient Bridge 相容性，不寫入。

用法（在專案根目錄）：
    python -m scripts.import_interaction_corpus --dry-run                                  # 不連線；Bridge 相容性以本機 corpus 驗證
    python -m scripts.import_interaction_corpus --dry-run --compare-supabase               # ＋唯讀比對 Bridge 前置資料與 0006 表既有列
    python -m scripts.import_interaction_corpus --apply --confirm-project nutri_safe_ai    # 寫入（需 0006 marker、需使用者同意）

只寫 0006 的表與完成標記；不寫 0001–0005 的任何表；不刪除；
不建立 SAME_AS / INTERACTS_WITH / Product→Interaction；不判斷風險或劑量。
"""

import argparse
import json
import sys
from pathlib import Path

from app.config.project import PROJECT_ID
from app.ingestion.ingredient_entity_mapping import CORPUS_DIR, OVERLAY_DIR, load_interaction_corpus, load_reviewed_overlay
from app.ingestion.interaction_corpus_import import (
    MIGRATION_0006_MARKER,
    STAGES,
    apply_interaction_import,
    bridge_prerequisites,
    load_interaction_corpus_data,
    neo4j_bridge_preflight,
    required_ingredient_participants,
    plan_interaction_import,
)

ORDER_KEYS = {table: pk for _, table, pk in STAGES} | {
    "app_schema_meta": "key", "interaction_corpus_entities": "entity_id", "interaction_corpus_sources": "source_id"}


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
                    for key in ORDER_KEYS.get(table, "id").split(","):
                        query = query.order(key)
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
    print(f"[{mode}] corpus {report['corpus_version']}  {report['corpus_counts']}")
    for stage, counts in report["stages"].items():
        print(f"  {stage:20s} {counts}")
    print(f"  Would update: {report['would_update']}   Would delete: {report['would_delete']}")
    for key in ("interaction_kinds", "pairwise_conclusion_eligible", "pairwise_explanation_or_review_only",
                "pairwise_review_status", "pairwise_severity", "evidence_use", "conflicts", "drug_taiwan_code_mapping"):
        print(f"  {key}: {report[key]}")
    print(f"  Neo4j preview: {json.dumps(report['neo4j_preview'], ensure_ascii=False)}")
    for note in report["notes"]:
        print(f"  note: {note}")
    for err in report["errors"]:
        print(f"  ERROR: {err}")


def main() -> int:
    parser = argparse.ArgumentParser(description="呷安心 Interaction Corpus importer")
    parser.add_argument("--corpus-dir", type=Path, default=CORPUS_DIR)
    parser.add_argument("--overlay-dir", type=Path, default=OVERLAY_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="預設；不寫入任何資料庫")
    mode.add_argument("--apply", action="store_true", help="寫入 Supabase（與 Neo4j，除非 --skip-graph）")
    parser.add_argument("--confirm-project", default=None, help=f"--apply 時必須為 {PROJECT_ID}")
    parser.add_argument("--compare-supabase", action="store_true", help="dry-run 時唯讀比對 Bridge 前置資料與 0006 表既有列")
    parser.add_argument("--preflight-neo4j", action="store_true",
                        help="dry-run 時唯讀檢查 Neo4j 既有 Bridge 節點（participant 是否存在、label 是否相符）")
    parser.add_argument("--skip-graph", action="store_true")
    parser.add_argument("--json-report", type=Path, default=None)
    args = parser.parse_args()

    if args.apply and args.confirm_project != PROJECT_ID:
        print(f"--apply 需要 --confirm-project {PROJECT_ID}")
        return 2

    data = load_interaction_corpus_data(args.corpus_dir)
    store = service = settings = None
    bridge_entities = bridge_sources = None
    if args.apply or args.compare_supabase:
        from app.config.settings import get_settings
        from app.services.supabase_service import SupabaseService

        settings = get_settings()
        service = SupabaseService(settings)
        service.ensure_schema()  # 不是呷安心 DB 時直接拋錯
        store = ordered_store(service, read_only=not args.apply)
        marker = service.select(service.table("app_schema_meta").select("value").eq("key", MIGRATION_0006_MARKER).limit(1))
        if args.apply and not marker:
            print(f"--apply 需要先套用 migrations/0006_interaction_corpus.sql（找不到 {MIGRATION_0006_MARKER}）")
            return 2
        bridge_entities, bridge_sources = bridge_prerequisites(store, data)
    else:
        # 離線：以 Ingredient Bridge 的 corpus loader（同一套 entity ID / label、同一份 source 檔）驗證相容性
        bridge = load_interaction_corpus(args.corpus_dir, overlay=load_reviewed_overlay(args.overlay_dir))
        bridge_entities = {eid: e.graph_label for eid, e in bridge.entities.items()}
        bridge_sources = set(data.sources)

    plan = plan_interaction_import(data, store, bridge_entities=bridge_entities, bridge_source_ids=bridge_sources)
    if store is not None and getattr(store, "missing_tables", None):
        plan.notes.append(f"0006 / Bridge 表尚未存在（視為空表）：{sorted(store.missing_tables)}")
    if not args.apply:
        report = plan.report()
        if args.preflight_neo4j:
            from app.services.neo4j_service import Neo4jService

            neo = Neo4jService(settings or __import__("app.config.settings", fromlist=["get_settings"]).get_settings())
            try:
                report["neo4j_preflight"] = neo4j_bridge_preflight(neo, required_ingredient_participants(data))
            finally:
                neo.close()
            report["errors"] = report["errors"] + report["neo4j_preflight"]["errors"]
            print(f"  Neo4j preflight: {json.dumps(report['neo4j_preflight'], ensure_ascii=False)}")
        _print_report(report, "dry-run")
        print("[dry-run] 未寫入 Supabase / Neo4j")
    else:
        neo4j = None
        if not args.skip_graph:
            from app.services.neo4j_service import Neo4jService

            neo4j = Neo4jService(settings)
            neo4j.ensure_schema()
        try:
            report = apply_interaction_import(plan, store, neo4j=neo4j)
        finally:
            if neo4j is not None:
                neo4j.close()
        _print_report(report, "apply")
    if args.json_report:
        args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
