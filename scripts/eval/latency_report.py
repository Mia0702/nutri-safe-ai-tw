"""Latency 報告：讀既有結構化 log（`analysis_done`），依既有 flow（planner_mode）分組。

* 只讀 log 檔，不連任何服務、不改 runtime logging。
* 輸入：JSON-lines（`app/observability.py` 的 JsonFormatter 輸出；例如 `uvicorn ... 2> app.log`、
  `docker logs <container> > app.log`）。非 JSON 行與其他 msg 一律略過。
* 輸出只含計數與毫秒統計，不含 request_id 以外的識別資訊，也沒有使用者原文
  （log 白名單本來就不記原文）。

指標（每組）：count / p50（median）/ p95（nearest-rank）/ max（毫秒）。

用法：
    python -m scripts.eval.latency_report --log app.log [--log more.log] [--output path]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from scripts.eval.common import build_report, default_output_path, sha256_of, write_report

EVAL_NAME = "latency"
UNGROUPED = "(no planner_mode)"
STAGE_FIELDS = ("semantic_extraction_ms", "product_resolution_ms", "db_kg_ms", "rag_ms", "planner_ms",
                "assessment_ms", "official_web_ms", "claim_policy_ms", "vision_ms", "transcription_ms", "fetch_ms")
# 目前 analysis_done 沒有、因而無法在這份報告中衡量的欄位（只回報，不改 frozen logging）
KNOWN_MISSING_FIELDS = (
    "response_type（回覆型態）",
    "fast_path_reason（只有 resumed_with_selected_product 的 analysis_done 才有）",
    "LINE queue 等待 / 送出（reply|push）耗時（line_stage 只有階段名稱，沒有 duration）",
    "端到端（收到 webhook → 訊息送達）耗時",
)


def percentile_nearest_rank(sorted_values: list[float], pct: float) -> float:
    """nearest-rank 百分位數（deterministic、不內插）。"""
    if not sorted_values:
        raise ValueError("empty")
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def summarize(values: Iterable[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {"count": len(ordered), "p50_ms": statistics.median(ordered),
            "p95_ms": percentile_nearest_rank(ordered, 95), "max_ms": ordered[-1]}


def parse_log_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    """只取 msg == analysis_done 且有 duration_ms 的 JSON 行。"""
    out = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("msg") == "analysis_done" and isinstance(record.get("duration_ms"), (int, float)):
            out.append(record)
    return out


def build_latency_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, list[float]] = defaultdict(list)
    by_input: dict[str, list[float]] = defaultdict(list)
    by_mode_input: dict[str, list[float]] = defaultdict(list)
    stages: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        mode = r.get("planner_mode") or UNGROUPED
        itype = r.get("input_type") or "(unknown)"
        d = float(r["duration_ms"])
        by_mode[mode].append(d)
        by_input[itype].append(d)
        by_mode_input[f"{mode} | {itype}"].append(d)
        for f in STAGE_FIELDS:
            if isinstance(r.get(f), (int, float)):
                stages[mode][f].append(float(r[f]))
    return {
        "overall": summarize(float(r["duration_ms"]) for r in records),
        "by_planner_mode": {k: summarize(v) for k, v in sorted(by_mode.items())},
        "by_input_type": {k: summarize(v) for k, v in sorted(by_input.items())},
        "by_planner_mode_and_input_type": {k: summarize(v) for k, v in sorted(by_mode_input.items())},
        "stage_ms_by_planner_mode": {m: {f: summarize(v) for f, v in sorted(fs.items())}
                                     for m, fs in sorted(stages.items())},
        "known_missing_fields": list(KNOWN_MISSING_FIELDS),
    }


def run_report(log_paths: list[Path]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for path in log_paths:
        records += parse_log_lines(Path(path).read_text(encoding="utf-8", errors="replace").splitlines())
    metrics = build_latency_metrics(records)
    return build_report(
        EVAL_NAME,
        dataset_versions={"logs": {"files": [Path(p).name for p in log_paths], "analysis_done_records": len(records),
                                   "sha256": sha256_of([[r.get("request_id"), r["duration_ms"]] for r in records])}},
        parameters={"percentile_method": "nearest-rank", "group_by": "planner_mode (existing flow label)"},
        metrics=metrics,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Latency 報告（讀既有結構化 log）")
    parser.add_argument("--log", type=Path, action="append", required=True, help="JSON-lines log 檔（可重複）")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    report = run_report(args.log)
    out = write_report(report, args.output or default_output_path(EVAL_NAME), overwrite=args.overwrite)
    print(f"analysis_done records={report['dataset_versions']['logs']['analysis_done_records']}  報告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
