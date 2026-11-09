"""Brand-assist 評估集（baseline-003，D5）：已審核品牌輔助候選取回。

**不修改 baseline-001 / baseline-002；不寫 production（唯讀）；不呼叫 OpenAI。**

| group | 查詢 | 期望 |
|---|---|---|
| A_ACCEPT_BRAND_ONLY | 已審核（ACCEPT）品牌詞（原樣＋小寫） | 0 個自動確認；候選都屬於該品牌的候選集合 |
| B_ACCEPT_BRAND_PLUS_PRODUCT | ACCEPT 品牌詞＋該品牌某產品的中文 / 英文品名片段 | 正確產品出現在候選；除非既有（非品牌）規則本來就會確認，否則 0 自動確認 |
| C_REJECT_BRAND | REJECT 品牌詞（單獨 / 加英文品名片段） | 與 D5 之前（不帶 overlay）的結果逐案相同 |
| D_NEEDS_REVIEW_BRAND | NEEDS_REVIEW 品牌詞（單獨 / 加英文品名片段） | 同上 |
| E_GENERIC_EN_CONTROL | baseline-002 GENERIC_EN_TOKEN | 同上 |
| F_OFFICIAL_EN_CONTROL | baseline-002 UNIQUE_EN_EXACT | 同上（D3 行為不變） |
| G_ZH_CONTROL | baseline-002 ZH_NAME_CONTROL | 同上 |

每個案例都跑兩次：不帶 overlay（= D5 之前的行為）與帶 overlay，再逐案比對。

**簽核**：D4 的 reviewer 是 `CLAUDE_D4_REVIEW`，runtime loader 會拒絕。尚未有真人簽核時，可用 `--simulate-d4-signoff`
只在評估程序內以 D4 的 ACCEPT 決定建立記憶體 overlay（報告會標示 `simulated_signoff: true`）；不寫任何檔案、不改 runtime loader。

用法：
    python -m scripts.eval.brand_assist_eval --overlay-dir data/product_alias_overlay/2026-11-08 --tag 2026-11-08 \\
        --simulate-d4-signoff [--live-readonly]
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from app import brand_assist as ba
from app.english_name import conservative_key
from app.ingestion.brand_alias_review import REVIEWED_BRAND_FILE
from app.ingestion.product_alias_review import english_leading_token, load_snapshot, read_review_csv
from app.tools.product_tools import lookup_product
from scripts import review_brand_candidates as cli
from scripts.eval.common import DEFAULT_BASELINE_DIR, build_report, deterministic_sample, rate, sha256_of, write_report
from scripts.eval.identity_alias_eval import _live_repo, catalog_from_snapshot
from scripts.eval.resolver_eval import AUTO_CONFIRM, CLARIFY, CaseResult, CatalogRepo, EvalCase, resolve_case

EVAL_NAME = "brand_assist"
DEFAULT_SEED = 20261019
GROUPS = ("A_ACCEPT_BRAND_ONLY", "B_ACCEPT_BRAND_PLUS_PRODUCT", "C_REJECT_BRAND", "D_NEEDS_REVIEW_BRAND",
          "E_GENERIC_EN_CONTROL", "F_OFFICIAL_EN_CONTROL", "G_ZH_CONTROL")
CONTROL_SOURCE = {"E_GENERIC_EN_CONTROL": "GENERIC_EN_TOKEN", "F_OFFICIAL_EN_CONTROL": "UNIQUE_EN_EXACT",
                  "G_ZH_CONTROL": "ZH_NAME_CONTROL"}
B_PER_BRAND = 8
B_TOTAL = 160


# ---------------------------------------------------------------- overlay（真實 loader；或僅供評估的簽核模擬）


def simulated_signoff_overlay(directory: Path) -> ba.ReviewedBrandOverlay:
    """只在評估程序內使用：D4 的 ACCEPT 決定＋完整 D4 驗證（manifest / 雜湊 / 列驗證），略過「reviewer 必須是真人」這一項。
    不寫檔、不改 loader；報告會標示 simulated。"""
    rows_rep, manifest_rep = cli.validate_directory(directory)
    if not (rows_rep.ok and manifest_rep.ok):
        raise SystemExit("D4 overlay 驗證失敗，停止。")
    manifest = json.loads((directory / cli.MANIFEST_FILE).read_text(encoding="utf-8"))
    version = str(manifest["overlay_version"])
    accepted = [r for r in read_review_csv(directory / REVIEWED_BRAND_FILE) if r["decision"] == ba.ACCEPT]
    leftovers = [i for r in accepted for i in ba._row_issues(r, version) if "reviewer" not in i]
    if leftovers:
        raise SystemExit(f"ACCEPT 列有其他 runtime 驗證問題：{leftovers[:3]}")
    brands = tuple(ba.ReviewedBrand(
        review_id=r["review_id"], token=r["alias_text_raw"], key=conservative_key(r["alias_text_normalized"]),
        applicant=r["target_official_name"], zh_brand=(r.get("zh_brand_candidate") or "").strip(),
        support_count=int(r.get("support_count") or 0), reviewer="SIMULATED_FOR_EVALUATION", reviewed_at=r["reviewed_at"],
        overlay_version=version, source_record_id=r["source_record_id"]) for r in accepted)
    return ba.ReviewedBrandOverlay(brands, ba.OverlayStatus(active=True, overlay_version=version, accepted_brands=len(brands),
                                                            directory=str(directory)))


# ---------------------------------------------------------------- 案例（deterministic）


def _members(brand: ba.ReviewedBrand, catalog) -> list:
    return sorted((e for e in catalog if ba.is_brand_member(brand, e.to_row())), key=lambda e: e.product_id)


def _zh_remainder(name: str, zh_brand: str) -> str:
    text = conservative_key(name)
    prefix = conservative_key(zh_brand)
    if prefix and text.startswith(prefix):
        text = text[len(prefix):]
    text = "".join(ch for ch in text if ch.isalnum())
    return text[:4]


def _en_tail(name_en: str, token: str) -> str:
    words = (name_en or "").split()
    if words and conservative_key(words[0]).rstrip(".-'’") == conservative_key(token).rstrip(".-'’"):
        words = words[1:]
    return " ".join(words[:2])


def _case(group: str, key: str, query: str, expected: frozenset[str]) -> EvalCase:
    return EvalCase(case_id=f"{group}:{key}", product_id=key, variant=group, query=query, expected_ids=expected)


def build_cases(overlay_dir: Path, accept_overlay: ba.ReviewedBrandOverlay, catalog, snapshot_records, *, seed: int | str,
                gold_set: dict[str, Any]) -> tuple[list[EvalCase], dict[str, Any]]:
    rows = read_review_csv(overlay_dir / REVIEWED_BRAND_FILE)
    cases: list[EvalCase] = []
    pools: dict[str, Any] = {}

    for brand in sorted(accept_overlay.brands, key=lambda b: b.key):                              # A
        for text in dict.fromkeys([brand.token, brand.token.lower()]):
            cases.append(_case("A_ACCEPT_BRAND_ONLY", f"{brand.key}:{text}", text, frozenset()))

    b_pool = []                                                                                    # B
    # 查詢本身若剛好等於某個官方品名（中文或英文）完全相同，就不是「品牌＋片段」而是既有 exact 查詢（標準答案不明）→ 排除並計數
    official_keys = {conservative_key(e.name) for e in catalog} | {conservative_key(e.name_en) for e in catalog if e.name_en}
    excluded_exact = 0
    for brand in sorted(accept_overlay.brands, key=lambda b: b.key):
        members = _members(brand, catalog)
        for e in deterministic_sample(members, B_PER_BRAND, f"{seed}:B:{brand.key}", key=lambda e: e.product_id):
            zh = _zh_remainder(e.name, brand.zh_brand)
            en = _en_tail(e.name_en or "", brand.token)
            for suffix, tail in (("zh", zh), ("en", en)):
                if len(tail.replace(" ", "")) < 2:
                    continue
                query = f"{brand.token} {tail}"
                if conservative_key(query) in official_keys:
                    excluded_exact += 1
                    continue
                b_pool.append(_case("B_ACCEPT_BRAND_PLUS_PRODUCT", f"{e.product_id}:{suffix}", query,
                                    frozenset({e.product_id})))
    pools["B_pool"] = len(b_pool)
    pools["B_excluded_query_equals_an_official_name"] = excluded_exact
    cases += deterministic_sample(b_pool, B_TOTAL, f"{seed}:B", key=lambda c: c.case_id)

    lead_index: dict[str, list] = {}
    for rec in snapshot_records:
        lead = english_leading_token(rec.name_en or "")
        if lead:
            lead_index.setdefault(conservative_key(lead).rstrip(".-'’"), []).append(rec)
    for group, decision in (("C_REJECT_BRAND", "REJECT"), ("D_NEEDS_REVIEW_BRAND", "NEEDS_REVIEW")):   # C / D
        n = 0
        for r in sorted((x for x in rows if x["decision"] == decision), key=lambda x: x["alias_text_normalized"]):
            token = r["alias_text_raw"]
            cases.append(_case(group, f"{r['alias_text_normalized']}:only", token, frozenset()))
            recs = sorted(lead_index.get(conservative_key(token).rstrip(".-'’"), []), key=lambda x: x.product_id)
            if recs:
                tail = _en_tail(recs[0].name_en, token)
                if len(tail.replace(" ", "")) >= 2:
                    cases.append(_case(group, f"{r['alias_text_normalized']}:text", f"{token} {tail}", frozenset()))
            n += 1
        pools[group] = n

    by_group: dict[str, list[dict[str, Any]]] = {}
    for c in gold_set["details"]["cases"]:
        by_group.setdefault(c["group"], []).append(c)
    for group, source in CONTROL_SOURCE.items():                                                  # E / F / G
        for c in by_group.get(source, []):
            cases.append(EvalCase(case_id=f"{group}:{c['case_id']}", product_id=c["case_id"], variant=group,
                                  query=c["query"], expected_ids=frozenset(c["expected_ids"])))
        pools[group] = len(by_group.get(source, []))
    cases.sort(key=lambda c: (GROUPS.index(c.variant), c.case_id))
    return cases, {"pool_sizes": pools, "B_total": B_TOTAL, "B_per_brand": B_PER_BRAND}


# ---------------------------------------------------------------- 判定與指標


def _same(a: CaseResult, b: CaseResult) -> bool:
    return (a.outcome, a.confirmed_id, a.match_type, a.candidate_ids) == (b.outcome, b.confirmed_id, b.match_type, b.candidate_ids)


def judge(group: str, pre: CaseResult, post: CaseResult, member_ids: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"identical_to_pre_d5": _same(pre, post), "safety_ok": True}
    auto = post.outcome == AUTO_CONFIRM
    if group == "A_ACCEPT_BRAND_ONLY":
        out["safety_ok"] = not auto
        out["has_candidates"] = bool(post.candidate_ids)
        out["candidates_all_brand_members"] = bool(post.candidate_ids) and set(post.candidate_ids) <= member_ids
        out["pre_has_candidates"] = bool(pre.candidate_ids)
    elif group == "B_ACCEPT_BRAND_PLUS_PRODUCT":
        # 自動確認只有在既有（非品牌）規則本來就會確認同一個產品時才允許
        out["safety_ok"] = (not auto) or (pre.outcome == AUTO_CONFIRM and pre.confirmed_id == post.confirmed_id
                                           and post.correct is True)
        out["target_in_candidates"] = bool(post.case.expected_ids & set(post.candidate_ids)) or (auto and bool(post.correct))
        out["pre_target_in_candidates"] = bool(pre.case.expected_ids & set(pre.candidate_ids)) or (
            pre.outcome == AUTO_CONFIRM and bool(pre.correct))
    else:
        out["safety_ok"] = out["identical_to_pre_d5"]
    # C / D 沒有可靠的標準答案（品牌詞＋完整官方英文名本來就可能被 D3 exact 規則確認）：只用「與 D5 之前逐案相同」判定
    if auto and post.correct is False and group not in ("C_REJECT_BRAND", "D_NEEDS_REVIEW_BRAND"):
        out["safety_ok"] = False
    return out


def run_group_cases(repo: Any, cases: list[EvalCase], overlay: ba.ReviewedBrandOverlay, catalog) -> list[dict[str, Any]]:
    from app.tools.product_tools import identity_generic_terms

    generic = identity_generic_terms(repo)
    brands = {b.key: b for b in overlay.brands}
    members = {k: {e.product_id for e in _members(b, catalog)} for k, b in brands.items()}
    results = []
    for case in cases:
        pre = resolve_case(repo, case, generic, brand_overlay=None)
        post = resolve_case(repo, case, generic, brand_overlay=overlay)
        mention = overlay.find_mention(case.query)
        verdict = judge(case.variant, pre, post, members.get(mention.brand.key, set()) if mention else set())
        results.append({"case": case, "pre": pre, "post": post, **verdict})
    return results


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_group: dict[str, Any] = {}
    violations = 0
    for group in GROUPS:
        rs = [r for r in results if r["case"].variant == group]
        if not rs:
            continue
        oc = Counter(r["post"].outcome for r in rs)
        v = sum(1 for r in rs if not r["safety_ok"])
        violations += v
        g: dict[str, Any] = {
            "cases": len(rs),
            "outcomes": {k: oc.get(k, 0) for k in ("AUTO_CONFIRM", "CLARIFY", "NOT_FOUND", "CONFLICT", "OTHER")},
            "auto_confirm": oc.get(AUTO_CONFIRM, 0), "identical_to_pre_d5": sum(1 for r in rs if r["identical_to_pre_d5"]),
            "changed_vs_pre_d5": sum(1 for r in rs if not r["identical_to_pre_d5"]), "safety_violations": v}
        if group == "A_ACCEPT_BRAND_ONLY":
            g |= dict(candidate_rate=rate(sum(1 for r in rs if r["has_candidates"]), len(rs)),
                     candidates_all_brand_members=sum(1 for r in rs if r["candidates_all_brand_members"]),
                     pre_d5_candidate_rate=rate(sum(1 for r in rs if r["pre_has_candidates"]), len(rs)),
                     brand_assist_match_type=sum(1 for r in rs if r["post"].match_type == "BRAND_CANDIDATE_ONLY"))
        if group == "B_ACCEPT_BRAND_PLUS_PRODUCT":
            g |= dict(target_in_candidates=sum(1 for r in rs if r["target_in_candidates"]),
                     target_recall=rate(sum(1 for r in rs if r["target_in_candidates"]), len(rs)),
                     pre_d5_target_recall=rate(sum(1 for r in rs if r["pre_target_in_candidates"]), len(rs)),
                     brand_assist_match_type=sum(1 for r in rs if r["post"].match_type == "BRAND_CANDIDATE_ONLY"))
        by_group[group] = g
    wrong = sum(1 for r in results if r["post"].outcome == AUTO_CONFIRM and r["post"].correct is False
                and r["case"].variant not in ("C_REJECT_BRAND", "D_NEEDS_REVIEW_BRAND"))
    return {"total_cases": len(results), "SAFETY_VIOLATIONS": violations, "wrong_auto_confirm_count": wrong,
            "WRONG_AUTO_CONFIRM_RATE": rate(wrong, len(results)), "by_group": by_group}


def violation_examples(results: list[dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
    return [{"case_id": r["case"].case_id, "group": r["case"].variant, "query": r["case"].query,
             "pre": [r["pre"].outcome, r["pre"].match_type], "post": [r["post"].outcome, r["post"].match_type,
                                                                      r["post"].confirmed_id]}
            for r in results if not r["safety_ok"]][:limit]


def performance(repo: Any, cases: list[EvalCase], overlay_dir: Path, overlay: ba.ReviewedBrandOverlay) -> dict[str, Any]:
    ba.clear_overlay_cache()
    t = time.perf_counter()
    cold = ba.load_reviewed_brand_overlay(overlay_dir)
    cold_ms = (time.perf_counter() - t) * 1000
    ba.get_overlay(overlay_dir)
    t = time.perf_counter()
    ba.get_overlay(overlay_dir)
    cached_ms = (time.perf_counter() - t) * 1000
    ba.clear_overlay_cache()
    timings, counts, lookups = [], [], []
    for case in cases:
        if case.variant not in ("A_ACCEPT_BRAND_ONLY", "B_ACCEPT_BRAND_PLUS_PRODUCT"):
            continue
        report = ba.BrandAssistReport()
        t = time.perf_counter()
        lookup_product(repo, [case.query], brand_overlay=overlay, brand_report=report)
        timings.append((time.perf_counter() - t) * 1000)
        lookups.append(report.lookup_ms)
        counts.append(report.candidate_count)
    t0 = time.perf_counter()
    for case in cases[:50]:
        overlay.find_mention(case.query)
    find_ms = (time.perf_counter() - t0) * 1000 / max(1, min(50, len(cases)))
    q = lambda xs, p: round(sorted(xs)[min(len(xs) - 1, int(len(xs) * p))], 3) if xs else None  # noqa: E731
    return {
        "overlay_cold_load_ms(manifest+hash+validation)": round(cold_ms, 3), "overlay_cold_load_active": cold.active,
        "overlay_cached_get_ms": round(cached_ms, 4), "brand_mention_scan_ms_per_query": round(find_ms, 4),
        "brand_lookup_ms": {"n": len(lookups), "p50": q(lookups, 0.5), "p95": q(lookups, 0.95), "max": round(max(lookups), 3) if lookups else None},
        "lookup_product_total_ms": {"p50": q(timings, 0.5), "p95": q(timings, 0.95),
                                    "mean": round(statistics.mean(timings), 3) if timings else None},
        "candidate_count": {"mean": round(statistics.mean(counts), 2) if counts else None,
                            "max": max(counts) if counts else None},
        "cache": "以 manifest / reviewed csv 的 mtime+size 指紋判斷失效；overlay 物件不可變；每個品牌詞至多 1 次 applicant 查詢（無其他往返）",
    }


# ---------------------------------------------------------------- 執行 / 輸出


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--overlay-dir", type=Path, required=True)
    ap.add_argument("--snapshot-dir", type=Path, default=Path("data/tfda_product_coverage/2026-10-04"))
    ap.add_argument("--gold-set", type=Path,
                    default=DEFAULT_BASELINE_DIR / "identity_alias-baseline-002-gold-set-2026-10-27.json")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--simulate-d4-signoff", action="store_true",
                    help="D4 reviewer 不是真人時，只在評估程序內模擬簽核（報告標示 simulated_signoff）")
    ap.add_argument("--live-readonly", action="store_true")
    ap.add_argument("--live-only", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    args = ap.parse_args(argv)

    real = ba.load_reviewed_brand_overlay(args.overlay_dir)
    if real.active:
        overlay, simulated = real, False
    elif args.simulate_d4_signoff:
        overlay, simulated = simulated_signoff_overlay(args.overlay_dir), True
    else:
        raise SystemExit(f"overlay 未生效（{real.status.issues[:2]}）；沒有真人簽核。可加 --simulate-d4-signoff 做評估用模擬。")
    catalog = catalog_from_snapshot(args.snapshot_dir)
    _, records = load_snapshot(args.snapshot_dir)
    gold = json.loads(args.gold_set.read_text(encoding="utf-8"))
    cases, info = build_cases(args.overlay_dir, overlay, catalog, records, seed=args.seed, gold_set=gold)
    cases_sha = sha256_of([[c.case_id, c.query, sorted(c.expected_ids)] for c in cases])
    overlay_info = {"overlay_version": overlay.status.overlay_version, "accepted_brands": len(overlay.brands),
                    "simulated_signoff": simulated, "reviewer_in_overlay_file": sorted({
                        r["reviewer"] for r in read_review_csv(args.overlay_dir / REVIEWED_BRAND_FILE)}),
                    "NOTE": ("簽核為評估用模擬（runtime loader 仍會拒絕 CLAUDE_D4_REVIEW）" if simulated else "真人簽核的 overlay")}

    def run(repo: Any, label: str, path_name: str) -> None:
        results = run_group_cases(repo, cases, overlay, catalog)
        perf = performance(repo, cases, args.overlay_dir, overlay) if not simulated else performance_simulated(repo, cases, overlay)
        report = build_report(
            EVAL_NAME + "_baseline_003",
            dataset_versions={"cases_sha256": cases_sha, "overlay": overlay_info,
                              "gold_set_002_cases_sha256": gold["metrics"]["cases_sha256"]},
            parameters={"seed": args.seed, "repository": label, **info},
            metrics=compute_metrics(results),
            details={"violation_examples": violation_examples(results), "performance": perf,
                     "case_results": [{"case_id": r["case"].case_id, "group": r["case"].variant, "query": r["case"].query,
                                       "pre": [r["pre"].outcome, r["pre"].match_type],
                                       "post": [r["post"].outcome, r["post"].match_type, r["post"].confirmed_id],
                                       "candidate_ids": list(r["post"].candidate_ids)[:8],
                                       **{k: v for k, v in r.items() if k not in ("case", "pre", "post")}} for r in results]})
        path = write_report(report, args.out_dir / f"{EVAL_NAME}-baseline-003-{path_name}-{args.tag}.json")
        print(f"{label}：{path}")
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=1))
        print(json.dumps(report["details"]["performance"], ensure_ascii=False, indent=1))

    if not args.live_only:
        run(CatalogRepo(catalog), "offline CatalogRepo", "offline")
    if args.live_readonly or args.live_only:
        run(_live_repo(), "production Supabase（唯讀）", "live")
    return 0


def performance_simulated(repo: Any, cases: list[EvalCase], overlay: ba.ReviewedBrandOverlay) -> dict[str, Any]:
    """模擬簽核時 loader 不會啟用：只量 lookup 相關耗時；overlay 載入時間另以真實 loader（驗證到 reviewer 為止）量測。"""
    perf = performance(repo, cases, Path(overlay.status.directory or "."), overlay)
    perf["note"] = "overlay_cold_load_* 來自真實 loader（simulated 時因 reviewer 被拒而停用，時間含完整 manifest / 雜湊 / 列驗證）"
    return perf


if __name__ == "__main__":
    raise SystemExit(main())
