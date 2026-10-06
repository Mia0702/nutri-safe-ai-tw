"""Reviewed Taiwan Drug Chinese Alias Overlay 建構 / 稽核（offline；只讀本機檔案，不連線、不寫資料庫）。

reviewer 候選（review_candidates.csv）＋ TFDA 官方快照（9123 / 9121 / 9122）→ 稽核過的 overlay CSV。
VERIFIED_OFFICIAL 只在官方「中文品名 / 英文品名」成對證據足量且無歧義時產生；不使用 AI 翻譯 / 相似度 / substring。

用法（專案根目錄；CSV 為 data.fda.gov.tw 開放資料解壓後的檔案）：
    python -m scripts.build_drug_zh_alias_overlay --permits 9123.csv --detail 9121.csv --all-permits 9122.csv ^
        --candidates data/taiwan_drug_alias_overlay/2026-10-06/review_candidates.csv ^
        --sources data/taiwan_drug_alias_overlay/2026-10-06/official_sources.csv --harvest-catalog ^
        --out-dir data/taiwan_drug_alias_overlay/2026-10-06 --retrieved-at 2026-10-06T12:09:00+00:00
"""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

from app.ingestion.drug_zh_alias_evidence import (
    OVERLAY_COLUMNS,
    STATUS_SUPPORTED,
    STATUS_VERIFIED,
    build_overlay_rows,
    collect_official_pairs,
    harvest_catalog_candidates,
)
from app.ingestion.taiwan_drug_catalog import DATASETS, build_catalog, parse_csv

DRUGS_CSV = "data/ingredient_interaction/2026-10-04/drug_substances.csv"
OVERLAY_FILE = "reviewed_drug_zh_aliases.csv"
MANIFEST_FILE = "overlay_manifest.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--permits", required=True, help="9123 未註銷藥品許可證 CSV")
    ap.add_argument("--detail", required=True, help="9121 藥品詳細處方成分 CSV（只用來重建名錄 identity）")
    ap.add_argument("--all-permits", required=True, help="9122 全部藥品許可證 CSV（只用來排除已註銷）")
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--sources", default=None, help="台灣官方直接說明 identity 的來源（official_sources.csv；選用）")
    ap.add_argument("--harvest-catalog", action="store_true", help="另外從名錄收集官方成對證據充足的中文名稱候選（仍須通過證據規則）")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--retrieved-at", required=True, help="官方快照下載時間（ISO 8601）")
    args = ap.parse_args(argv)

    raw = {k: Path(getattr(args, a)).read_bytes() for k, a in (("permits_current", "permits"), ("detail", "detail"), ("permits_all", "all_permits"))}
    current, detail, allp = (parse_csv(raw[k]) for k in ("permits_current", "detail", "permits_all"))
    cancelled = {r["許可證字號"] for r in allp if (r.get("註銷狀態") or "").strip()}
    drug_rows = list(csv.DictReader(open(DRUGS_CSV, encoding="utf-8-sig")))
    catalog = build_catalog(current, detail, allp, reviewed_drugs=drug_rows)
    pairs = collect_official_pairs(current, cancelled)
    candidates = list(csv.DictReader(open(args.candidates, encoding="utf-8")))
    sources = list(csv.DictReader(open(args.sources, encoding="utf-8"))) if args.sources else []
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if args.harvest_catalog:
        harvested = harvest_catalog_candidates(pairs, catalog.ingredients, exclude_aliases=[c["alias_text_zh_tw"] for c in candidates])
        with open(out / "harvested_candidates.csv", "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(harvested[0].keys())) if harvested else None
            if writer:
                writer.writeheader()
                writer.writerows(harvested)
        candidates += harvested
    rows = build_overlay_rows(
        candidates, pairs, {d["drug_substance_id"]: d["canonical_name_en"] for d in drug_rows},
        {r["ingredient_code"]: r["official_name"] for r in catalog.ingredients if r["ingredient_code"]}, args.retrieved_at, sources)

    with open(out / OVERLAY_FILE, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OVERLAY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["review_status"]] = counts.get(r["review_status"], 0) + 1
    manifest = {
        "overlay_version": "1.1", "retrieved_at": args.retrieved_at, "candidate_rows": len(candidates), "status_counts": counts,
        "snapshots": {k: {"gov_dataset_id": DATASETS[k]["gov_dataset_id"], "title": DATASETS[k]["title"], "sha256": hashlib.sha256(v).hexdigest(),
                          "rows": len({"permits_current": current, "detail": detail, "permits_all": allp}[k])} for k, v in raw.items()},
        "evidence_rule": "9123 同一張未註銷藥證內的中文品名 / 英文品名成對；至少 2 張不同藥證；同一中文名稱不得對應其他英文名稱；英文名稱需與 identity 完全相同",
    }
    (out / MANIFEST_FILE).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status_counts": counts, "verified": sum(r["review_status"] == STATUS_VERIFIED for r in rows),
                      "supported": sum(r["review_status"] == STATUS_SUPPORTED for r in rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
