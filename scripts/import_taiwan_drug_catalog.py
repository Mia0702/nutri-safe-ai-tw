"""Taiwan Official Drug Ingredient Catalog importer（0007 表；Supabase-only，不寫 Neo4j）。

預設 dry-run：只讀本機下載的三份 TFDA CSV，印出稽核與計畫寫入的列數，不連線、不寫入。

用法（專案根目錄；CSV 為 data.fda.gov.tw 開放資料解壓後的檔案）：
    python -m scripts.import_taiwan_drug_catalog --permits 9123.csv --detail 9121.csv --all-permits 9122.csv --dry-run
    python -m scripts.import_taiwan_drug_catalog ... --apply --confirm-project nutri_safe_ai   # 需先套用 0007、需使用者同意

只寫 taiwan_drug_ingredients / taiwan_drug_ingredient_permits；不寫 0001–0006 的任何表；不刪除既有列；
名錄成員資格不是 interaction 證據：不建立任何 interaction / evidence。
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config.project import PROJECT_ID
from app.ingestion.taiwan_drug_catalog import DATASETS, build_catalog, parse_csv

REVIEWED_DRUG_FIELDS = "drug_substance_id,canonical_name_en"
BATCH = 500


def _read(path: str) -> tuple[bytes, str]:
    raw = Path(path).read_bytes()
    return raw, hashlib.sha256(raw).hexdigest()


def _pages(service, table: str, columns: str, order: list[str]):
    start = 0
    while True:
        query = service.table(table).select(columns)
        for key in order:  # 完整唯一排序，range 分頁才不會漏列 / 重複
            query = query.order(key)
        page = service.select(query.range(start, start + 999))
        yield from page
        if len(page) < 1000:
            return
        start += 1000


def _existing(service) -> tuple[dict[str, str], set[tuple[str, str]]]:
    """既有名錄（唯讀）：ingredient_code → id；以及 (ingredient_id, permit_number)。"""
    codes = {r["ingredient_code"]: r["id"] for r in _pages(service, "taiwan_drug_ingredients", "id,ingredient_code", ["id"]) if r["ingredient_code"]}
    links = {(r["ingredient_id"], r["permit_number"]) for r in _pages(service, "taiwan_drug_ingredient_permits", "ingredient_id,permit_number", ["ingredient_id", "permit_number"])}
    return codes, links


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--permits", required=True, help="9123 未註銷藥品許可證 CSV")
    ap.add_argument("--detail", required=True, help="9121 藥品詳細處方成分 CSV")
    ap.add_argument("--all-permits", required=True, help="9122 全部藥品許可證 CSV（只做交叉檢查）")
    ap.add_argument("--top-n", type=int, default=1000)
    ap.add_argument("--dataset-modified", nargs=3, metavar=("9123", "9121", "9122"), default=None,
                    help="data.gov.tw metadata 的資料集更新時間（ISO 格式），依 9123 9121 9122 順序")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--read-drugs", action="store_true", help="dry-run 時唯讀取得 drug_substances 以評估 exact 對應（不寫入）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--confirm-project", default=None)
    args = ap.parse_args(argv)
    if args.apply and args.confirm_project != PROJECT_ID:
        print(f"拒絕寫入：--apply 需要 --confirm-project {PROJECT_ID}", file=sys.stderr)
        return 2

    (cur_raw, cur_sha), (det_raw, det_sha), (all_raw, all_sha) = _read(args.permits), _read(args.detail), _read(args.all_permits)
    service = None
    reviewed: list[dict] = []
    if args.apply or args.read_drugs:  # 預設 dry-run 不連線；已審核 DrugSubstance 只在 apply / --read-drugs 時唯讀取得，用來做 exact 對應
        from app.config.settings import get_settings
        from app.services.supabase_service import SupabaseService

        service = SupabaseService(get_settings())
        reviewed = service.select(service.table("drug_substances").select(REVIEWED_DRUG_FIELDS))
    build = build_catalog(parse_csv(cur_raw), parse_csv(det_raw), parse_csv(all_raw), top_n=args.top_n, reviewed_drugs=reviewed)
    print(json.dumps(build.audit, ensure_ascii=False, indent=2))
    print(f"planned rows: taiwan_drug_ingredients={len(build.ingredients)} taiwan_drug_ingredient_permits={len(build.permit_rows)}")
    if args.read_drugs:
        mapped = {m["drug_substance_id"] for m in build.mappings}
        print("reviewed exact mappings:", json.dumps(build.mappings, ensure_ascii=False))
        print("ambiguous mappings:", build.ambiguous_mappings)
        print("unmapped existing DrugSubstances:", sorted(d["canonical_name_en"] for d in reviewed if d["drug_substance_id"] not in mapped))
        print("salt/form variant candidates (NOT mapped):", json.dumps(build.variant_candidates, ensure_ascii=False))
    codes = [r["ingredient_code"] for r in build.ingredients]
    print("duplicate catalog codes:", len(codes) - len(set(codes)), "| duplicate permit links:", len(build.permit_rows) - len(set(build.permit_rows)))
    existing_codes: dict[str, str] = {}
    existing_links: set[tuple[str, str]] = set()
    if service is not None:  # 唯讀比對既有名錄：只新增缺少的列；不更新、不刪除
        existing_codes, existing_links = _existing(service)
        planned_codes = {r["ingredient_code"] for r in build.ingredients}
        code_of_key = {f"code:{r['ingredient_code']}": r["ingredient_code"] for r in build.ingredients}
        new_links = [(code_of_key[k], p) for k, p in build.permit_rows if k in code_of_key
                     and (existing_codes.get(code_of_key[k]) is None or (existing_codes[code_of_key[k]], p) not in existing_links)]
        print(f"compare: existing catalog rows={len(existing_codes)} existing links={len(existing_links)}")
        print(f"Would INSERT catalog rows={len(planned_codes - set(existing_codes))} Would INSERT permit links={len(new_links)} "
              f"Would UPDATE=0 Would DELETE=0 | existing rows not in plan={len(set(existing_codes) - planned_codes)}")
    if not args.apply:
        print("[dry-run] 未寫入" + ("（僅唯讀取得 drug_substances）。" if args.read_drugs else "，未連線。"))
        return 0

    if build.audit["without_code"]:  # 沒有成分代碼者無法以代碼 upsert；Phase 1 實際資料為 0，出現時先人工處理，不猜
        print(f"拒絕寫入：{build.audit['without_code']} 個名錄成分沒有成分代碼", file=sys.stderr)
        return 3
    retrieved_at = datetime.now(timezone.utc).isoformat()
    modified = dict(zip(("permits_current", "detail", "permits_all"), args.dataset_modified or (None, None, None)))
    snapshot = {"permits_current": cur_sha, "detail": det_sha, "permits_all": all_sha}
    rows = []
    for r in build.ingredients:
        rows.append({**r, "source_dataset": "9123+9121+9122", "source_url": DATASETS["permits_current"]["export_url"],
                     "dataset_modified_at": modified["permits_current"], "retrieved_at": retrieved_at,
                     "snapshot_sha256": cur_sha, "raw_data": {"snapshot_sha256": snapshot, "dataset_modified": modified,
                                                              "count_definition": build.audit["count_definition"],
                                                              "active_ingredient_rule": build.audit["active_ingredient_rule"]}})
    # insert-only：已存在的列不更新、不刪除（partial unique index 也無法作為 upsert 的 on_conflict）
    todo = [r for r in rows if r["ingredient_code"] not in existing_codes]
    for i in range(0, len(todo), BATCH):
        service.insert("taiwan_drug_ingredients", todo[i:i + BATCH])
    stored, _ = _existing(service)
    code_of = {f"code:{r['ingredient_code']}": r["ingredient_code"] for r in build.ingredients if r["ingredient_code"]}
    links = [{"ingredient_id": stored[code_of[k]], "permit_number": p} for k, p in build.permit_rows
             if k in code_of and code_of[k] in stored and (stored[code_of[k]], p) not in existing_links]
    for i in range(0, len(links), BATCH):
        service.insert("taiwan_drug_ingredient_permits", links[i:i + BATCH])
    print(f"[apply] inserted ingredients={len(todo)} inserted permit_links={len(links)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
