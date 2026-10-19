"""Resolver identity-safety 評估（READ-ONLY）。

由官方產品目錄 deterministic 抽樣，對每個樣本以通用的字串變形（完整品名、字號、省略一字、替換一字、
截掉尾端）產生查詢，走與 Researcher 相同的 deterministic 路徑：
    lookup_product / lookup_permit → resolve_product_match（含 Candidate Relevance Gate）
然後比對 ground truth（官方品名相同的產品群）。

最重要的指標是 WRONG_AUTO_CONFIRM_RATE：自動確認成「不是原本那個產品」。
wrong auto-confirm 比多一次確認（candidate）或 NOT_FOUND 更危險（FC-B3）。

範圍與限制：
* 只評估 deterministic resolver 階段；等同於「Controller 完美地把使用者原文放進 product_candidates」。
  不含 Controller 抽取、planner、圖片 / 語音誤差。
* 不修改 resolver；不為了提高分數調整任何門檻。

用法：
    python -m scripts.eval.resolver_eval --source live --sample-size 150 --seed 20261019
    python -m scripts.eval.resolver_eval --source json --catalog-json <catalog.json> --sample-size 50
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.normalization import extract_permit_code, normalize_name
from app.risk.assessment_rules import resolve_product_match
from app.schemas.enums import InputType, Intent, ProductVerificationStatus
from app.schemas.evidence import ProductCandidateMatch
from app.schemas.user_request import NormalizedUserRequest
from app.tools.product_tools import identity_generic_terms, lookup_permit, lookup_product
from scripts.eval.common import (
    EvalWriteAttempt,
    ReadOnlySupabase,
    build_report,
    default_output_path,
    deterministic_sample,
    rate,
    sha256_of,
    write_report,
)

EVAL_NAME = "resolver_identity_safety"
DEFAULT_SEED = 20261019
DEFAULT_SAMPLE_SIZE = 100
MIN_NAME_LEN_FOR_MUTATION = 6

# 變形類型（通用字串操作，不針對任何產品）
EXACT_NAME = "EXACT_NAME"
PERMIT_NUMBER = "PERMIT_NUMBER"
OMIT_ONE_CHAR = "OMIT_ONE_CHAR"
SUBSTITUTE_ONE_CHAR = "SUBSTITUTE_ONE_CHAR"
TRUNCATE_TAIL = "TRUNCATE_TAIL"
VARIANTS = (EXACT_NAME, PERMIT_NUMBER, OMIT_ONE_CHAR, SUBSTITUTE_ONE_CHAR, TRUNCATE_TAIL)
EXACT_VARIANTS = (EXACT_NAME, PERMIT_NUMBER)

# 結果分類
AUTO_CONFIRM = "AUTO_CONFIRM"
CLARIFY = "CLARIFY"
NOT_FOUND = "NOT_FOUND"
CONFLICT = "CONFLICT"
OTHER = "OTHER"


@dataclass(frozen=True)
class CatalogEntry:
    product_id: str
    name: str
    permit_number: str | None
    category: str
    company_name: str | None = None
    status: str | None = None

    def to_row(self) -> dict[str, Any]:
        """OfficialDataRepository 回傳列的最小欄位（供離線 CatalogRepo 使用）。"""
        return {
            "product_id": self.product_id, "product_name_zh": self.name, "permit_number": self.permit_number,
            "permit_code": extract_permit_code(self.permit_number or ""), "product_category": self.category,
            "company_name": self.company_name, "status": self.status or "UNKNOWN", "country_code": "TW",
            "source_name": "EVAL_CATALOG", "is_official": True,
        }


@dataclass
class EvalCase:
    case_id: str
    product_id: str
    variant: str
    query: str
    expected_ids: frozenset[str]
    is_permit_query: bool = False


@dataclass
class CaseResult:
    case: EvalCase
    outcome: str
    confirmed_id: str | None = None
    candidate_ids: tuple[str, ...] = ()
    match_type: str | None = None
    correct: bool | None = None  # AUTO_CONFIRM 才有意義


# ---------------------------------------------------------------- 目錄


def catalog_digest(catalog: list[CatalogEntry]) -> str:
    return sha256_of([[e.product_id, e.name, e.permit_number, e.category]
                      for e in sorted(catalog, key=lambda e: e.product_id)])


def load_catalog_json(path: Path) -> list[CatalogEntry]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return [CatalogEntry(product_id=str(r["product_id"]), name=r["name"], permit_number=r.get("permit_number"),
                         category=r.get("category") or "unknown", company_name=r.get("company_name"),
                         status=r.get("status")) for r in rows]


def load_catalog_live(db: ReadOnlySupabase, page_size: int = 1000) -> list[CatalogEntry]:
    """唯讀分頁讀取 product_details（依 product_id 排序，避免漏列 / 重複）。"""
    out: list[CatalogEntry] = []
    start = 0
    while True:
        builder = (db.table("product_details")
                   .select("product_id,product_name_zh,permit_number,product_category,company_name,status")
                   .order("product_id").range(start, start + page_size - 1))
        rows = db.select(builder)
        out += [CatalogEntry(product_id=str(r["product_id"]), name=r["product_name_zh"],
                             permit_number=r.get("permit_number"), category=r.get("product_category") or "health_food",
                             company_name=r.get("company_name"), status=r.get("status")) for r in rows]
        if len(rows) < page_size:
            return out
        start += page_size


class CatalogRepo:
    """離線用 OfficialDataRepository：以詞片段命中數篩選（與 Supabase search_products 契約相近，非完全相同）。"""

    def __init__(self, catalog: list[CatalogEntry]):
        self._rows = [(e, normalize_name(e.name)) for e in catalog]

    def search_products(self, terms: list[str], limit: int) -> list[dict[str, Any]]:
        scored = [(e, sum(1 for t in terms if t and t in norm), len(norm)) for e, norm in self._rows]
        scored = [s for s in scored if s[1] > 0]
        scored.sort(key=lambda s: (-s[1], s[2], s[0].product_id))
        return [s[0].to_row() for s in scored[:limit]]

    def get_products_by_permit_code(self, permit_code: str) -> list[dict[str, Any]]:
        return [e.to_row() for e, _ in self._rows if extract_permit_code(e.permit_number or "") == permit_code]

    def search_entity_aliases(self, terms: list[str], entity_types: list[str], limit: int) -> list[dict[str, Any]]:
        return []

    def get_identity_generic_terms(self) -> list[str]:
        return []


# ---------------------------------------------------------------- 案例產生（deterministic）


def _name_groups(catalog: list[CatalogEntry]) -> dict[str, frozenset[str]]:
    groups: dict[str, set[str]] = {}
    for e in catalog:
        groups.setdefault(normalize_name(e.name), set()).add(e.product_id)
    return {k: frozenset(v) for k, v in groups.items()}


def _permit_groups(catalog: list[CatalogEntry]) -> dict[str, frozenset[str]]:
    groups: dict[str, set[str]] = {}
    for e in catalog:
        code = extract_permit_code(e.permit_number or "")
        if code:
            groups.setdefault(code, set()).add(e.product_id)
    return {k: frozenset(v) for k, v in groups.items()}


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def generate_cases(catalog: list[CatalogEntry], sample: list[CatalogEntry], seed: int | str,
                   variants: tuple[str, ...] = VARIANTS) -> tuple[list[EvalCase], Counter]:
    """為每個樣本產生查詢。變形後剛好等於另一個官方品名者，ground truth 不明 → 排除並計數。"""
    names = _name_groups(catalog)
    permits = _permit_groups(catalog)
    pool = sorted({ch for e in catalog for ch in normalize_name(e.name) if _is_cjk(ch)})
    cases: list[EvalCase] = []
    skipped: Counter = Counter()

    for entry in sample:
        own_ids = names[normalize_name(entry.name)]
        rng = random.Random(f"case:{seed}:{entry.product_id}")
        chars = list(entry.name.strip())
        queries: dict[str, str | None] = {EXACT_NAME: entry.name}
        has_permit = bool(entry.permit_number and extract_permit_code(entry.permit_number))
        queries[PERMIT_NUMBER] = entry.permit_number if has_permit else None
        if len(normalize_name(entry.name)) >= MIN_NAME_LEN_FOR_MUTATION and len(chars) > 4:
            i = rng.randrange(1, len(chars) - 1)
            queries[OMIT_ONE_CHAR] = "".join(chars[:i] + chars[i + 1:])
            j = rng.randrange(1, len(chars) - 1)
            replacement = rng.choice([c for c in pool if c != chars[j]] or ["x"])
            queries[SUBSTITUTE_ONE_CHAR] = "".join(chars[:j] + [replacement] + chars[j + 1:])
            queries[TRUNCATE_TAIL] = "".join(chars[:-2])
        for variant in variants:
            query = queries.get(variant)
            if not query:
                skipped[f"{variant}:not_applicable"] += 1
                continue
            if variant == PERMIT_NUMBER:
                expected = permits.get(extract_permit_code(query) or "", frozenset({entry.product_id}))
            else:
                collides = names.get(normalize_name(query))
                if variant != EXACT_NAME and collides is not None and not (collides & own_ids):
                    skipped[f"{variant}:equals_other_official_name"] += 1
                    continue
                expected = own_ids
            cases.append(EvalCase(case_id=f"{entry.product_id}:{variant}", product_id=entry.product_id,
                                  variant=variant, query=query, expected_ids=expected,
                                  is_permit_query=variant == PERMIT_NUMBER))
    return cases, skipped


# ---------------------------------------------------------------- 執行（deterministic resolver 階段）


def _request(case: EvalCase) -> NormalizedUserRequest:
    kwargs: dict[str, Any] = ({"permit_number_candidates": [case.query]} if case.is_permit_query
                              else {"product_candidates": [case.query]})
    req = NormalizedUserRequest(request_id=f"eval-{case.case_id}", input_type=InputType.TEXT, original_text=case.query,
                                normalized_text=case.query, intent=Intent.PRODUCT_VERIFICATION, **kwargs)
    req.needs_product_lookup = True
    return req


def resolve_case(repo: Any, case: EvalCase, generic_terms: list[str] | None = None) -> CaseResult:
    req = _request(case)
    name_matches: dict[str, ProductCandidateMatch] = {}
    permit_matches: list[ProductCandidateMatch] = []
    if case.is_permit_query:
        permit_matches = lookup_permit(repo, case.query).candidates
    else:
        for c in lookup_product(repo, [case.query]).candidates:
            name_matches[c.product.product_id] = c
    generic = generic_terms if generic_terms is not None else identity_generic_terms(repo)
    match = resolve_product_match(req, list(name_matches.values()), permit_matches, [], [case.query], True,
                                  generic_terms=generic)
    candidate_ids = tuple(c.product.product_id for c in match.candidates)
    mtype = str(match.match_type) if match.match_type else None
    if match.status == ProductVerificationStatus.VERIFIED and match.best_match:
        pid = match.best_match.product.product_id
        return CaseResult(case, AUTO_CONFIRM, pid, candidate_ids, mtype, correct=pid in case.expected_ids)
    if match.status == ProductVerificationStatus.PARTIAL_MATCH:
        return CaseResult(case, CLARIFY, None, candidate_ids, mtype)
    if match.status == ProductVerificationStatus.NOT_FOUND:
        return CaseResult(case, NOT_FOUND, None, candidate_ids, mtype)
    if match.status == ProductVerificationStatus.CONFLICT:
        return CaseResult(case, CONFLICT, match.best_match.product.product_id if match.best_match else None,
                          candidate_ids, mtype)
    return CaseResult(case, OTHER, None, candidate_ids, mtype)


# ---------------------------------------------------------------- 指標（純函式）


def compute_metrics(results: list[CaseResult]) -> dict[str, Any]:
    total = len(results)
    outcomes = Counter(r.outcome for r in results)
    auto = [r for r in results if r.outcome == AUTO_CONFIRM]
    wrong = [r for r in auto if r.correct is False]
    right = [r for r in auto if r.correct]
    clarify = [r for r in results if r.outcome == CLARIFY]
    clarify_hit = [r for r in clarify if r.case.expected_ids & set(r.candidate_ids)]
    not_found = [r for r in results if r.outcome == NOT_FOUND]
    exact = [r for r in results if r.case.variant in EXACT_VARIANTS]
    # exact 查詢「正確」：確認到正確產品，或（同名多筆）請使用者確認且候選含正確產品；其他都算不正確
    exact_ok = [r for r in exact if (r.outcome == AUTO_CONFIRM and r.correct)
                or (r.outcome == CLARIFY and r.case.expected_ids & set(r.candidate_ids))]

    by_variant: dict[str, dict[str, Any]] = {}
    for variant in sorted({r.case.variant for r in results}):
        rs = [r for r in results if r.case.variant == variant]
        oc = Counter(r.outcome for r in rs)
        wrong_v = sum(1 for r in rs if r.outcome == AUTO_CONFIRM and r.correct is False)
        by_variant[variant] = {
            "cases": len(rs), "auto_confirm": oc[AUTO_CONFIRM], "wrong_auto_confirm": wrong_v,
            "clarify": oc[CLARIFY], "not_found": oc[NOT_FOUND], "conflict": oc[CONFLICT], "other": oc[OTHER],
            "wrong_auto_confirm_rate": rate(wrong_v, len(rs)),
        }
    return {
        "total_cases": total,
        "outcome_counts": {k: outcomes.get(k, 0) for k in (AUTO_CONFIRM, CLARIFY, NOT_FOUND, CONFLICT, OTHER)},
        "auto_confirm_count": len(auto),
        "correct_auto_confirm_count": len(right),
        "wrong_auto_confirm_count": len(wrong),
        # 最重要指標：分母固定為全部案例（跨版本可比）；另列「在自動確認之中」的錯誤比例
        "WRONG_AUTO_CONFIRM_RATE": rate(len(wrong), total),
        "wrong_auto_confirm_rate_among_auto_confirms": rate(len(wrong), len(auto)),
        "auto_confirm_precision": rate(len(right), len(auto)),
        "clarification_count": len(clarify),
        "clarification_rate": rate(len(clarify), total),
        "clarification_candidate_recall": rate(len(clarify_hit), len(clarify)),
        "not_found_count": len(not_found),
        "not_found_rate": rate(len(not_found), total),
        "exact_cases": len(exact),
        "exact_match_correct_count": len(exact_ok),
        "exact_match_correctness": rate(len(exact_ok), len(exact)),
        "by_variant": by_variant,
    }


def wrong_auto_confirm_examples(results: list[CaseResult], limit: int = 25) -> list[dict[str, Any]]:
    """錯誤自動確認的明細（查詢由官方品名變形而來，不含使用者內容）。"""
    wrong = [r for r in results if r.outcome == AUTO_CONFIRM and r.correct is False]
    return [{"case_id": r.case.case_id, "variant": r.case.variant, "query": r.case.query,
             "expected_ids": sorted(r.case.expected_ids), "confirmed_id": r.confirmed_id, "match_type": r.match_type}
            for r in wrong[:limit]]


# ---------------------------------------------------------------- 主流程


def run_eval(catalog: list[CatalogEntry], repo: Any, *, sample_size: int, seed: int | str,
             variants: tuple[str, ...] = VARIANTS, source_label: str = "unknown") -> dict[str, Any]:
    sample = deterministic_sample(catalog, sample_size, seed, key=lambda e: e.product_id)
    cases, skipped = generate_cases(catalog, sample, seed, variants)
    generic = identity_generic_terms(repo)
    results = [resolve_case(repo, c, generic) for c in cases]
    metrics = compute_metrics(results)
    by_category = Counter(e.category for e in catalog)
    return build_report(
        EVAL_NAME,
        dataset_versions={
            "product_catalog": {"source": source_label, "product_count": len(catalog),
                                "sha256": catalog_digest(catalog), "by_category": dict(sorted(by_category.items()))},
        },
        parameters={"seed": seed, "sample_size": len(sample), "requested_sample_size": sample_size,
                    "variants": list(variants), "stage": "deterministic_resolver_only",
                    "skipped_cases": dict(sorted(skipped.items()))},
        metrics=metrics,
        details={"wrong_auto_confirm_examples": wrong_auto_confirm_examples(results)},
    )


def _live_repo_and_catalog():
    from app.config.settings import Settings
    from app.services.supabase_service import SupabaseOfficialDataRepository, SupabaseService

    service = SupabaseService(Settings())
    if not service.is_project_database():
        raise SystemExit("目前的 Supabase 不是 nutri_safe_ai 資料庫，停止。")
    db = ReadOnlySupabase(service)
    return SupabaseOfficialDataRepository(db), load_catalog_live(db)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolver identity-safety 評估（唯讀）")
    parser.add_argument("--source", choices=["live", "json"], default="live")
    parser.add_argument("--catalog-json", type=Path,
                        help="--source json 時的產品目錄（product_id, name, permit_number, category）")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, help="預設 eval_baselines/resolver_identity_safety-<時間>.json")
    parser.add_argument("--overwrite", action="store_true", help="允許覆蓋既有檔案（預設拒絕）")
    args = parser.parse_args(argv)

    if args.source == "json":
        if not args.catalog_json:
            parser.error("--source json 需要 --catalog-json")
        catalog = load_catalog_json(args.catalog_json)
        repo: Any = CatalogRepo(catalog)
        label = f"json:{args.catalog_json.name}"
    else:
        repo, catalog = _live_repo_and_catalog()
        label = "supabase:product_details(read-only)"
    try:
        report = run_eval(catalog, repo, sample_size=args.sample_size, seed=args.seed, source_label=label)
    except EvalWriteAttempt as exc:
        print(f"評估中止：{exc}", file=sys.stderr)
        return 2
    out = write_report(report, args.output or default_output_path(EVAL_NAME), overwrite=args.overwrite)
    m = report["metrics"]
    print(f"cases={m['total_cases']} WRONG_AUTO_CONFIRM_RATE={m['WRONG_AUTO_CONFIRM_RATE']} "
          f"wrong={m['wrong_auto_confirm_count']} clarify={m['clarification_count']} not_found={m['not_found_count']}")
    print(f"報告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
