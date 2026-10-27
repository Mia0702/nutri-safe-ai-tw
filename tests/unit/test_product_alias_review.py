"""Product / Brand alias 審核管線（D2）：候選產生、正規化、碰撞分類、品牌規則、validator、manifest / hash。

全部使用虛構 TEST_ONLY 資料；不連 OpenAI / Supabase / Neo4j，不讀真實快照。
"""

import hashlib
import json

import pytest

from app.ingestion import product_alias_review as par
from app.ingestion.product_alias_review import OfficialRecord

SHA = "a" * 64


def rec(i, zh, en, applicant="TEST_ONLY公司甲", category="IMPORTED_TABLET_CAPSULE", **kw) -> OfficialRecord:
    base = dict(product_id=f"pid-{i:03d}", source_row_key=f"TFDA:23:T{i:04d}", permit_number=f"T{i:05d}", name_zh=zh,
                name_en=en, category=category, applicant=applicant, source_id="SRC_IMPORTED_CSV",
                source_dataset_id="9047", source_url="https://data.fda.gov.tw/data/opendata/export/23/csv",
                retrieved_at="2026-10-04T00:00:00+00:00", raw_row_sha256="b" * 64, raw_row_number=str(i),
                dataset_sha256=SHA, source_name="TFDA TEST_ONLY 資料集")
    base.update(kw)
    return OfficialRecord(**base)


RECORDS = [
    rec(1, "測試甲錠", "TESTONLY ALPHA TABLET"),
    rec(2, "測試乙膠囊", "TESTONLY BETA CAPSULE"),
    rec(3, "測試丙粉", "Shared  Gamma Powder"),          # 與 4 同名（空白 / 大小寫不同）→ 碰撞
    rec(4, "測試丁粉", "SHARED GAMMA POWDER", applicant="TEST_ONLY公司乙"),
    rec(5, "測試戊", "D3"),                               # 字母太少 → INVALID
    rec(6, "測試己", "NULL"),                             # 沒有英文名稱
    rec(7, "測試庚", "Delta-X Plus"),                     # 去標點後與 8 相同 → runtime key collision
    rec(8, "測試辛", "Delta X Plus"),
]


def candidates(records=RECORDS):
    return par.build_product_candidates(records, "1.0")


# ---------------------------------------------------------------- 正規化


def test_normalize_alias_text_is_conservative():
    assert par.normalize_alias_text("  Foo   BAR\tBaz ") == "foo bar baz"
    assert par.normalize_alias_text("ＡＢＣ") == "abc"                                   # NFKC
    assert par.normalize_alias_text("Delta-X") != par.normalize_alias_text("Delta X")   # 不移除標點
    assert par.normalize_alias_text("Zinc Gluconate") != par.normalize_alias_text("Zinc")  # 不移除詞 / 鹽類字樣
    assert par.normalize_alias_text(None) == ""


def test_invalid_english_rules_are_structural():
    assert par.invalid_english_reason("") == "EMPTY_OR_NULL_MARKER"
    assert par.invalid_english_reason("NULL") == "EMPTY_OR_NULL_MARKER"
    assert par.invalid_english_reason("123 456") == "NO_LATIN_LETTERS"
    assert par.invalid_english_reason("D3") == "FEWER_THAN_4_LETTERS"
    assert par.invalid_english_reason("Alpha") is None


# ---------------------------------------------------------------- 候選與碰撞分類


def test_candidates_are_deterministic_and_independent_of_input_order():
    assert candidates() == candidates(list(reversed(RECORDS)))
    assert par.build_brand_candidates(RECORDS, "1.0") == par.build_brand_candidates(list(reversed(RECORDS)), "1.0")


def test_collision_classification_and_policies():
    rows = {r["target_id"]: r for r in candidates()}
    assert rows["pid-001"]["classification"] == par.UNIQUE
    assert rows["pid-001"]["match_policy"] == "EXACT_REVIEW_REQUIRED" and rows["pid-001"]["collision_count"] == "1"
    for pid in ("pid-003", "pid-004"):                      # 空白 / 大小寫不同仍視為同名
        assert rows[pid]["classification"] == par.COLLIDING
        assert rows[pid]["match_policy"] == "CANDIDATE_ONLY" and rows[pid]["collision_count"] == "2"
    assert rows["pid-005"]["classification"] == par.INVALID and rows["pid-005"]["match_policy"] == "NEVER_RUNTIME"
    assert "pid-006" not in rows                            # 沒有英文名稱：不產生列，只計數


def test_runtime_key_collision_is_flagged_but_remains_unique_by_spec():
    rows = {r["target_id"]: r for r in candidates()}
    assert rows["pid-007"]["classification"] == par.UNIQUE            # 保守正規化下是唯一
    assert "RUNTIME_KEY_COLLISION" in rows["pid-007"]["conflict_check"]
    assert rows["pid-007"]["runtime_key_collision_count"] == "2"


def test_every_row_is_needs_review_and_keeps_provenance():
    rows = candidates()
    assert {r["decision"] for r in rows} == {"NEEDS_REVIEW"}
    assert {r["proposed_by"] for r in rows} == {"MECHANICAL_OFFICIAL"}
    for r in rows:
        assert r["source_record_id"].startswith("TFDA:") and r["source_sha256"] == SHA and r["retrieved_at"]
        assert r["evidence_quote"] == f"中文品名={r['target_official_name']} | 英文品名={r['alias_text_raw']}"
        assert r["alias_text_normalized"] == par.normalize_alias_text(r["alias_text_raw"])  # 不翻譯、不改字


def test_missing_source_metadata_is_marked_needs_review_not_invented():
    broken = rec(9, "測試壬", "TESTONLY OMEGA TABLET", retrieved_at="", raw_row_sha256="")
    row = candidates([broken])[0]
    assert row["decision"] == "NEEDS_REVIEW" and row["decision_reason"].startswith("MISSING_SOURCE_METADATA")
    assert "retrieved_at" in row["decision_reason"] and row["retrieved_at"] == ""
    report = par.validate_rows([row])
    assert report.ok and "MISSING_PROVENANCE" in report.codes("WARNING")


def test_classification_counts():
    counts = par.classification_counts(candidates(), RECORDS, [])
    assert counts["official_records"] == 8 and counts["records_without_english_name"] == 1
    assert counts["official_english_names_total"] == 6        # 001 002 003 004 007 008（005 無效、006 無英文名）
    assert counts["unique_products_covered"] == 6
    assert counts["UNIQUE_OFFICIAL_ENGLISH"]["rows"] == 4
    assert counts["COLLIDING_OFFICIAL_ENGLISH"] == {"rows": 2, "normalized_names": 1, "products": 2}
    assert counts["INVALID_OR_GENERIC"]["rows"] == 1
    assert counts["unique_rows_with_runtime_key_collision"] == 2


# ---------------------------------------------------------------- 品牌候選規則


def brand_records(n, applicant="TEST_ONLY公司甲", token="Brandx", prefix="測試牌"):
    return [rec(100 + i, f"{prefix}產品{i}", f"{token} Item {i}", applicant=applicant) for i in range(n)]


def test_brand_rule_requires_five_records_single_applicant_and_non_generic():
    ok = par.build_brand_candidates(brand_records(5), "1.0")
    assert len(ok) == 1 and ok[0]["passes_rule"] == "true" and ok[0]["zh_brand_candidate"].startswith("測試牌")
    assert par.build_brand_candidates(brand_records(4), "1.0") == []                  # < 5 筆不輸出

    two = brand_records(5) + [rec(200 + i, f"他牌{i}", f"Brandx Other {i}", applicant="TEST_ONLY公司乙") for i in range(5)]
    row = par.build_brand_candidates(two, "1.0")[0]
    assert row["passes_rule"] == "false" and "APPLICANTS>1" in row["rule_failures"]

    # 單一申請商，但別的申請商也在英文品名中用到這個詞 → 非獨特品牌詞
    shared = brand_records(5) + [rec(300, "他牌品", "Another Brandx Thing", applicant="TEST_ONLY公司乙")]
    row = par.build_brand_candidates(shared, "1.0")[0]
    assert row["passes_rule"] == "false" and "TOKEN_USED_BY_OTHER_APPLICANTS" in row["rule_failures"]


def test_brand_candidates_are_never_automatic_or_ownership():
    rows = par.build_brand_candidates(brand_records(6), "1.0")
    assert rows
    for row in rows:
        assert row["match_policy"] == "CANDIDATE_ONLY" and row["decision"] == "NEEDS_REVIEW"
        assert row["relation"] == "BRAND_CANDIDATE_OF" and "OWN" not in row["relation"]
        assert row["evidence_tier"] == "OFFICIAL_RECORD_INFERENCE"
    assert par.validate_rows(rows).ok


def test_zh_majority_prefix_needs_enough_support():
    names = ["測試牌A", "測試牌B", "測試牌C", "測試牌D", "測試牌E", "其他F"]
    assert par.zh_majority_prefix(names)[0] == "測試牌"
    assert par.zh_majority_prefix(["甲乙丙", "丁戊己", "庚辛壬", "癸子丑", "寅卯辰"]) == ("", 0)


# ---------------------------------------------------------------- validator


def good_row(**kw):
    row = candidates([RECORDS[0]])[0]
    row.update(kw)
    return row


def codes(rows, **kw):
    return par.validate_rows(rows, **kw).codes()


def test_validator_accepts_generated_candidates_without_errors():
    rep = par.validate_rows(candidates())
    assert rep.ok, rep.errors
    assert "RUNTIME_KEY_COLLISION" in rep.codes("WARNING")


def test_validator_blocks_duplicate_review_id_and_invalid_enums():
    assert "DUPLICATE_REVIEW_ID" in codes([good_row(), good_row()])
    assert "INVALID_DECISION" in codes([good_row(decision="APPROVED")])
    assert "INVALID_MATCH_POLICY" in codes([good_row(match_policy="AUTO")])
    assert "INVALID_ALIAS_KIND" in codes([good_row(alias_kind="OTHER")])
    assert "INVALID_PROPOSED_BY" in codes([good_row(proposed_by="ROBOT")])
    assert "NORMALIZED_MISMATCH" in codes([good_row(alias_text_normalized="not-the-same")])


def test_validator_blocks_missing_provenance_unless_marked_pending():
    assert "MISSING_PROVENANCE" in codes([good_row(source_url="")])
    assert "MISSING_PROVENANCE" in codes([good_row(retrieved_at="", decision_reason="")])
    pending = par.validate_rows([good_row(retrieved_at="", decision_reason="MISSING_SOURCE_METADATA:retrieved_at")])
    assert pending.ok and "MISSING_PROVENANCE" in pending.codes("WARNING")


def test_validator_blocks_accept_without_official_evidence():
    accept = dict(decision="ACCEPT", reviewer="TEST_REVIEWER", reviewed_at="2026-10-27")
    assert par.validate_rows([good_row(**accept)]).ok
    assert "ACCEPT_WITHOUT_OFFICIAL_EVIDENCE" in codes([good_row(source_url="https://example.com/x", **accept)])
    assert "ACCEPT_WITHOUT_OFFICIAL_EVIDENCE" in codes([good_row(evidence_tier="SECONDARY", **accept)])
    assert "ACCEPT_WITHOUT_OFFICIAL_EVIDENCE" in codes([good_row(evidence_quote="", **accept)])
    assert "ACCEPT_WITHOUT_REVIEWER" in codes([good_row(decision="ACCEPT")])
    ai = good_row(proposed_by="AI_SUGGESTED", evidence_tier="NONE", **accept)
    assert "AI_SUGGESTED_ACCEPT_WITHOUT_OFFICIAL_EVIDENCE" in codes([ai])


def test_validator_blocks_unique_marked_with_conflicting_targets():
    a = good_row()
    b = good_row(review_id="PAR_other", target_id="pid-xyz", target_official_name="另一產品")
    assert "UNIQUE_ALIAS_CONFLICTING_TARGETS" in codes([a, b])
    assert "UNIQUE_MARKED_WITH_COLLISION" in codes([good_row(collision_count="2")])
    colliding = next(r for r in candidates() if r["classification"] == par.COLLIDING)
    assert "COLLIDING_NOT_CANDIDATE_ONLY" in codes([{**colliding, "match_policy": "EXACT_REVIEW_REQUIRED"}])


def test_validator_blocks_alias_equal_to_other_product_official_name():
    a = good_row()
    assert "ALIAS_EQUALS_OTHER_PRODUCT_NAME" in codes([a], official_names={a["alias_text_normalized"]: {"someone-else"}})
    assert par.validate_rows([a], official_names={a["alias_text_normalized"]: {a["target_id"]}}).ok


def test_validator_blocks_automatic_brand_and_ownership():
    brand = par.build_brand_candidates(brand_records(5), "1.0")[0]
    assert "BRAND_NOT_CANDIDATE_ONLY" in codes([{**brand, "match_policy": "EXACT_REVIEW_REQUIRED"}])
    assert "BRAND_OWNERSHIP_RELATION" in codes([{**brand, "relation": "OWNS_BRAND"}])


def test_validator_blocks_generic_runtime_and_runtime_key_collision_accept():
    rows = candidates()
    accept = dict(decision="ACCEPT", reviewer="r", reviewed_at="d")
    invalid = next(r for r in rows if r["classification"] == par.INVALID)
    assert "GENERIC_NOT_NEVER_RUNTIME" in codes([{**invalid, "match_policy": "CANDIDATE_ONLY"}])
    assert "ACCEPT_NEVER_RUNTIME" in codes([{**invalid, **accept}])
    rk = next(r for r in rows if "RUNTIME_KEY" in r["conflict_check"])
    assert "ACCEPT_RUNTIME_KEY_COLLISION" in codes([{**rk, **accept}])


def test_validator_does_not_modify_rows():
    rows = candidates()
    before = json.dumps(rows, sort_keys=True)
    par.validate_rows(rows)
    assert json.dumps(rows, sort_keys=True) == before


def test_validator_blocks_unknown_supersedes():
    assert "UNKNOWN_SUPERSEDES" in codes([good_row(supersedes_review_id="PAR_missing")])
    assert par.validate_rows([good_row(supersedes_review_id="PAR_prev")], existing_review_ids=["PAR_prev"]).ok


# ---------------------------------------------------------------- manifest / hash / CSV


def make_manifest(tmp_path, sha=SHA):
    rows = candidates()
    file_sha = par.write_review_csv(tmp_path / "candidates.csv", rows, par.PRODUCT_EXTRA_COLUMNS)
    counts = par.classification_counts(rows, RECORDS, [])
    counts["brand_candidates_total"] = 0
    ds = par.SourceDataset("SRC_IMPORTED_CSV", "TEST_ONLY", "9047", "23", "https://data.fda.gov.tw/x", "2026-10-04",
                           "raw.csv", sha, "norm.csv", "c" * 64, 8)
    return par.build_manifest(overlay_version="1.0", created_at="2026-10-27T00:00:00+00:00", datasets=[ds],
                              files={"candidates.csv": {"sha256": file_sha, "rows": len(rows)}}, counts=counts,
                              decisions=par.count_decisions(rows))


def test_manifest_valid_and_detects_tampered_file(tmp_path):
    manifest = make_manifest(tmp_path)
    assert par.validate_manifest(manifest, tmp_path).ok
    assert manifest["classification_counts"]["UNIQUE_OFFICIAL_ENGLISH"]["rows"] == 4
    assert manifest["candidate_counts"]["review_decisions"] == {"ACCEPT": 0, "REJECT": 0, "NEEDS_REVIEW": 7}
    (tmp_path / "candidates.csv").write_text("tampered", encoding="utf-8")
    assert "MANIFEST_HASH_MISMATCH" in par.validate_manifest(manifest, tmp_path).codes()


def test_manifest_malformed_cases(tmp_path):
    manifest = make_manifest(tmp_path)
    assert "MALFORMED_MANIFEST" in par.validate_manifest({k: v for k, v in manifest.items() if k != "source_datasets"}).codes()
    assert "MALFORMED_MANIFEST" in par.validate_manifest({**manifest, "overlay_version": "v1"}).codes()
    assert "MALFORMED_MANIFEST" in par.validate_manifest([]).codes()
    broken = json.loads(json.dumps(manifest))
    broken["source_datasets"][0]["raw_sha256"] = "xyz"
    assert "MALFORMED_MANIFEST" in par.validate_manifest(broken).codes()


def test_changed_source_hash_requires_version_change(tmp_path):
    old = make_manifest(tmp_path, sha="d" * 64)
    new = make_manifest(tmp_path, sha="e" * 64)
    assert "SOURCE_HASH_CHANGED_WITHOUT_VERSION" in par.validate_manifest(new, None, [old]).codes()
    assert par.validate_manifest({**new, "overlay_version": "1.1"}, None, [old]).ok


def test_csv_roundtrip_columns_and_deterministic_bytes(tmp_path):
    rows = candidates()
    path = tmp_path / "c.csv"
    sha = par.write_review_csv(path, rows, par.PRODUCT_EXTRA_COLUMNS)
    assert sha == hashlib.sha256(path.read_bytes()).hexdigest()
    back = par.read_review_csv(path)
    assert list(back[0].keys())[: len(par.REVIEW_COLUMNS)] == list(par.REVIEW_COLUMNS)
    assert back == [{c: r.get(c, "") for c in list(par.REVIEW_COLUMNS) + list(par.PRODUCT_EXTRA_COLUMNS)} for r in rows]
    assert par.write_review_csv(tmp_path / "again.csv", rows, par.PRODUCT_EXTRA_COLUMNS) == sha


def test_snapshot_loader_rejects_unverified_hash(tmp_path):
    (tmp_path / "SHA256SUMS.txt").write_text(f"{'0' * 64}  x.csv\n", encoding="utf-8")
    with pytest.raises((par.SnapshotError, FileNotFoundError)):
        par.load_snapshot(tmp_path)  # 缺少來源登記表 / 雜湊不符 → 停止，不使用未驗證的資料
