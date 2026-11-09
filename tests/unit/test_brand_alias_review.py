"""品牌候選審核（D4）：決定驗證、ACCEPT 閘門、衝突稽核、validator / manifest 的 deterministic 行為。

全部使用虛構 TEST_ONLY 資料；不連 OpenAI / Supabase / Neo4j，不讀真實快照。
另有一個檢查已提交的 reviewed 檔案與 manifest 一致（只讀本機檔案）。
"""

import json
from pathlib import Path

import pytest

from app.ingestion import brand_alias_review as bar
from app.ingestion import product_alias_review as par
from app.ingestion.product_alias_review import OfficialRecord
from scripts import review_brand_candidates as cli

SHA = "a" * 64


def rec(i, zh, en, applicant="TEST_ONLY公司甲") -> OfficialRecord:
    return OfficialRecord(
        product_id=f"pid-{i:03d}", source_row_key=f"TFDA:23:T{i:04d}", permit_number=f"T{i:05d}", name_zh=zh, name_en=en,
        category="IMPORTED_TABLET_CAPSULE", applicant=applicant, source_id="SRC_IMPORTED_CSV", source_dataset_id="9047",
        source_url="https://data.fda.gov.tw/data/opendata/export/23/csv", retrieved_at="2026-10-04T00:00:00+00:00",
        raw_row_sha256="b" * 64, raw_row_number=str(i), dataset_sha256=SHA, source_name="TFDA TEST_ONLY 資料集")


def brand(token="Brandx", prefix="測試牌", n=6, applicant="TEST_ONLY公司甲", start=1):
    return [rec(start + i, f"{prefix}{chr(0x4e00 + i * 13)}品", f"{token} Item {i}", applicant=applicant) for i in range(n)]


def candidates_for(records):
    return par.build_brand_candidates(records, "1.0")


def reviewed(records, decisions, **kw):
    cands = candidates_for(records)
    return bar.build_reviewed_brand_rows(cands, records, decisions, overlay_version="1.1", reviewer="TEST_REVIEWER",
                                         reviewed_at="2026-11-08", **kw), cands


def decision(token, value, flag="NONE", reason="TEST: 理由"):
    return {token: {"alias_text_normalized": token, "decision": value, "descriptor_flag": flag, "decision_reason": reason}}


def codes(rows, cands=()):
    return bar.validate_reviewed_brand_rows(rows, list(cands)).codes()


# ---------------------------------------------------------------- 中文品牌證據分類


def test_clean_cjk_pair_is_classified_clean():
    rows, _ = reviewed(brand(), decision("brandx", "ACCEPT"))
    assert rows[0]["zh_evidence_class"] == par.ZH_CLEAN_BRAND_PAIR and rows[0]["conflict_check"] == "NONE"
    assert rows[0]["match_policy"] == "CANDIDATE_ONLY" and rows[0]["decision"] == "ACCEPT"


def test_latin_self_pair_counts_as_clean():
    recs = [rec(i, f"Brandx 品項{i}", f"BRANDX Item {i}") for i in range(6)]
    rows, _ = reviewed(recs, decision("brandx", "ACCEPT"))
    assert rows[0]["zh_pair_basis"] == bar.LATIN_SELF_PAIR and rows[0]["zh_evidence_class"] == par.ZH_CLEAN_BRAND_PAIR


@pytest.mark.parametrize("prefix", ["測試牌(", "“測試”", "測試牌R", "測試牌-"])
def test_punctuation_or_registered_mark_residue_is_noisy(prefix):
    assert bar.prefix_boundary_noise(prefix)
    cls, _ = bar.classify_zh_evidence(support_records=6, latin_support=0, prefix=prefix, prefix_support=6,
                                      shorter_prefix_support=6, prefix_other_applicants=0, shared_brand_tokens=0,
                                      form_counts=[6])
    assert cls == par.ZH_NOISY_PREFIX


def test_overextended_prefix_is_noisy_and_split_prefix_is_inconsistent():
    base = dict(support_records=10, latin_support=0, prefix_other_applicants=0, shared_brand_tokens=0)
    assert bar.classify_zh_evidence(prefix="測試牌營養素", prefix_support=6, shorter_prefix_support=10,
                                    form_counts=[10], **base)[0] == par.ZH_NOISY_PREFIX
    assert bar.classify_zh_evidence(prefix="測試牌", prefix_support=7, shorter_prefix_support=7,
                                    form_counts=[7, 3], **base)[0] == par.ZH_INCONSISTENT_PAIR
    assert bar.classify_zh_evidence(prefix="", prefix_support=0, shorter_prefix_support=0,
                                    form_counts=[5, 5], **base)[0] == par.ZH_INCONSISTENT_PAIR
    assert bar.classify_zh_evidence(prefix="", prefix_support=0, shorter_prefix_support=0,
                                    form_counts=[1] * 10, **base)[0] == par.ZH_NO_CLEAR_BRAND


def test_no_common_chinese_prefix_is_no_clear_brand():
    recs = [rec(i, f"{chr(0x4e00 + i * 31)}{chr(0x4e01 + i * 31)}名稱", f"Brandx Item {i}") for i in range(6)]
    rows, _ = reviewed(recs, decision("brandx", "NEEDS_REVIEW"))
    assert rows[0]["zh_evidence_class"] == par.ZH_NO_CLEAR_BRAND


# ---------------------------------------------------------------- 衝突稽核與 ACCEPT 閘門


def test_other_applicant_using_token_in_chinese_name_blocks_accept():
    recs = brand() + [rec(100, "Brandx 他牌品", "Other Thing", applicant="TEST_ONLY公司乙")]
    cands = candidates_for(recs)
    assert cands[0]["passes_rule"] == "true"               # 機械規則只看英文名稱，看不到這個衝突
    with pytest.raises(bar.BrandReviewError, match="BRAND_ACCEPT"):
        bar.build_reviewed_brand_rows(cands, recs, decision("brandx", "ACCEPT"), overlay_version="1.1",
                                      reviewer="r", reviewed_at="d")
    rows, _ = reviewed(recs, decision("brandx", "NEEDS_REVIEW"))
    assert "TOKEN_USED_BY_MULTIPLE_APPLICANTS" in rows[0]["conflict_check"]


def test_chinese_prefix_shared_with_other_applicant_is_a_conflict():
    recs = brand() + [rec(100, "測試牌另一商品", "Unrelated Product", applicant="TEST_ONLY公司乙")]
    rows, _ = reviewed(recs, decision("brandx", "NEEDS_REVIEW"))
    assert "ZH_PREFIX_USED_BY_OTHER_APPLICANT" in rows[0]["conflict_check"]


def test_token_equal_to_official_product_name_is_a_conflict():
    recs = brand() + [rec(100, "測試牌錠", "Brandx")]
    rows, _ = reviewed(recs, decision("brandx", "NEEDS_REVIEW"))
    assert "TOKEN_EQUALS_OFFICIAL_PRODUCT_NAME" in rows[0]["conflict_check"]


def test_accept_requires_audit_gates_in_validator():
    rows, cands = reviewed(brand(), decision("brandx", "ACCEPT"))
    good = rows[0]
    assert codes(rows, cands) == set()
    bad_cases = {
        "BRAND_ACCEPT_ZH_NOT_CLEAN": {"zh_evidence_class": par.ZH_NOISY_PREFIX},
        "BRAND_ACCEPT_WITH_CONFLICT": {"conflict_check": "MULTIPLE_ZH_BRAND_FORMS"},
        "BRAND_ACCEPT_DESCRIPTOR": {"descriptor_flag": "GENERIC_ENGLISH_WORD"},
        "BRAND_ACCEPT_MULTI_APPLICANT": {"token_applicant_count": "2"},
        "BRAND_ACCEPT_INSUFFICIENT_SUPPORT": {"support_count": "4"},
        "BRAND_ACCEPT_MISSING_AUDIT": {"zh_evidence_class": ""},
    }
    for code, patch in bad_cases.items():
        assert code in codes([{**good, **patch}], cands), code


def test_generic_descriptor_cannot_be_accepted():
    with pytest.raises(bar.BrandReviewError, match="BRAND_ACCEPT_DESCRIPTOR"):
        reviewed(brand(), decision("brandx", "ACCEPT", flag="GENERIC_ENGLISH_WORD"))
    rows, cands = reviewed(brand(), decision("brandx", "REJECT", flag="GENERIC_ENGLISH_WORD"))
    assert codes(rows, cands) == set()


def test_unknown_descriptor_flag_and_mismatched_decisions_are_rejected():
    with pytest.raises(bar.BrandReviewError, match="descriptor_flag"):
        reviewed(brand(), decision("brandx", "REJECT", flag="MADE_UP"))
    with pytest.raises(bar.BrandReviewError, match="不一致"):
        reviewed(brand(), decision("otherbrand", "REJECT"))
    with pytest.raises(bar.BrandReviewError, match="不一致"):
        reviewed(brand(), {})


# ---------------------------------------------------------------- validator：決定、reviewer、證據、CANDIDATE_ONLY


def test_accept_stays_candidate_only_and_never_ownership():
    rows, cands = reviewed(brand(), decision("brandx", "ACCEPT"))
    assert "BRAND_NOT_CANDIDATE_ONLY" in codes([{**rows[0], "match_policy": "EXACT_REVIEW_REQUIRED"}], cands)
    assert "BRAND_OWNERSHIP_RELATION" in codes([{**rows[0], "relation": "OWNS_BRAND"}], cands)
    assert rows[0]["relation"] == "BRAND_CANDIDATE_OF"


def test_missing_official_evidence_cannot_accept():
    rows, cands = reviewed(brand(), decision("brandx", "ACCEPT"))
    row = rows[0]
    assert "ACCEPT_WITHOUT_OFFICIAL_EVIDENCE" in codes([{**row, "source_url": "https://example.com/x"}], cands)
    assert "ACCEPT_WITHOUT_OFFICIAL_EVIDENCE" in codes([{**row, "evidence_quote": ""}], cands)
    assert "ACCEPT_WITHOUT_OFFICIAL_EVIDENCE" in codes([{**row, "evidence_tier": "SECONDARY"}], cands)


def test_reviewer_and_date_required_for_accept_and_reject_but_not_unreviewed():
    accept, cands = reviewed(brand(), decision("brandx", "ACCEPT"))
    reject, _ = reviewed(brand(), decision("brandx", "REJECT", flag="GENERIC_ENGLISH_WORD"))
    pending, _ = reviewed(brand(), decision("brandx", "NEEDS_REVIEW"))
    assert pending[0]["reviewer"] == "" and pending[0]["reviewed_at"] == ""        # 未決定的列不署名
    assert codes(pending, cands) == set()
    for rows in (accept, reject):
        assert "REVIEW_DECISION_WITHOUT_REVIEWER" in codes([{**rows[0], "reviewer": ""}], cands)
        assert "REVIEW_DECISION_WITHOUT_REVIEWER" in codes([{**rows[0], "reviewed_at": ""}], cands)


def test_invalid_decision_missing_reason_and_foreign_rows_are_errors():
    rows, cands = reviewed(brand(), decision("brandx", "ACCEPT"))
    row = rows[0]
    assert "INVALID_DECISION" in codes([{**row, "decision": "MAYBE"}], cands)
    assert "MISSING_DECISION_REASON" in codes([{**row, "decision_reason": ""}], cands)
    assert "REVIEW_ROW_NOT_IN_CANDIDATES" in codes([{**row, "review_id": "PAR_unknown"}], cands)
    assert "NOT_A_BRAND_ROW" in codes([{**row, "alias_kind": "PRODUCT"}], cands)
    assert "DUPLICATE_BRAND_ALIAS" in codes([row, {**row, "review_id": row["review_id"]}], cands) or \
        "DUPLICATE_REVIEW_ID" in codes([row, dict(row)], cands)


def test_validator_is_deterministic_and_does_not_mutate_rows():
    rows, cands = reviewed(brand(), decision("brandx", "ACCEPT"))
    snapshot = json.dumps(rows, sort_keys=True)
    first = bar.validate_reviewed_brand_rows(rows, cands)
    second = bar.validate_reviewed_brand_rows(rows, cands)
    assert first.issues == second.issues and json.dumps(rows, sort_keys=True) == snapshot


def test_reviewed_rows_are_deterministic_and_independent_of_record_order():
    recs = brand() + brand("Otherx", "他牌", start=50)
    cands = candidates_for(recs)
    decisions = {**decision("brandx", "ACCEPT"), **decision("otherx", "ACCEPT")}
    kw = dict(overlay_version="1.1", reviewer="r", reviewed_at="d")
    assert (bar.build_reviewed_brand_rows(cands, recs, decisions, **kw)
            == bar.build_reviewed_brand_rows(cands, list(reversed(recs)), decisions, **kw))


# ---------------------------------------------------------------- manifest / hash


def _write_dir(tmp_path, recs, decisions):
    cands = candidates_for(recs)
    cand_path = tmp_path / "brand_candidates.csv"
    par.write_review_csv(cand_path, cands, par.BRAND_EXTRA_COLUMNS)
    rows = bar.build_reviewed_brand_rows(par.read_review_csv(cand_path), recs, decisions, overlay_version="1.1",
                                         reviewer="r", reviewed_at="2026-11-08")
    out = tmp_path / "out"
    out.mkdir()
    files = {bar.REVIEWED_BRAND_FILE: {"sha256": par.write_review_csv(out / bar.REVIEWED_BRAND_FILE, rows,
                                                                        bar.BRAND_REVIEW_EXTRA_COLUMNS), "rows": len(rows)}}
    ds = par.SourceDataset("SRC_IMPORTED_CSV", "TEST", "9047", "23", "https://data.fda.gov.tw/x", "2026-10-04",
                           "raw.csv", SHA, "norm.csv", SHA, 1)
    manifest = cli.build_review_manifest(
        overlay_version="1.1", created_at="2026-11-08T00:00:00+00:00", datasets=[ds], files=files,
        inputs={"brand_candidates.csv": {"path": str(cand_path), "sha256": par.sha256_file(cand_path)}},
        summary=bar.summarize_reviewed_rows(rows))
    par.write_manifest(out / cli.MANIFEST_FILE, manifest)
    return out, cand_path


def test_manifest_hash_matches_and_detects_tampering(tmp_path):
    out, cand_path = _write_dir(tmp_path, brand(), decision("brandx", "ACCEPT"))
    rows_rep, manifest_rep = cli.validate_directory(out)
    assert rows_rep.ok and manifest_rep.ok
    manifest = json.loads((out / cli.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["files"][bar.REVIEWED_BRAND_FILE]["sha256"] == par.sha256_file(out / bar.REVIEWED_BRAND_FILE)
    assert manifest["candidate_counts"]["review_decisions"] == {"ACCEPT": 1, "REJECT": 0, "NEEDS_REVIEW": 0}

    reviewed_path = out / bar.REVIEWED_BRAND_FILE
    reviewed_path.write_text(reviewed_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert "MANIFEST_HASH_MISMATCH" in cli.validate_directory(out)[1].codes()

    cand_path.write_text(cand_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert "INPUT_HASH_MISMATCH" in cli.validate_directory(out)[1].codes()


# ---------------------------------------------------------------- 已提交的審核結果


REVIEW_DIR = Path("data/product_alias_overlay/2026-11-08")


@pytest.mark.skipif(not (REVIEW_DIR / cli.MANIFEST_FILE).exists(), reason="D4 review dataset not present")
def test_committed_review_dataset_is_valid_and_candidate_only():
    rows_rep, manifest_rep = cli.validate_directory(REVIEW_DIR)
    assert rows_rep.ok and manifest_rep.ok, (rows_rep.errors[:3], manifest_rep.errors[:3])
    rows = par.read_review_csv(REVIEW_DIR / bar.REVIEWED_BRAND_FILE)
    assert len(rows) == 51 and {r["match_policy"] for r in rows} == {"CANDIDATE_ONLY"}
    assert {r["relation"] for r in rows} == {"BRAND_CANDIDATE_OF"}
    manifest = json.loads((REVIEW_DIR / cli.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["candidate_counts"]["review_decisions"] == par_count(rows)
    assert all(r["zh_evidence_class"] == par.ZH_CLEAN_BRAND_PAIR and r["conflict_check"] == "NONE"
               for r in rows if r["decision"] == "ACCEPT")


def par_count(rows):
    return par.count_decisions(rows)
