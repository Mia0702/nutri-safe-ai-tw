"""Interaction 呈現層（不改資料）：「為什麼？」固定區塊、證據漸進揭露（摘要 → 全部證據）。

* 「為什麼？」：🔎 可能影響 / 💡 建議 / 📌 適用條件 / ℹ️ 其他說明；沒有內容的區塊省略；全部取自已保存的結構化欄位，不由 LLM 改寫。
* 「查看證據」預設是簡短摘要（證據總數、整體狀態、最多 2 筆代表性證據）；「查看全部證據」才是完整明細。
* 所有證據 postback 都是 deterministic：Researcher / RAG / 官方網站 / LLM 皆 0 次。
全部使用 TEST_ONLY 虛構資料。
"""

import pytest

from app.agents import interaction_reporter as ir
from app.response.actions import parse_postback_data, reachable_actions
from app.schemas.enums import ResponseAction
from app.schemas.interaction import (
    Applicability,
    EvidenceDirectionRole,
    InteractionConclusionStatus as Status,
    InteractionEvidenceItem,
    InteractionFinding,
)
from tests.unit.test_interaction_reporter import CONFLICT_Q, ELIGIBLE_Q, TOGETHER, Service, ingredient_data, postbacks, texts_of  # noqa: F401


def item(eid, role=EvidenceDirectionRole.SUPPORTS, *, official=True, certainty="MODERATE", use="DECISION_ELIGIBLE", title=None):
    support = role in (EvidenceDirectionRole.SUPPORTS, EvidenceDirectionRole.SUPPORTS_QUALIFIED)
    return InteractionEvidenceItem(
        evidence_id=eid, source_id=f"S-{eid}", source_title=title or f"TEST_ONLY 來源 {eid}", authority="TEST_ONLY 機構",
        source_type="OFFICIAL_FACT_SHEET", is_official=official, supports_direction="SUPPORTS" if support else "MIXED",
        direction_role=role, counts_as_support=support, evidence_use=use, evidence_level="OFFICIAL_SYNTHESIS", certainty=certainty,
        source_retrieval_status="OK", result_summary_zh=f"TEST_ONLY 摘要 {eid}", limitations_zh=f"TEST_ONLY 限制 {eid}")


def finding(status, evidence=(), **fields):
    base = dict(interaction_id="TEST_I", record_kind="PAIRWISE_INTERACTION", interaction_type="X", directionality="ONE_WAY",
                participants=[], severity="MODERATE", certainty="MODERATE", evidence_level="OFFICIAL_SYNTHESIS",
                evidence_use="DECISION_ELIGIBLE", review_status="READY", decision_mode="CONDITIONAL_MESSAGE_ONLY",
                deterministic_conclusion_allowed=True, conclusion_status=status, evidence=list(evidence),
                applicability=Applicability(conditions="", identity_match="DIRECT"))
    base.update(fields)
    return InteractionFinding(**base)


NAMES = ir._Names(type("R", (), {"subjects": [], "findings": [], "duplicates": []})())


# ============================================================ 為什麼？：固定區塊


def test_why_sections_map_structured_fields_in_fixed_order():
    f = finding(Status.CONCLUSION_ELIGIBLE, clinical_effect="TEST_ONLY 影響", recommendation="TEST_ONLY 建議",
                mechanism_summary="TEST_ONLY 機制",
                applicability=Applicability(conditions="TEST_ONLY 條件", timing_advice="TEST_ONLY 間隔", dose_context="TEST_ONLY 劑量背景",
                                            population_context="TEST_ONLY 族群背景", identity_match="DIRECT"))
    sections = ir.why_sections(f, NAMES)
    assert [s.split("\n")[0] for s in sections] == ["🔎 可能影響", "💡 建議", "📌 適用條件", "ℹ️ 其他說明"]
    by_title = {s.split("\n")[0]: s.split("\n")[1:] for s in sections}
    assert by_title["🔎 可能影響"] == ["TEST_ONLY 影響"]
    assert by_title["💡 建議"] == ["TEST_ONLY 建議", "TEST_ONLY 間隔"]
    assert by_title["📌 適用條件"] == ["TEST_ONLY 條件"]
    assert "TEST_ONLY 機制" in by_title["ℹ️ 其他說明"] and ir.APPLICABILITY_NOT_EVALUATED in by_title["ℹ️ 其他說明"]


def test_empty_sections_are_omitted_and_conditions_only_show_for_conclusion_eligible():
    only_recommendation = finding(Status.NEEDS_REVIEW, recommendation="TEST_ONLY 建議",
                                  applicability=Applicability(conditions="TEST_ONLY 條件（待審，不當結論）", identity_match="DIRECT"))
    sections = ir.why_sections(only_recommendation, NAMES)
    assert [s.split("\n")[0] for s in sections] == ["💡 建議"]  # 沒有影響 / 其他說明；待審紀錄不顯示適用條件
    assert ir.why_sections(finding(Status.NEEDS_REVIEW), NAMES) == []


def test_why_card_has_sections_and_is_deterministic(monkeypatch):
    s = Service(ELIGIBLE_Q, question=TOGETHER)
    _, rec = s.send()
    llm_calls, repo_calls = list(s.llm.calls), list(s.repo.calls)
    monkeypatch.setattr(s.pipeline, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no rerun")))
    why = texts_of(s.action(rec, ResponseAction.WHY))
    assert "🔎 可能影響" in why and "ℹ️ 其他說明" in why and "資料庫記載的影響" not in why
    assert why.index("🔎 可能影響") < why.index("ℹ️ 其他說明")
    assert s.llm.calls == llm_calls and s.repo.calls == repo_calls


# ============================================================ 證據：摘要（漸進揭露）


def test_conflict_summary_picks_one_supporting_and_one_opposing_item():
    f = finding(Status.EVIDENCE_CONFLICT, [
        item("E1", EvidenceDirectionRole.SUPPORTS, certainty="LOW"), item("E2", EvidenceDirectionRole.SUPPORTS, certainty="HIGH"),
        item("E3", EvidenceDirectionRole.NOT_SUPPORTING, official=False), item("E4", EvidenceDirectionRole.MIXED),
        item("E5", EvidenceDirectionRole.CONTEXT_ONLY, use="CONTEXT_ONLY")])
    picked = ir.representative_evidence(f)
    assert len(picked) == 2
    assert picked[0].counts_as_support and picked[0].evidence_id == "E2"  # 最強的支持證據（HIGH）
    assert picked[1].direction_role in (EvidenceDirectionRole.NOT_SUPPORTING, EvidenceDirectionRole.MIXED)
    assert picked[1].evidence_id == "E4"  # 官方優先的相反 / 不一致證據


def test_eligible_summary_has_at_most_two_decision_eligible_supporting_items():
    f = finding(Status.CONCLUSION_ELIGIBLE, [
        item("E1", certainty="LOW"), item("E2", certainty="HIGH"), item("E3", certainty="MODERATE"),
        item("E4", EvidenceDirectionRole.SUPPORTS, use="SUPPORTING", certainty="HIGH"), item("E5", EvidenceDirectionRole.MIXED)])
    picked = ir.representative_evidence(f)
    assert [e.evidence_id for e in picked] == ["E2", "E3"]  # DECISION_ELIGIBLE 且最強的兩筆
    assert all(e.evidence_use == "DECISION_ELIGIBLE" and e.counts_as_support for e in picked)


def test_needs_review_summary_has_at_most_two_items_supporting_first():
    f = finding(Status.NEEDS_REVIEW, [item("E1", EvidenceDirectionRole.MIXED), item("E2"), item("E3"), item("E4", EvidenceDirectionRole.NOT_SUPPORTING)])
    assert [e.evidence_id for e in ir.representative_evidence(f)] == ["E2", "E3"]
    assert len(ir.representative_evidence(finding(Status.NEEDS_REVIEW, [item("E1")]))) == 1


def test_evidence_postback_is_a_short_summary_with_full_evidence_one_click_away():
    s = Service(CONFLICT_Q, question=TOGETHER)
    _, rec = s.send()
    total = ir.evidence_total(rec.evidence)
    assert total > ir.EVIDENCE_SUMMARY_MAX_ITEMS
    summary = s.action(rec, ResponseAction.INTERACTION_EVIDENCE)
    text = texts_of(summary)
    assert f"共 {total} 筆證據" in text and "整體：" in text
    assert text.count("證據方向：") == 2  # 最多 2 筆代表性證據
    assert "限制：" not in text and "來源類型" not in text  # 預設不顯示限制 / 中繼資料
    assert "支持此交互作用紀錄" in text and any(w in text for w in ("未發現明顯影響", "結果不一致"))  # 一支持 + 一相反 / 不一致
    assert "查看全部證據" in text and "查看資料來源" in text
    actions = [parse_postback_data(d).action for d in postbacks(summary)]
    assert actions == [ResponseAction.INTERACTION_EVIDENCE_ALL, ResponseAction.SOURCES]
    assert set(actions) <= reachable_actions(rec.response.available_actions)  # 沿用 request-scoped 按鈕權限


def test_full_evidence_is_still_accessible_and_complete():
    s = Service(CONFLICT_Q, question=TOGETHER)
    _, rec = s.send()
    full = texts_of(s.action(rec, ResponseAction.INTERACTION_EVIDENCE_ALL))
    expected = {e.evidence_id for f in rec.evidence.interaction_research.findings for e in f.evidence}
    assert full.count("證據方向：") >= len(expected) - 1 and "限制：" in full  # 逐筆明細（超過上限者會註明未列出）
    assert "TEST_ONLY 來源" in full and "查看資料來源" in full


def test_small_evidence_set_does_not_offer_a_redundant_full_evidence_button():
    s = Service(ELIGIBLE_Q, question=TOGETHER)
    _, rec = s.send()
    assert ir.evidence_total(rec.evidence) <= ir.EVIDENCE_SUMMARY_MAX_ITEMS
    summary = s.action(rec, ResponseAction.INTERACTION_EVIDENCE)
    assert "查看全部證據" not in texts_of(summary) and "另有" not in texts_of(summary)


def test_evidence_and_why_postbacks_make_no_external_calls(monkeypatch):
    s = Service(CONFLICT_Q, question=TOGETHER)
    _, rec = s.send()
    llm_calls, repo_calls = list(s.llm.calls), list(s.repo.calls)
    monkeypatch.setattr(s.pipeline, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no rerun")))
    monkeypatch.setattr(s.pipeline, "resume_with_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no research")))
    for action in (ResponseAction.INTERACTION_EVIDENCE, ResponseAction.INTERACTION_EVIDENCE_ALL, ResponseAction.WHY, ResponseAction.SOURCES):
        s.action(rec, action)
    assert s.llm.calls == llm_calls and s.repo.calls == repo_calls  # Researcher / RAG / web / LLM 全部 0
    assert not any(c[0] in ("embed", "web_search_structured", "plan_step", "generate_text") for c in s.llm.calls[len(llm_calls):])


def test_evidence_all_button_is_rejected_for_other_users_and_unoffered_cards():
    s = Service(CONFLICT_Q, question=TOGETHER)
    _, rec = s.send()
    from app.response.service import EXPIRED_ACTION_TEXT

    assert EXPIRED_ACTION_TEXT in texts_of(s.action(rec, ResponseAction.INTERACTION_EVIDENCE_ALL, owner="U_OTHER"))
