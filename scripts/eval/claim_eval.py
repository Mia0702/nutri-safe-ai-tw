"""Claim verdict 評估（READ-ONLY、完全離線、deterministic）。

把 scripts/eval/cases/claim_cases.json 的案例（虛構 SYNTHETIC 資料）送進既有的
`assess_claim`（claim policy）與 `RiskEngine.assess`，記錄「期望 vs 實際」：

* claim policy 結果（ClaimPolicyResult）與 ClaimAssessmentStatus
* claim role（statement role / origin）
* 命題風險 claim_risk（單一來源，見 FC-C3 / FC-C4）
* 產品層風險 risk_level（RiskEngine；用 risk_level 或 risk_level_not 斷言）

不新增任何 policy 規則、不呼叫 LLM / DB / 網路。案例的期望值來自 docs/FROZEN_CONTRACTS.md
（FC-C1 ~ FC-C4）；期望與實際不同是「回歸訊號」，不是要調整 production 規則。

用法：
    python -m scripts.eval.claim_eval [--cases path] [--output path]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.risk.assessment_rules import assess_claim
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimOrigin,
    ClaimSemanticCategory,
    InputType,
    Intent,
    MedicalAssertionType,
    EvidenceStrength,
    ProductVerificationStatus,
    SourceType,
)
from app.schemas.evidence import ApprovedClaimEvidence, EvidenceBundle, ProductMatch, SourceRef
from app.schemas.user_request import ExtractedClaim, NormalizedUserRequest
from scripts.eval.common import build_report, default_output_path, rate, sha256_of, write_report

EVAL_NAME = "claim_verdict"
DEFAULT_CASES = Path(__file__).parent / "cases" / "claim_cases.json"
SRC = SourceRef(source_name="EVAL_FIXTURE", source_type=SourceType.STRUCTURED_DB, is_official=True)

# 期望欄位 → 從實際結果取值的函式名稱
CHECKED_FIELDS = ("policy_result", "status", "claim_risk", "claim_role", "risk_level")


def load_cases(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _claim(spec: dict[str, Any]) -> ExtractedClaim:
    return ExtractedClaim(
        claim_text=spec["text"], origin=ClaimOrigin(spec["origin"]),
        semantic_category=ClaimSemanticCategory(spec.get("category", "health_function")),
        target_condition=spec.get("target_condition"), target_function=spec.get("target_function"),
        uses_absolute_language=bool(spec.get("absolute", False)),
        medical_assertion=MedicalAssertionType(spec["medical_assertion"]) if spec.get("medical_assertion") else None,
    )


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    claim = _claim(case["claim"])
    status = ProductVerificationStatus(case.get("product_status", "VERIFIED"))
    approved = [ApprovedClaimEvidence(evidence_id=f"ac:{i}", product_id="eval-product", approved_claim_id=f"ac-{i}",
                                      function_name=a["function_name"], claim_text=a["claim_text"], source=SRC)
                for i, a in enumerate(case.get("approved_claims", []))]
    ids = {a.evidence_id for a in approved}
    assessment = assess_claim(
        0, claim, [], ids, ids, True, approved_claims=approved,
        product_unresolved=status != ProductVerificationStatus.VERIFIED,
    )
    request = NormalizedUserRequest(request_id=f"eval-{case['id']}", input_type=InputType.TEXT,
                                    original_text="EVAL", normalized_text="EVAL", intent=Intent.CLAIM_VERIFICATION,
                                    claims=[claim])
    # 已確認產品且有官方核准功效 = STRONG 證據；其餘不假設任何證據強度（NONE）
    strength = EvidenceStrength.STRONG if status == ProductVerificationStatus.VERIFIED and approved else EvidenceStrength.NONE
    bundle = EvidenceBundle(request_id=request.request_id, product_match=ProductMatch(status=status),
                            approved_claims=approved, claim_assessment=[assessment], evidence_strength=strength)
    risk = RiskEngine().assess(request, bundle)
    return {
        "policy_result": str(assessment.policy.policy_result) if assessment.policy else None,
        "status": str(assessment.status),
        "claim_risk": str(assessment.claim_risk) if assessment.claim_risk else None,
        "claim_role": str(claim.claim_role),
        "risk_level": str(risk.risk_level),
        "rule_ids": list(assessment.rule_ids),
    }


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """回傳不符的欄位名稱。`<field>_not` 表示「不可等於」。"""
    mismatches = []
    for field in CHECKED_FIELDS:
        if field in expected and expected[field] != actual.get(field):
            mismatches.append(field)
        negative = expected.get(f"{field}_not")
        if negative is not None and actual.get(field) == negative:
            mismatches.append(f"{field}_not")
    return mismatches


def run_eval(cases_doc: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for case in cases_doc["cases"]:
        actual = evaluate_case(case)
        mismatches = compare(case["expected"], actual)
        rows.append({"id": case["id"], "group": case.get("group"), "passed": not mismatches,
                     "mismatches": mismatches, "expected": case["expected"], "actual": actual})
    total, passed = len(rows), sum(1 for r in rows if r["passed"])
    field_totals = {f: 0 for f in CHECKED_FIELDS}
    field_hits = {f: 0 for f in CHECKED_FIELDS}
    for case, row in zip(cases_doc["cases"], rows):
        for f in CHECKED_FIELDS:
            if f in case["expected"] or f"{f}_not" in case["expected"]:
                field_totals[f] += 1
                field_hits[f] += 0 if (f in row["mismatches"] or f"{f}_not" in row["mismatches"]) else 1
    metrics = {
        "total_cases": total, "passed": passed, "failed": total - passed, "pass_rate": rate(passed, total),
        "field_accuracy": {f: {"checked": field_totals[f], "correct": field_hits[f],
                               "accuracy": rate(field_hits[f], field_totals[f])} for f in CHECKED_FIELDS},
        "failed_case_ids": [r["id"] for r in rows if not r["passed"]],
    }
    return build_report(
        EVAL_NAME,
        dataset_versions={"claim_cases": {"version": cases_doc.get("version"), "case_count": total,
                                          "sha256": sha256_of(cases_doc["cases"])}},
        parameters={"stage": "assess_claim+RiskEngine(deterministic, offline)"},
        metrics=metrics,
        details={"cases": rows},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claim verdict 評估（離線、deterministic）")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    report = run_eval(load_cases(args.cases))
    out = write_report(report, args.output or default_output_path(EVAL_NAME), overwrite=args.overwrite)
    m = report["metrics"]
    print(f"cases={m['total_cases']} passed={m['passed']} failed={m['failed']} failed_ids={m['failed_case_ids']}")
    print(f"報告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
