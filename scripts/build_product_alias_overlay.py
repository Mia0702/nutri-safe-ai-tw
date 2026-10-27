"""Product / Brand alias 審核資料集建構與驗證（offline；只讀本機快照，不連線、不寫資料庫、不改 runtime）。

建構（產生 candidates.csv / reviewed_aliases.csv / brand_candidates.csv / overlay_manifest.json / README.md）：
    python -m scripts.build_product_alias_overlay build ^
        --snapshot-dir data/tfda_product_coverage/2026-10-04 --out-dir data/product_alias_overlay/2026-10-27 ^
        --overlay-version 1.0 --created-at 2026-10-27T00:00:00+00:00

驗證（只回報，不修改任何檔案；有 ERROR 時 exit 1）：
    python -m scripts.build_product_alias_overlay validate --dir data/product_alias_overlay/2026-10-27
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.ingestion.product_alias_review import (
    BRAND_EXTRA_COLUMNS,
    BUILDER_VERSION,
    PRODUCT_EXTRA_COLUMNS,
    REVIEW_BATCH_SIZE,
    VALIDATOR_VERSION,
    ValidationReport,
    build_brand_candidates,
    build_manifest,
    build_product_candidates,
    classification_counts,
    count_decisions,
    load_snapshot,
    official_name_index,
    raw_first_token_count,
    read_review_csv,
    validate_manifest,
    validate_rows,
    write_manifest,
    write_review_csv,
)

CANDIDATES_FILE = "candidates.csv"
REVIEWED_FILE = "reviewed_aliases.csv"
BRANDS_FILE = "brand_candidates.csv"
MANIFEST_FILE = "overlay_manifest.json"
README_FILE = "README.md"
DATA_ROOT = Path("data/product_alias_overlay")


def render_readme(manifest: dict) -> str:
    c = manifest["classification_counts"]
    d = manifest["candidate_counts"]["review_decisions"]
    return f"""# Product / Brand Alias 審核資料集 v{manifest['overlay_version']}

> 由 `python -m scripts.build_product_alias_overlay build` 產生（builder {BUILDER_VERSION}）。**離線審核資料，尚未接入 runtime，沒有任何 runtime-active 別名。**
> 範圍：Taiwan Phase 1 only；證據只來自台灣 TFDA 開放資料中「同一筆官方紀錄」的中文品名 ↔ 英文品名，不翻譯。

## 檔案

| 檔案 | 內容 |
|---|---|
| `candidates.csv` | 官方英文品名候選（每個產品一列）：UNIQUE / COLLIDING / INVALID_OR_GENERIC |
| `brand_candidates.csv` | 英文品牌詞 ↔ 中文品名前綴候選（全部 CANDIDATE_ONLY） |
| `reviewed_aliases.csv` | 人工審核後的結果；目前只有表頭。**只有這個檔案的 ACCEPT 列才可能在日後 D3 被讀取** |
| `overlay_manifest.json` | 版本、來源雜湊、檔案雜湊、計數、validator 版本 |

## 計數（產生當下）

- 官方紀錄 {c['official_records']}；沒有英文品名 {c['records_without_english_name']}
- 可用官方英文品名 {c['official_english_names_total']}；不同正規化名稱 {c['unique_normalized_english_names']}；涵蓋產品 {c['unique_products_covered']}
- UNIQUE_OFFICIAL_ENGLISH：{c['UNIQUE_OFFICIAL_ENGLISH']['rows']} 列（`EXACT_REVIEW_REQUIRED`；其中去標點後仍與他品相同 {c['unique_rows_with_runtime_key_collision']} 列，需特別看 `conflict_check`）
- COLLIDING_OFFICIAL_ENGLISH：{c['COLLIDING_OFFICIAL_ENGLISH']['normalized_names']} 組 / {c['COLLIDING_OFFICIAL_ENGLISH']['products']} 產品（`CANDIDATE_ONLY`，永遠不得自動確認）
- INVALID_OR_GENERIC：{c['INVALID_OR_GENERIC']['rows']} 列（`NEVER_RUNTIME`）
- 品牌候選 {c['brand_candidates_total']}，通過規則 {c['brand_candidates_passing_rule']}（其中有中文前綴 {c['brand_passing_with_zh_prefix']}）
- 審核決定：ACCEPT {d['ACCEPT']} / REJECT {d['REJECT']} / NEEDS_REVIEW {d['NEEDS_REVIEW']}

## 審核方式

1. 依 `review_batch`（每批約 {REVIEW_BATCH_SIZE} 列；品牌每批 50 列）逐批審核，先審 `UNIQUE-*`。
2. 在 `decision` 填 `ACCEPT` / `REJECT` / `NEEDS_REVIEW`，並填 `decision_reason`、`reviewer`、`reviewed_at`。**只有這三個值；不可自訂狀態。**
3. 對照 `evidence_quote`（官方原文）與 `source_url` / `source_record_id`；不可用購物網站、品牌官網、社群貼文、AI 翻譯當證據。
4. 把審核完的列複製到 `reviewed_aliases.csv`（保留原 `review_id`），並升版（新目錄或新 `overlay_version`）；不要原地改動候選檔。
5. 執行 validator；有 ERROR 不得採用。

## 不可違反

- UNIQUE 不等於 runtime-active；ACCEPT 也只代表「日後可進入 reviewed exact retrieval」，runtime 證據等級日後由證據欄位推導，不手填。
- COLLIDING 與品牌永遠是候選；品牌不產生所有權（OWNS_BRAND）或官方品牌關係。
- 來源 sha256 改變必須升版（validator 會檢查）；validator 不會自動修改任何列。
"""


def cmd_build(args: argparse.Namespace) -> int:
    out = Path(args.out_dir)
    datasets, records = load_snapshot(Path(args.snapshot_dir))
    product_rows = build_product_candidates(records, args.overlay_version)
    brand_rows = build_brand_candidates(records, args.overlay_version)
    counts = classification_counts(product_rows, records, brand_rows)
    counts["raw_english_first_tokens"] = raw_first_token_count(records)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict] = {}
    for name, rows, extra in ((CANDIDATES_FILE, product_rows, PRODUCT_EXTRA_COLUMNS),
                              (BRANDS_FILE, brand_rows, BRAND_EXTRA_COLUMNS), (REVIEWED_FILE, [], PRODUCT_EXTRA_COLUMNS)):
        path = out / name
        if path.exists() and not args.overwrite:
            raise SystemExit(f"{path} 已存在；請換目錄或明確指定 --overwrite")
        files[name] = {"sha256": write_review_csv(path, rows, extra), "rows": len(rows)}
    decisions = count_decisions(product_rows + brand_rows)
    manifest = build_manifest(overlay_version=args.overlay_version, created_at=args.created_at, datasets=datasets,
                              files=files, counts=counts, decisions=decisions)
    (out / README_FILE).write_text(render_readme(manifest), encoding="utf-8")
    write_manifest(out / MANIFEST_FILE, manifest)
    print(json.dumps({"out_dir": str(out), "counts": counts, "decisions": decisions}, ensure_ascii=False, indent=2))
    return cmd_validate(argparse.Namespace(dir=str(out)))


def validate_directory(directory: Path) -> tuple[ValidationReport, ValidationReport]:
    directory = Path(directory)
    rows: list[dict[str, str]] = []
    for name in (CANDIDATES_FILE, BRANDS_FILE, REVIEWED_FILE):
        rows += read_review_csv(directory / name)
    manifest_path = directory / MANIFEST_FILE
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        bad = ValidationReport()
        from app.ingestion.product_alias_review import Issue
        bad.issues.append(Issue("ERROR", "MALFORMED_MANIFEST", "manifest", f"無法讀取 manifest：{exc}"))
        return validate_rows(rows, official_names=official_name_index(rows)), bad
    others = []
    for sibling in sorted(directory.parent.glob(f"*/{MANIFEST_FILE}")):
        if sibling.resolve() != manifest_path.resolve():
            try:
                others.append(json.loads(sibling.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
    return (validate_rows(rows, official_names=official_name_index(rows)),
            validate_manifest(manifest, directory, others))


def cmd_validate(args: argparse.Namespace) -> int:
    rows_rep, manifest_rep = validate_directory(Path(args.dir))
    summary = {"validator_version": VALIDATOR_VERSION, "rows_checked": rows_rep.rows_checked,
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
    b.add_argument("--out-dir", required=True)
    b.add_argument("--overlay-version", default="1.0")
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
