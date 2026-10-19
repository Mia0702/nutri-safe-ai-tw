"""評估工具本身（scripts/eval）的測試：deterministic 抽樣、指標計算、JSON 輸出、wrong-auto-confirm 計數、不寫 production。

全部使用虛構 TEST_ONLY 資料；不連 OpenAI / Supabase / Neo4j。
"""

import json
import re
from pathlib import Path

import pytest

from scripts.eval import claim_eval, controller_eval, gpt_baseline, latency_report, resolver_eval
from scripts.eval.common import (
    EVALUATION_VERSION,
    EvalWriteAttempt,
    ReadOnlySupabase,
    build_report,
    deterministic_sample,
    rate,
    write_report,
)
from scripts.eval.resolver_eval import (
    AUTO_CONFIRM,
    CLARIFY,
    NOT_FOUND,
    CaseResult,
    CatalogEntry,
    CatalogRepo,
    EvalCase,
    compute_metrics,
    generate_cases,
    resolve_case,
    run_eval,
)

NAMES = ["虛構評估甲強化膠囊", "虛構評估乙鈣片錠劑", "虛構評估丙魚油軟膠囊", "虛構評估丁維他命飲",
         "虛構評估戊葉黃素膠囊", "虛構評估己益生菌粉包", "虛構評估庚蔓越莓錠", "虛構評估辛薑黃萃取膠囊"]


def catalog() -> list[CatalogEntry]:
    return [CatalogEntry(product_id=f"eval-p{i:02d}", name=n, permit_number=f"TEST_ONLY字第Z{i:05d}號",
                         category="health_food") for i, n in enumerate(NAMES, start=1)]


def case(variant="EXACT_NAME", expected=("eval-p01",), pid="eval-p01") -> EvalCase:
    return EvalCase(case_id=f"{pid}:{variant}", product_id=pid, variant=variant, query="q",
                    expected_ids=frozenset(expected))


def result(outcome, *, confirmed=None, candidates=(), expected=("eval-p01",), variant="OMIT_ONE_CHAR") -> CaseResult:
    c = case(variant, expected)
    return CaseResult(c, outcome, confirmed, tuple(candidates), None,
                      correct=(confirmed in c.expected_ids) if outcome == AUTO_CONFIRM else None)


# ---------------------------------------------------------------- deterministic 抽樣


def test_sample_is_deterministic_and_independent_of_input_order():
    items = list(range(100))
    a = deterministic_sample(items, 10, 7, key=lambda x: x)
    b = deterministic_sample(list(reversed(items)), 10, 7, key=lambda x: x)
    assert a == b and len(a) == 10 and a == sorted(a)
    assert deterministic_sample(items, 10, 8, key=lambda x: x) != a  # 不同 seed → 不同樣本
    assert deterministic_sample(items, 500, 7, key=lambda x: x) == items  # 樣本數大於母體 → 全部


def test_generated_cases_are_reproducible_for_same_seed():
    cat = catalog()
    sample = deterministic_sample(cat, 5, 123, key=lambda e: e.product_id)
    first, _ = generate_cases(cat, sample, 123)
    second, _ = generate_cases(cat, sample, 123)
    assert [(c.case_id, c.query) for c in first] == [(c.case_id, c.query) for c in second]
    assert {c.variant for c in first} >= {"EXACT_NAME", "PERMIT_NUMBER", "OMIT_ONE_CHAR"}


def test_mutation_that_equals_another_official_name_is_excluded():
    cat = [CatalogEntry("eval-a", "虛構評估甲強化膠囊", "TEST_ONLY字第Z00001號", "x"),
           CatalogEntry("eval-b", "虛構評估強化膠囊", "TEST_ONLY字第Z00002號", "x")]
    cases, skipped = generate_cases(cat, [cat[0]], 1, variants=("OMIT_ONE_CHAR",))
    # 省略「甲」的某個字若剛好得到另一個官方品名，ground truth 不明 → 排除並計數（不是 OMIT 結果的任一種都算錯）
    assert all(c.query != "虛構評估強化膠囊" for c in cases) or skipped


# ---------------------------------------------------------------- 指標計算 / wrong auto-confirm 計數


def test_metric_calculation_counts_wrong_auto_confirms():
    results = [
        result(AUTO_CONFIRM, confirmed="eval-p01"),                      # 正確
        result(AUTO_CONFIRM, confirmed="eval-p99"),                      # 錯誤自動確認
        result(AUTO_CONFIRM, confirmed="eval-p99"),                      # 錯誤自動確認
        result(CLARIFY, candidates=["eval-p01", "eval-p02"]),            # 候選含正確
        result(CLARIFY, candidates=["eval-p02"]),                        # 候選不含正確
        result(NOT_FOUND),
        result(AUTO_CONFIRM, confirmed="eval-p01", variant="EXACT_NAME"),  # exact 正確
        result(NOT_FOUND, variant="PERMIT_NUMBER"),                       # exact 卻查無 → 不正確
    ]
    m = compute_metrics(results)
    assert m["total_cases"] == 8
    assert m["wrong_auto_confirm_count"] == 2 and m["correct_auto_confirm_count"] == 2
    assert m["WRONG_AUTO_CONFIRM_RATE"] == rate(2, 8) == 0.25
    assert m["wrong_auto_confirm_rate_among_auto_confirms"] == 0.5
    assert m["clarification_count"] == 2 and m["clarification_candidate_recall"] == 0.5
    assert m["not_found_count"] == 2
    assert m["exact_cases"] == 2 and m["exact_match_correct_count"] == 1 and m["exact_match_correctness"] == 0.5
    assert m["by_variant"]["OMIT_ONE_CHAR"]["wrong_auto_confirm"] == 2


def test_rate_with_zero_denominator_is_none_not_zero():
    assert rate(0, 0) is None
    assert compute_metrics([])["WRONG_AUTO_CONFIRM_RATE"] is None


class WrongPermitRepo(CatalogRepo):
    """故意讓字號查詢回傳另一個（字號相同的）產品：評估必須把它計為 wrong auto-confirm。"""

    def get_products_by_permit_code(self, permit_code):
        return [CatalogEntry("eval-other", "虛構評估他牌錠", "TEST_ONLY字第Z00001號", "health_food").to_row()]


def test_resolve_case_flags_a_wrong_auto_confirm():
    c = EvalCase("eval-p01:PERMIT_NUMBER", "eval-p01", "PERMIT_NUMBER", "TEST_ONLY字第Z00001號",
                 frozenset({"eval-p01"}), is_permit_query=True)
    r = resolve_case(WrongPermitRepo(catalog()), c, [])
    assert r.outcome == AUTO_CONFIRM and r.confirmed_id == "eval-other" and r.correct is False
    assert compute_metrics([r])["wrong_auto_confirm_count"] == 1


def test_exact_name_and_permit_resolve_to_the_right_product():
    cat = catalog()
    repo = CatalogRepo(cat)
    exact = resolve_case(repo, EvalCase("e", "eval-p03", "EXACT_NAME", NAMES[2], frozenset({"eval-p03"})), [])
    permit = resolve_case(repo, EvalCase("p", "eval-p04", "PERMIT_NUMBER", "TEST_ONLY字第Z00004號",
                                         frozenset({"eval-p04"}), is_permit_query=True), [])
    assert (exact.outcome, exact.confirmed_id, exact.correct) == (AUTO_CONFIRM, "eval-p03", True)
    assert (permit.outcome, permit.confirmed_id, permit.correct) == (AUTO_CONFIRM, "eval-p04", True)


# ---------------------------------------------------------------- JSON 報告


def test_resolver_report_is_json_with_required_metadata_and_is_reproducible():
    cat = catalog()
    r1 = run_eval(cat, CatalogRepo(cat), sample_size=6, seed=42, source_label="unit-test")
    r2 = run_eval(cat, CatalogRepo(cat), sample_size=6, seed=42, source_label="unit-test")
    text = json.dumps(r1, ensure_ascii=False)  # 必須可序列化
    assert json.loads(text)["evaluation_version"] == EVALUATION_VERSION
    for key in ("created_at", "git_commit", "dataset_versions", "parameters", "metrics"):
        assert key in r1
    assert r1["parameters"]["seed"] == 42 and r1["parameters"]["sample_size"] == 6
    assert r1["dataset_versions"]["product_catalog"]["product_count"] == len(cat)
    assert r1["metrics"] == r2["metrics"]  # deterministic
    assert r1["dataset_versions"] == r2["dataset_versions"]
    assert "WRONG_AUTO_CONFIRM_RATE" in r1["metrics"]


def test_report_contains_no_secret_looking_values(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value-1234567890")
    cat = catalog()
    text = json.dumps(run_eval(cat, CatalogRepo(cat), sample_size=3, seed=1), ensure_ascii=False)
    assert "sk-test-secret" not in text and "SUPABASE" not in text.upper().replace("SUPABASE:PRODUCT_DETAILS", "")


def test_write_report_never_overwrites_existing_baseline_by_default(tmp_path):
    path = tmp_path / "baseline.json"
    first = build_report("t", dataset_versions={}, parameters={}, metrics={"a": 1})
    write_report(first, path)
    with pytest.raises(FileExistsError):
        write_report(build_report("t", dataset_versions={}, parameters={}, metrics={"a": 2}), path)
    assert json.loads(path.read_text(encoding="utf-8"))["metrics"] == {"a": 1}  # 舊 baseline 未被改動
    write_report(build_report("t", dataset_versions={}, parameters={}, metrics={"a": 3}), path, overwrite=True)
    assert json.loads(path.read_text(encoding="utf-8"))["metrics"] == {"a": 3}


# ---------------------------------------------------------------- 不寫 production


class FakeBuilder:
    def select(self, *a, **k):
        return self

    def insert(self, *a, **k):  # pragma: no cover - 不應被呼叫到
        raise AssertionError("write reached the underlying client")


class FakeService:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return FakeBuilder()

    def select(self, builder):
        self.calls.append("select")
        return [{"ok": True}]

    def rpc(self, fn, params):
        self.calls.append(f"rpc:{fn}")
        return []

    def source_info(self, source_id):
        return {}

    def is_project_database(self):
        return True

    def upsert(self, *a, **k):  # pragma: no cover
        raise AssertionError("write reached the service")

    update = insert = upsert


def test_read_only_wrapper_blocks_every_write_path():
    service = FakeService()
    db = ReadOnlySupabase(service)
    assert db.select(db.table("product_details").select("*")) == [{"ok": True}]
    assert db.rpc("search_products", {}) == []
    for write in (lambda: db.upsert("t", []), lambda: db.update("t", {}, {}), lambda: db.insert("t", []),
                  lambda: db.table("t").insert([]), lambda: db.table("t").delete(), lambda: db.rpc("some_write_fn", {})):
        with pytest.raises(EvalWriteAttempt):
            write()
    assert service.calls == ["select", "rpc:search_products"]  # 寫入嘗試沒有任何一個到達底層服務


def test_eval_code_contains_no_write_calls():
    """靜態檢查：scripts/eval 不得出現資料庫 / 圖寫入呼叫（唯讀包裝中的 raising stub 是 def，不是呼叫）。"""
    root = Path(__file__).resolve().parents[2] / "scripts" / "eval"
    offenders = []
    for py in root.glob("*.py"):
        for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#")[0]
            if "def " in code or "raise " in code or "不允許" in code or "digest.update(" in code:
                continue
            if re.search(r"\.(upsert|insert|delete)\(", code) or re.search(r"(?<!_)\.update\((?!\{)", code):
                offenders.append(f"{py.name}:{n}: {line.strip()}")
    assert not offenders, offenders
    sources = "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    assert "Neo4jService" not in sources and "neo4j_service" not in sources  # 評估完全不碰 Neo4j


# ---------------------------------------------------------------- claim eval（既有 deterministic 規則的回歸）


def test_claim_eval_bundled_cases_pass_and_report_is_json():
    report = claim_eval.run_eval(claim_eval.load_cases())
    json.dumps(report, ensure_ascii=False)
    assert report["metrics"]["failed_case_ids"] == [], report["metrics"]
    assert report["metrics"]["total_cases"] >= 8


def test_claim_compare_detects_mismatch_and_negative_expectation():
    actual = {"policy_result": "APPROVED_MATCH", "status": "CONSISTENT_WITH_OFFICIAL", "claim_risk": "LOW",
              "claim_role": "ADVERTISEMENT_CLAIM", "risk_level": "LOW"}
    assert claim_eval.compare({"claim_risk": "LOW", "risk_level_not": "HIGH"}, actual) == []
    assert claim_eval.compare({"claim_risk": "HIGH"}, actual) == ["claim_risk"]
    assert claim_eval.compare({"risk_level_not": "LOW"}, actual) == ["risk_level_not"]


# ---------------------------------------------------------------- controller eval（離線 harness；live 預設不執行）


def test_controller_scoring_with_a_stub_extractor():
    doc = controller_eval.load_cases()

    def perfect(case):
        e = case["expected"]
        return {"intent": e.get("intent"), "product_candidates": e.get("product_candidates", []),
                "claim_roles": e.get("claim_roles", []), "image_relevance": e.get("image_relevance")}

    report = controller_eval.run_scoring(doc, perfect)
    assert report["metrics"]["case_accuracy"] == 1.0
    assert report["metrics"]["skipped"]["requires_media_not_supported_in_this_phase"] >= 1

    def wrong_intent(case):
        return {**perfect(case), "intent": "unclear"}

    bad = controller_eval.run_scoring(doc, wrong_intent)
    assert bad["metrics"]["field_accuracy"]["intent"]["accuracy"] == 0.0
    assert bad["metrics"]["field_accuracy"]["product_candidates"]["accuracy"] == 1.0


def test_controller_product_candidates_compare_ignores_format_not_content():
    s = controller_eval.score_case({"product_candidates": ["虛構 牌甲"], "claim_roles": ["A", "B"]},
                                   {"product_candidates": ["虛構牌甲"], "claim_roles": ["B", "A"]})
    assert s == {"product_candidates": True, "claim_roles": True}
    assert controller_eval.score_case({"product_candidates": ["甲"]}, {"product_candidates": ["甲", "乙"]}) == {
        "product_candidates": False}


def test_controller_live_requires_explicit_opt_in(capsys, monkeypatch):
    def boom():  # pragma: no cover
        raise AssertionError("live extractor must not be built without --live --confirm-live")

    monkeypatch.setattr(controller_eval, "live_extractor", boom)
    assert controller_eval.main([]) == 3
    assert controller_eval.main(["--live"]) == 3  # 只有 --live 不夠


# ---------------------------------------------------------------- latency


def test_latency_report_groups_by_planner_mode_with_p50_p95_max():
    lines = []
    for ms in (1000, 2000, 3000, 4000, 100000):
        lines.append(json.dumps({"msg": "analysis_done", "planner_mode": "structured_fast_path", "input_type": "text",
                                 "duration_ms": ms, "rag_ms": 0}))
    lines.append(json.dumps({"msg": "analysis_done", "planner_mode": "llm_planner", "input_type": "url", "duration_ms": 50000}))
    lines.append(json.dumps({"msg": "analysis_done", "duration_ms": 7}))           # 沒有 planner_mode
    lines += ["not json", json.dumps({"msg": "something_else", "duration_ms": 1}), "{broken"]
    records = latency_report.parse_log_lines(lines)
    assert len(records) == 7
    m = latency_report.build_latency_metrics(records)
    fast = m["by_planner_mode"]["structured_fast_path"]
    assert fast == {"count": 5, "p50_ms": 3000, "p95_ms": 100000, "max_ms": 100000}
    assert m["by_planner_mode"]["llm_planner"]["count"] == 1
    assert m["by_planner_mode"][latency_report.UNGROUPED]["count"] == 1
    assert m["overall"]["count"] == 7 and m["known_missing_fields"]


def test_percentile_nearest_rank():
    values = list(range(1, 21))
    assert latency_report.percentile_nearest_rank(values, 95) == 19
    assert latency_report.percentile_nearest_rank(values, 100) == 20
    assert latency_report.summarize([]) == {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}


def test_latency_report_from_files_is_json(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(json.dumps({"msg": "analysis_done", "planner_mode": "m", "duration_ms": 10, "request_id": "r1"}) + "\n",
                   encoding="utf-8")
    report = latency_report.run_report([log])
    json.dumps(report)
    assert report["dataset_versions"]["logs"]["analysis_done_records"] == 1


# ---------------------------------------------------------------- GPT baseline 結構（不呼叫 GPT）


def test_gpt_baseline_template_has_both_systems_and_no_answers():
    cases = [gpt_baseline.GoldCase(case_id="g1", category="product_identity", input_text="虛構測試問題",
                                   gold=gpt_baseline.GoldFacts(product_exists_in_official_data=False))]
    report = gpt_baseline.build_template(cases)
    answers = report["details"]["answers"]
    assert {a["system"] for a in answers} == {"vanilla_gpt", "nutrisafe"}
    assert all(a["answer_text"] is None for a in answers)
    assert report["parameters"]["live_calls"] is False and report["metrics"]["answers_filled"] == 0
