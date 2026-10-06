"""繁中藥品別名：官方來源分級（SUPPORTED_OFFICIAL）與錯字 / 同音字候選建議（只是候選，一律需要使用者確認）。

全部使用 TEST_ONLY 虛構名稱；沒有任何個別藥名 / 錯字的對照。最後一節檢查實際 production 檔案沒有寫死名稱。
"""

import csv
import logging
from pathlib import Path

import pytest

from app.ingestion.drug_zh_alias_evidence import (
    MATCH_EXACT,
    MATCH_NONE,
    ORIGIN_AI,
    STATUS_NEEDS_REVIEW,
    STATUS_SUPPORTED,
    STATUS_VERIFIED,
    build_overlay_rows,
    collect_official_pairs,
    harvest_catalog_candidates,
)
from app.schemas.enums import Intent, ResponseAction, ResponseType
from app.schemas.interaction import (
    InteractionMode,
    InteractionResearchStatus,
    InteractionSubjectExtraction,
    InteractionSubjectKind,
    SubjectResolutionStatus,
)
from app.services import drug_zh_alias
from app.services.drug_zh_alias import DrugZhAliasIndex
from app.services.drug_zh_typo import typo_candidates
from tests.builders import extraction
from tests.fakes import InMemoryInteractionRepo
from tests.unit.test_drug_zh_alias_overlay import PAIRS_RAW, CAND, CATALOG, DRUGS, OVERLAY_ROWS, built, permit, row
from tests.unit.test_overview_and_asr_buttons import OWNER, Chat, buttons, family_data, research  # noqa: F401（autouse fixture）
from tests.unit.test_ingredient_retrieval_runtime import ingredient_data  # noqa: F401（autouse fixture）
from tests.unit.test_interaction_reporter import texts_of

DRUG = InteractionSubjectKind.DRUG
ING = InteractionSubjectKind.INGREDIENT
CATALOG_ROW = {"ingredient_code": "K1", "official_name": "Catalogonly-Q", "normalized_exact_name": "catalogonly-q", "active_permit_count": 9,
               "rank": 1, "interaction_mapping_status": "NOT_MAPPED", "mapped_drug_substance_id": None}


def hit_rows(*aliases_and_targets):
    return DrugZhAliasIndex([row(a, entity=t) for a, t in aliases_and_targets]).entries()


def names(cands):
    return [c.alias_text for c in cands]


# ============================================================ SUPPORTED_OFFICIAL（台灣官方來源直接說明 identity）

SOURCE = {"alias_text_zh_tw": "單一證甲試藥", "target_kind": "DRUG_SUBSTANCE", "canonical_entity_id": "TD_A", "taiwan_drug_ingredient_code": "",
          "source_name": "TEST_ONLY 官方仿單平台", "source_url": "https://label.example.gov.tw/x", "source_locator": "TEST_ONLY 1.1 有效成分",
          "identity_statement": "中文品名：某錠（單一證甲試藥）；英文品名：X (Testdrug-A)；有效成分：Testdrug-A 5mg",
          "active_ingredient_in_source": "Testdrug-A", "retrieved_at": "2026-10-06T00:00:00+00:00"}
ONE_PERMIT = [permit("U1", "某錠（單一證甲試藥）", "X TABLETS (Testdrug-A)", "TESTDRUG-A")]


def supported(alias, source=SOURCE, pairs=None, **override):
    pairs = collect_official_pairs(ONE_PERMIT) if pairs is None else pairs
    cand = {**CAND, "alias_text_zh_tw": alias, **override}
    return build_overlay_rows([cand], pairs, DRUGS, CATALOG, "2026-10-06T00:00:00+00:00", [source] if source else [])[0]


def test_official_direct_identity_source_creates_supported_official_that_is_runtime_active():
    r = supported("單一證甲試藥")
    assert r["review_status"] == STATUS_SUPPORTED and r["match_policy"] == MATCH_EXACT and r["conflict_check"] == "NONE"
    assert r["evidence_source_url"].endswith("gov.tw/x") and r["identity_statement"] and r["official_source"] == "TEST_ONLY 官方仿單平台"
    assert DrugZhAliasIndex([r]).lookup("單一證甲試藥").canonical_entity_id == "TD_A"


def test_single_permit_without_a_direct_source_stays_needs_review_and_inactive():
    r = supported("單一證甲試藥", source=None)
    assert r["review_status"] == STATUS_NEEDS_REVIEW and DrugZhAliasIndex([r]).lookup("單一證甲試藥") is None


@pytest.mark.parametrize("patch", [
    {"source_url": "https://blog.example.com/x"},                       # 非台灣政府網域
    {"identity_statement": "完全沒有提到該名稱 Testdrug-A"},             # 引文裡沒有中文名稱
    {"active_ingredient_in_source": "Testdrug-A Sodium"},               # 來源標示的是鈉鹽：不在同一個 declared level
    {"active_ingredient_in_source": ""},
])
def test_source_that_does_not_establish_the_same_level_identity_is_rejected(patch):
    r = supported("單一證甲試藥", source={**SOURCE, **patch})
    assert r["review_status"] == STATUS_NEEDS_REVIEW and DrugZhAliasIndex([r]).lookup("單一證甲試藥") is None


def test_conflicting_official_identity_blocks_supported_even_with_a_source():
    raw = ONE_PERMIT + [permit("U2", "另一錠（單一證甲試藥）", "Y TABLETS (Testdrug-B)", "TESTDRUG-B")]
    r = supported("單一證甲試藥", pairs=collect_official_pairs(raw))
    assert r["review_status"] == STATUS_NEEDS_REVIEW and r["conflict_check"] == "CONFLICT"


def test_supported_row_without_traceable_source_fields_is_not_active():
    good = supported("單一證甲試藥")
    for key, bad in (("evidence_source_url", "https://example.com/"), ("identity_statement", ""), ("conflict_check", "CONFLICT")):
        assert len(DrugZhAliasIndex([{**good, key: bad}])) == 0, key


def test_ai_translation_only_creates_needs_review_candidates_that_are_never_active():
    r = supported("單一證甲試藥", source=None, candidate_origin=ORIGIN_AI)
    assert r["review_status"] == STATUS_NEEDS_REVIEW and r["candidate_origin"] == ORIGIN_AI and "AI 翻譯" in r["review_reason"]
    assert len(DrugZhAliasIndex([r])) == 0
    forged = {**r, "review_status": STATUS_SUPPORTED, "match_policy": MATCH_EXACT}  # 即使被改成 SUPPORTED：缺來源欄位 → 不啟用
    assert len(DrugZhAliasIndex([forged])) == 0


def test_harvest_requires_multiple_applicants_so_a_single_company_brand_name_is_not_collected():
    raw = [{**permit(f"B{i}", "單一藥廠品牌名", "Catalogonly-Q"), "申請商統一編號": "111"} for i in range(4)]
    raw += [{**permit(f"G{i}", "多藥廠通用名", "Catalogonly-Q"), "申請商統一編號": str(200 + i)} for i in range(4)]
    out = harvest_catalog_candidates(collect_official_pairs(raw), [{"ingredient_code": "K1", "official_name": "Catalogonly-Q"}])
    assert [c["alias_text_zh_tw"] for c in out] == ["多藥廠通用名"]


# ============================================================ typo candidate generation（純函式）


def test_one_character_typo_returns_one_reviewed_candidate():
    entries = hit_rows(("賽甲乙酮", "TD_A"), ("無關的藥名", "TD_B"))
    got = typo_candidates("賽甲乙同", entries)
    assert names(got) == ["賽甲乙酮"] and got[0].method in ("edit_distance", "edit_distance+pinyin")


def test_homophone_typo_beyond_edit_distance_is_found_by_pinyin():
    entries = hit_rows(("安利達諾平", "TD_A"))
    got = typo_candidates("安力答諾平", entries)  # 兩個字同音（edit distance 2，相似度 0.6 < 門檻）
    assert names(got) == ["安利達諾平"] and got[0].method == "pinyin"


def test_accent_confusion_is_a_lower_confidence_pinyin_candidate():
    entries = hit_rows(("詩甲乙丙", "TD_A"))
    got = typo_candidates("斯甲乙丙", entries)  # sh / s 混淆（拼音只差口音；信心低於「完全同音」）
    assert names(got) == ["詩甲乙丙"] and got[0].score < 0.9


def test_aliases_of_the_same_canonical_collapse_into_one_candidate():
    got = typo_candidates("測藥甲辛", hit_rows(("測藥甲乙", "TD_A"), ("測藥甲己", "TD_A")))
    assert len(got) == 1 and got[0].target_id == "TD_A"


def test_similar_aliases_of_different_canonicals_are_both_returned_never_auto_selected():
    got = typo_candidates("測藥甲丁", hit_rows(("測藥甲乙", "TD_A"), ("測藥甲丙", "TD_B")))
    assert sorted(c.target_id for c in got) == ["TD_A", "TD_B"]


def test_more_than_three_canonicals_or_weak_similarity_returns_nothing():
    many = hit_rows(*[(f"測藥甲{c}", f"TD_{c}") for c in "乙丙丁戊己"])
    assert typo_candidates("測藥甲庚", many) == []
    assert typo_candidates("完全不相干的詞", hit_rows(("賽甲乙酮", "TD_A"))) == []


def test_unknown_typo_outside_the_reviewed_alias_set_is_not_guessed():
    assert typo_candidates("某某某某某", hit_rows(("賽甲乙酮", "TD_A"))) == []
    assert typo_candidates("英文 text", hit_rows(("賽甲乙酮", "TD_A"))) == []  # 非純中文：不做


def test_substring_relationships_are_never_candidates():
    entries = hit_rows(("賽甲乙酮", "TD_A"))
    for text in ("賽甲乙酮錠", "賽甲乙", "鹽酸賽甲乙酮", "服用賽甲乙酮", "甲乙"):
        assert typo_candidates(text, entries) == [], text


def test_parent_and_salt_forms_are_never_crossed_by_typo_recovery():
    entries = hit_rows(("甲乙丙丁戊", "TD_A"), ("甲乙丙丁鈉", "TD_B"))  # 母體 / 鈉鹽：只差一個字，但形式字樣不同
    assert [c.target_id for c in typo_candidates("甲乙丙丁己", entries)] == ["TD_A"]
    assert [c.target_id for c in typo_candidates("甲乙丙丁鉀", entries)] == []  # 鈉 / 鉀 也是不同形式


def test_short_aliases_need_a_unique_same_pinyin_and_no_edit_candidates():
    assert typo_candidates("乙丙", hit_rows(("乙丁", "TD_A"))) == []  # 兩個字：不做編輯距離候選
    assert names(typo_candidates("鋅", hit_rows(("辛", "TD_A")))) == []  # 單字不處理


def test_script_variant_only_is_a_candidate_needing_confirmation_not_an_exact_match():
    got = typo_candidates("甲试药錠".replace("錠", ""), hit_rows(("甲試藥", "TD_A")))
    assert names(got) == ["甲試藥"] and got[0].method == "traditional_normalization"


# ============================================================ pipeline：確認、不默默更正、續跑原題

CATALOG_OVERLAY = [*OVERLAY_ROWS, row("賽甲乙酮", entity="TD_A"), row("目錄藥專用名稱", kind="TAIWAN_DRUG_INGREDIENT", entity="",
                                                                  code="K1", official="Catalogonly-Q")]


@pytest.fixture
def overlay(monkeypatch):
    monkeypatch.setattr("app.services.interaction_service.load_default_index", lambda: DrugZhAliasIndex(CATALOG_OVERLAY))


def typo_chat(typo="賽甲乙同", partner="鋅"):
    q = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE, interaction_subjects=[
        InteractionSubjectExtraction(text=typo, kind=DRUG), InteractionSubjectExtraction(text=partner, kind=ING)])
    repo = InMemoryInteractionRepo()
    repo.catalog = [CATALOG_ROW]
    return Chat({typo: q}, repo=repo)


def test_unique_typo_candidate_still_requires_confirmation_and_never_replaces_the_input(overlay):
    chat = typo_chat()
    flex, rec = chat.say("賽甲乙同和鋅可以一起吃嗎")
    sub = next(s for s in research(rec).subjects if s.text == "賽甲乙同")
    assert sub.status == SubjectResolutionStatus.ENTITY_CANDIDATE_NEEDS_CONFIRMATION and sub.recovery_method
    assert research(rec).findings == [] and rec.response.response_type == ResponseType.MISSING_INFORMATION
    text = texts_of(flex)
    assert "找不到完全相同的藥品成分名稱。" in text and "你是不是要問『賽甲乙酮』？" in text
    assert [b["label"] for b in buttons(flex["contents"])] == ["確認：賽甲乙酮", "不是，重新輸入"]
    assert sub.candidates[0].entity_id == "TD_A"  # postback 帶 canonical ID，不是按鈕文字


def test_confirmation_resumes_the_original_interaction_without_rerunning_the_controller(overlay):
    chat = typo_chat()
    flex, rec = chat.say("賽甲乙同和鋅可以一起吃嗎")
    before = chat.controller_calls
    sub = next(s for s in research(rec).subjects if s.text == "賽甲乙同")
    out = chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, sub.candidates[0].entity_id)
    after = list(chat.store._data.values())[-1]
    assert chat.controller_calls == before  # 沒有重跑 Controller
    assert after.request_id != rec.request_id and after.normalized_request.intent == Intent.INTERACTION_INQUIRY
    pinned = next(s for s in after.normalized_request.interaction_subjects if s.text == "賽甲乙同")
    assert pinned.pinned_entity_id == "TD_A"
    r = research(after)
    assert r.status == InteractionResearchStatus.COMPLETE and any(f.interaction_id == "TI_ZN_DRUG" for f in r.findings)
    assert after.response.response_type == ResponseType.INTERACTION_RESULT and "確認：" not in texts_of(out)


def test_confirmation_validates_user_original_request_and_candidate_set(overlay):
    chat = typo_chat()
    _, rec = chat.say("賽甲乙同和鋅可以一起吃嗎")
    before_other = len(chat.store._data)
    chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, "TD_A", owner="U_OTHER")  # 不同使用者的按鈕不能續跑
    assert len(chat.store._data) == before_other
    chat2 = typo_chat()
    _, rec2 = chat2.say("賽甲乙同和鋅可以一起吃嗎")
    before = len(chat2.store._data)
    chat2.press(rec2, ResponseAction.SELECT_ASR_ENTITY, "TD_B")  # 不是這一題提供的候選
    assert len(chat2.store._data) == before  # 沒有新的 request / 沒有續跑


def test_extra_character_is_a_substring_relation_so_no_candidate_while_exact_alias_is_recognized(overlay):
    _, rec = typo_chat(typo="目錄藥專用名稱錯").say("目錄藥專用名稱錯和鋅可以一起吃嗎")
    sub = next(s for s in research(rec).subjects if s.text == "目錄藥專用名稱錯")
    assert sub.status != SubjectResolutionStatus.ENTITY_CANDIDATE_NEEDS_CONFIRMATION
    _, rec = typo_chat(typo="目錄藥專用名稱").say("目錄藥專用名稱和鋅可以一起吃嗎")  # exact → 直接辨識
    assert next(s for s in research(rec).subjects if s.text == "目錄藥專用名稱").status == SubjectResolutionStatus.IDENTITY_NOT_IN_INTERACTION_CORPUS


def test_catalog_typo_candidate_confirmation_resumes(overlay):
    chat = typo_chat(typo="目錄藥專用名秤")
    flex, rec = chat.say("目錄藥專用名秤和鋅可以一起吃嗎")
    sub = next(s for s in research(rec).subjects if s.text == "目錄藥專用名秤")
    assert sub.status == SubjectResolutionStatus.ENTITY_CANDIDATE_NEEDS_CONFIRMATION and sub.candidates[0].entity_id == "CAT_K1"
    chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, "CAT_K1")
    after = list(chat.store._data.values())[-1]
    resolved = next(s for s in research(after).subjects if s.text == "目錄藥專用名秤")
    assert resolved.status == SubjectResolutionStatus.IDENTITY_NOT_IN_INTERACTION_CORPUS and resolved.catalog_official_name == "Catalogonly-Q"
    assert "已辨識到台灣官方藥品成分『Catalogonly-Q』" in after.response.plain_text()


def test_multiple_candidates_require_selection_and_more_than_three_fall_back_to_active_ingredient(monkeypatch):
    rows = [row("測藥甲乙", entity="TD_A"), row("測藥甲丙", entity="TD_B"), row("測藥甲丁", entity="TD_ANTICOAG")]
    monkeypatch.setattr("app.services.interaction_service.load_default_index", lambda: DrugZhAliasIndex(rows))
    chat = typo_chat(typo="測藥甲庚")
    flex, rec = chat.say("測藥甲庚和鋅可以一起吃嗎")
    assert "你是不是要問下面哪一個？" in texts_of(flex)
    assert [b["label"] for b in buttons(flex["contents"])][-1] == "不是，重新輸入" and len(buttons(flex["contents"])) == 4
    more = rows + [row("測藥甲戊", entity="TD_X")]
    monkeypatch.setattr("app.services.interaction_service.load_default_index", lambda: DrugZhAliasIndex(more))
    chat = typo_chat(typo="測藥甲庚")
    _, rec = chat.say("測藥甲庚和鋅可以一起吃嗎")
    sub = next(s for s in research(rec).subjects if s.text == "測藥甲庚")
    assert sub.status == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED and "有效成分" in rec.response.plain_text()


def test_unknown_typo_is_not_guessed(overlay):
    chat = typo_chat(typo="完全沒收錄的藥")
    _, rec = chat.say("完全沒收錄的藥和鋅可以一起吃嗎")
    sub = next(s for s in research(rec).subjects if s.text == "完全沒收錄的藥")
    assert sub.status == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED and not sub.candidates


def test_structured_logs_have_recovery_fields_and_no_user_text(overlay, caplog):
    caplog.set_level(logging.INFO)
    chat = typo_chat()
    _, rec = chat.say("賽甲乙同和鋅可以一起吃嗎")
    used = next(r for r in caplog.records if r.message == "drug_zh_alias_recovery_used")
    assert used.recovery_candidate_count == 1 and used.recovery_candidate_ids == ["TD_A"] and used.recovery_best_score > 0.6 and used.recovery_method
    assert "賽甲乙同" not in str(used.__dict__)
    sub = next(s for s in research(rec).subjects if s.text == "賽甲乙同")
    chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, sub.candidates[0].entity_id)
    assert any(r.message == "drug_zh_alias_recovery_confirmed" and r.recovery_confirmed for r in caplog.records)


def test_exact_alias_match_is_logged(overlay, caplog):
    caplog.set_level(logging.INFO)
    q = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE, interaction_subjects=[
        InteractionSubjectExtraction(text="賽甲乙酮", kind=DRUG), InteractionSubjectExtraction(text="鋅", kind=ING)])
    chat = Chat({"賽甲乙酮": q}, repo=InMemoryInteractionRepo())
    chat.say("賽甲乙酮和鋅可以一起吃嗎")
    rec = next(r for r in caplog.records if r.message == "drug_zh_alias_exact_match")
    assert rec.alias_target_kind == "DRUG_SUBSTANCE" and rec.alias_id


# ============================================================ production 檔案沒有寫死名稱 / 錯字對照

APP = Path(__file__).resolve().parents[2] / "app"


def test_no_hard_coded_drug_names_or_typo_mappings_in_production_code():
    real = Path(drug_zh_alias.DEFAULT_OVERLAY_PATH)
    aliases = {r["alias_text_zh_tw"] for r in csv.DictReader(open(real, encoding="utf-8")) if len(r["alias_text_zh_tw"]) >= 3}
    for rel in ("services/drug_zh_typo.py", "services/drug_zh_alias.py", "services/interaction_service.py", "agents/interaction_research.py",
                "agents/interaction_reporter.py", "ingestion/drug_zh_alias_evidence.py"):
        source = (APP / rel).read_text(encoding="utf-8")
        leaked = [a for a in aliases if a in source]
        assert not leaked, (rel, leaked[:3])
