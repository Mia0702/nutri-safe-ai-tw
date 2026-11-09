"""D4：品牌候選人工審核結果的建構與驗證（offline；只讀本機快照與 D2 候選檔，不連線、不寫資料庫、不改 runtime）。

建構（讀 D2 brand_candidates.csv + 決定檔，產生 reviewed_brand_aliases.csv 與 overlay_manifest.json）：
    python -m scripts.review_brand_candidates build ^
        --snapshot-dir data/tfda_product_coverage/2026-10-04 ^
        --candidates data/product_alias_overlay/2026-10-27/brand_candidates.csv ^
        --out-dir data/product_alias_overlay/2026-11-08 --overlay-version 1.1 ^
        --reviewer CLAUDE_D4_REVIEW --reviewed-at 2026-11-08 --created-at 2026-11-08T00:00:00+00:00

驗證（只回報，不修改任何檔案；有 ERROR 時 exit 1）：
    python -m scripts.review_brand_candidates validate --dir data/product_alias_overlay/2026-11-08
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.ingestion.brand_alias_review import (
    BRAND_REVIEW_EXTRA_COLUMNS,
    BRAND_REVIEW_VERSION,
    DECISIONS_FILE,
    REVIEWED_BRAND_FILE,
    build_reviewed_brand_rows,
    read_decisions,
    summarize_reviewed_rows,
    validate_reviewed_brand_rows,
)
from app.ingestion.product_alias_review import (
    Issue,
    ValidationReport,
    load_snapshot,
    read_review_csv,
    sha256_file,
    validate_manifest,
    write_manifest,
    write_review_csv,
)

MANIFEST_FILE = "overlay_manifest.json"


def build_review_manifest(*, overlay_version: str, created_at: str, datasets: list, files: dict[str, dict[str, Any]],
                          inputs: dict[str, dict[str, str]], summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "overlay_version": overlay_version, "created_at": created_at, "builder_version": BRAND_REVIEW_VERSION,
        "validator_version": BRAND_REVIEW_VERSION, "scope": "Taiwan Phase 1 only（TW）；品牌候選審核，離線資料",
        "source_datasets": [{
            "source_id": d.source_id, "dataset_title": d.dataset_title, "source_dataset_id": d.source_dataset_id,
            "source_info_id": d.source_info_id, "source_url": d.source_url, "retrieved_at": d.retrieved_at,
            "raw_file": d.raw_file, "raw_sha256": d.raw_sha256, "normalized_file": d.normalized_file,
            "normalized_sha256": d.normalized_sha256, "rows": d.rows} for d in datasets],
        "files": files, "inputs": inputs,
        "candidate_counts": {"brand_rows": summary["total_reviewed"], "review_decisions": summary["decisions"]},
        "classification_counts": {"zh_evidence_classes": summary["zh_evidence_classes"]},
        "policies": {
            "BRAND": "CANDIDATE_ONLY（ACCEPT 也是）", "ACCEPT_MEANS": "已審核的品牌別名關係；不是品牌所有權、不是產品身份",
            "runtime_active_alias_file": "none（沒有 runtime loader、沒有資料庫匯入）",
            "accept_gate": "CLEAN_BRAND_PAIR + conflict_check=NONE + 單一申請商 + support>=5 + descriptor_flag=NONE"},
        "review_summary": summary,
    }


def cmd_build(args: argparse.Namespace) -> int:
    out = Path(args.out_dir)
    candidates_path = Path(args.candidates)
    decisions_path = out / DECISIONS_FILE
    datasets, records = load_snapshot(Path(args.snapshot_dir))
    candidate_rows = read_review_csv(candidates_path)
    rows = build_reviewed_brand_rows(candidate_rows, records, read_decisions(decisions_path),
                                     overlay_version=args.overlay_version, reviewer=args.reviewer,
                                     reviewed_at=args.reviewed_at)
    reviewed_path = out / REVIEWED_BRAND_FILE
    if reviewed_path.exists() and not args.overwrite:
        raise SystemExit(f"{reviewed_path} 已存在；請換目錄或明確指定 --overwrite")
    files = {REVIEWED_BRAND_FILE: {"sha256": write_review_csv(reviewed_path, rows, BRAND_REVIEW_EXTRA_COLUMNS),
                                   "rows": len(rows)},
             DECISIONS_FILE: {"sha256": sha256_file(decisions_path), "rows": len(read_decisions(decisions_path))}}
    summary = summarize_reviewed_rows(rows)
    inputs = {"brand_candidates.csv": {"path": candidates_path.as_posix(), "sha256": sha256_file(candidates_path)}}
    manifest = build_review_manifest(overlay_version=args.overlay_version, created_at=args.created_at,
                                     datasets=datasets, files=files, inputs=inputs, summary=summary)
    write_manifest(out / MANIFEST_FILE, manifest)
    print(json.dumps({"out_dir": str(out), "summary": summary}, ensure_ascii=False, indent=2))
    return cmd_validate(argparse.Namespace(dir=str(out)))


def validate_directory(directory: Path) -> tuple[ValidationReport, ValidationReport]:
    directory = Path(directory)
    manifest_rep = ValidationReport()
    try:
        manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        manifest_rep.issues.append(Issue("ERROR", "MALFORMED_MANIFEST", "manifest", f"無法讀取 manifest：{exc}"))
        return ValidationReport(), manifest_rep
    manifest_rep = validate_manifest(manifest, directory)
    candidates_rows: list[dict[str, str]] = []
    for name, meta in (manifest.get("inputs") or {}).items():
        path = Path(meta.get("path", ""))
        if not path.exists() or sha256_file(path) != meta.get("sha256"):
            manifest_rep.issues.append(Issue("ERROR", "INPUT_HASH_MISMATCH", "manifest", f"輸入 {name} 不存在或 sha256 不一致"))
        elif name == "brand_candidates.csv":
            candidates_rows = read_review_csv(path)
    rows = read_review_csv(directory / REVIEWED_BRAND_FILE)
    return validate_reviewed_brand_rows(rows, candidates_rows), manifest_rep


def cmd_validate(args: argparse.Namespace) -> int:
    rows_rep, manifest_rep = validate_directory(Path(args.dir))
    summary = {"validator_version": BRAND_REVIEW_VERSION, "rows_checked": rows_rep.rows_checked,
               "row_issue_counts": rows_rep.counts(), "manifest_issue_counts": manifest_rep.counts()}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for issue in (rows_rep.errors + manifest_rep.errors)[:20]:
        print(f"ERROR {issue.code} {issue.row_id}: {issue.message}", file=sys.stderr)
    return 0 if rows_rep.ok and manifest_rep.ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--snapshot-dir", default="data/tfda_product_coverage/2026-10-04")
    b.add_argument("--candidates", default="data/product_alias_overlay/2026-10-27/brand_candidates.csv")
    b.add_argument("--out-dir", required=True)
    b.add_argument("--overlay-version", default="1.1")
    b.add_argument("--reviewer", required=True)
    b.add_argument("--reviewed-at", required=True)
    b.add_argument("--created-at", default=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    b.add_argument("--overwrite", action="store_true")
    b.set_defaults(func=cmd_build)
    v = sub.add_parser("validate")
    v.add_argument("--dir", required=True)
    v.set_defaults(func=cmd_validate)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
