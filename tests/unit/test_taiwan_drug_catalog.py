"""Taiwan Official Drug Ingredient Catalog：ingestion（排序 / 過濾 / identity / exact mapping）與 runtime（只是 identity，不是證據）。

全部使用 TEST_ONLY 虛構成分與合成 CSV；不 hard-code 任何真實藥名。
"""

from app.agents.interaction_reporter import coverage_gap_sentence
from app.ingestion.taiwan_drug_catalog import (
    STATUS_NOT_MAPPED,
    STATUS_REVIEWED_EXACT,
    build_catalog,
    main_ingredient_items,
    normalize_exact_name,
)
from app.schemas.enums import InputType, Intent, ResponseType
from app.schemas.interaction import (
    InteractionConclusionStatus,
    InteractionMode,
    InteractionResearchStatus,
    InteractionSubjectExtraction,
    InteractionSubjectKind,
    SubjectResolutionStatus,
)
from tests.fakes import InMemoryInteractionRepo
from tests.unit.test_interaction_runtime import ING, build, ingredient_data, research_of  # noqa: F401（autouse fixture）
from app.schemas.user_request import RawUserInput
from tests.builders import extraction


def permit(no, summary, status=""):
    return {"許可證字號": no, "註銷狀態": status, "主成分略述": summary}


def detail(no, name, code, label="處方成分"):
    return {"許可證字號": no, "處方標示": label, "成分名稱": name, "成分代碼": code}


def all_row(no, status=""):
    return {"許可證字號": no, "註銷狀態": status}


# ------------------------------------------------------------ ingestion


def test_ranking_is_distinct_permit_count_and_ignores_duplicate_permit_rows():
    cur = [permit("P1", "Alpha-X;;Beta-Y"), permit("P1", "Alpha-X;;Beta-Y"), permit("P2", "Alpha-X"), permit("P3", "Beta-Y;;Beta-Y")]
    det = [detail(p, n, c) for p, n, c in [("P1", "Alpha-X", "C1"), ("P1", "Beta-Y", "C2"), ("P2", "Alpha-X", "C1"), ("P3", "Beta-Y", "C2")]]
    b = build_catalog(cur, det, [all_row(p) for p in ("P1", "P2", "P3")])
    assert [(r["official_name"], r["active_permit_count"], r["rank"]) for r in b.ingredients] == [("Alpha-X", 2, 1), ("Beta-Y", 2, 2)]
    assert b.audit["permits_current_unique"] == 3 and b.audit["with_code"] == 2 and b.audit["without_code"] == 0


def test_permits_cancelled_in_the_all_permits_dataset_are_excluded():
    cur = [permit("P1", "Alpha-X"), permit("P2", "Alpha-X")]
    det = [detail("P1", "Alpha-X", "C1"), detail("P2", "Alpha-X", "C1")]
    b = build_catalog(cur, det, [all_row("P1"), all_row("P2", "已註銷")])
    assert b.ingredients[0]["active_permit_count"] == 1 and b.audit["excluded_cancelled_in_9122"] == 1


def test_only_listed_main_ingredients_count_excipient_detail_rows_are_not_guessed():
    cur = [permit("P1", "Alpha-X")]
    det = [detail("P1", "Alpha-X", "C1"), detail("P1", "Filler-Z", "C9", label="賦形劑")]
    b = build_catalog(cur, det, [all_row("P1")])
    assert [r["official_name"] for r in b.ingredients] == ["Alpha-X"]


def test_salt_variants_stay_distinct_identities_and_names_are_never_translated_or_stripped():
    cur = [permit("P1", "Alpha-X"), permit("P2", "Alpha-X HCL")]
    det = [detail("P1", "Alpha-X", "C1"), detail("P2", "Alpha-X HCL", "C2")]
    b = build_catalog(cur, det, [all_row("P1"), all_row("P2")])
    assert {r["normalized_exact_name"] for r in b.ingredients} == {"alpha-x", "alpha-x hcl"}


def test_ingredient_code_is_identity_even_when_permit_names_differ_in_case_and_spacing():
    cur = [permit("P1", "Alpha-X"), permit("P2", "ALPHA-X ")]
    det = [detail("P1", "Alpha-X", "C1"), detail("P2", "ALPHA-X", "C1")]
    b = build_catalog(cur, det, [all_row("P1"), all_row("P2")])
    assert len(b.ingredients) == 1 and b.ingredients[0]["active_permit_count"] == 2 and b.ingredients[0]["ingredient_code"] == "C1"


def test_name_without_a_code_is_kept_by_name_only_never_given_a_guessed_code():
    b = build_catalog([permit("P1", "Alpha-X")], [], [all_row("P1")])
    assert b.ingredients[0]["ingredient_code"] is None and b.audit["without_code"] == 1


def test_top_n_cuts_after_ranking_with_deterministic_tie_break():
    cur = [permit("P1", "A1;;B1;;C1"), permit("P2", "A1;;B1")]
    det = [detail(p, n, "K" + n) for p, n in [("P1", "A1"), ("P1", "B1"), ("P1", "C1"), ("P2", "A1"), ("P2", "B1")]]
    b = build_catalog(cur, det, [all_row("P1"), all_row("P2")], top_n=2)
    assert [r["official_name"] for r in b.ingredients] == ["A1", "B1"] and b.audit["cutoff_permit_count"] == 2


def test_all_ties_at_the_nominal_cutoff_are_kept_and_selection_is_deterministic():
    cur = [permit("P1", "A1;;B1;;C1;;D1"), permit("P2", "A1;;B1;;C1;;D1"), permit("P3", "B1;;C1;;D1"), permit("P4", "E1")]
    det = [detail(p, n, "K" + n) for p, ns in [("P1", "A1 B1 C1 D1"), ("P2", "A1 B1 C1 D1"), ("P3", "B1 C1 D1"), ("P4", "E1")] for n in ns.split()]
    args = (cur, det, [all_row(p) for p in ("P1", "P2", "P3", "P4")])
    b = build_catalog(*args, top_n=2)  # nominal 第 2 名門檻 = 3 張；B1、C1、D1 並列 → 全部保留（3 筆 > 2），A1（2 張）不收
    assert [r["official_name"] for r in b.ingredients] == ["B1", "C1", "D1"]
    assert b.audit["selected_count"] == 3 and b.audit["cutoff_permit_count"] == 3 and b.audit["tie_count_at_cutoff"] == 3
    assert [r["official_name"] for r in build_catalog(*args, top_n=2).ingredients] == [r["official_name"] for r in b.ingredients]
    assert [r["rank"] for r in b.ingredients] == [1, 2, 3]
    assert len(build_catalog(*args, top_n=10).ingredients) == 5  # 不足 top_n：全部收錄


REVIEWED = [{"drug_substance_id": "TD_A", "canonical_name_en": "Testdrug-A"}, {"drug_substance_id": "TD_B", "canonical_name_en": "Testdrug-B"}]


def test_reviewed_mapping_only_for_exact_normalized_name_salt_variants_are_candidates_only():
    cur = [permit("P1", "TESTDRUG-A"), permit("P2", "Testdrug-B Sodium")]
    det = [detail("P1", "TESTDRUG-A", "C1"), detail("P2", "Testdrug-B Sodium", "C2")]
    b = build_catalog(cur, det, [all_row("P1"), all_row("P2")], reviewed_drugs=REVIEWED)
    by_name = {r["official_name"]: r for r in b.ingredients}
    assert by_name["TESTDRUG-A"]["interaction_mapping_status"] == STATUS_REVIEWED_EXACT
    assert by_name["TESTDRUG-A"]["mapped_drug_substance_id"] == "TD_A"
    assert by_name["Testdrug-B Sodium"]["interaction_mapping_status"] == STATUS_NOT_MAPPED
    assert by_name["Testdrug-B Sodium"]["mapped_drug_substance_id"] is None
    assert [v["drug_substance_id"] for v in b.variant_candidates] == ["TD_B"]  # 只列出供人工審核


def test_helpers():
    assert main_ingredient_items(" A ;; B ;;") == ["A", "B"] and main_ingredient_items(None) == []
    assert normalize_exact_name("  ＡBC   d ") == "abc d"


# ------------------------------------------------------------ runtime：只是 identity，不是證據

CATALOG = [
    {"ingredient_code": "K1", "official_name": "Catalogonly-Q", "normalized_exact_name": "catalogonly-q", "active_permit_count": 9,
     "rank": 1, "interaction_mapping_status": STATUS_NOT_MAPPED, "mapped_drug_substance_id": None},
    {"ingredient_code": "K2", "official_name": "Catalogmapped-M", "normalized_exact_name": "catalogmapped-m", "active_permit_count": 8,
     "rank": 2, "interaction_mapping_status": STATUS_REVIEWED_EXACT, "mapped_drug_substance_id": "TD_A"},
]


def ask(subjects, catalog=CATALOG):
    repo = InMemoryInteractionRepo()
    repo.catalog = catalog
    ex = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE, interaction_subjects=[
        InteractionSubjectExtraction(text=t, kind=InteractionSubjectKind.DRUG) for t in subjects])
    pipeline, _ = build(repo=repo, ex=ex)
    return pipeline.run(RawUserInput(input_type=InputType.TEXT, text=" 跟 ".join(subjects) + " 可以一起吃嗎"))


def test_catalog_member_without_mapping_is_a_coverage_gap_with_official_recognition_wording():
    result = ask(["Catalogonly-Q", "Testdrug-B"])
    r = research_of(result)
    sub = next(s for s in r.subjects if s.text == "Catalogonly-Q")
    assert sub.status == SubjectResolutionStatus.IDENTITY_NOT_IN_INTERACTION_CORPUS and sub.catalog_official_name == "Catalogonly-Q"
    assert r.coverage_gap_subjects == ["Catalogonly-Q"] and r.catalog_recognized == {"Catalogonly-Q": "Catalogonly-Q"}
    text = result.response.plain_text()
    assert "已辨識到台灣官方藥品成分『Catalogonly-Q』，但目前結構化交互作用資料尚未收錄這組關係。" in text
    assert "找不到這個藥品成分" not in text and "請確認寫法" not in text


def test_catalog_membership_never_creates_findings_evidence_or_eligibility():
    result = ask(["Catalogonly-Q", "Testdrug-B"])
    r = research_of(result)
    assert r.findings == [] and r.conclusion_eligible == [] and not r.corpus_sufficient
    assert all(f.conclusion_status != InteractionConclusionStatus.CONCLUSION_ELIGIBLE for f in r.findings)
    assert result.response.response_type == ResponseType.INTERACTION_RESULT
    for forbidden in ("可以放心", "沒有交互作用", "安全", "有害"):
        assert forbidden not in result.response.plain_text().replace("不能當作可以放心", "")


def test_catalog_member_with_reviewed_mapping_continues_through_the_existing_interaction_runtime():
    result = ask(["Catalogmapped-M", "Testdrug-B"])
    r = research_of(result)
    sub = next(s for s in r.subjects if s.text == "Catalogmapped-M")
    assert sub.status == SubjectResolutionStatus.RESOLVED and [e.entity_id for e in sub.entities] == ["TD_A"]
    assert r.coverage_gap_subjects == [] and r.catalog_recognized == {}


def test_without_a_catalog_behaviour_is_unchanged():
    r = research_of(ask(["Catalogonly-Q", "Testdrug-B"], catalog=None))
    sub = next(s for s in r.subjects if s.text == "Catalogonly-Q")
    assert sub.status == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED and sub.catalog_official_name is None
    assert r.status == InteractionResearchStatus.NEEDS_CLARIFICATION


def test_catalog_lookup_failure_degrades_to_the_existing_behaviour():
    repo = InMemoryInteractionRepo()
    repo.catalog = CATALOG
    repo.find_taiwan_drug_ingredient = lambda text: (_ for _ in ()).throw(RuntimeError("boom"))
    ex = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE, interaction_subjects=[
        InteractionSubjectExtraction(text=t, kind=InteractionSubjectKind.DRUG) for t in ("Catalogonly-Q", "Testdrug-B")])
    pipeline, _ = build(repo=repo, ex=ex)
    result = pipeline.run(RawUserInput(input_type=InputType.TEXT, text="Catalogonly-Q 跟 Testdrug-B 可以一起吃嗎"))
    sub = next(s for s in research_of(result).subjects if s.text == "Catalogonly-Q")
    assert sub.status == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED


def test_gap_sentence_uses_catalog_wording_only_when_every_gap_subject_is_recognized():
    assert coverage_gap_sentence(["甲"], {"甲": "Official-A"}) == "已辨識到台灣官方藥品成分『Official-A』，但目前結構化交互作用資料尚未收錄這組關係。"
    assert coverage_gap_sentence(["甲", "乙"], {"甲": "Official-A"}).startswith("目前結構化交互作用資料尚未收錄")
    assert coverage_gap_sentence(["甲"]).startswith("目前結構化交互作用資料尚未收錄")
