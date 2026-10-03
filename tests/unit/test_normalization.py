from app.normalization import (
    extract_permit_code,
    extract_permit_type,
    name_match_score,
    normalize_name,
    search_terms,
    stable_id,
)


def test_normalize_removes_spaces_symbols_and_fullwidth():
    assert normalize_name(" ＡＢＣ® 測試（膠囊） ") == "abc測試膠囊"


def test_search_terms_include_full_name_and_bigrams():
    terms = search_terms("測試膠囊")
    assert terms[0] == "測試膠囊"
    assert {"測試", "試膠", "膠囊"} <= set(terms)


def test_name_match_score_exact_is_one():
    assert name_match_score("測試膠囊", "測試 膠囊") == 1.0


def test_name_match_score_partial_lower_than_exact_and_unrelated_lowest():
    exact = name_match_score("虛構牌晶亮測試膠囊", "虛構牌晶亮測試膠囊")
    partial = name_match_score("晶亮膠囊", "虛構牌晶亮測試膠囊")
    unrelated = name_match_score("完全無關", "虛構牌晶亮測試膠囊")
    assert exact > partial > unrelated
    assert unrelated < 0.5


def test_extract_permit_code_variants():
    assert extract_permit_code("衛部健食字第A00235號") == "A00235"
    assert extract_permit_code("衛部健食規字第000042號(本證失效)") == "000042"
    assert extract_permit_code("ａ00235") == "A00235"
    assert extract_permit_code("沒有字號") is None
    assert extract_permit_type("衛部健食規字第000042號") == "健食規字"


def test_stable_id_is_deterministic():
    assert stable_id("product", "x") == stable_id("product", "x")
    assert stable_id("product", "x") != stable_id("company", "x")
