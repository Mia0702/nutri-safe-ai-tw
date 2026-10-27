"""Identity alias 評估集（baseline-002）：英文官方品名 / 同名碰撞 / 品牌-only / 泛用詞 / 中文與字號對照。

用途：D3（runtime 取回已審核別名）之前，先定義「期望的安全結果」並記錄「目前（pre-D3）行為」。
**不修改 runtime、不改 baseline-001、不寫 production。**

案例由已產生的審核資料集（candidates.csv / brand_candidates.csv）與同一份官方快照 deterministic 抽樣：

| group | 查詢 | 期望（D3 之後） | 目前必須遵守 |
|---|---|---|---|
| UNIQUE_EN_EXACT | 唯一官方英文品名 | 可 exact 解析到該產品（需 reviewed） | 不得誤確認 |
| COLLIDING_EN_EXACT | 2+ 產品共用的官方英文品名 | 必須請使用者確認（候選含該群） | 不得自動確認（任何一個都不行） |
| BRAND_EN_ONLY | 只有英文品牌詞 | 不得自動確認產品 | 同左 |
| GENERIC_EN_TOKEN | 泛用英文詞 / 無辨識力的官方英文名 | 不得自動確認 | 同左 |
| ZH_NAME_CONTROL | 中文官方品名 | 行為不得退步（baseline-001） | 同左 |
| PERMIT_CONTROL | 許可證字號 | 行為不得退步（baseline-001） | 同左 |

用法：
    python -m scripts.eval.identity_alias_eval --overlay-dir data/product_alias_overlay/2026-10-27 \\
        --snapshot-dir data/tfda_product_coverage/2026-10-04 --seed 20261019 --tag 2026-10-27
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.english_name import is_single_token
from app.normalization import normalize_name
from app.ingestion.product_alias_review import (
    COLLIDING,
    GENERIC_TOKEN_MIN_APPLICANTS,
    INVALID,
    UNIQUE,
    load_snapshot,
    read_review_csv,
    sha256_file,
)
from scripts.eval.common import DEFAULT_BASELINE_DIR, build_report, deterministic_sample, rate, sha256_of, write_report
from scripts.eval.resolver_eval import (
    AUTO_CONFIRM,
    CLARIFY,
    EXACT_NAME,
    PERMIT_NUMBER,
    CaseResult,
    CatalogEntry,
    CatalogRepo,
    EvalCase,
    generate_cases,
    resolve_case,
)

EVAL_NAME = "identity_alias"
DEFAULT_SEED = 20261019

UNIQUE_EN_EXACT = "UNIQUE_EN_EXACT"
COLLIDING_EN_EXACT = "COLLIDING_EN_EXACT"
BRAND_EN_ONLY = "BRAND_EN_ONLY"
GENERIC_EN_TOKEN = "GENERIC_EN_TOKEN"
ZH_NAME_CONTROL = "ZH_NAME_CONTROL"
PERMIT_CONTROL = "PERMIT_CONTROL"
GROUPS = (UNIQUE_EN_EXACT, COLLIDING_EN_EXACT, BRAND_EN_ONLY, GENERIC_EN_TOKEN, ZH_NAME_CONTROL, PERMIT_CONTROL)

DEFAULT_SIZES = {UNIQUE_EN_EXACT: 100, COLLIDING_EN_EXACT: 100, BRAND_EN_ONLY: 40, GENERIC_EN_TOKEN: 40,
                 ZH_NAME_CONTROL: 100, PERMIT_CONTROL: 100}

# D3 之後期望的結果（documentation；目前 runtime 不需要達成 target，只需要 safety）
DESIRED = {
    UNIQUE_EN_EXACT: ("多詞英文名：AUTO_CONFIRM_CORRECT_PRODUCT；單一詞英文名：候選含正確產品、不得自動確認（D3 核准規則）"),
    COLLIDING_EN_EXACT: "CLARIFY_WITH_GROUP_CANDIDATES（不得自動確認）",
    BRAND_EN_ONLY: "NO_AUTO_CONFIRM",
    GENERIC_EN_TOKEN: "NO_AUTO_CONFIRM",
    ZH_NAME_CONTROL: "NO_REGRESSION（確認正確產品，或同名時候選含正確產品）",
    PERMIT_CONTROL: "NO_REGRESSION（確認正確產品，或同字號多筆時候選含正確產品）",
}

D3_SAFETY_GATES = {
    "baseline-001": {"WRONG_AUTO_CONFIRM_RATE": "必須維持 0", "exact_match_correctness": "必須維持 1.0"},
    "baseline-002": {
        UNIQUE_EN_EXACT: "已審核的 unique 英文 exact 必須解析到正確產品",
        COLLIDING_EN_EXACT: "碰撞英文名稱永遠不得自動確認",
        BRAND_EN_ONLY: "品牌-only 永遠不得自動確認",
        GENERIC_EN_TOKEN: "泛用詞永遠不得自動確認",
        "SAFETY_VIOLATIONS": "全部 group 必須為 0",
    },
    "precondition": "沒有本評估集（含 pre-D3 行為紀錄），不得開始 D3 runtime 工作",
}


# ---------------------------------------------------------------- 案例產生（deterministic）


def catalog_from_snapshot(snapshot_dir: Path) -> list[CatalogEntry]:
    _, records = load_snapshot(snapshot_dir)
    return [CatalogEntry(product_id=r.product_id, name=r.name_zh, permit_number=r.permit_number or None,
                         category=(r.category or "unknown").lower(), company_name=r.applicant or None,
                         name_en=r.name_en or None) for r in records]


def _case(group: str, key: str, query: str, expected: frozenset[str], review_id: str = "") -> EvalCase:
    case = EvalCase(case_id=f"{group}:{key}", product_id=key, variant=group, query=query, expected_ids=expected,
                    is_permit_query=False)
    return case


def build_gold_cases(overlay_dir: Path, catalog: list[CatalogEntry], *, seed: int | str,
                     sizes: dict[str, int] | None = None) -> tuple[list[EvalCase], dict[str, Any]]:
    sizes = {**DEFAULT_SIZES, **(sizes or {})}
    cands = read_review_csv(Path(overlay_dir) / "candidates.csv")
    brands = read_review_csv(Path(overlay_dir) / "brand_candidates.csv")
    cases: list[EvalCase] = []
    pools: dict[str, int] = {}

    # A. 唯一官方英文名稱（排除去標點後仍與他品相同者：它們在 D3 runtime key 下其實是碰撞）
    unique_rows = [r for r in cands if r["classification"] == UNIQUE and r["conflict_check"] == "NONE"]
    pools[UNIQUE_EN_EXACT] = len(unique_rows)
    for r in deterministic_sample(unique_rows, sizes[UNIQUE_EN_EXACT], f"{seed}:A", key=lambda r: r["review_id"]):
        cases.append(_case(UNIQUE_EN_EXACT, r["target_id"], r["alias_text_raw"], frozenset({r["target_id"]})))

    # B. 碰撞：以「正規化名稱」為單位抽樣，期望候選 = 整個群
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in cands:
        if r["classification"] == COLLIDING:
            groups[r["alias_text_normalized"]].append(r)
    pools[COLLIDING_EN_EXACT] = len(groups)
    for norm in deterministic_sample(sorted(groups), sizes[COLLIDING_EN_EXACT], f"{seed}:B", key=lambda k: k):
        members = sorted(groups[norm], key=lambda r: r["target_id"])
        cases.append(_case(COLLIDING_EN_EXACT, members[0]["review_id"], members[0]["alias_text_raw"],
                           frozenset(m["target_id"] for m in members)))

    # C. 品牌-only：通過 D1 規則的英文品牌詞
    passing = [r for r in brands if r["passes_rule"] == "true"]
    pools[BRAND_EN_ONLY] = len(passing)
    for r in deterministic_sample(passing, sizes[BRAND_EN_ONLY], f"{seed}:C", key=lambda r: r["review_id"]):
        cases.append(_case(BRAND_EN_ONLY, r["review_id"], r["alias_text_raw"], frozenset()))

    # D. 泛用詞：被 >= N 個申請商使用的英文詞（資料推導，不是詞表）＋ 無辨識力的官方英文名
    generic = [(r["review_id"], r["alias_text_raw"]) for r in brands
               if int(r["applicant_count"] or 0) >= GENERIC_TOKEN_MIN_APPLICANTS]
    # 無辨識力的官方英文名：若它本身就是該產品「中文官方品名」的一段（例如 "VC 1000" ⊂ "VC1000 膠囊"），
    # 既有的中文品名路徑（FC-B2 省略字規則）本來就會確認它，不屬於「別名」問題 → 不放進泛用詞組（只計數）
    own_zh = [r for r in cands if r["classification"] == INVALID
              and normalize_name(r["alias_text_raw"]) in normalize_name(r["target_official_name"])]
    generic += [(r["review_id"], r["alias_text_raw"]) for r in cands
                if r["classification"] == INVALID and r not in own_zh]
    pools[GENERIC_EN_TOKEN] = len(generic)
    pools["GENERIC_EXCLUDED_OWN_ZH_NAME_SEGMENT"] = len(own_zh)
    for rid, text in deterministic_sample(generic, sizes[GENERIC_EN_TOKEN], f"{seed}:D", key=lambda t: t[0]):
        cases.append(_case(GENERIC_EN_TOKEN, rid, text, frozenset()))

    # E / F. 中文品名與字號對照組（沿用 baseline-001 的案例產生器，只取 exact 類）
    sample_e = deterministic_sample(catalog, sizes[ZH_NAME_CONTROL], f"{seed}:E", key=lambda e: e.product_id)
    for c in generate_cases(catalog, sample_e, seed, (EXACT_NAME,))[0]:
        cases.append(EvalCase(case_id=f"{ZH_NAME_CONTROL}:{c.product_id}", product_id=c.product_id,
                              variant=ZH_NAME_CONTROL, query=c.query, expected_ids=c.expected_ids))
    pools[ZH_NAME_CONTROL] = len(catalog)
    sample_f = deterministic_sample([e for e in catalog if e.permit_number], sizes[PERMIT_CONTROL], f"{seed}:F",
                                    key=lambda e: e.product_id)
    for c in generate_cases(catalog, sample_f, seed, (PERMIT_NUMBER,))[0]:
        cases.append(EvalCase(case_id=f"{PERMIT_CONTROL}:{c.product_id}", product_id=c.product_id,
                              variant=PERMIT_CONTROL, query=c.query, expected_ids=c.expected_ids, is_permit_query=True))
    pools[PERMIT_CONTROL] = sum(1 for e in catalog if e.permit_number)
    cases.sort(key=lambda c: (GROUPS.index(c.variant), c.case_id))
    return cases, {"pool_sizes": pools, "sizes": sizes}


# ---------------------------------------------------------------- 判定


def judge(result: CaseResult) -> dict[str, bool]:
    """target_met：是否達到 D3 之後的期望；safety_ok：是否沒有違反「不得誤確認」。"""
    group, outcome = result.case.variant, result.outcome
    auto = outcome == AUTO_CONFIRM
    if group == UNIQUE_EN_EXACT:
        if is_single_token(result.case.query):  # 核准規則：單一詞的官方英文名不得自動確認，只需候選含正確產品
            offered = outcome == CLARIFY and bool(result.case.expected_ids & set(result.candidate_ids))
            return {"target_met": offered, "safety_ok": not auto}
        return {"target_met": auto and bool(result.correct), "safety_ok": not (auto and not result.correct)}
    if group == COLLIDING_EN_EXACT:
        clarified = outcome == CLARIFY and bool(result.case.expected_ids & set(result.candidate_ids))
        return {"target_met": clarified, "safety_ok": not auto}
    if group in (BRAND_EN_ONLY, GENERIC_EN_TOKEN):
        return {"target_met": not auto, "safety_ok": not auto}
    ok = (auto and bool(result.correct)) or (outcome == CLARIFY and bool(result.case.expected_ids & set(result.candidate_ids)))
    return {"target_met": ok, "safety_ok": not (auto and not result.correct)}


def compute_metrics(results: list[CaseResult]) -> dict[str, Any]:
    by_group: dict[str, Any] = {}
    violations = 0
    for group in GROUPS:
        rs = [r for r in results if r.case.variant == group]
        if not rs:
            continue
        verdicts = [judge(r) for r in rs]
        outcomes = Counter(r.outcome for r in rs)
        v = sum(1 for j in verdicts if not j["safety_ok"])
        violations += v
        by_group[group] = {
            "cases": len(rs), "desired": DESIRED[group],
            "outcomes": {k: outcomes.get(k, 0) for k in ("AUTO_CONFIRM", "CLARIFY", "NOT_FOUND", "CONFLICT", "OTHER")},
            "target_met": sum(1 for j in verdicts if j["target_met"]),
            "target_met_rate": rate(sum(1 for j in verdicts if j["target_met"]), len(rs)),
            "safety_violations": v,
        }
    wrong = sum(1 for r in results if r.outcome == AUTO_CONFIRM and r.correct is False)
    return {"total_cases": len(results), "SAFETY_VIOLATIONS": violations,
            "wrong_auto_confirm_count": wrong, "WRONG_AUTO_CONFIRM_RATE": rate(wrong, len(results)),
            "unique_split": unique_split(results), "by_group": by_group}


def unique_split(results: list[CaseResult]) -> dict[str, Any]:
    """UNIQUE_EN_EXACT 依官方英文名是否單一詞拆開報告（單一詞不得自動確認，只需候選含正確產品）。"""
    uniq = [r for r in results if r.case.variant == UNIQUE_EN_EXACT]
    multi = [r for r in uniq if not is_single_token(r.case.query)]
    single = [r for r in uniq if is_single_token(r.case.query)]
    offered = lambda r: bool(r.case.expected_ids & set(r.candidate_ids))  # noqa: E731
    return {
        "UNIQUE_MULTI_TOKEN": {
            "cases": len(multi),
            "correct_auto_confirm": sum(1 for r in multi if r.outcome == AUTO_CONFIRM and r.correct),
            "clarify": sum(1 for r in multi if r.outcome == CLARIFY),
            "wrong_auto_confirm": sum(1 for r in multi if r.outcome == AUTO_CONFIRM and not r.correct),
            "other": sum(1 for r in multi if r.outcome not in (AUTO_CONFIRM, CLARIFY))},
        "UNIQUE_SINGLE_TOKEN": {
            "cases": len(single),
            "correct_candidate_present": sum(1 for r in single if r.outcome == CLARIFY and offered(r)),
            "incorrect_candidate": sum(1 for r in single if r.outcome == CLARIFY and not offered(r)),
            "auto_confirm": sum(1 for r in single if r.outcome == AUTO_CONFIRM),
            "other": sum(1 for r in single if r.outcome not in (AUTO_CONFIRM, CLARIFY))},
        "other_groups_auto_confirm": {g: sum(1 for r in results if r.case.variant == g and r.outcome == AUTO_CONFIRM)
                                      for g in (COLLIDING_EN_EXACT, BRAND_EN_ONLY, GENERIC_EN_TOKEN)},
    }


def violation_examples(results: list[CaseResult], limit: int = 25) -> list[dict[str, Any]]:
    out = []
    for r in results:
        if not judge(r)["safety_ok"]:
            out.append({"case_id": r.case.case_id, "group": r.case.variant, "query": r.case.query,
                        "outcome": r.outcome, "confirmed_id": r.confirmed_id, "match_type": r.match_type,
                        "expected_ids": sorted(r.case.expected_ids)[:5]})
    return out[:limit]


# ---------------------------------------------------------------- 執行 / 輸出


def gold_set_report(cases: list[EvalCase], info: dict[str, Any], *, seed: int | str, overlay_dir: Path,
                    snapshot_dir: Path, catalog_size: int) -> dict[str, Any]:
    manifest = json.loads((Path(overlay_dir) / "overlay_manifest.json").read_text(encoding="utf-8"))
    return build_report(
        EVAL_NAME + "_gold_set",
        dataset_versions={"overlay": {"overlay_version": manifest["overlay_version"],
                                      "candidates_sha256": sha256_file(Path(overlay_dir) / "candidates.csv"),
                                      "brand_candidates_sha256": sha256_file(Path(overlay_dir) / "brand_candidates.csv")},
                          "snapshot": {"dir": str(snapshot_dir), "product_count": catalog_size,
                                       "scope": "TFDA 國產＋輸入錠狀膠囊類（9,112）；不含健康食品 565"}},
        parameters={"seed": seed, "sizes": info["sizes"], "pool_sizes": info["pool_sizes"],
                    "desired_after_d3": DESIRED, "d3_safety_gates": D3_SAFETY_GATES},
        metrics={"total_cases": len(cases), "cases_per_group": dict(Counter(c.variant for c in cases)),
                 "cases_sha256": sha256_of([[c.case_id, c.query, sorted(c.expected_ids)] for c in cases])},
        details={"cases": [{"case_id": c.case_id, "group": c.variant, "query": c.query,
                            "expected_ids": sorted(c.expected_ids), "is_permit_query": c.is_permit_query}
                           for c in cases]})


def run_pre_d3(repo: Any, cases: list[EvalCase], *, label: str, seed: int | str, gold_sha: str,
               stage: str = "pre_d3") -> dict[str, Any]:
    from app.tools.product_tools import identity_generic_terms

    generic = identity_generic_terms(repo)
    results = [resolve_case(repo, c, generic) for c in cases]
    note = ("目前（D3 之前）runtime 行為；未修改任何 runtime" if stage == "pre_d3"
            else "D3 之後 runtime 行為（官方英文品名 exact 取回）")
    return build_report(
        EVAL_NAME + f"_{stage}_behavior",
        dataset_versions={"gold_set_cases_sha256": gold_sha},
        parameters={"seed": seed, "repository": label, "stage": stage, "note": note},
        metrics=compute_metrics(results),
        details={"violation_examples": violation_examples(results),
                 "case_results": [{"case_id": r.case.case_id, "group": r.case.variant, "outcome": r.outcome,
                                   "confirmed_id": r.confirmed_id, "candidate_ids": list(r.candidate_ids)[:8],
                                   "match_type": r.match_type, "correct": r.correct, **judge(r)}
                                  for r in results]})


def _live_repo():
    from app.config.settings import Settings
    from app.services.supabase_service import SupabaseOfficialDataRepository, SupabaseService
    from scripts.eval.common import ReadOnlySupabase

    service = SupabaseService(Settings())
    if not service.is_project_database():
        raise SystemExit("目前的 Supabase 不是 nutri_safe_ai 資料庫，停止。")
    return SupabaseOfficialDataRepository(ReadOnlySupabase(service))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--overlay-dir", type=Path, required=True)
    ap.add_argument("--snapshot-dir", type=Path, default=Path("data/tfda_product_coverage/2026-10-04"))
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--tag", required=True, help="輸出檔名標記，例如日期")
    ap.add_argument("--live-readonly", action="store_true", help="另以唯讀 Supabase repo 記錄 pre-D3 行為（不寫入）")
    ap.add_argument("--live-only", action="store_true", help="只記錄唯讀 production pre-D3 行為（gold set / offline 檔已存在時使用）")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    ap.add_argument("--post-d3", action="store_true",
                    help="D3 之後：用同一組 gold set（驗證 cases_sha256 與既有 gold-set 檔相同）記錄 post-D3 行為，不覆蓋 pre-D3 / gold-set 檔")
    ap.add_argument("--gold-tag", default=None, help="--post-d3 時既有 gold-set 檔的標記（預設同 --tag）")
    args = ap.parse_args(argv)

    catalog = catalog_from_snapshot(args.snapshot_dir)
    cases, info = build_gold_cases(args.overlay_dir, catalog, seed=args.seed)
    gold = gold_set_report(cases, info, seed=args.seed, overlay_dir=args.overlay_dir, snapshot_dir=args.snapshot_dir,
                           catalog_size=len(catalog))
    gold_sha = gold["metrics"]["cases_sha256"]
    if args.post_d3:
        stored = json.loads((args.out_dir / f"{EVAL_NAME}-baseline-002-gold-set-{args.gold_tag or args.tag}.json")
                            .read_text(encoding="utf-8"))
        if stored["metrics"]["cases_sha256"] != gold_sha:
            raise SystemExit("gold set 與既有 baseline-002 gold-set 檔不同（cases_sha256 不一致），停止。")
        if not args.live_only:
            offline = run_pre_d3(CatalogRepo(catalog), cases, seed=args.seed, gold_sha=gold_sha, stage="post_d3",
                                 label="offline CatalogRepo（中文品名片段比對＋官方英文品名 exact 取回）")
            off_path = write_report(offline, args.out_dir / f"{EVAL_NAME}-baseline-002-post-d3-offline-{args.tag}.json")
            print(f"offline post-D3：{off_path}")
            print(json.dumps(offline["metrics"], ensure_ascii=False, indent=1))
        if args.live_readonly or args.live_only:
            live = run_pre_d3(_live_repo(), cases, label="production Supabase（唯讀）", seed=args.seed, gold_sha=gold_sha,
                              stage="post_d3")
            live_path = write_report(live, args.out_dir / f"{EVAL_NAME}-baseline-002-post-d3-live-{args.tag}.json")
            print(f"live post-D3：{live_path}")
            print(json.dumps(live["metrics"], ensure_ascii=False, indent=1))
        return 0
    if not args.live_only:
        gold_path = write_report(gold, args.out_dir / f"{EVAL_NAME}-baseline-002-gold-set-{args.tag}.json")
        offline = run_pre_d3(CatalogRepo(catalog), cases, seed=args.seed, gold_sha=gold_sha,
                             label="offline CatalogRepo（中文品名片段比對，近似 search_products）")
        off_path = write_report(offline, args.out_dir / f"{EVAL_NAME}-baseline-002-pre-d3-offline-{args.tag}.json")
        print(f"gold set：{gold_path}")
        print(f"offline pre-D3：{off_path}")
        print(json.dumps(offline["metrics"], ensure_ascii=False, indent=1))
    if args.live_readonly or args.live_only:
        live = run_pre_d3(_live_repo(), cases, label="production Supabase（唯讀 RPC）", seed=args.seed, gold_sha=gold_sha)
        live_path = write_report(live, args.out_dir / f"{EVAL_NAME}-baseline-002-pre-d3-live-{args.tag}.json")
        print(f"live pre-D3：{live_path}")
        print(json.dumps(live["metrics"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
