"""Resolver identity safety（2026-10-04，使用者同意調整 FC-B1 / FC-B2 / FC-B3）。全部 TEST_ONLY 虛構名稱。

原則：wrong auto-confirm 比多一次確認更危險。
* 強證據（字號 / 官方品名 / 官方別名完全相同）→ VERIFIED；名稱＋公司 / 品牌佐證 → 依既有規則。
* 純名稱相似（NAME_SIMILARITY）只有 A（只有省略字、沒有替換字）＋ B（沒有其他品名也包含同一段文字）＋
  C（第二名 < 0.80 或領先 >= 0.10）全部成立才自動確認；否則請使用者確認。
"""

from app.normalization import name_match_score
from app.risk.assessment_rules import (
    SIMILARITY_RUNNER_UP_CEILING,
    SIMILARITY_SAFE_GAP,
    VERIFIED_MIN_SCORE,
    resolve_product_match,
)
from app.schemas.enums import ProductAmbiguityReason, ProductCategory, ProductMatchType, ProductVerificationStatus, SourceType
from app.schemas.evidence import OFFICIAL_ALIAS_MATCHED_BY, ProductCandidateMatch, ProductRecord, SourceRef
from tests.builders import request

SRC = SourceRef(source_name="TEST_ONLY", source_type=SourceType.STRUCTURED_DB, is_official=True)
V = ProductVerificationStatus


def cand(pid, name, query, *, category=ProductCategory.IMPORTED_TABLET_CAPSULE, permit=None, by="product_name",
         company=None):
    record = ProductRecord(product_id=pid, product_name_zh=name, permit_number=permit, source=SRC,
                           product_category=category, company_name=company)
    score = 1.0 if by == OFFICIAL_ALIAS_MATCHED_BY else name_match_score(query, name)
    return ProductCandidateMatch(product=record, match_score=score, matched_by=by, query=query)


def resolve(query, names, **req):
    matches = [cand(f"p{i}", n, query) for i, n in enumerate(names)]
    return resolve_product_match(request(product_candidates=[query], **req), matches, [], [], [query], True)


def asks_user(m) -> bool:
    """請使用者確認：沒有自動確認、沒有 best_match 被當成答案（多候選）或只有一個待確認候選。"""
    return m.status == V.PARTIAL_MATCH and (m.is_ambiguous or m.needs_confirmation)


# ============================================================ 強證據：可確認


def test_exact_canonical_name_verifies():
    m = resolve("虛構審查甲膠囊", ["虛構審查甲膠囊", "虛構審查甲膠囊食品", "虛構審查乙膠囊"])
    assert m.status == V.VERIFIED and m.match_type == ProductMatchType.CANONICAL_EXACT and m.best_match.product.product_id == "p0"


def test_exact_official_alias_verifies():
    alias = cand("pa", "虛構審查正式品名錠", "虛構審查俗稱", by=OFFICIAL_ALIAS_MATCHED_BY)
    m = resolve_product_match(request(product_candidates=["虛構審查俗稱"]), [alias], [], [], [], True)
    assert m.status == V.VERIFIED and m.match_type == ProductMatchType.OFFICIAL_ALIAS_EXACT


def test_exact_registration_number_verifies():
    rec = cand("pr", "虛構審查字號錠", "x", permit="0099001234")
    m = resolve_product_match(request(permit_number_candidates=["0099001234"]), [],
                              [rec.model_copy(update={"matched_by": "permit_number"})], [], [], True)
    assert m.status == V.VERIFIED and m.match_type == ProductMatchType.PERMIT_NUMBER


def test_name_company_corroboration_does_not_regress():
    """名稱分數接近的兩個候選，只有一項對應到使用者給的公司 → 既有 NAME_COMPANY_CORROBORATED 規則不變。"""
    q = "虛構審查佐證膠囊"
    a = cand("pa", "虛構審查佐證膠囊甲", q, company="虛構審查甲公司")
    b = cand("pb", "虛構審查佐證膠囊乙", q, company="虛構審查乙公司")
    m = resolve_product_match(request(product_candidates=[q], company_candidates=["虛構審查甲公司"]), [a, b], [], [], [q], True)
    assert m.match_type == ProductMatchType.NAME_COMPANY_CORROBORATED and m.best_match.product.product_id == "pa"
    assert m.status == V.VERIFIED


# ============================================================ 純名稱相似：安全時才確認


def test_safe_omission_unique_candidate_large_margin_verifies():
    m = resolve("虛構審查獨特配方營養", ["虛構審查獨特配方營養包", "虛構無關品名錠"])
    assert m.status == V.VERIFIED and m.match_type == ProductMatchType.NAME_SIMILARITY and m.best_match.product.product_id == "p0"


def test_gap_of_at_least_010_with_safe_omission_verifies():
    names = ["虛構審查甲乙丙丁戊包", "虛構審查甲乙丙丁壬癸"]
    query = "虛構審查甲乙丙丁戊"
    top, second = (name_match_score(query, n) for n in names)
    assert top >= VERIFIED_MIN_SCORE and second >= SIMILARITY_RUNNER_UP_CEILING and top - second >= SIMILARITY_SAFE_GAP
    m = resolve(query, names)
    assert m.status == V.VERIFIED and m.best_match.product.product_id == "p0"


def test_one_substituted_character_asks_for_confirmation():
    m = resolve("虛構審查獨特甲方營養包", ["虛構審查獨特配方營養包", "虛構無關品名錠"])
    assert name_match_score("虛構審查獨特甲方營養包", "虛構審查獨特配方營養包") >= VERIFIED_MIN_SCORE
    assert asks_user(m) and m.status != V.VERIFIED
    assert m.ambiguity_reason == ProductAmbiguityReason.LOW_MATCH_CONFIDENCE and m.best_match.product.product_id == "p0"


def test_typo_closer_to_a_wrong_shorter_product_asks_for_choice():
    names = ["虛構順軟膠囊", "虛構順通軟膠囊"]  # 使用者要的是第二項，第三個字誤寫
    m = resolve("虛構順甲軟膠囊", names)
    assert asks_user(m) and m.is_ambiguous and m.best_match is None
    assert {c.product.product_id for c in m.candidates} == {"p0", "p1"}  # 正確的那一項在候選中


def test_top_gap_0057_asks_for_choice():
    names = ["虛構順軟膠囊", "虛構順通軟膠囊"]
    s0, s1 = (name_match_score("虛構順甲軟膠囊", n) for n in names)
    assert 0.05 < s0 - s1 < 0.06
    assert resolve("虛構順甲軟膠囊", names).is_ambiguous


def test_generic_substring_in_multiple_products_asks_for_choice():
    names = ["虛構綜合維他命錠", "虛構綜合維他命膜衣錠", "虛構綜合維他命軟膠囊"]
    m = resolve("虛構綜合維他命", names)
    assert m.is_ambiguous and m.best_match is None and len(m.candidates) == 3


def test_runner_up_above_080_with_gap_below_010_asks_for_choice():
    names = ["虛構活力女性配方營養包", "虛構活力男性配方營養包"]
    query = "虛構活力女性配方營養"
    top, second = (name_match_score(query, n) for n in names)
    assert top >= VERIFIED_MIN_SCORE and second >= SIMILARITY_RUNNER_UP_CEILING and top - second < SIMILARITY_SAFE_GAP
    m = resolve(query, names)
    assert m.is_ambiguous and m.best_match is None


def test_typo_on_the_discriminating_character_asks_for_choice():
    m = resolve("虛構活力甲性配方營養包", ["虛構活力女性配方營養包", "虛構活力男性配方營養包"])
    assert m.is_ambiguous and m.best_match is None


def test_two_high_similarity_candidates_within_margin_ask_for_choice():
    m = resolve("虛構魚油膠囊食", ["虛構魚油膠囊食品", "虛構魚油軟膠囊食品"])
    assert m.is_ambiguous and m.ambiguity_reason == ProductAmbiguityReason.MULTIPLE_SIMILAR_NAMES


def test_same_name_multiple_records_ask_for_choice():
    m = resolve("虛構審查同名錠", ["虛構審查同名錠", "虛構審查同名錠"])
    assert m.is_ambiguous and m.ambiguity_reason == ProductAmbiguityReason.MULTIPLE_EXACT_NAMES and m.best_match is None


def test_cross_category_same_name_asks_for_choice():
    a = cand("pd", "虛構審查跨類錠", "虛構審查跨類錠", category=ProductCategory.DOMESTIC_VITAMIN_TABLET_CAPSULE)
    b = cand("pi", "虛構審查跨類錠", "虛構審查跨類錠", category=ProductCategory.IMPORTED_TABLET_CAPSULE)
    m = resolve_product_match(request(product_candidates=["虛構審查跨類錠"]), [a, b], [], [], [], True)
    assert m.is_ambiguous and {c.product.product_id for c in m.candidates} == {"pd", "pi"}


def test_cross_category_similar_truncated_name_asks_for_choice():
    """截斷的品名同時被兩個類別的官方品名包含 → 不自動選其中一類。"""
    a = cand("pd", "虛構審查跨類相似錠", "虛構審查跨類相似", category=ProductCategory.DOMESTIC_VITAMIN_TABLET_CAPSULE)
    b = cand("pi", "虛構審查跨類相似錠食品", "虛構審查跨類相似", category=ProductCategory.IMPORTED_TABLET_CAPSULE)
    m = resolve_product_match(request(product_candidates=["虛構審查跨類相似"]), [a, b], [], [], ["虛構審查跨類相似"], True)
    assert m.is_ambiguous and m.best_match is None
