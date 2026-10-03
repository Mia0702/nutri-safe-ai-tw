"""Ingredient canonicalization / alias 品質把關測試。

全部使用虛構（synthetic）成分名稱，只驗證一般化規則。
"""

import pytest

from app.ingestion.ingredient_canonical import (
    IngredientMention,
    build_ingredient_canonicalization,
    canonical_key,
    has_designator,
    is_single_name,
    specificity_mismatch,
    split_inline_bilingual,
)
from app.normalization import stable_id


def _mention(surface, english=None, basis="official_parenthetical_english", record="R1"):
    zh, inline = split_inline_bilingual(surface)
    en = english or inline
    return IngredientMention(
        ingredient_id=stable_id("ingredient", canonical_key(zh)), surface=surface, name_zh=zh, english=en,
        english_basis=(basis if english else "official_inline_bilingual") if en else None,
        source_record_key=record, source_url=None,
    )


@pytest.mark.parametrize(
    "a, b",
    [
        ("Fakeol B", "fakeol b"),                 # 大小寫
        ("虛構素 Q", "虛構素Q"),                    # 空白
        ("虛構皂苷-Zb1", "虛構皂苷Zb1"),            # 標點
        ("Beta-虛構聚糖", "β-虛構聚糖"),            # 希臘字母拼寫
        ("alpha-Fakoic acid", "α-Fakoic acid"),
    ],
)
def test_format_variants_share_canonical_key(a, b):
    assert canonical_key(a) == canonical_key(b)


@pytest.mark.parametrize(
    "a, b",
    [
        ("虛構聚醣", "虛構聚糖"),        # 異體字：不自動合併
        ("總虛構酸", "虛構酸"),          # 「總」代表不同量測
        ("虛構酸A", "虛構酸B"),
        ("Fakebacillus alpha ZZ1", "Fakebacillus alpha"),
    ],
)
def test_different_names_not_merged(a, b):
    assert canonical_key(a) != canonical_key(b)


@pytest.mark.parametrize(
    "surface, zh, en",
    [
        ("虛構酸Fakoic acid", "虛構酸", "Fakoic acid"),
        ("虛構菌 Fakebacillus imaginarius", "虛構菌", "Fakebacillus imaginarius"),
        ("虛構菌 F. imaginarius", "虛構菌", "F. imaginarius"),
        ("β-虛構聚糖Fakeglucan", "β-虛構聚糖", "Fakeglucan"),
        # 不拆：英文帶數字代號（菌株／成員），或只有短代號字母
        ("虛構皂苷 Fakeoside Zb2", "虛構皂苷 Fakeoside Zb2", None),
        ("虛構菌 Fakebacillus alpha QQ019", "虛構菌 Fakebacillus alpha QQ019", None),
        ("虛構酸A", "虛構酸A", None),
        ("虛構酮IIA", "虛構酮IIA", None),
        # 英文在前不拆
        ("Fakebacillus alpha 虛構菌", "Fakebacillus alpha 虛構菌", None),
        ("FAKE-CoA虛構抑制劑", "FAKE-CoA虛構抑制劑", None),
    ],
)
def test_split_inline_bilingual(surface, zh, en):
    assert split_inline_bilingual(surface) == (zh, en)


@pytest.mark.parametrize(
    "text, ok",
    [
        ("Fakoic acid", True), ("Fx", True),
        ("Fakein A、Fakein B", False), ("Fakein+Fakeol", False), ("F. alpha & F. beta", False),
        ("A,B,C,D", False), ("Fake protein,native form", False), ("", False),
    ],
)
def test_is_single_name(text, ok):
    assert is_single_name(text) is ok


@pytest.mark.parametrize(
    "a, b, mismatch",
    [
        ("虛構皂苷", "fakeoside Re", True),             # 類別 vs 單一成員
        ("虛構乳酸菌", "Fakebacillus alpha ZZ1", True),  # 菌種 vs 菌株
        ("虛構酸A", "Fakoic acid", True),
        ("虛構酸K", "Fakcin K", False),
        ("虛構皂苷Zb1", "Fakeoside Zb1", False),
        ("虛構多酚", "Fake polyphenols", False),
    ],
)
def test_specificity_mismatch(a, b, mismatch):
    assert specificity_mismatch(a, b) is mismatch


@pytest.mark.parametrize("name, expected", [("Fakcin K", True), ("Fake QQ019", True), ("虛構酸A", True),
                                            ("Fakoic acid", False), ("虛構酸", False), ("Fx", False)])
def test_has_designator(name, expected):
    assert has_designator(name) is expected


def test_inline_bilingual_merges_into_one_canonical_with_official_alias():
    r = build_ingredient_canonicalization([_mention("虛構酸", record="R1"), _mention("虛構酸Fakoic acid", record="R2")])
    assert r.canonical_count == 1
    by_alias = {a["alias"]: a for a in r.aliases}
    assert by_alias["Fakoic acid"]["evidence_status"] == "OFFICIAL_RECORD"
    assert by_alias["Fakoic acid"]["alias_type"] == "ENGLISH_NAME"
    assert by_alias["Fakoic acid"]["source_record_key"] == "R2"
    assert by_alias["虛構酸Fakoic acid"]["inference_basis"]["rule"] == "official_inline_bilingual_full_surface"
    for a in r.aliases:
        assert {"ingredient_id", "alias", "alias_normalized", "evidence_status", "inference_basis", "confidence",
                "source_record_key"} <= a.keys()


def test_greek_spelling_alias_is_inferred_with_basis():
    r = build_ingredient_canonicalization([_mention("β-虛構聚糖"), _mention("β-虛構聚糖"), _mention("Beta-虛構聚糖")])
    assert r.canonical_count == 1
    (a,) = [a for a in r.aliases if a["alias"] == "Beta-虛構聚糖"]
    assert a["evidence_status"] == "INFERRED" and a["confidence"] < 1 and a["inference_basis"]["rule"] == "greek_letter_spelling"


@pytest.mark.parametrize(
    "mentions, kind",
    [
        # 同一英文對應兩個中文 → 不建立 alias
        ([("虛構酸甲", "Fakoic acid"), ("虛構酸乙", "Fakoic acid")], "english_name_shared_by_multiple_ingredients"),
        # 同一中文對應兩個不同英文
        ([("虛構菌", "F. alpha"), ("虛構菌", "F. beta")], "multiple_english_names"),
        # 類別 vs 成員
        ([("虛構皂苷", "fakeoside Re")], "specificity_mismatch"),
        # 中文名含英文字母的類別名，括號英文又是另一個獨立成分
        ([("FAKE-CoA虛構抑制劑", "Fakolin K"), ("Fakolin K", None)], "cross_record_pairing_needs_review"),
        # 異體字
        ([("虛構聚醣", None), ("虛構聚糖", None)], "variant_characters"),
        # 中英以空白相連未拆開
        ([("FKA 虛構素", None)], "mixed_script_unsplit_name"),
        # 名稱包含另一個英文成分名
        ([("Fakolin K", None), ("虛構抑制劑Fakolin K", None)], "name_contains_other_ingredient"),
    ],
)
def test_risky_cases_become_review_candidates_without_alias(mentions, kind):
    r = build_ingredient_canonicalization([_mention(s, e) for s, e in mentions])
    assert kind in {c.kind for c in r.review_candidates}
    assert not [a for a in r.aliases if a["alias_type"] == "ENGLISH_NAME"]
    assert r.canonical_count == len({canonical_key(split_inline_bilingual(s)[0]) for s, _ in mentions})


def test_cross_record_pairing_is_inferred_candidate_not_merge():
    r = build_ingredient_canonicalization([_mention("虛構苷", "Fakoside", record="R1"), _mention("Fakoside", record="R2")])
    assert r.canonical_count == 2
    (c,) = r.inferred_candidates
    assert c.names == ["虛構苷", "Fakoside"] and c.source_record_keys == ["R1"]
    assert not [a for a in r.aliases if a["alias"] == "Fakoside"]


def test_list_parenthetical_rejected_and_raw_material_name_kept():
    r = build_ingredient_canonicalization([_mention("總虛構酸", "Fakein A、Fakein B"), _mention("虛構菌絲體粉末")])
    assert r.rejected_parentheticals[0]["reason"] == "not_single_name"
    assert {d["name_zh"] for d in r.display_names.values()} == {"總虛構酸", "虛構菌絲體粉末"}
    assert not r.aliases


def test_manual_reviewed_alias_is_distinguished():
    m = _mention("虛構酸")
    r = build_ingredient_canonicalization([m], manual_reviews=[{"ingredient_id": m.ingredient_id, "alias": "虛構俗名",
                                                                 "reviewer_note": "synthetic"}])
    (a,) = r.aliases
    assert a["evidence_status"] == "MANUAL_REVIEWED" and a["inference_basis"]["rule"] == "manual_review"


def test_display_name_is_deterministic_most_frequent_surface():
    ms = [_mention("Fakeol B"), _mention("fakeol b"), _mention("fakeol b")]
    r = build_ingredient_canonicalization(ms)
    assert r.display_names[ms[0].ingredient_id]["name_zh"] == "fakeol b"
    assert r.display_names == build_ingredient_canonicalization(list(reversed(ms))).display_names
