"""Brand inference quality gate：只使用虛構品牌 / 產品名稱（TEST_ONLY synthetic data）。"""

from datetime import datetime, timezone

import pytest

from app.ingestion.alias_builder import (
    AUTO_ACCEPT_CONFIDENCE, BrandGateStatus, build_aliases, derive_brand_candidates, derive_brand_prefixes,
)
from app.ingestion.tfda_health_food import normalize_record
from app.schemas.enums import BrandCompanyRelation, EntityType, EvidenceStatus

# 其他公司的品名中段都出現「紅果」→ 一般描述詞（虛構）
DESCRIPTOR_FILLERS = {"o1": ["丁紅果飲"], "o2": ["戊紅果片"], "o3": ["己紅果茶"]}


def _gate(companies, short_names=None, cid="c1"):
    [info] = derive_brand_candidates(companies, short_names)[cid]  # 單一前綴鏈 → 只有一個候選
    return info


@pytest.mark.parametrize("names,reason", [
    (["甲片花保健膠囊", "甲片花保健膠囊"], "single_distinct_product"),  # 只有同名重複紀錄
    (["甲片花保健膠囊", "甲片花保健膠囊", "完全不同"], "single_distinct_product"),
])
def test_duplicate_records_are_not_multiple_products(names, reason):
    info = _gate({"c1": names})
    assert info["gate_status"] == BrandGateStatus.REJECTED and reason in info["gate_reasons"]
    assert "c1" not in derive_brand_prefixes({"c1": names})


def test_generic_descriptor_prefix_is_rejected():
    info = _gate({"c1": ["紅果甲錠", "紅果乙粉", "紅果丙飲"], **DESCRIPTOR_FILLERS})
    assert info["gate_status"] == BrandGateStatus.REJECTED and "generic_descriptor" in info["gate_reasons"]


@pytest.mark.parametrize("short_names,status", [
    (None, BrandGateStatus.REVIEW_CANDIDATE),
    ({"c1": "乙丙生技"}, BrandGateStatus.ACCEPTED),  # 與公司簡稱相關 → 信心較高
])
def test_prefix_stops_before_descriptor(short_names, status):
    info = _gate({"c1": ["乙丙紅果粉", "乙丙紅果錠"], **DESCRIPTOR_FILLERS}, short_names)
    assert info["prefix"] == "乙丙"  # 不會切成「乙丙紅果」或「乙丙紅」
    assert info["natural_boundary"] and info["gate_status"] == status


def test_prefix_extending_into_descriptor_is_rejected_when_only_option():
    info = _gate({"c1": ["精丙紅果粉", "精丙紅果粉"], **DESCRIPTOR_FILLERS})
    assert info["gate_status"] == BrandGateStatus.REJECTED


def test_ambiguous_boundary_is_review_candidate():
    info = _gate({"c1": ["庚辛壬癸子粉", "庚辛壬癸子精"]}, {"c1": "庚辛壬癸子生技"})
    assert info["gate_status"] == BrandGateStatus.REVIEW_CANDIDATE
    assert "ambiguous_boundary" in info["gate_reasons"]


@pytest.mark.parametrize("names", [
    ["子丑牌壬癸粉", "子丑牌壬癸精", "子丑牌壬癸飲"],
    ["子丑牌甲粉", "子丑牌乙精", "子丑牌丙飲", "另一款"],
])
def test_brand_marker_gives_natural_boundary(names):
    info = _gate({"c1": names})
    assert info["prefix"] == "子丑牌" and info["brand_marker"] and info["gate_status"] == BrandGateStatus.ACCEPTED


@pytest.mark.parametrize("short_names,expected", [
    (None, (BrandGateStatus.REVIEW_CANDIDATE, 0.6)),  # 只有 2 項品名、無公司名稱支持 → 不自動採用
    ({"c1": "辰巳生技"}, (BrandGateStatus.ACCEPTED, 0.75)),
])
def test_two_product_confidence_threshold(short_names, expected):
    info = _gate({"c1": ["辰巳甲粉", "辰巳乙錠"]}, short_names)
    assert (info["gate_status"], info["confidence"]) == expected
    assert (info["confidence"] >= AUTO_ACCEPT_CONFIDENCE) == (expected[0] == BrandGateStatus.ACCEPTED)


@pytest.mark.parametrize("names,status", [
    (["酉戌甲粉", "酉戌乙錠", "酉戌丙飲", "酉戌丁片"], BrandGateStatus.ACCEPTED),
    (["酉戌甲粉", "酉戌乙錠", "甲一", "乙二", "丙三", "丁四"], BrandGateStatus.REVIEW_CANDIDATE),  # 覆蓋率低
])
def test_coverage_affects_acceptance(names, status):
    assert _gate({"c1": names})["gate_status"] == status


def test_prefix_shared_by_two_companies_is_not_auto_assigned():
    companies = {"c1": ["寅卯甲粉", "寅卯乙錠", "寅卯丙飲"], "c2": ["寅卯丁粉", "寅卯戊錠"]}
    result = derive_brand_candidates(companies, {"c1": "寅卯生技", "c2": "其他生技"})
    for cid in ("c1", "c2"):
        [info] = result[cid]
        assert info["gate_status"] == BrandGateStatus.REVIEW_CANDIDATE
        assert "prefix_shared_with_other_company" in info["gate_reasons"]
    assert derive_brand_prefixes(companies) == {}


@pytest.mark.parametrize("short_names,status", [
    ({}, BrandGateStatus.REJECTED),  # 多家公司共用、沒有公司名稱支持 → 通用詞
    ({"c9": "午未國際"}, BrandGateStatus.REVIEW_CANDIDATE),  # 某申請商名稱含此字樣 → 可能是跨公司品牌
])
def test_prefix_shared_by_many_companies(short_names, status):
    companies = {"c1": ["午未甲粉", "午未乙錠"], "c2": ["午未丙粉", "午未丁錠"], "c3": ["午未戊粉", "午未己錠"]}
    assert _gate(companies, short_names)["gate_status"] == status


# ---------------- build_aliases：採用 / 候選 / evidence status ----------------

def _bundles(company: str, names: list[str]):
    """TEST_ONLY：虛構申請商與產品。"""
    records = [
        {"許可證字號": f"衛部健食字第Z9{i:04d}號", "類別": "衛部健食字", "中文品名": n, "核可日期": "2020/01/01",
         "申請商": company, "證況": "核可", "保健功效相關成分": "測試成分", "保健功效": "測試功效",
         "保健功效宣稱": "TEST_ONLY", "警語": None, "注意事項": None, "網址": None}
        for i, n in enumerate(names)
    ]
    return [normalize_record(r, datetime(2026, 1, 1, tzinfo=timezone.utc)) for r in records]


ACCEPTED_CASE = ("TEST_ONLY 辰巳生技股份有限公司", ["辰巳甲粉", "辰巳乙錠", "辰巳丙飲"])
REVIEW_CASE = ("TEST_ONLY 虛構實業股份有限公司", ["申酉甲粉", "申酉乙錠"])
REJECTED_CASE = ("TEST_ONLY 虛構食品股份有限公司", ["亥子花保健膠囊", "亥子花保健膠囊"])


def test_accepted_brand_is_inferred_never_official():
    built = build_aliases(_bundles(*ACCEPTED_CASE), "src")
    [brand] = built.brands
    assert brand["evidence_status"] == EvidenceStatus.INFERRED != EvidenceStatus.OFFICIAL_RECORD
    assert brand["derivation"] == "DERIVED_BRAND_PREFIX" and 0 < brand["confidence"] < 1
    basis = brand["inference_basis"]
    assert basis["official_brand_field"] is False and basis["gate_status"] == "ACCEPTED"
    assert {"shared", "total", "share", "gate_reasons", "related_to_company_name"} <= basis.keys()
    assert len(built.product_brands) == 3
    assert all(pb["evidence_status"] == EvidenceStatus.INFERRED for pb in built.product_brands)
    [cand] = built.brand_candidates
    assert cand["is_inferred"] and cand["source_id"] == "src"


@pytest.mark.parametrize("case,status", [(REVIEW_CASE, "REVIEW_CANDIDATE"), (REJECTED_CASE, "REJECTED")])
def test_uncertain_brand_kept_as_candidate_but_not_adopted(case, status):
    built = build_aliases(_bundles(*case), "src")
    assert built.brands == [] and built.product_brands == []
    assert not [a for a in built.aliases if a["entity_type"] == EntityType.BRAND]  # Agent B 無法解析 → Reporter 看不到
    [cand] = built.brand_candidates
    assert cand["gate_status"] == status and cand["evidence_status"] == EvidenceStatus.INFERRED
    assert cand["inference_basis"]["gate_reasons"] and cand["confidence"] < 1


def test_manual_review_adopts_candidate_as_manual_reviewed():
    bundles = _bundles(*REVIEW_CASE)
    cid = bundles[0].company["id"]
    built = build_aliases(bundles, "src", [{"company_id": cid, "brand": "申酉", "reviewer_note": "TEST_ONLY"}])
    [brand] = built.brands
    assert brand["evidence_status"] == EvidenceStatus.MANUAL_REVIEWED and brand["derivation"] == "MANUAL_REVIEWED"
    assert brand["inference_basis"]["gate_status"] == "REVIEW_CANDIDATE"  # 保留原本 gate 結果
    assert all(pb["evidence_status"] == EvidenceStatus.MANUAL_REVIEWED for pb in built.product_brands)
    assert not built.brand_candidates[0]["is_inferred"]


# ---------------- 一家公司 0..N 個品牌 ----------------

TWO_BRANDS = ["子丑牌甲粉", "子丑牌乙精", "子丑牌丙飲", "寅卯牌甲粉", "寅卯牌乙精", "寅卯牌丙飲"]


def test_company_can_have_multiple_accepted_brands():
    infos = derive_brand_candidates({"c1": TWO_BRANDS})["c1"]
    assert sorted(i["prefix"] for i in infos) == ["子丑牌", "寅卯牌"]
    assert all(i["gate_status"] == BrandGateStatus.ACCEPTED for i in infos)
    assert sorted(i["prefix"] for i in derive_brand_prefixes({"c1": TWO_BRANDS})["c1"]) == ["子丑牌", "寅卯牌"]


def test_multiple_brands_are_built_with_disjoint_products():
    built = build_aliases(_bundles("TEST_ONLY 虛構多品牌股份有限公司", TWO_BRANDS), "src")
    assert len(built.brands) == 2 and len({b["company_id"] for b in built.brands}) == 1
    assert len(built.product_brands) == 6
    assert len({pb["product_id"] for pb in built.product_brands}) == 6  # 每項產品只屬於一個推導品牌
    by_brand = {b["id"]: b["name"] for b in built.brands}
    for pb in built.product_brands:
        assert pb["evidence_status"] == EvidenceStatus.INFERRED and 0 < pb["confidence"] < 1
        assert pb["inference_basis"]["prefix"] == by_brand[pb["brand_id"]]


def test_nested_prefixes_are_one_brand_not_many():
    infos = derive_brand_candidates({"c1": ["子丑牌甲粉", "子丑牌乙精", "子丑牌丙飲"]})["c1"]
    assert [i["prefix"] for i in infos] == ["子丑牌"]  # 不會再另立「子丑」


def test_each_candidate_passes_gate_independently():
    # 第二條前綴鏈只有 2 項品名、無公司名稱支持 → REVIEW；不因同公司已有 ACCEPTED 而被放寬或被刪除
    names = ["子丑牌甲粉", "子丑牌乙精", "子丑牌丙飲", "辰巳甲粉", "辰巳乙錠"]
    infos = {i["prefix"]: i for i in derive_brand_candidates({"c1": names})["c1"]}
    assert infos["子丑牌"]["gate_status"] == BrandGateStatus.ACCEPTED
    assert infos["辰巳"]["gate_status"] == BrandGateStatus.REVIEW_CANDIDATE
    built = build_aliases(_bundles("TEST_ONLY 虛構實業股份有限公司", names), "src")
    assert [b["name"] for b in built.brands] == ["子丑牌"]
    assert {c["name"]: c["gate_status"] for c in built.brand_candidates} == {"子丑牌": "ACCEPTED", "辰巳": "REVIEW_CANDIDATE"}


# ---------------- Company → Brand 關係語意 ----------------

def test_inferred_brand_is_only_associated_not_owned():
    built = build_aliases(_bundles(*ACCEPTED_CASE), "src")
    assert {b["company_relation"] for b in built.brands} == {BrandCompanyRelation.ASSOCIATED_WITH_BRAND}
    assert {c["company_relation"] for c in built.brand_candidates} == {BrandCompanyRelation.ASSOCIATED_WITH_BRAND}


@pytest.mark.parametrize("review,relation", [
    ({"reviewer_note": "TEST_ONLY"}, BrandCompanyRelation.ASSOCIATED_WITH_BRAND),  # 只確認品牌，未確認擁有
    ({"reviewer_note": "TEST_ONLY", "ownership_evidence": {"source_url": "https://example.invalid/TEST_ONLY"}},
     BrandCompanyRelation.OWNS_BRAND),
])
def test_owns_brand_requires_manual_review_with_ownership_evidence(review, relation):
    bundles = _bundles(*REVIEW_CASE)
    cid = bundles[0].company["id"]
    [brand] = build_aliases(bundles, "src", [{"company_id": cid, "brand": "申酉", **review}]).brands
    assert brand["company_relation"] == relation
    assert ("ownership_evidence" in brand["inference_basis"]) == (relation == BrandCompanyRelation.OWNS_BRAND)
