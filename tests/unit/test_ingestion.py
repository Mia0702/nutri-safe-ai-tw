import json
from datetime import datetime, timezone
from pathlib import Path

from app.ingestion.rag_ingest import chunk_text
from app.ingestion.tfda_health_food import (
    clean_permit_number,
    normalize_record,
    parse_functions,
    parse_ingredients,
    split_numbered,
)
from app.ingestion.ingredient_parser import split_segments

SAMPLE = json.loads(Path("tests/fixtures/tfda_health_food_sample.json").read_text(encoding="utf-8"))["records"]
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


def test_permit_suffix_removed():
    assert clean_permit_number("衛部健食字第A00297號(本證失效)") == "衛部健食字第A00297號"


def test_split_does_not_break_inside_parentheses_or_numeric_names():
    assert split_segments("甲(A、B)、乙") == ["甲(A、B)", "乙"]
    assert split_segments("2,3,4-三羥基丁酸鈣") == ["2,3,4-三羥基丁酸鈣"]


def test_parse_ingredients_extracts_names_english_and_amounts():
    parsed = parse_ingredients("EPA含量： 182~273 毫克 \r\nβ-聚葡萄糖（β-glucan）\r\n品管指標成分\r\n原兒茶酸1.36-2.04 mg/瓶")
    names = [(p["name_zh"], p["name_en"]) for p in parsed]
    assert ("EPA", None) in names and ("β-聚葡萄糖", "β-glucan") in names and ("原兒茶酸", None) in names
    assert all(n != "品管指標成分" for n, _ in names)


def test_parse_functions_dedupes_spec_standard_variants():
    assert parse_functions("調節血脂功能, 紅麴（規格標準）-調節血脂功能") == ["調節血脂"]
    assert parse_functions(None) == []


def test_split_numbered_warnings():
    assert split_numbered("(1)第一點。\r\n(2)第二點。") == ["第一點。", "第二點。"]
    assert split_numbered("單一警語") == ["單一警語"]


def test_normalize_real_sample_records():
    bundles = [normalize_record(r, NOW) for r in SAMPLE]
    assert all(b is not None for b in bundles)
    first = bundles[0]
    # 每一筆都保留來源追蹤欄位與原始資料
    assert first.raw_data == SAMPLE[0] and first.raw_hash
    for row in (first.company, first.permit, first.product):
        assert row["source_record_key"] == first.permit["permit_number"]
        assert row["retrieved_at"]
    assert first.product["permit_id"] == first.permit["id"] and first.product["company_id"] == first.company["id"]
    assert first.product_claims and first.product_claims[0]["claim_type"] == "APPROVED_OFFICIAL"
    # 失效證號狀態正確對應
    expired = [b for b in bundles if "本證失效" in b.raw_data["許可證字號"]]
    assert expired and all(b.permit["status"] == "EXPIRED" for b in expired)


def test_canonical_ids_are_stable_across_runs():
    a = normalize_record(SAMPLE[0], NOW)
    b = normalize_record(SAMPLE[0], datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert a.product["id"] == b.product["id"] and a.company["id"] == b.company["id"]


def test_chunk_text_respects_size_and_keeps_all_content():
    text = "\n".join(f"第{i}段" + "內容" * 60 for i in range(10))
    chunks = chunk_text(text, max_chars=300, overlap=50)
    assert all(len(c) <= 300 + 60 for c in chunks)
    for i in range(10):
        assert any(f"第{i}段" in c for c in chunks)


# ---------------- alias builder ----------------

from app.ingestion.alias_builder import company_short_name, derive_brand_prefixes  # noqa: E402


def test_company_short_name_strips_legal_form_generally():
    assert company_short_name("範例測試食品股份有限公司") == "範例測試食品"
    assert company_short_name("香港商範例測試有限公司台灣分公司") == "範例測試"
    assert company_short_name("短") is None


def test_brand_prefix_derivation_is_generic_and_rejects_shared_prefixes():
    companies = {
        "c1": ["甲乙牌產品一號", "甲乙牌產品二號", "甲乙牌另一款", "完全不同名稱"],
        "c2": ["通用詞產品a", "丙丁產品"],
        "c3": ["通用詞產品b", "戊己產品"],
        "c4": ["通用詞產品c", "庚辛產品"],
        "c5": ["唯一產品"],
    }
    result = derive_brand_prefixes(companies)
    assert result["c1"][0]["prefix"] == "甲乙牌" and result["c1"][0]["confidence"] > 0.6
    # 被 3 家以上公司共用的前綴視為通用詞，不當品牌
    assert all(info["prefix"] != "通用詞" for infos in result.values() for info in infos)
    assert "c5" not in result


def test_brand_prefix_does_not_cut_latin_word():
    # 前綴最長 4 字元；英文單字較長時，任何前綴都會切在單字中間 → 不推導品牌
    assert "c1" not in derive_brand_prefixes({"c1": ["abcdef一號", "abcdef二號", "abcdef三號"]})
    # 英文字結束後接中文時可以作為品牌
    assert derive_brand_prefixes({"c2": ["abc牌一號", "abc牌二號", "abc牌三號"]})["c2"][0]["prefix"] == "abc牌"
