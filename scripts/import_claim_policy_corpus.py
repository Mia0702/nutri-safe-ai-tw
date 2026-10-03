"""TFDA Claim Policy Corpus importer（Supabase / RAG / Neo4j）。

預設 dry-run：只讀 corpus 檔案、驗證、計算 insert / update / unchanged / review / quarantine，不連線、不寫入。

用法（在專案根目錄）：
    python -m scripts.import_claim_policy_corpus --dry-run
    python -m scripts.import_claim_policy_corpus --dry-run --compare-supabase --read-approved-claims   # 唯讀比對
    python -m scripts.import_claim_policy_corpus --apply --confirm-project nutri_safe_ai               # 寫入（需使用者同意）

前置：Supabase 依序執行 migrations/0001、0002、0003_claim_policy_corpus.sql。
不下載違規廣告案例；illegal_ad_cases 不寫入。
"""

import argparse
import json
import sys
from pathlib import Path

from app.config.project import PROJECT_ID
from app.ingestion.claim_policy_corpus import (
    SupabaseCorpusStore,
    apply_import,
    load_corpus,
    plan_import,
)

DEFAULT_CORPUS_DIR = Path("data/tfda_claim_policy")
MIGRATION_MARKER = "migration_0003_claim_policy_corpus"


def _print_report(report: dict, mode: str) -> None:
    print(f"[{mode}] corpus_version {report['corpus_version']}  batch_key {report['batch_key'][:16]}…")
    for table, c in report["tables"].items():
        print(f"  {table:36s} insert {c['insert']:4d}  update {c['update']:4d}  unchanged {c['unchanged']:4d}"
              f"  not_in_corpus {c['not_in_corpus']}")
    rag = report["rag"]
    print(f"  rag: sections {rag['source_sections']} → documents {rag['documents']}（current {rag['current_documents']} / "
          f"superseded {rag['superseded_documents']}）→ chunks {rag['chunks']}；embed {rag['chunks_to_embed']}；"
          f"insert {rag['insert']} update {rag['update']} unchanged {rag['unchanged']}")
    print(f"  quarantine: {json.dumps(report['quarantine'], ensure_ascii=False)}")
    print(f"  review: {json.dumps(report['review'], ensure_ascii=False)}")
    print(f"  graph: {json.dumps(report['graph'], ensure_ascii=False)}")
    for note in report["notes"]:
        print(f"  note: {note}")
    for err in report["errors"]:
        print(f"  ERROR: {err}")


def main() -> int:
    parser = argparse.ArgumentParser(description="呷安心 TFDA Claim Policy Corpus importer")
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="預設；不寫入任何資料庫")
    mode.add_argument("--apply", action="store_true", help="寫入 Supabase / RAG / Neo4j")
    parser.add_argument("--confirm-project", default=None, help=f"--apply 時必須為 {PROJECT_ID}")
    parser.add_argument("--compare-supabase", action="store_true", help="dry-run 時唯讀比對 Supabase 既有資料")
    parser.add_argument("--read-approved-claims", action="store_true",
                        help="dry-run 時唯讀讀取 approved_claims，預覽 Claim→HealthFunction deterministic mapping")
    parser.add_argument("--skip-rag", action="store_true")
    parser.add_argument("--skip-graph", action="store_true")
    parser.add_argument("--allow-older-corpus", action="store_true", help="允許匯入比資料庫已匯入版本舊的 corpus")
    parser.add_argument("--json-report", type=Path, default=None)
    args = parser.parse_args()

    corpus = load_corpus(args.corpus_dir)
    needs_supabase = args.apply or args.compare_supabase or args.read_approved_claims
    service = store = None
    approved = None
    if needs_supabase:
        from app.config.settings import get_settings
        from app.services.supabase_service import SupabaseService

        settings = get_settings()
        service = SupabaseService(settings)
        service.ensure_schema()
        store = SupabaseCorpusStore(service, read_only=not args.apply)
        if args.read_approved_claims or args.apply:
            approved = store.fetch("approved_claims", ["id", "function_name", "claim_kind"])

    if args.apply:
        if args.confirm_project != PROJECT_ID:
            print(f"--apply 需要 --confirm-project {PROJECT_ID}")
            return 2
        marker = service.select(service.table("app_schema_meta").select("value").eq("key", MIGRATION_MARKER).limit(1))
        if not marker:
            print(f"Supabase 尚未套用 0003（找不到 app_schema_meta.{MIGRATION_MARKER}），拒絕寫入")
            return 2

    plan = plan_import(corpus, store if args.compare_supabase or args.apply else None,
                       approved_claims=approved, allow_older_corpus=args.allow_older_corpus)
    if store is not None and store.missing_tables:
        plan.notes.append(f"Supabase 尚無資料表（0003 未套用），視為空：{sorted(store.missing_tables)}")

    if not args.apply:
        report = plan.report()
        _print_report(report, "dry-run")
        print("[dry-run] 未寫入 Supabase / Neo4j / RAG")
    else:
        from app.services.neo4j_service import Neo4jService
        from app.services.openai_service import OpenAIService

        neo4j = None if args.skip_graph else Neo4jService(settings)
        try:
            report = apply_import(
                plan, store, embedder=None if args.skip_rag else OpenAIService(settings),
                embedding_model=settings.openai_embedding_model, skip_rag=args.skip_rag, neo4j=neo4j,
                read_approved_claims=lambda: store.fetch("approved_claims", ["id", "function_name", "claim_kind"]),
            )
        finally:
            if neo4j is not None:
                neo4j.close()
        _print_report(report, "apply")
    if args.json_report:
        args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
