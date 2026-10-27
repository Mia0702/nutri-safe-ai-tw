"""Identity alias 評估集（baseline-002）：deterministic 抽樣、判定規則、目前（pre-D3）行為不得誤確認。

虛構 TEST_ONLY 資料；離線 CatalogRepo；不連 OpenAI / Supabase / Neo4j。
"""

import json

from app.ingestion import product_alias_review as par
from scripts.eval import identity_alias_eval as ev
from scripts.eval.resolver_eval import AUTO_CONFIRM, CLARIFY, NOT_FOUND, CaseResult, CatalogEntry, CatalogRepo, EvalCase
from tests.unit.test_product_alias_review import RECORDS, brand_records, rec


def overlay(tmp_path, records):
    cands = par.build_product_candidates(records, "1.0")
    brands = par.build_brand_candidates(records, "1.0")
    par.write_review_csv(tmp_path / "candidates.csv", cands, par.PRODUCT_EXTRA_COLUMNS)
    par.write_review_csv(tmp_path / "brand_candidates.csv", brands, par.BRAND_EXTRA_COLUMNS)
    (tmp_path / "overlay_manifest.json").write_text(json.dumps({"overlay_version": "1.0"}), encoding="utf-8")
    return tmp_path


def catalog(records):
    return [CatalogEntry(product_id=r.product_id, name=r.name_zh, permit_number=r.permit_number,
                         category="imported_tablet_capsule", company_name=r.applicant) for r in records]


MANY = RECORDS + brand_records(6) + [rec(400 + i, f"測試牌甲{i}號", f"Common Word Item {i}", applicant=f"TEST_ONLY公司{i}")
                                     for i in range(12)]


def test_gold_set_is_deterministic_and_covers_all_groups(tmp_path):
    d = overlay(tmp_path, MANY)
    a, _ = ev.build_gold_cases(d, catalog(MANY), seed=7, sizes={g: 5 for g in ev.GROUPS})
    b, _ = ev.build_gold_cases(d, catalog(list(reversed(MANY))), seed=7, sizes={g: 5 for g in ev.GROUPS})
    assert [(c.case_id, c.query, sorted(c.expected_ids)) for c in a] == [(c.case_id, c.query, sorted(c.expected_ids)) for c in b]
    assert {c.variant for c in a} == set(ev.GROUPS)
    other, _ = ev.build_gold_cases(d, catalog(MANY), seed=8, sizes={g: 2 for g in ev.GROUPS})
    assert len(other) < len(a)


def test_gold_group_semantics(tmp_path):
    cases, info = ev.build_gold_cases(overlay(tmp_path, MANY), catalog(MANY), seed=1, sizes={g: 50 for g in ev.GROUPS})
    by = {g: [c for c in cases if c.variant == g] for g in ev.GROUPS}
    assert all(len(c.expected_ids) == 1 for c in by[ev.UNIQUE_EN_EXACT])
    assert {c.product_id for c in by[ev.UNIQUE_EN_EXACT]}.isdisjoint({"pid-007", "pid-008"})  # runtime key 碰撞者不當 unique
    colliding = by[ev.COLLIDING_EN_EXACT]
    assert len(colliding) == 1 and colliding[0].expected_ids == frozenset({"pid-003", "pid-004"})
    assert all(not c.expected_ids for c in by[ev.BRAND_EN_ONLY] + by[ev.GENERIC_EN_TOKEN])
    assert all(c.is_permit_query for c in by[ev.PERMIT_CONTROL]) and not any(c.is_permit_query for c in by[ev.ZH_NAME_CONTROL])
    assert info["pool_sizes"][ev.COLLIDING_EN_EXACT] == 1


def result(group, outcome, *, correct=None, expected=("p1",), candidates=(), query="two words"):
    case = EvalCase(case_id=f"{group}:x", product_id="p1", variant=group, query=query, expected_ids=frozenset(expected))
    return CaseResult(case, outcome, "p1" if outcome == AUTO_CONFIRM else None, tuple(candidates), None, correct=correct)


def test_judge_rules():
    assert ev.judge(result(ev.UNIQUE_EN_EXACT, AUTO_CONFIRM, correct=True)) == {"target_met": True, "safety_ok": True}
    assert ev.judge(result(ev.UNIQUE_EN_EXACT, AUTO_CONFIRM, correct=False))["safety_ok"] is False
    assert ev.judge(result(ev.UNIQUE_EN_EXACT, NOT_FOUND)) == {"target_met": False, "safety_ok": True}   # 目前：不足但安全
    # 單一詞的唯一官方英文名（D3 核准規則）：不得自動確認（即使正確也是違規）；候選含正確產品才算達標
    one = dict(query="oneword")
    assert ev.judge(result(ev.UNIQUE_EN_EXACT, AUTO_CONFIRM, correct=True, **one)) == {"target_met": False, "safety_ok": False}
    assert ev.judge(result(ev.UNIQUE_EN_EXACT, CLARIFY, candidates=("p1",), **one)) == {"target_met": True, "safety_ok": True}
    assert ev.judge(result(ev.UNIQUE_EN_EXACT, CLARIFY, candidates=("p9",), **one)) == {"target_met": False, "safety_ok": True}
    # 碰撞：任何自動確認都違規（即使剛好是群內其中一個）
    assert ev.judge(result(ev.COLLIDING_EN_EXACT, AUTO_CONFIRM, correct=True))["safety_ok"] is False
    assert ev.judge(result(ev.COLLIDING_EN_EXACT, CLARIFY, candidates=("p1", "p2")))["target_met"] is True
    assert ev.judge(result(ev.COLLIDING_EN_EXACT, NOT_FOUND)) == {"target_met": False, "safety_ok": True}
    for g in (ev.BRAND_EN_ONLY, ev.GENERIC_EN_TOKEN):
        assert ev.judge(result(g, AUTO_CONFIRM, correct=False, expected=()))["safety_ok"] is False
        assert ev.judge(result(g, CLARIFY, expected=())) == {"target_met": True, "safety_ok": True}
    assert ev.judge(result(ev.ZH_NAME_CONTROL, CLARIFY, candidates=("p1",)))["target_met"] is True
    assert ev.judge(result(ev.PERMIT_CONTROL, AUTO_CONFIRM, correct=False))["safety_ok"] is False


def test_pre_d3_behavior_never_auto_confirms_english_aliases(tmp_path):
    """目前 runtime 不搜尋英文品名：英文查詢不得被自動確認；中文 / 字號對照組要全部正確。"""
    d = overlay(tmp_path, MANY)
    cat = catalog(MANY)
    cases, _ = ev.build_gold_cases(d, cat, seed=3, sizes={g: 20 for g in ev.GROUPS})
    report = ev.run_pre_d3(CatalogRepo(cat), cases, label="test", seed=3, gold_sha="x")
    m = report["metrics"]
    assert m["SAFETY_VIOLATIONS"] == 0 and m["WRONG_AUTO_CONFIRM_RATE"] == 0.0
    assert m["by_group"][ev.UNIQUE_EN_EXACT]["outcomes"]["AUTO_CONFIRM"] == 0
    assert m["by_group"][ev.ZH_NAME_CONTROL]["target_met_rate"] == 1.0
    assert m["by_group"][ev.PERMIT_CONTROL]["target_met_rate"] == 1.0
    assert "d3" in json.dumps(ev.D3_SAFETY_GATES).lower() or ev.D3_SAFETY_GATES["precondition"]


def test_gold_set_report_records_gates_and_version(tmp_path):
    d = overlay(tmp_path, MANY)
    cases, info = ev.build_gold_cases(d, catalog(MANY), seed=3, sizes={g: 3 for g in ev.GROUPS})
    gold = ev.gold_set_report(cases, info, seed=3, overlay_dir=d, snapshot_dir=tmp_path, catalog_size=len(MANY))
    assert gold["parameters"]["d3_safety_gates"]["baseline-001"]["WRONG_AUTO_CONFIRM_RATE"]
    assert gold["metrics"]["total_cases"] == len(cases) == len(gold["details"]["cases"])
    assert gold["dataset_versions"]["overlay"]["overlay_version"] == "1.0"
