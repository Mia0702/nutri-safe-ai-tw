"""Interaction identity-coverage gap：IDENTITY_NOT_IN_INTERACTION_CORPUS 與 UNKNOWN_TERM 分開。

* UNKNOWN_TERM（Controller 無法可靠辨識它指什麼）→ 維持 clarification，不上網猜 identity。
* IDENTITY_NOT_IN_INTERACTION_CORPUS（Controller 認得是合理的成分 / 藥品概念，但 Interaction corpus 沒有 canonical identity）
  → 不請使用者改寫法；明說「結構化交互作用資料尚未收錄」；允許受控補查（RAG 最多一次、官方網站最多一次），
  補查結果只是參考：不產生 CONCLUSION_ELIGIBLE、不建立 interaction、不暗示沒有交互作用 / 可以放心 / 有害。
全部使用 TEST_ONLY 虛構名稱；不 hard-code 任何個別成分。
"""

from app.agents.interaction_reporter import (
    FORBIDDEN_RE,
    GAP_EVIDENCE_FOUND,
    GAP_NO_EVIDENCE,
    NO_RECORD_MEANING,
    body_is_safe,
    deterministic_body,
    interaction_facts,
)
from app.agents.intake_agent import interaction_subjects_for
from app.agents.verification_agent import CLARIFICATION_MODE
from app.schemas.enums import InputType, Intent, ResponseType
from app.schemas.interaction import (
    InteractionConclusionStatus,
    InteractionMode,
    InteractionResearchStatus,
    InteractionSubjectExtraction,
    InteractionSubjectKind,
    SubjectResolutionStatus,
)
from app.schemas.user_request import IntakeExtraction, RawUserInput
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryRagStore
from tests.unit.test_interaction_runtime import (  # noqa: F401（autouse fixture：虛構產品 / Bridge mapping）
    ING,
    DRUG,
    PRODUCT,
    build,
    ingredient_data,
    research_of,
    tool_names,
)

GAP_A = "測試草本甲"  # TEST_ONLY：合理的成分概念，但不在 Interaction corpus
GAP_B = "測試草本乙"
GARBAGE = "測試亂碼丙"  # TEST_ONLY：Controller 無法辨識


def ask(subjects, rag=None, web=False, mode=InteractionMode.PAIRWISE):
    """subjects: [(原文名稱, kind, concept_recognized)]。"""
    ex = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=mode, interaction_subjects=[
        InteractionSubjectExtraction(text=t, kind=k, concept_recognized=rec) for t, k, rec in subjects])
    pipeline, _ = build(ex=ex, rag=rag, web=web)
    text = " 跟 ".join(t for t, _, _ in subjects) + " 可以一起吃嗎"
    return pipeline.run(RawUserInput(input_type=InputType.TEXT, text=text))


def card_text(result):
    return result.response.plain_text()


# ============================================================ 既有路徑不變


def test_known_corpus_entities_use_the_existing_structured_path():
    result = ask([("鋅", ING, True), ("銅", ING, True)])
    r = research_of(result)
    assert r.status == InteractionResearchStatus.COMPLETE and r.corpus_sufficient and r.coverage_gap_subjects == []
    assert r.findings[0].conclusion_status == InteractionConclusionStatus.CONCLUSION_ELIGIBLE
    assert "search_rag" not in tool_names(result) and result.evidence.fast_path_reason == "interaction_corpus:sufficient"


def test_recognized_flag_does_not_change_known_identities():
    # 即使 Controller 沒填 / 填 false，corpus 內的 identity 仍由 deterministic resolver 確認
    result = ask([("鋅", ING, False), ("銅", ING, False)])
    assert research_of(result).corpus_sufficient and research_of(result).coverage_gap_subjects == []


# ============================================================ 一邊在 corpus、一邊合理但不在 corpus


def test_known_plus_out_of_corpus_concept_is_a_coverage_gap_not_a_spelling_problem():
    result = ask([("鋅", ING, True), (GAP_A, ING, True)])
    r = research_of(result)
    gap = next(s for s in r.subjects if s.text == GAP_A)
    assert gap.status == SubjectResolutionStatus.IDENTITY_NOT_IN_INTERACTION_CORPUS
    assert r.status == InteractionResearchStatus.COMPLETE and r.coverage_gap_subjects == [GAP_A]
    assert result.response.response_type == ResponseType.INTERACTION_RESULT
    text = card_text(result)
    assert f"目前結構化交互作用資料尚未收錄『{GAP_A}』，因此無法直接用知識圖譜判定。" in text
    assert "請確認寫法" not in text and "找不到" not in text
    assert result.evidence.planner_mode != CLARIFICATION_MODE
    assert "lookup_interactions" not in tool_names(result)  # 只有一個已確認對象：沒有 corpus 配對可查
    assert r.findings == [] and r.conclusion_eligible == []


def test_two_out_of_corpus_concepts_are_the_same_coverage_gap():
    result = ask([(GAP_A, ING, True), (GAP_B, ING, True)])
    r = research_of(result)
    assert r.status == InteractionResearchStatus.COMPLETE and r.coverage_gap_subjects == [GAP_A, GAP_B]
    text = card_text(result)
    assert GAP_A in text and GAP_B in text and "尚未收錄" in text
    assert "請確認寫法" not in text and "重新輸入" not in text and "請再說明" not in text
    assert r.fallback_used and tool_names(result).count("search_rag") == 1  # 受控補查仍允許


# ============================================================ 真的無法辨識：維持 clarification


def test_truly_unknown_term_keeps_clarification_and_never_searches():
    result = ask([("鋅", ING, True), (GARBAGE, ING, False)], web=True)
    r = research_of(result)
    assert r.status == InteractionResearchStatus.NEEDS_CLARIFICATION and r.clarification_reason == "UNRESOLVED"
    assert result.evidence.planner_mode == CLARIFICATION_MODE and r.coverage_gap_subjects == []
    assert "search_rag" not in tool_names(result) and "search_tw_official_web" not in tool_names(result)
    assert not result.evidence.official_web_search_attempted
    assert result.response.response_type == ResponseType.MISSING_INFORMATION and "請確認寫法" in card_text(result)


def test_unknown_term_wins_over_a_coverage_gap_in_the_same_question():
    result = ask([(GAP_A, ING, True), (GARBAGE, ING, False)], web=True)
    assert research_of(result).status == InteractionResearchStatus.NEEDS_CLARIFICATION
    assert "search_rag" not in tool_names(result) and "請確認寫法" in card_text(result)
    assert GARBAGE in card_text(result)  # 問的是真的無法辨識的那一個


def test_unflagged_unknown_term_defaults_to_unknown_not_gap():
    assert InteractionSubjectExtraction(text="x", kind=ING).concept_recognized is False
    result = ask([("鋅", ING, True), (GAP_A, ING, False)])
    assert research_of(result).status == InteractionResearchStatus.NEEDS_CLARIFICATION


def test_controller_flag_is_carried_into_subjects():
    ex = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE, interaction_subjects=[
        InteractionSubjectExtraction(text=GAP_A, kind=ING, concept_recognized=True),
        InteractionSubjectExtraction(text=GARBAGE, kind=ING, concept_recognized=False)])
    subjects, _ = interaction_subjects_for(ex, f"{GAP_A} 跟 {GARBAGE} 可以一起吃嗎", [], [])
    assert [(s.text, s.concept_recognized) for s in subjects] == [(GAP_A, True), (GARBAGE, False)]


# ============================================================ 補查：RAG 一次、官方網站一次，只是參考


def test_rag_finds_informational_evidence_which_never_becomes_a_conclusion():
    result = ask([("鋅", ING, True), (GAP_A, ING, True)], rag=InMemoryRagStore())
    r = research_of(result)
    assert tool_names(result).count("search_rag") == 1 and result.evidence.rag_evidence
    assert GAP_EVIDENCE_FOUND in result.response.key_points
    assert r.findings == [] and r.conclusion_eligible == [] and r.pairs == []
    assert all(f.conclusion_status != InteractionConclusionStatus.CONCLUSION_ELIGIBLE for f in r.findings)
    assert "search_tw_official_web" not in tool_names(result)  # 內部證據已找到：不再查官方網站


def test_rag_insufficient_falls_back_to_official_web_at_most_once():
    result = ask([(GAP_A, ING, True), (GAP_B, ING, True)], rag=InMemoryRagStore(rows=[]), web=True)
    assert tool_names(result).count("search_rag") == 1
    assert tool_names(result).count("search_tw_official_web") <= 1 and result.evidence.official_web_search_attempted
    assert sum(1 for c in result.evidence.tools_called if c.tool_name == "search_tw_official_web") == 1
    assert GAP_NO_EVIDENCE in result.response.key_points  # 沒有相關證據：如實說證據不足
    assert research_of(result).findings == []


def test_web_fallback_is_skipped_when_disabled():
    result = ask([(GAP_A, ING, True), (GAP_B, ING, True)], rag=InMemoryRagStore(rows=[]), web=False)
    assert "search_tw_official_web" not in tool_names(result) and GAP_NO_EVIDENCE in result.response.key_points


# ============================================================ 安全：no record ≠ no interaction


def test_no_record_is_never_presented_as_no_interaction_or_safe_or_harmful():
    for rag in (InMemoryRagStore(), InMemoryRagStore(rows=[])):
        result = ask([("鋅", ING, True), (GAP_A, ING, True)], rag=rag)
        text = card_text(result).replace(NO_RECORD_MEANING, "")  # 固定說明本身是否定句（「不能當作可以放心…」）
        assert not FORBIDDEN_RE.search(text)  # 沒有「沒有交互作用 / 可以一起吃 / 可以放心 / 不能一起吃 / 過量」等說法
        assert NO_RECORD_MEANING in card_text(result) and "ELIGIBLE" not in text
        assert research_of(result).findings == [] and research_of(result).conclusion_eligible == []


def test_full_analysis_facts_and_fixed_body_keep_the_gap_non_conclusive():
    result = ask([("鋅", ING, True), (GAP_A, ING, True)], rag=InMemoryRagStore())
    facts = interaction_facts(result.normalized_request, result.evidence)
    key = next(k for k in facts if "尚未收錄的對象" in k)
    assert facts[key] == [GAP_A] and facts["交互作用紀錄"] == []
    body = deterministic_body(result.normalized_request, result.evidence)
    assert "尚未收錄" in body and body_is_safe(body, research_of(result))
    assert "沒有交互作用" not in body and "可以一起吃" not in body


def test_product_subject_that_is_not_found_is_not_a_coverage_gap():
    result = ask([("鋅", ING, True), ("測試不存在的產品名", PRODUCT, True)])
    r = research_of(result)
    assert r.status == InteractionResearchStatus.NEEDS_CLARIFICATION and r.coverage_gap_subjects == []
    assert next(s for s in r.subjects if s.kind == PRODUCT).status == SubjectResolutionStatus.PRODUCT_NEEDS_CONFIRMATION


def test_drug_name_without_active_substance_mapping_keeps_its_existing_clarification():
    """藥品寫法無法對應有效成分（可能是商品名 / 譯名）是另一個可行動的問題（請補有效成分），沿用既有 clarification。"""
    result = ask([("鋅", ING, True), (GAP_B, DRUG, True)])
    r = research_of(result)
    assert r.status == InteractionResearchStatus.NEEDS_CLARIFICATION and r.clarification_reason == "DRUG_ACTIVE_SUBSTANCE_UNMAPPED"
    assert "search_rag" not in tool_names(result)
