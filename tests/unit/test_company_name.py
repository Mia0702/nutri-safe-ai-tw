"""申請商正規化規則測試（全部為 synthetic 公司名稱）。"""

import pytest

from app.ingestion.company_name import CompanyNameStatus, normalize_company_name

CLEANED, UNCHANGED, AMBIGUOUS = CompanyNameStatus.CLEANED, CompanyNameStatus.UNCHANGED, CompanyNameStatus.AMBIGUOUS


@pytest.mark.parametrize(
    ("raw", "expected_name", "expected_status"),
    [
        # 行政移轉附註：日期 + 關鍵詞，位於尾端 → 移除
        ("範例生技股份有限公司(99/1/2核准將許可證由甲公司轉移給乙)", "範例生技股份有限公司", CLEANED),
        ("範例生技股份有限公司（民國101年3月4日許可證移轉）", "範例生技股份有限公司", CLEANED),
        ("範例生技有限公司 (2015-06-07 核准更名)", "範例生技有限公司", CLEANED),
        ("範例生技有限公司(許可證由甲公司讓與)", "範例生技有限公司", CLEANED),  # 無日期但 2 個關鍵詞
        # 正式名稱中的括號 → 保留
        ("範例(台灣)股份有限公司", "範例(台灣)股份有限公司", UNCHANGED),
        ("香港商範例 (遠東) 有限公司台灣分公司", "香港商範例 (遠東) 有限公司台灣分公司", UNCHANGED),
        ("範例（亞洲）國際有限公司", "範例（亞洲）國際有限公司", UNCHANGED),
        # 無括號
        ("範例測試食品股份有限公司", "範例測試食品股份有限公司", UNCHANGED),
        ("", "", UNCHANGED),
        # 多組括號：正式括號保留、尾端行政附註移除
        ("範例(台灣)股份有限公司(98/7/6核准轉移)", "範例(台灣)股份有限公司", CLEANED),
        ("範例(台灣)股份有限公司(98/7/6核准轉移)(100/1/1變更許可證)", "範例(台灣)股份有限公司", CLEANED),
        ("範例(台灣)(遠東)有限公司", "範例(台灣)(遠東)有限公司", UNCHANGED),
        # 無法可靠判斷 → 保留原文
        ("範例生技有限公司(核准)", "範例生技有限公司(核准)", AMBIGUOUS),  # 證據不足
        ("範例生技有限公司(98/7/6)", "範例生技有限公司(98/7/6)", AMBIGUOUS),  # 只有日期
        ("範例(98/7/6核准轉移)生技有限公司", "範例(98/7/6核准轉移)生技有限公司", AMBIGUOUS),  # 不在尾端
        ("範例生技(98/7/6核准轉移)", "範例生技(98/7/6核准轉移)", AMBIGUOUS),  # 剩餘不是完整公司名
        ("範例生技有限公司(98/7/6核准轉移", "範例生技有限公司(98/7/6核准轉移", AMBIGUOUS),  # 括號不配對
        ("範例(台灣)有限公司(更名)(98/7/6核准轉移)", "範例(台灣)有限公司(更名)(98/7/6核准轉移)", AMBIGUOUS),
    ],
)
def test_normalize_company_name(raw, expected_name, expected_status):
    result = normalize_company_name(raw)
    assert result.name == expected_name
    assert result.status == expected_status
    assert result.raw == raw.strip()


def test_removed_notes_are_kept_for_traceability():
    result = normalize_company_name("範例(台灣)股份有限公司(98/7/6核准轉移)(100/1/1變更許可證)")
    assert result.removed_notes == ["98/7/6核准轉移", "100/1/1變更許可證"]


def test_ingestion_keeps_raw_company_name_and_merges_ids():
    from datetime import datetime, timezone

    from app.ingestion.tfda_health_food import normalize_record

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    base = {"許可證字號": "衛部健食字第A99901號", "中文品名": "範例膠囊", "證況": "核可"}
    plain = normalize_record({**base, "申請商": "範例生技股份有限公司"}, now)
    noted_raw = "範例生技股份有限公司(99/1/2核准將許可證由甲公司轉移給乙)"
    noted = normalize_record({**base, "許可證字號": "衛部健食字第A99902號", "申請商": noted_raw}, now)

    assert noted.raw_data["申請商"] == noted_raw
    assert noted.company_name_raw == noted_raw
    assert noted.company_name_status == "cleaned"
    assert noted.company["name_zh"] == "範例生技股份有限公司"
    assert noted.company["id"] == plain.company["id"]
