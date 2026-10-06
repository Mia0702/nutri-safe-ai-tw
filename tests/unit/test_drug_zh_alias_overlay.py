"""Reviewed Taiwan Drug Chinese Alias Overlay：證據稽核（ingestion）與 runtime（只是 identity，不是證據）。

邏輯測試全部使用 TEST_ONLY 虛構名稱與合成官方資料列；不 hard-code 任何真實藥名。
最後一節「REAL OVERLAY」是對實際 overlay 檔案的回歸 / 不變量檢查（真實案例只作為 regression，不是 production rule）。
"""

import csv
from pathlib import Path

import pytest

from app.ingestion.drug_zh_alias_evidence import (
    MATCH_EXACT,
    MATCH_NONE,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    STATUS_SUPPORTED,
    STATUS_VERIFIED,
    build_overlay_rows,
    collect_official_pairs,
    evaluate_candidate,
    official_equivalents,
    strip_official_eq_annotation,
)
from app.ingestion.taiwan_drug_catalog import STATUS_NOT_MAPPED
from app.schemas.enums import InputType, Intent
from app.schemas.interaction import (
    InteractionConclusionStatus,
    InteractionMode,
    InteractionSubjectExtraction,
    InteractionSubjectKind,
    SubjectResolutionStatus,
)
from app.schemas.user_request import RawUserInput
from app.services import drug_zh_alias
from app.services.drug_zh_alias import DrugZhAliasIndex, load_index
from tests.builders import extraction
from tests.fakes import InMemoryInteractionRepo
from tests.unit.test_interaction_runtime import ING, build, ingredient_data, research_of  # noqa: F401（autouse fixture）


def permit(no, zh, en, ingredients=""):
    return {"許可證字號": no, "中文品名": zh, "英文品名": en, "主成分略述": ingredients}


def row(alias, *, status=STATUS_VERIFIED, kind="DRUG_SUBSTANCE", entity="TD_A", code="", official="", policy=None, support="2"):
    return {"alias_id": "T_" + alias, "alias_text_zh_tw": alias, "target_kind": kind, "canonical_entity_id": entity,
            "taiwan_drug_ingredient_code": code, "taiwan_drug_ingredient_official_name": official, "official_source": "TEST_ONLY 官方來源",
            "source_locator": "TEST_ONLY 許可證字號 P1、P2", "support_permit_count": support, "review_status": status,
            "match_policy": policy or (MATCH_EXACT if status == STATUS_VERIFIED else MATCH_NONE), "retrieved_at": "2026-10-06T00:00:00+00:00"}


# ============================================================ ingestion：官方成對證據

PAIRS_RAW = [
    permit("P1", "甲試藥", "Testdrug-A", ""),                                    # 原料藥許可證：品名本身就是通用名
    permit("P2", "某牌止痛錠（甲試藥）", "BRAND TABLETS (Testdrug-A)", "TESTDRUG-A"),  # 括號成對
    permit("P3", "某牌止痛錠（甲試藥）", "OTHER TABLETS (Testdrug-A)", "TESTDRUG-A"),
    permit("P4", "只提到甲試藥的商品名", "NOT PAIRED TABLETS", "TESTDRUG-A"),          # 中文名稱夾在商品名裡：不算成對
    permit("P5", "鹽試藥甲", "Testdrug-A Sodium", ""),
    permit("P6", "鹽試藥甲", "Testdrug-A Sodium", ""),
    permit("P7", "混名", "Testdrug-A", ""),
    permit("P8", "混名", "Testdrug-B", ""),
]


def test_pairs_only_come_from_the_same_permit_and_embedded_brand_names_do_not_count():
    pairs = collect_official_pairs(PAIRS_RAW)
    assert sorted(pairs["甲試藥"]["testdrug-a"].permits) == ["P1", "P2", "P3"]  # P4 只是商品名夾帶：不計
    assert "testdrug-a sodium" not in pairs["甲試藥"]
    assert evaluate_candidate("甲試藥", "Testdrug-A", pairs).passed


def test_cancelled_permits_are_excluded_from_evidence():
    pairs = collect_official_pairs(PAIRS_RAW, cancelled={"P2", "P3"})
    res = evaluate_candidate("甲試藥", "Testdrug-A", pairs)
    assert res.support_permits == ["P1"] and not res.passed and "不足" in res.reasons[0]


def test_single_permit_is_not_enough_for_verification():
    pairs = collect_official_pairs(PAIRS_RAW[:1])
    assert not evaluate_candidate("甲試藥", "Testdrug-A", pairs).passed


def test_same_chinese_name_pointing_to_two_ingredients_is_ambiguous_not_verified():
    pairs = collect_official_pairs(PAIRS_RAW)
    res = evaluate_candidate("混名", "Testdrug-A", pairs, min_support=1)
    assert not res.passed and res.conflicts == {"testdrug-b": 1}


def test_parent_vs_salt_conflict_blocks_auto_mapping():
    raw = [permit("Q1", "混合名", "Testdrug-A", ""), permit("Q2", "混合名", "Testdrug-A", ""),
           permit("Q3", "混合名", "Testdrug-A Sodium", "")]
    pairs = collect_official_pairs(raw)
    assert not evaluate_candidate("混合名", "Testdrug-A", pairs).passed  # 母體 vs 鈉鹽：不自動併入
    assert not evaluate_candidate("混合名", "Testdrug-A Sodium", pairs, min_support=1).passed


def test_salt_name_never_supports_the_parent_identity():
    pairs = collect_official_pairs(PAIRS_RAW)
    assert not evaluate_candidate("鹽試藥甲", "Testdrug-A", pairs, min_support=1).support_permits  # 鈉鹽名稱只對應鈉鹽
    assert evaluate_candidate("鹽試藥甲", "Testdrug-A Sodium", pairs).passed


def test_official_eq_annotation_synonyms_are_the_same_identity_but_salts_are_not_stripped():
    assert strip_official_eq_annotation("ALPHA-X (EQ TO BETA-Y)") == "alpha-x"
    assert official_equivalents("ALPHA-X (EQ TO BETA-Y)") == {"alpha-x", "beta-y"}
    assert official_equivalents("ALPHA-X HCL") == {"alpha-x hcl"}  # 鹽類字樣不去除
    raw = [permit("R1", "同義名", "Alpha-X (Beta-Y)", ""), permit("R2", "同義名", "Beta-Y (Alpha-X)", ""), permit("R3", "同義名", "Alpha-X", "")]
    res = evaluate_candidate("同義名", "Alpha-X", collect_official_pairs(raw), equivalents={"beta-y"})
    assert res.passed and len(res.support_permits) == 3


def test_english_paren_that_is_not_an_ingredient_of_the_permit_is_not_a_pair():
    raw = [permit("S1", "某某錠（甲試藥）", "X TABLETS (Testdrug-A)", "SOMETHING-ELSE"), permit("S2", "某某錠（甲試藥）", "Y TABLETS (Testdrug-A)", "SOMETHING-ELSE")]
    assert "甲試藥" not in collect_official_pairs(raw)


DRUGS = {"TD_A": "Testdrug-A"}
CATALOG = {"K1": "CATALOGONLY-Q (EQ TO QUALIA)", "K2": "TESTDRUG-A SODIUM"}
CAND = {"target_kind": "DRUG_SUBSTANCE", "canonical_entity_id": "TD_A", "taiwan_drug_ingredient_code": "", "target_english_name": "Testdrug-A",
        "identity_level": "ACTIVE_MOIETY", "reviewer_decision": "ACCEPT", "reviewer_note": ""}


def built(alias, pairs, **override):
    return build_overlay_rows([{**CAND, "alias_text_zh_tw": alias, **override}], pairs, DRUGS, CATALOG, "2026-10-06T00:00:00+00:00")[0]


def test_builder_verifies_only_accepted_candidates_with_passing_evidence():
    pairs = collect_official_pairs(PAIRS_RAW)
    ok = built("甲試藥", pairs)
    assert ok["review_status"] == STATUS_VERIFIED and ok["match_policy"] == MATCH_EXACT and ok["support_permit_count"] == "3"
    assert ok["source_locator"] and ok["official_source"] and ok["retrieved_at"]  # source traceability
    assert built("甲試藥", pairs, reviewer_decision="REJECT", reviewer_note="鹽類不併入")["review_status"] == STATUS_REJECTED
    assert built("甲試藥", pairs, reviewer_decision="HOLD")["review_status"] == STATUS_NEEDS_REVIEW
    assert built("甲試藥", pairs, reviewer_decision="HOLD")["match_policy"] == MATCH_NONE


def test_mainland_only_wording_without_taiwan_official_pair_is_needs_review_not_auto_match():
    pairs = collect_official_pairs(PAIRS_RAW)
    r = built("甲试药", pairs)  # 簡體 / 只存在於非台灣寫法：官方資料裡沒有成對證據
    assert r["review_status"] == STATUS_NEEDS_REVIEW and r["match_policy"] == MATCH_NONE and r["support_permit_count"] == "0"


def test_the_same_wording_becomes_verified_only_when_a_taiwan_official_source_supports_it():
    raw = PAIRS_RAW + [permit("T1", "台灣官方有的寫法", "Testdrug-A", ""), permit("T2", "台灣官方有的寫法", "Testdrug-A", "")]
    assert built("台灣官方有的寫法", collect_official_pairs(raw))["review_status"] == STATUS_VERIFIED


def test_salt_target_requires_the_salt_catalog_name_not_the_parent():
    pairs = collect_official_pairs(PAIRS_RAW)
    mismatched = built("鹽試藥甲", pairs, target_kind="TAIWAN_DRUG_INGREDIENT", canonical_entity_id="", taiwan_drug_ingredient_code="K2",
                       target_english_name="Testdrug-A")  # 想把鹽類名稱掛到母體名稱：不接受
    assert mismatched["review_status"] == STATUS_NEEDS_REVIEW and "不同" in mismatched["review_reason"]
    right = built("鹽試藥甲", pairs, target_kind="TAIWAN_DRUG_INGREDIENT", canonical_entity_id="", taiwan_drug_ingredient_code="K2",
                  target_english_name="Testdrug-A Sodium")
    assert right["review_status"] == STATUS_VERIFIED and right["taiwan_drug_ingredient_official_name"] == "TESTDRUG-A SODIUM"


def test_drug_target_must_match_the_reviewed_canonical_english_name():
    pairs = collect_official_pairs(PAIRS_RAW)
    assert built("甲試藥", pairs, target_english_name="Testdrug-A Sodium")["review_status"] == STATUS_NEEDS_REVIEW
    assert built("甲試藥", pairs, canonical_entity_id="TD_MISSING")["review_status"] == STATUS_NEEDS_REVIEW


# ============================================================ runtime：index（exact only）


def test_only_verified_exact_rows_participate_in_matching():
    idx = DrugZhAliasIndex([row("甲試藥"), row("待審名", status=STATUS_NEEDS_REVIEW), row("拒絕名", status=STATUS_REJECTED),
                            row("偽裝名", status=STATUS_NEEDS_REVIEW, policy=MATCH_EXACT), row("非精確", policy="DISPLAY_ONLY_NOT_AUTOMERGE")])
    assert idx.lookup("甲試藥").canonical_entity_id == "TD_A"
    assert idx.lookup("待審名") is None and idx.lookup("拒絕名") is None and idx.lookup("偽裝名") is None and idx.lookup("非精確") is None


def test_substring_prefix_and_suffix_variants_never_match():
    idx = DrugZhAliasIndex([row("甲試藥")])
    for text in ("甲試", "試藥", "甲試藥錠", "鹽酸甲試藥", "甲試藥500毫克", "我吃甲試藥", "甲試藥鈉", ""):
        assert idx.lookup(text) is None, text


def test_simplified_forms_are_not_converted_or_guessed():
    idx = DrugZhAliasIndex([row("甲試藥")])
    assert idx.lookup("甲试药") is None
    assert idx.lookup(" 甲 試藥 ").canonical_entity_id == "TD_A"  # 只消除空白 / 格式差異


def test_key_pointing_to_two_different_identities_is_ignored():
    idx = DrugZhAliasIndex([row("同名", entity="TD_A"), row("同名", entity="TD_B")])
    assert idx.lookup("同名") is None


def test_invalid_verified_rows_are_ignored():
    bad = [row("無來源", entity="TD_A"), row("無目標", entity=""), row("零支持", support="0"), row("目錄缺代碼", kind="TAIWAN_DRUG_INGREDIENT", entity="")]
    bad[0]["source_locator"] = ""
    assert len(DrugZhAliasIndex(bad)) == 0


def test_missing_overlay_file_degrades_to_an_empty_index(monkeypatch):
    monkeypatch.setattr(drug_zh_alias, "DEFAULT_OVERLAY_PATH", Path("no_such_dir") / "missing.csv")
    drug_zh_alias.load_default_index.cache_clear()
    try:
        assert len(drug_zh_alias.load_default_index()) == 0
    finally:
        drug_zh_alias.load_default_index.cache_clear()


# ============================================================ runtime：pipeline


CATALOG_ROWS = [
    {"ingredient_code": "K1", "official_name": "Catalogonly-Q", "normalized_exact_name": "catalogonly-q", "active_permit_count": 9, "rank": 1,
     "interaction_mapping_status": STATUS_NOT_MAPPED, "mapped_drug_substance_id": None},
]
OVERLAY_ROWS = [
    row("甲試藥"),                                                     # → DrugSubstance TD_A
    row("目錄專用藥", kind="TAIWAN_DRUG_INGREDIENT", entity="", code="K1", official="Catalogonly-Q"),   # → 名錄項目
    row("目錄錯代碼藥", kind="TAIWAN_DRUG_INGREDIENT", entity="", code="K_OTHER", official="Catalogonly-Q"),  # 代碼與名錄不一致
    row("鹽試藥甲", status=STATUS_REJECTED),                            # 鹽類不併入母體
    row("待審藥", status=STATUS_NEEDS_REVIEW),
]


@pytest.fixture
def overlay(monkeypatch):
    monkeypatch.setattr("app.services.interaction_service.load_default_index", lambda: DrugZhAliasIndex(OVERLAY_ROWS))


def ask(subjects, catalog=CATALOG_ROWS, kind=InteractionSubjectKind.DRUG):
    repo = InMemoryInteractionRepo()
    repo.catalog = catalog
    ex = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE, interaction_subjects=[
        InteractionSubjectExtraction(text=t, kind=kind) for t in subjects])
    pipeline, _ = build(repo=repo, ex=ex)
    return pipeline.run(RawUserInput(input_type=InputType.TEXT, text=" 跟 ".join(subjects) + " 可以一起吃嗎"))


def subject(result, text):
    return next(s for s in research_of(result).subjects if s.text == text)


def test_verified_traditional_chinese_alias_resolves_to_the_canonical_drug_substance(overlay):
    result = ask(["甲試藥", "Testdrug-B"])
    sub = subject(result, "甲試藥")
    assert sub.status == SubjectResolutionStatus.RESOLVED and [e.entity_id for e in sub.entities] == ["TD_A"]
    assert research_of(result).coverage_gap_subjects == []


def test_verified_alias_continues_through_the_existing_interaction_flow(overlay):
    result = ask(["甲試藥", "鋅"], kind=InteractionSubjectKind.INGREDIENT)  # 與英文 canonical 名稱走同一條 interaction 流程
    r = research_of(result)
    assert subject(result, "甲試藥").status == SubjectResolutionStatus.RESOLVED
    assert any(f.interaction_id == "TI_ZN_DRUG" for f in r.findings)


def test_verified_catalog_alias_is_recognized_as_a_taiwan_official_drug_ingredient(overlay):
    result = ask(["目錄專用藥", "Testdrug-B"])
    sub = subject(result, "目錄專用藥")
    assert sub.status == SubjectResolutionStatus.IDENTITY_NOT_IN_INTERACTION_CORPUS and sub.catalog_official_name == "Catalogonly-Q"


def test_catalog_only_chinese_alias_without_interaction_coverage_returns_the_safe_gap_response(overlay):
    result = ask(["目錄專用藥", "Testdrug-B"])
    r = research_of(result)
    assert r.coverage_gap_subjects == ["目錄專用藥"] and r.catalog_recognized == {"目錄專用藥": "Catalogonly-Q"}
    assert r.findings == [] and r.conclusion_eligible == [] and not r.corpus_sufficient
    assert all(f.conclusion_status != InteractionConclusionStatus.CONCLUSION_ELIGIBLE for f in r.findings)
    text = result.response.plain_text()
    assert "已辨識到台灣官方藥品成分『Catalogonly-Q』，但目前結構化交互作用資料尚未收錄這組關係。" in text
    for forbidden in ("可以放心", "沒有交互作用", "安全", "有害", "找不到這個藥品成分"):
        assert forbidden not in text.replace("不能當作可以放心", "")


def test_catalog_alias_whose_code_disagrees_with_the_catalog_is_not_used(overlay):
    sub = subject(ask(["目錄錯代碼藥", "Testdrug-B"], catalog=[{**CATALOG_ROWS[0], "ingredient_code": "K9"}]), "目錄錯代碼藥")
    assert sub.status == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED


def test_unknown_chinese_name_is_not_guessed_and_asks_for_the_active_ingredient(overlay):
    result = ask(["完全未收錄的譯名", "Testdrug-B"])
    sub = subject(result, "完全未收錄的譯名")
    assert sub.status == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED and not sub.entities and sub.catalog_official_name is None
    text = result.response.plain_text()
    assert "有效成分" in text and "已辨識到台灣官方藥品成分" not in text


def test_salt_or_form_variant_is_not_auto_mapped_to_the_parent(overlay):
    for text in ("鹽試藥甲", "待審藥", "甲試藥錠", "鹽酸甲試藥"):  # REJECTED / NEEDS_REVIEW / 劑型字尾 / 鹽類前綴
        sub = subject(ask([text, "Testdrug-B"]), text)
        assert sub.status == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED and not sub.entities, text


def test_simplified_wording_is_not_auto_mapped(overlay):
    sub = subject(ask(["甲试药", "Testdrug-B"]), "甲试药")
    assert sub.status != SubjectResolutionStatus.RESOLVED and not sub.entities  # 最多只是「需要確認」的候選（見 recovery 測試）


def test_without_any_overlay_behaviour_is_unchanged(monkeypatch):
    monkeypatch.setattr("app.services.interaction_service.load_default_index", lambda: DrugZhAliasIndex())
    sub = subject(ask(["目錄專用藥", "Testdrug-B"]), "目錄專用藥")
    assert sub.status == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED and sub.catalog_official_name is None


def test_english_catalog_lookup_still_wins_over_the_overlay(overlay):
    sub = subject(ask(["Catalogonly-Q", "Testdrug-B"]), "Catalogonly-Q")
    assert sub.status == SubjectResolutionStatus.IDENTITY_NOT_IN_INTERACTION_CORPUS and sub.catalog_official_name == "Catalogonly-Q"


# ============================================================ REAL OVERLAY：實際檔案的不變量與回歸

REAL = Path(drug_zh_alias.DEFAULT_OVERLAY_PATH)
DRUG_CSV = Path("data/ingredient_interaction/2026-10-04/drug_substances.csv")


def real_rows():
    with open(REAL, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_real_overlay_rows_have_traceability_and_only_verified_rows_are_matchable():
    rows = real_rows()
    assert rows and {r["review_status"] for r in rows} <= {STATUS_VERIFIED, STATUS_SUPPORTED, STATUS_NEEDS_REVIEW, STATUS_REJECTED}
    for r in rows:
        assert r["official_source"] and r["source_url"].split("/")[2].endswith("fda.gov.tw") and r["retrieved_at"] and r["source_locator"]
        active = r["review_status"] in (STATUS_VERIFIED, STATUS_SUPPORTED)
        assert (r["match_policy"] == MATCH_EXACT) == active, r["alias_text_zh_tw"]
        if r["review_status"] == STATUS_VERIFIED:
            assert int(r["support_permit_count"]) >= 2 and r["evidence_types"], r["alias_text_zh_tw"]
        if r["review_status"] == STATUS_SUPPORTED:
            assert r["evidence_source_url"].split("/")[2].endswith("gov.tw") and r["identity_statement"] and r["conflict_check"] == "NONE"
    # 啟用的列數 = VERIFIED + SUPPORTED（同一個鍵對到多個 identity 的歧義列由 index 排除）
    assert 0 < len(load_index(REAL)) <= sum(r["review_status"] in (STATUS_VERIFIED, STATUS_SUPPORTED) for r in rows)


def test_real_overlay_covers_every_reviewed_drug_substance_in_the_audit():
    ids = {d["drug_substance_id"] for d in csv.DictReader(open(DRUG_CSV, encoding="utf-8-sig"))}
    audited = {r["canonical_entity_id"] for r in real_rows() if r["target_kind"] == "DRUG_SUBSTANCE"}
    assert audited <= ids and len(audited) >= 14 and "D_DTG_SODIUM" not in audited  # 鹽類 entity 沒有任何自動 alias


def test_real_verified_drug_aliases_never_target_a_salt_and_salt_wording_is_not_auto_mapped():
    idx = load_index(REAL)
    for hit in idx.entries("DRUG_SUBSTANCE"):
        assert hit.canonical_entity_id != "D_DTG_SODIUM"
    for r in real_rows():
        if r["target_kind"] == "DRUG_SUBSTANCE" and r["alias_text_zh_tw"].startswith(("鹽酸", "硫酸")):
            assert r["review_status"] != STATUS_VERIFIED, r["alias_text_zh_tw"]
    assert idx.lookup("鹽酸二甲二脈").target_kind == "TAIWAN_DRUG_INGREDIENT"  # 鹽酸鹽只對應鹽酸鹽名錄項目，不是 D_MET


def test_real_regression_examples():
    idx = load_index(REAL)
    assert idx.lookup("二甲二脈").canonical_entity_id == "D_MET"
    assert idx.lookup("奧美拉唑").canonical_entity_id == "D_OME"
    assert idx.lookup("二甲雙胍") is None and idx.lookup("二甲双胍") is None  # 對岸 / 簡體寫法：沒有台灣官方成對證據
    assert idx.lookup("二甲二") is None and idx.lookup("二甲二脈錠") is None  # 不做 substring / 劑型字尾比對
    assert idx.lookup("待克菲那") is None  # 官方資料同時對應母體與鈉鹽：歧義，不自動對應
