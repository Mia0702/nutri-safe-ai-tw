"""Controller（Agent A）抽取評估 — harness 與 schema（預設完全離線）。

評估與 routing 相關的欄位：

* intent
* product_candidates（使用者寫法，依 normalize_name 比對；多或少都算錯）
* claim_roles（statement role 的多重集合，與順序無關）
* image_relevance（只在案例有圖片時；本階段 live 只支援純文字案例，圖片案例會被標為 skipped）

離線時：`score_case` / `run_scoring` 是純函式，可用任何「extractor」測試。
Live（會呼叫 OpenAI，產生費用）：**預設不執行**，必須同時給 `--live --confirm-live`，
並以 `--max-cases` 限制數量（預設 5）。這個階段不要執行大規模 live 評估：

    python -m scripts.eval.controller_eval --live --confirm-live --max-cases 5 --output eval_baselines/controller-<日期>.json

報告不含使用者原文：只存案例 ID、欄位對錯與計數。案例文字是虛構 SYNTHETIC。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from app.normalization import normalize_name
from scripts.eval.common import build_report, default_output_path, rate, sha256_of, write_report

EVAL_NAME = "controller_extraction"
DEFAULT_CASES = Path(__file__).parent / "cases" / "controller_cases.json"
SCORED_FIELDS = ("intent", "product_candidates", "claim_roles", "image_relevance")

# extractor：案例 → 實際抽取結果 {"intent": str, "product_candidates": [str], "claim_roles": [str], "image_relevance": str}
Extractor = Callable[[dict[str, Any]], dict[str, Any]]


def load_cases(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _norm_list(values: list[str] | None) -> list[str]:
    return sorted(normalize_name(v) for v in (values or []))


def score_case(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, bool]:
    """每個「期望中有指定」的欄位 → 是否正確。未指定的欄位不評分。"""
    scores: dict[str, bool] = {}
    if "intent" in expected:
        scores["intent"] = actual.get("intent") == expected["intent"]
    if "product_candidates" in expected:
        scores["product_candidates"] = _norm_list(actual.get("product_candidates")) == _norm_list(expected["product_candidates"])
    if "claim_roles" in expected:
        scores["claim_roles"] = sorted(actual.get("claim_roles") or []) == sorted(expected["claim_roles"])
    if "image_relevance" in expected:
        scores["image_relevance"] = actual.get("image_relevance") == expected["image_relevance"]
    return scores


def run_scoring(cases_doc: dict[str, Any], extractor: Extractor, max_cases: int | None = None) -> dict[str, Any]:
    cases = cases_doc["cases"]
    skipped = Counter()
    rows: list[dict[str, Any]] = []
    for case in cases:
        if max_cases is not None and len(rows) >= max_cases:
            skipped["max_cases_reached"] += 1
            continue
        if case.get("requires_media"):
            skipped["requires_media_not_supported_in_this_phase"] += 1
            continue
        actual = extractor(case)
        scores = score_case(case["expected"], actual)
        rows.append({"id": case["id"], "scores": scores, "all_correct": all(scores.values()),
                     "actual": {k: actual.get(k) for k in SCORED_FIELDS if k in case["expected"]}})
    field_stats = {}
    for field in SCORED_FIELDS:
        scored = [r["scores"][field] for r in rows if field in r["scores"]]
        field_stats[field] = {"scored": len(scored), "correct": sum(scored), "accuracy": rate(sum(scored), len(scored))}
    metrics = {
        "cases_run": len(rows), "cases_all_correct": sum(1 for r in rows if r["all_correct"]),
        "case_accuracy": rate(sum(1 for r in rows if r["all_correct"]), len(rows)),
        "field_accuracy": field_stats, "skipped": dict(sorted(skipped.items())),
        "failed_case_ids": [r["id"] for r in rows if not r["all_correct"]],
    }
    return build_report(
        EVAL_NAME,
        dataset_versions={"controller_cases": {"version": cases_doc.get("version"), "case_count": len(cases),
                                               "sha256": sha256_of(cases)}},
        parameters={"max_cases": max_cases, "scored_fields": list(SCORED_FIELDS)},
        metrics=metrics,
        details={"cases": rows},
    )


# ---------------------------------------------------------------- live extractor（opt-in，會呼叫 OpenAI）


def live_extractor() -> Extractor:
    """以真實 IntakeAgent.understand 抽取（純文字）。只在 --live --confirm-live 時才會被建立。"""
    from app.agents.intake_agent import IntakeAgent
    from app.config.settings import Settings
    from app.schemas.enums import InputType
    from app.schemas.user_request import RawUserInput
    from app.services.openai_service import OpenAIService

    settings = Settings()
    if not settings.openai_configured:
        raise SystemExit("未設定 OPENAI_API_KEY，無法執行 live Controller 評估。")
    agent = IntakeAgent(OpenAIService(settings))

    def extract(case: dict[str, Any]) -> dict[str, Any]:
        req = agent.understand(f"eval-{case['id']}", RawUserInput(input_type=InputType.TEXT, text=case["input_text"]))
        return {"intent": str(req.intent), "product_candidates": list(req.product_candidates),
                "claim_roles": [str(c.claim_role) for c in req.claims], "image_relevance": str(req.image_relevance)}

    return extract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controller 抽取評估（預設離線；live 為 opt-in）")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--live", action="store_true", help="呼叫真實 OpenAI（產生費用）")
    parser.add_argument("--confirm-live", action="store_true", help="確認要執行 live（與 --live 同時才生效）")
    parser.add_argument("--max-cases", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    if not (args.live and args.confirm_live):
        print("Controller 評估需要 live OpenAI：本命令預設不執行。\n"
              "之後要執行請加 --live --confirm-live（並以 --max-cases 控制數量）。", file=sys.stderr)
        return 3
    report = run_scoring(load_cases(args.cases), live_extractor(), max_cases=args.max_cases)
    out = write_report(report, args.output or default_output_path(EVAL_NAME), overwrite=args.overwrite)
    m = report["metrics"]
    print(f"cases_run={m['cases_run']} all_correct={m['cases_all_correct']} failed={m['failed_case_ids']}")
    print(f"報告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
