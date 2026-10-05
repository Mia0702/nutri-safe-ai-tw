"""Interaction Runtime Phase 2：Researcher Agent tools（Agent B interaction 研究路徑）。

全部 TEST_ONLY 虛構產品 / entity（TN_ / TF_ / TD_ / TI_），不呼叫外部服務、不寫任何 DB。
* identity 全部 deterministic（成分 Bridge exact、藥品 corpus exact、產品既有 resolver）；任一對象未確認 → clarification，不研究。
* 只有 CONCLUSION_ELIGIBLE 可肯定陳述；衝突 / 待審 / 輔助 / lineage-only 只能是說明；NO_CORPUS_RECORD ≠ 沒有交互作用。
* 重複成分只代表「存在」：dose_evaluation = NOT_EVALUATED，並列出禁止推論事項。
"""

import json

import pytest

from app.agents.evidence_tools import INTERACTION_TOOLS, PlannerState
from app.agents.intake_agent import IntakeAgent, interaction_subjects_for
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import CLARIFICATION_MODE, VerificationAgent
from app.pipeline import AnalysisPipeline, request_for_selected_product
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import InputType, Intent, ProductVerificationStatus
from app.schemas.interaction import (
    DoseEvaluation,
    EvidenceDirectionRole,
    InteractionConclusionStatus,
    InteractionMode,
    InteractionResearchStatus,
    InteractionSubjectExtraction,
    InteractionSubjectKind,
    PairStatus,
    ProhibitedInference,
    SubjectResolutionStatus,
    direction_role,
)
from app.schemas.user_request import IntakeExtraction, RawUserInput
from app.services.interaction_service import DIRECT, VIA_FORM, EntityHandle, InteractionService, Side
from app.tools import interaction_tools
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryInteractionRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import interaction_data as idata
from tests.unit.test_ingredient_retrieval_runtime import (  # noqa: F401（autouse fixture：虛構產品 / Bridge mapping）
    DOM_NAME,
    EPA_NAME,
    FOLATE_NAME,
    HF_ZINC,
    IMP_NAME,
    ingredient_data,
)

ING, DRUG, PRODUCT = InteractionSubjectKind.INGREDIENT, InteractionSubjectKind.DRUG, InteractionSubjectKind.PRODUCT


def build(repo=None, rag=None, llm=None, ex=None, web=False):
    llm = llm or FakeLLM(structured={IntakeExtraction: ex} if ex is not None else {})
    verifier = VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), rag or InMemoryRagStore(), llm,
                                 enable_official_web=web, interaction_repo=repo if repo is not None else InMemoryInteractionRepo())
    return AnalysisPipeline(intake=IntakeAgent(llm), verifier=verifier, risk_engine=RiskEngine(), reporter=ReporterAgent(llm)), verifier


def ask(subjects, mode=InteractionMode.PAIRWISE, ingredients=(), **kw):
    """subjects: [(原文名稱, kind)]；使用者原文必須包含這些名稱（與真實 Agent A 相同）。"""
    ex = extraction(intent=Intent.INTERACTION_INQUIRY, ingredient_candidates=list(ingredients), interaction_mode=mode,
                    interaction_subjects=[InteractionSubjectExtraction(text=t, kind=k) for t, k in subjects])
    pipeline, verifier = build(ex=ex, **kw)
    text = " 跟 ".join(t for t, _ in subjects) + " 可以一起吃嗎"
    return pipeline.run(RawUserInput(input_type=InputType.TEXT, text=text))


def research_of(result):
    return result.evidence.interaction_research


def service(repo=None):
    return InteractionService(repo or InMemoryInteractionRepo(), InMemoryOfficialRepo().get_ingredient_bridge_reference())


def finding(result, iid):
    return next(f for f in research_of(result).findings if f.interaction_id == iid)


def tool_names(result):
    return [t.tool_name for t in result.evidence.tools_called]


# ============================================================ A / B：pairwise


def test_eligible_ingredient_ingredient_with_applicability():
    r = research_of(ask([("鋅", ING), ("銅", ING)]))
    assert r.status == InteractionResearchStatus.COMPLETE and r.corpus_sufficient
    f = next(f for f in r.findings if f.interaction_id == "TI_ZN_CU")
    assert f.conclusion_status == InteractionConclusionStatus.CONCLUSION_ELIGIBLE and f.deterministic_conclusion_allowed
    assert f.applicability.conditions.startswith("TEST_ONLY 條件") and f.applicability.evaluated is False
    assert f.applicability.identity_match == DIRECT
    assert (f.severity, f.certainty, f.evidence_level, f.review_status) == ("MODERATE", "MODERATE", "OFFICIAL_SYNTHESIS", "READY")
    assert {p.role for p in f.participants} == {"AFFECTING", "AFFECTED"} and f.supporting_evidence_count == 1
    assert [p.status for p in r.pairs] == [PairStatus.RECORDS_FOUND]


def test_eligible_ingredient_drug():
    result = ask([("鋅", ING), ("Testdrug-A", DRUG)])
    r = research_of(result)
    assert finding(result, "TI_ZN_DRUG").conclusion_status == InteractionConclusionStatus.CONCLUSION_ELIGIBLE
    assert {s.entities[0].kind for s in r.subjects} == {"NutrientGroup", "DrugSubstance"}
    assert "lookup_interactions" in tool_names(result)
    # 只有 TI_ZN_DRUG 與此組有關（TI_MG_DRUG 是鎂）
    assert [f.interaction_id for f in r.findings] == ["TI_ZN_DRUG"]


def test_conflicting_pair_is_never_conclusive_and_keeps_directions():
    result = ask([("魚油", ING), ("Testdrug-Anticoag", DRUG)])
    f = finding(result, "TI_FISH_ANTICOAG")
    assert f.conclusion_status == InteractionConclusionStatus.EVIDENCE_CONFLICT and not f.deterministic_conclusion_allowed
    assert (f.review_status, f.certainty, f.evidence_use) == ("EVIDENCE_CONFLICT", "CONFLICTING", "NEEDS_REVIEW")
    by_dir = {e.supports_direction: e for e in f.evidence}
    assert set(by_dir) == {"MIXED", "SUPPORTS", "NO_SIGNIFICANT_EFFECT", "CONTEXT_NOT_PAIR_SPECIFIC"}
    assert by_dir["NO_SIGNIFICANT_EFFECT"].direction_role == EvidenceDirectionRole.NOT_SUPPORTING and not by_dir["NO_SIGNIFICANT_EFFECT"].counts_as_support
    assert by_dir["MIXED"].direction_role == EvidenceDirectionRole.MIXED and not by_dir["MIXED"].counts_as_support
    assert by_dir["SUPPORTS"].counts_as_support and by_dir["CONTEXT_NOT_PAIR_SPECIFIC"].direction_role == EvidenceDirectionRole.CONTEXT_ONLY
    assert (f.supporting_evidence_count, f.non_supporting_evidence_count) == (1, 3)
    assert research_of(result).conclusion_eligible == []


def test_needs_review_and_context_only_are_explanation_only():
    mg = finding(ask([("鎂", ING), ("Testdrug-A", DRUG)]), "TI_MG_DRUG")
    assert mg.conclusion_status == InteractionConclusionStatus.NEEDS_REVIEW and not mg.deterministic_conclusion_allowed
    ctx = finding(ask([("葉酸營養素群", ING), ("銅", ING)]), "TI_B9_CU")
    assert ctx.conclusion_status == InteractionConclusionStatus.INSUFFICIENT_FOR_CONCLUSION
    assert ctx.evidence[0].evidence_use == "CONTEXT_ONLY"


def test_data_flag_cannot_be_overridden_by_evidence_or_identity():
    svc = service()
    row = dict(idata.INTERACTIONS[1])
    ev = svc.evidence_for(["TI_ZN_DRUG"])["TI_ZN_DRUG"]
    assert svc.conclusion_status(row, ev, DIRECT, True) == InteractionConclusionStatus.CONCLUSION_ELIGIBLE
    assert svc.conclusion_status({**row, "deterministic_conclusion_allowed": False}, ev, DIRECT, True) \
        == InteractionConclusionStatus.INSUFFICIENT_FOR_CONCLUSION
    assert svc.conclusion_status({**row, "review_status": "EVIDENCE_CONFLICT"}, ev, DIRECT, True) \
        == InteractionConclusionStatus.EVIDENCE_CONFLICT
    assert svc.conclusion_status({**row, "certainty": "CONFLICTING"}, ev, DIRECT, True) == InteractionConclusionStatus.EVIDENCE_CONFLICT
    only_non_support = [e.model_copy(update={"counts_as_support": False}) for e in ev]
    assert svc.conclusion_status(row, only_non_support, DIRECT, True) == InteractionConclusionStatus.INSUFFICIENT_FOR_CONCLUSION


def test_no_corpus_record_is_not_no_interaction():
    result = ask([("鋅", ING), ("鎂", ING)], web=False)
    r = research_of(result)
    assert r.findings == [] and r.pairs[0].status == PairStatus.NO_CORPUS_RECORD and not r.corpus_sufficient
    assert r.status == InteractionResearchStatus.COMPLETE   # 沒有紀錄不是失敗，也不是「安全」


def test_unknown_new_direction_value_is_conservative():
    assert direction_role("SOMETHING_NEW") == EvidenceDirectionRole.UNCLASSIFIED
    assert direction_role(None) == EvidenceDirectionRole.UNCLASSIFIED


# ============================================================ DrugSubstance 範圍


def test_drug_identity_is_exact_only_never_translated():
    svc = service()
    assert svc.resolve_named("testdrug-a", DRUG)[0] == SubjectResolutionStatus.RESOLVED
    # 中文譯名只是顯示（DISPLAY_ONLY），NEEDS_REVIEW 的官方中文名也不用來比對
    assert svc.resolve_named("測試藥甲", DRUG)[0] == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED
    assert svc.resolve_named("華法林", DRUG)[0] == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED
    assert svc.resolve_named("某某牌藥品商品名", DRUG)[0] == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED


def test_drug_brand_without_active_substance_mapping_asks_clarification_and_does_not_research():
    result = ask([("鋅", ING), ("某某牌藥品商品名", DRUG)])
    r = research_of(result)
    assert r.status == InteractionResearchStatus.NEEDS_CLARIFICATION
    assert r.clarification_reason == "DRUG_ACTIVE_SUBSTANCE_UNMAPPED"
    assert result.evidence.planner_mode == CLARIFICATION_MODE
    assert "lookup_interactions" not in tool_names(result) and r.findings == []


def test_same_text_as_ingredient_and_drug_is_ambiguous():
    repo = InMemoryInteractionRepo()
    svc = service(repo)
    svc._drug_ref = None
    repo_drugs = repo.get_drug_reference
    repo.get_drug_reference = lambda: {**repo_drugs(), "aliases": repo_drugs()["aliases"] + [
        {"alias_id": "X", "drug_substance_id": "TD_A", "alias_text": "鋅", "language": "zh-Hant",
         "match_policy": "EXACT_AT_DECLARED_IDENTITY_LEVEL"}]}
    assert service(repo).resolve_named("鋅", ING)[0] == SubjectResolutionStatus.AMBIGUOUS


# ============================================================ C：產品重複成分


def test_duplicate_nutrient_across_two_products():
    result = ask([(HF_ZINC, PRODUCT), (DOM_NAME, PRODUCT)], mode=InteractionMode.DUPLICATE_COMPARISON)
    r = research_of(result)
    assert r.status == InteractionResearchStatus.COMPLETE and "lookup_interactions" not in tool_names(result)
    zinc = next(d for d in r.duplicates if d.nutrient.entity_id == "TN_ZN")
    assert zinc.finding.value == "DUPLICATE_INGREDIENT_DETECTED" and {p.product_id for p in zinc.products} == {"ing-hf", "ing-dom"}
    assert zinc.same_identity_level_across_products and zinc.shared_entity_ids == ["TF_ZN_GLUCONATE"]
    assert zinc.duplicate_rule_id == "TDUP_ZN" and zinc.duplicate_rule_evidence[0].evidence_id == "TE_10"
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED


def test_duplicate_specific_forms_contributing_to_same_group_use_bridge_lineage():
    result = ask([(IMP_NAME, PRODUCT), (EPA_NAME, PRODUCT)], mode=InteractionMode.DUPLICATE_COMPARISON)
    d = next(d for d in research_of(result).duplicates if d.nutrient.entity_id == "TN_OMEGA3")
    assert {p.product_id: [e.entity_id for e in p.via_entities] for p in d.products} == {
        "ing-imp": ["TF_FISH_OIL"], "ing-epa": ["TF_EPA"]}
    assert d.same_identity_level_across_products is False and d.shared_entity_ids == []   # 魚油 ≠ EPA：不可比較重量，仍是同營養素群


def test_duplicate_is_not_overdose_or_risk():
    result = ask([(IMP_NAME, PRODUCT), (EPA_NAME, PRODUCT)], mode=InteractionMode.DUPLICATE_COMPARISON)
    for d in research_of(result).duplicates:
        assert d.dose_evaluation == DoseEvaluation.NOT_EVALUATED and d.severity == "DOSE_DEPENDENT_NOT_ASSIGNED"
        assert set(d.prohibited_inferences) == set(ProhibitedInference)
        assert {"AMOUNT_PER_SERVING", "DAILY_SERVINGS", "AMOUNT_BASIS"} <= set(d.missing_information)
    dumped = json.dumps(research_of(result).model_dump(mode="json"))
    assert "OVERDOSE" in dumped                    # 只出現在「禁止推論」清單
    assert research_of(result).findings == []      # duplicate 不是 pairwise interaction（沒有被當成有害交互作用）


def test_unresolved_and_candidate_ingredients_are_excluded_and_reported():
    result = ask([(HF_ZINC, PRODUCT), (DOM_NAME, PRODUCT)], mode=InteractionMode.DUPLICATE_COMPARISON)
    r = research_of(result)
    # DOM 的「硬脂酸鎂」只是候選（AMBIGUOUS → TN_MG）：不參與重複判定
    assert all(d.nutrient.entity_id != "TN_MG" for d in r.duplicates)
    cov = {c.product_id: c for c in r.duplicate_coverage}
    assert cov["ing-dom"].unresolved_count == 3 and cov["ing-hf"].unresolved_count == 1
    dom = next(s for s in r.subjects if s.product_id == "ing-dom")
    assert dom.unresolved_ingredient_count == 3 and dom.official_ingredient_count == 4


def test_duplicate_focus_filters_to_asked_nutrient():
    result = ask([(IMP_NAME, PRODUCT), (EPA_NAME, PRODUCT)], mode=InteractionMode.DUPLICATE_COMPARISON, ingredients=["鋅"])
    assert research_of(result).duplicates == []   # 兩個產品都沒有鋅：沒有該營養素的重複（不是沒有其他重複）


def test_pairwise_between_two_products_uses_confirmed_entities_and_also_checks_duplicates():
    result = ask([(HF_ZINC, PRODUCT), (IMP_NAME, PRODUCT)])    # 鋅（葡萄糖酸鋅）vs 銅（葡萄糖酸銅）＋魚油
    r = research_of(result)
    f = finding(result, "TI_ZN_CU")
    # 產品的鋅 / 銅都只經 form → group 對應：Bridge 規定不自動繼承 → 只說明，不肯定
    assert f.conclusion_status == InteractionConclusionStatus.LINEAGE_NOT_INHERITED and f.applicability.identity_match == VIA_FORM
    assert {m.match_basis for m in f.subject_matches} == {VIA_FORM}
    assert r.duplicates == []     # 沒有共同營養素群


# ============================================================ lineage 與產品↔藥品


def test_product_form_to_group_does_not_inherit_group_interaction_by_default():
    result = ask([(IMP_NAME, PRODUCT), ("Testdrug-B", DRUG)])
    f = finding(result, "TI_OMEGA3_DRUG")
    assert f.conclusion_status == InteractionConclusionStatus.LINEAGE_NOT_INHERITED
    assert f.deterministic_conclusion_allowed       # corpus 資料欄位允許，但 identity 只有 lineage → 不肯定
    assert result.evidence.interaction_research.conclusion_eligible == []


def test_lineage_inheritance_is_data_driven_not_hard_coded():
    links = [{**l, "automatic_interaction_inheritance": True} for l in idata.FORM_LINKS]
    result = ask([(IMP_NAME, PRODUCT), ("Testdrug-B", DRUG)], repo=InMemoryInteractionRepo(links=links))
    assert finding(result, "TI_OMEGA3_DRUG").conclusion_status == InteractionConclusionStatus.CONCLUSION_ELIGIBLE


def test_product_with_direct_group_identity_is_direct_match():
    result = ask([(FOLATE_NAME, PRODUCT), ("銅", ING)])       # 產品直接對應 TN_B9
    f = finding(result, "TI_B9_CU")
    assert f.applicability.identity_match == DIRECT and f.conclusion_status == InteractionConclusionStatus.INSUFFICIENT_FOR_CONCLUSION


# ============================================================ 產品確認

def test_unconfirmed_product_stops_before_interaction_research():
    result = ask([("完全不存在的虛構測試錠", PRODUCT), ("鋅", ING)])
    r = research_of(result)
    assert r.status == InteractionResearchStatus.NEEDS_CLARIFICATION
    assert r.subjects[0].status == SubjectResolutionStatus.PRODUCT_NEEDS_CONFIRMATION
    assert result.evidence.planner_mode == CLARIFICATION_MODE
    assert not {"lookup_interactions", "lookup_duplicate_ingredients"} & set(tool_names(result))
    assert r.findings == [] and r.duplicates == []


TYPO_ZINC = "虛構牌補給測式錠"   # 「測試」誤寫成「測式」：只有低信心候選（虛構牌補給測試錠），需要使用者確認


def test_candidate_selection_continues_with_original_request_without_controller():
    ex = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE,
                    interaction_subjects=[InteractionSubjectExtraction(text=TYPO_ZINC, kind=PRODUCT),
                                          InteractionSubjectExtraction(text="Testdrug-A", kind=DRUG)])
    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipeline, _ = build(llm=llm)
    first = pipeline.run(RawUserInput(input_type=InputType.TEXT, text=f"{TYPO_ZINC} 跟 Testdrug-A 可以一起吃嗎"))
    match = first.evidence.product_match
    assert research_of(first).status == InteractionResearchStatus.NEEDS_CLARIFICATION and match.candidates
    assert not {"lookup_interactions", "lookup_duplicate_ingredients"} & set(tool_names(first))
    chosen = next(c.product for c in match.candidates if c.product.product_id == "ing-hf")
    controller_calls = lambda: [c for c in llm.calls if c == ("parse_structured", "IntakeExtraction")]   # noqa: E731
    assert len(controller_calls()) == 1
    resumed = pipeline.resume_with_request(request_for_selected_product(first.normalized_request, chosen, "req-resume"))
    assert research_of(resumed).status == InteractionResearchStatus.COMPLETE
    assert resumed.evidence.product_match.best_match.product.product_id == "ing-hf"
    assert resumed.normalized_request.interaction_subjects[0].pinned_permit_number == chosen.permit_number
    # 鋅只經 form → group 對應（葡萄糖酸鋅 → 鋅）：不自動繼承，只說明
    assert finding(resumed, "TI_ZN_DRUG").conclusion_status == InteractionConclusionStatus.LINEAGE_NOT_INHERITED
    assert len(controller_calls()) == 1                           # 沒有重跑 Controller（Agent A）


def test_selection_pins_only_the_matching_unpinned_product_subject():
    ex = extraction(intent=Intent.INTERACTION_INQUIRY,
                    interaction_subjects=[InteractionSubjectExtraction(text=DOM_NAME, kind=PRODUCT),
                                          InteractionSubjectExtraction(text=TYPO_ZINC, kind=PRODUCT)])
    pipeline, _ = build(ex=ex)
    first = pipeline.run(RawUserInput(input_type=InputType.TEXT, text=f"{DOM_NAME} 跟 {TYPO_ZINC} 可以一起吃嗎"))
    r = research_of(first)
    assert [s.status for s in r.subjects] == [SubjectResolutionStatus.RESOLVED, SubjectResolutionStatus.PRODUCT_NEEDS_CONFIRMATION]
    chosen = first.evidence.product_match.candidates[0].product          # 確認卡對應的是第二個對象的候選
    req = request_for_selected_product(first.normalized_request, chosen, "req-2")
    assert [(s.text, s.selection_order) for s in req.interaction_subjects if s.pinned_permit_number] == [(TYPO_ZINC, 1)]
    assert req.interaction_subjects[0].pinned_permit_number is None     # 已用名稱確認的對象不被改寫


# ============================================================ routing 與 fast path


def test_structured_evidence_sufficient_does_not_run_rag_or_web():
    rag = InMemoryRagStore()
    result = ask([("鋅", ING), ("Testdrug-A", DRUG)], rag=rag, web=True)
    assert result.evidence.official_web_search_attempted is False
    assert "search_rag" not in tool_names(result) and "search_tw_official_web" not in tool_names(result)
    assert result.evidence.rag_evidence == [] and research_of(result).fallback_used is False
    assert result.evidence.fast_path_reason == "interaction_corpus:sufficient"


def test_duplicate_found_is_structured_evidence_so_no_rag_or_web():
    result = ask([(HF_ZINC, PRODUCT), (DOM_NAME, PRODUCT)], mode=InteractionMode.DUPLICATE_COMPARISON, web=True)
    assert "search_rag" not in tool_names(result) and result.evidence.official_web_search_attempted is False


def test_insufficient_corpus_falls_back_to_existing_researcher_policy_once():
    result = ask([("鋅", ING), ("鎂", ING)])
    r = research_of(result)
    assert r.corpus_sufficient is False and r.fallback_used is True
    assert tool_names(result).count("search_rag") == 1
    assert r.findings == []        # fallback 的文件不會變成 interaction 結論


def test_corpus_unavailable_is_reported_not_treated_as_no_interaction():
    result = ask([("鋅", ING), ("銅", ING)], repo=InMemoryInteractionRepo(version=None))
    assert research_of(result).status == InteractionResearchStatus.CORPUS_UNAVAILABLE
    assert result.evidence.errors and research_of(result).findings == []
    no_repo = ask([("鋅", ING), ("銅", ING)], repo=False)
    assert research_of(no_repo).status == InteractionResearchStatus.CORPUS_UNAVAILABLE


def test_lookup_failure_is_not_no_record():
    result = ask([("鋅", ING), ("銅", ING)], repo=InMemoryInteractionRepo(fail=True))
    r = research_of(result)
    assert r.status == InteractionResearchStatus.CORPUS_UNAVAILABLE and not r.corpus_sufficient
    assert any(e.source for e in result.evidence.errors)


def test_fewer_than_two_subjects_asks_for_the_other_one():
    r = research_of(ask([("鋅", ING)]))
    assert r.status == InteractionResearchStatus.INSUFFICIENT_SUBJECTS and r.findings == []


def test_other_intents_never_enter_interaction_path():
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[HF_ZINC], needs_product_lookup=True)
    pipeline, _ = build(ex=ex)
    result = pipeline.run(RawUserInput(input_type=InputType.TEXT, text=f"{HF_ZINC} 有登記嗎"))
    assert result.evidence.interaction_research is None


# ============================================================ Controller（Agent A）


def _req(text, subjects, mode=None, intent=Intent.INTERACTION_INQUIRY, products=(), ingredients=()):
    ex = extraction(intent=intent, interaction_mode=mode, product_candidates=list(products), ingredient_candidates=list(ingredients),
                    interaction_subjects=[InteractionSubjectExtraction(text=t, kind=k) for t, k in subjects],
                    needs_product_lookup=bool(products))
    llm = FakeLLM(structured={IntakeExtraction: ex})
    return IntakeAgent(llm).understand("r", RawUserInput(input_type=InputType.TEXT, text=text))


def test_controller_pairwise_duplicate_and_ingredient_drug_questions():
    a = _req("鈣片 跟 魚油 可以一起吃嗎", [("鈣片", PRODUCT), ("魚油", ING)], InteractionMode.PAIRWISE)
    assert a.intent == Intent.INTERACTION_INQUIRY and a.interaction_mode == InteractionMode.PAIRWISE
    assert [(s.text, s.kind) for s in a.interaction_subjects] == [("鈣片", PRODUCT), ("魚油", ING)]
    b = _req("甲產品 跟 乙產品 都有鋅嗎", [("甲產品", PRODUCT), ("乙產品", PRODUCT)], InteractionMode.DUPLICATE_COMPARISON, ingredients=["鋅"])
    assert b.interaction_mode == InteractionMode.DUPLICATE_COMPARISON and b.ingredient_candidates == ["鋅"]
    c = _req("魚油 跟 warfarin 有衝突嗎", [("魚油", ING), ("warfarin", DRUG)])
    assert c.interaction_mode == InteractionMode.PAIRWISE and c.interaction_subjects[1].kind == DRUG


def test_controller_drops_subjects_not_in_user_text_so_llm_cannot_translate_identity():
    req = _req("魚油 跟 華法林 有衝突嗎", [("魚油", ING), ("Warfarin", DRUG), ("華法林", DRUG)])
    assert [s.text for s in req.interaction_subjects] == ["魚油", "華法林"]     # LLM 自行翻成英文的 Warfarin 被丟棄


def test_controller_keeps_products_out_of_single_product_resolution():
    req = _req("甲產品 跟 乙產品 可以一起吃嗎", [("甲產品", PRODUCT), ("乙產品", PRODUCT)], products=["甲產品", "乙產品"])
    assert req.product_candidates == [] and req.needs_product_lookup is False and req.claims == []
    assert len(req.interaction_subjects) == 2


def test_controller_non_interaction_intent_has_no_subjects():
    req = _req("鈣 有什麼功效", [("鈣", ING)], intent=Intent.EFFICACY_INQUIRY)
    assert req.interaction_subjects == [] and req.interaction_mode is None
    assert interaction_subjects_for(extraction(intent=Intent.UNCLEAR), "x", [], []) == ([], None)


# ============================================================ tools（受控 read-only、canonical ID）


def _state(repo=None):
    svc = service(repo)
    return PlannerState(request=extraction_request(), bundle=None, repo=InMemoryOfficialRepo(), interaction_service=svc)


def extraction_request():
    from tests.builders import request

    return request(intent=Intent.INTERACTION_INQUIRY)


def test_tool_guards_require_canonical_known_ids():
    state = _state()
    spec = INTERACTION_TOOLS["lookup_interactions"]
    bad = spec.args_model.model_validate({"sides": [
        {"label": "a", "handles": [{"entity_id": "鋅", "via_entity_id": None}]},
        {"label": "b", "handles": [{"entity_id": "TN_CU", "via_entity_id": None}]}]})
    assert "canonical" in spec.guard(state, bad)                 # display name 不是 canonical ID
    one_side = spec.args_model.model_validate({"sides": [{"label": "a", "handles": [{"entity_id": "TN_ZN", "via_entity_id": None}]}]})
    assert spec.guard(state, one_side)
    dup = INTERACTION_TOOLS["lookup_duplicate_ingredients"]
    assert dup.guard(state, dup.args_model(product_ids=["ing-hf", "ing-dom"], focus_entity_ids=[]))   # 未確認的產品
    ev = INTERACTION_TOOLS["lookup_interaction_evidence"]
    assert ev.guard(state, ev.args_model(interaction_ids=["TI_ZN_CU"]))                              # 未先查過 interaction
    state.known_interaction_ids.add("TI_ZN_CU")
    assert ev.guard(state, ev.args_model(interaction_ids=["TI_ZN_CU"])) is None


def test_tools_are_not_in_llm_planner_allowlist_and_are_strict_schemas():
    from app.agents.evidence_tools import EVIDENCE_TOOLS, OFFICIAL_WEB_TOOLS

    assert not set(INTERACTION_TOOLS) & (set(EVIDENCE_TOOLS) | set(OFFICIAL_WEB_TOOLS))
    for spec in INTERACTION_TOOLS.values():
        assert spec.openai_tool()["strict"] is True


def test_evidence_tool_preserves_directions_and_is_read_only():
    svc = service()
    out = interaction_tools.lookup_interaction_evidence(svc, ["TI_FISH_ANTICOAG"])
    assert {e.supports_direction for e in out.evidence["TI_FISH_ANTICOAG"]} == {
        "MIXED", "SUPPORTS", "NO_SIGNIFICANT_EFFECT", "CONTEXT_NOT_PAIR_SPECIFIC"}
    assert {c for c in svc._repo.calls} <= {"get_interaction_evidence", "get_drug_reference", "get_interactions_involving",
                                            "get_form_nutrient_links"}      # 只有讀取


def test_no_product_to_product_interaction_edge_or_record():
    svc = service()
    side = lambda label, ids: Side(label, [EntityHandle(i, "NutrientGroup") for i in ids])   # noqa: E731
    findings, _ = svc.pairwise([side("甲", ["TN_ZN"]), side("乙", ["TN_CU"])])
    kinds = {p.entity.kind for f in findings for p in f.participants}
    assert kinds <= {"NutrientGroup", "IngredientForm", "DrugSubstance"} and all(f.record_kind == "PAIRWISE_INTERACTION" for f in findings)
    # duplicate rule 不會出現在 pairwise 結果
    findings, _ = svc.pairwise([side("甲", ["TN_ZN"]), side("乙", ["TN_ZN"])])
    assert findings == []
    code = [ln for f in ("app/services/interaction_service.py", "app/tools/interaction_tools.py", "app/agents/interaction_research.py")
            for ln in open(f, encoding="utf-8").read().splitlines() if not ln.lstrip().startswith(("#", '"""'))]
    assert not any("INTERACTS_WITH" in ln and "沒有" not in ln and "不" not in ln for ln in code)


def test_evidence_sources_flow_into_bundle_sources_with_official_flag():
    result = ask([("魚油", ING), ("Testdrug-Anticoag", DRUG)])
    sources = {s.source_record_id: s for s in result.evidence.all_sources() if s.source_record_id and s.source_record_id.startswith("TE_")}
    assert sources["TE_3"].is_official is False and sources["TE_4"].is_official is True   # PubMed 不是官方裁示


def test_pipeline_returns_a_response_for_every_interaction_state():
    for subjects in ([("鋅", ING), ("Testdrug-A", DRUG)], [("魚油", ING), ("Testdrug-Anticoag", DRUG)],
                     [("鋅", ING), ("某某牌藥品商品名", DRUG)], [("鋅", ING)]):
        result = ask(subjects)
        assert result.response is not None and result.response.response_type is not None
