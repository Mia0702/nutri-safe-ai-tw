"""Agent B 的 Interaction 研究路徑（intent = interaction_inquiry）。

由 VerificationAgent.verify() 在最前面分派（其他 intent 的流程完全不變）。流程：

  1. 逐一確認每個對象的 identity（全部 deterministic，LLM 不參與）：
     * 成分 / 營養素：Ingredient Bridge exact；藥品：corpus DrugSubstance（英文 canonical / 來源同義詞 exact）。
       只有藥品商品名、譯名、無法對應 → DRUG_ACTIVE_SUBSTANCE_UNMAPPED / UNRESOLVED（不翻譯、不猜 active ingredient）。
     * 產品：既有 deterministic resolver（每個產品各自解析）；必須 VERIFIED 才取成分。
  2. 任一對象 identity 未確認 → 先 clarification（`structured_clarification`），**不做 interaction research**。
     未確認的產品以 `bundle.product_match` 呈現候選，沿用既有確認卡；選定後以原 request 繼續（不重跑 Controller）。
  3. 全部確認 → 以 canonical ID 呼叫 interaction tools（只用 INTERACTION_TOOLS，不經 LLM planner）：
     lookup_interactions（pairwise，產品經 Bridge 的已確認成分）、lookup_duplicate_ingredients（兩個以上產品）。
  4. Interaction corpus 有紀錄（含證據 / 衝突 / 待審）→ 結構化證據已足夠：不跑 RAG / 官方網站。
     完全沒有紀錄（NO_CORPUS_RECORD，不等於沒有交互作用）才依既有 Researcher policy 補 RAG 一次＋官方網站一次（Relevance Gate 不變）。
"""

import logging

from app.agents.evidence_tools import (
    EVIDENCE_TOOLS,
    INTERACTION_TOOLS,
    DuplicateIngredientsArgs,
    InteractionHandleArg,
    InteractionSideArg,
    LookupInteractionsArgs,
    PlannerState,
    ProductIngredientsArgs,
    QueryArgs,
)
from app.agents.intake_agent import product_mentions
from app.risk.assessment_rules import compute_evidence_strength
from app.schemas.enums import ErrorCode, ProductVerificationStatus
from app.schemas.evidence import EvidenceBundle, PlannerStep, ProductMatch, ToolError
from app.schemas.interaction import (
    EntityRefOut,
    IngredientMappingOut,
    InteractionMode,
    InteractionResearch,
    InteractionResearchStatus,
    InteractionSubject,
    InteractionSubjectKind,
    SubjectResolution,
    SubjectResolutionStatus,
)
from app.schemas.user_request import NormalizedUserRequest
from app.services.errors import ServiceError
from app.services.interaction_service import DIRECT, VIA_FORM, InteractionService, Side
from app.tools.base import ToolCallRecorder

logger = logging.getLogger("nutri_safe_ai.agents.interaction")

INTERACTION_MODE = "interaction_research"
CLARIFICATION_MODE = "structured_clarification"  # 與 verification_agent 相同的語意：先請使用者確認，不研究


def _entity_out(ref) -> EntityRefOut | None:
    if ref is None:
        return None
    return EntityRefOut(entity_id=ref.entity_id, kind=ref.entity_type, canonical_name_en=ref.canonical_name_en,
                        display_name_zh=ref.display_name_zh, identity_level=ref.identity_level)


def _mapping_out(confirmed) -> IngredientMappingOut:
    """已確認的產品成分（ConfirmedIngredientEntry）→ Reporter 用的成分對應（只搬運，不新增判斷）。"""
    return IngredientMappingOut(raw_texts=list(confirmed.raw_texts), entity=_entity_out(confirmed.entity),
                                parent=_entity_out(confirmed.parent_nutrient), via=_entity_out(confirmed.via_entity))


class InteractionResearchMixin:
    """VerificationAgent 的 interaction 路徑（使用 VerificationAgent 的 _repo / _interaction / _llm 與既有 helper）。"""

    # ------------------------------------------------------------------ entry

    def _verify_interaction(self, request: NormalizedUserRequest, rec: ToolCallRecorder, bundle: EvidenceBundle,
                            state: PlannerState) -> EvidenceBundle:
        research = InteractionResearch(mode=request.interaction_mode or InteractionMode.PAIRWISE,
                                       status=InteractionResearchStatus.COMPLETE)
        bundle.interaction_research = research
        bundle.planner_mode = INTERACTION_MODE
        service = self._interaction_service()
        if service is None or not service.available:
            research.status = InteractionResearchStatus.CORPUS_UNAVAILABLE
            bundle.errors.append(ToolError(code=ErrorCode.NOT_CONFIGURED, message="Interaction corpus 尚未匯入或不可用",
                                           source="interaction_corpus"))
            return self._finish_interaction(request, rec, bundle, state)
        state.interaction_service = service
        research.corpus_version = service.corpus_version

        sides: list[Side] = []
        unresolved_matches: list[ProductMatch] = []
        pinned_matches: list[tuple[int, ProductMatch]] = []
        product_matches: list[ProductMatch] = []
        for subject in request.interaction_subjects:
            try:
                resolution, side, match = self._resolve_subject(request, state, rec, service, subject)
            except Exception as exc:  # noqa: BLE001 - identity 查詢失敗是「查詢失敗」，不是「查無」也不是「沒有交互作用」
                bundle.errors.append(exc.to_tool_error() if isinstance(exc, ServiceError) else ToolError(
                    code=ErrorCode.INTERNAL_ERROR, message=type(exc).__name__, source="interaction_corpus"))
                resolution, side, match = SubjectResolution(text=subject.text, kind=subject.kind,
                                                            status=SubjectResolutionStatus.LOOKUP_FAILED), Side(subject.text), None
            research.subjects.append(resolution)
            if match is not None:
                product_matches.append(match)
                if resolution.status == SubjectResolutionStatus.PRODUCT_NEEDS_CONFIRMATION:
                    unresolved_matches.append(match)
                elif subject.pinned_permit_number and subject.selection_order is not None:
                    pinned_matches.append((subject.selection_order, match))
            if resolution.status == SubjectResolutionStatus.RESOLVED:
                sides.append(side)
        # 呈現給既有確認卡的產品比對：未確認者優先（候選按鈕）；否則以最後一次使用者選定的產品為準
        if unresolved_matches:
            bundle.product_match = unresolved_matches[0]
        elif pinned_matches:
            bundle.product_match = max(pinned_matches, key=lambda m: m[0])[1]
        elif product_matches:
            bundle.product_match = product_matches[0]

        unresolved = [r for r in research.subjects if r.status != SubjectResolutionStatus.RESOLVED]
        if any(r.status == SubjectResolutionStatus.LOOKUP_FAILED for r in unresolved):
            research.status = InteractionResearchStatus.CORPUS_UNAVAILABLE   # 查詢失敗：如實回報，不請使用者重述、也不當成查無
            research.clarification_reason = SubjectResolutionStatus.LOOKUP_FAILED.value
            return self._finish_interaction(request, rec, bundle, state)
        if unresolved or len(request.interaction_subjects) < 2:
            research.status = (InteractionResearchStatus.NEEDS_CLARIFICATION if unresolved
                               else InteractionResearchStatus.INSUFFICIENT_SUBJECTS)
            research.clarification_reason = unresolved[0].status.value if unresolved else "FEWER_THAN_TWO_SUBJECTS"
            bundle.planner_mode = CLARIFICATION_MODE
            bundle.fast_path_reason = f"interaction_clarification:{research.clarification_reason}"
            bundle.planner_trace.append(PlannerStep(
                round=0, note=f"interaction 對象身份尚未確定（{research.clarification_reason}），先請使用者確認，不進行 interaction 研究"))
            return self._finish_interaction(request, rec, bundle, state)

        self._run_interaction_tools(request, rec, state, service, research, sides)
        if not research.corpus_sufficient and not state.failed:
            self._interaction_fallback(state, rec, research)
        bundle.fast_path_reason = f"interaction_corpus:{'sufficient' if research.corpus_sufficient else 'no_record'}"
        bundle.planner_trace.append(PlannerStep(
            round=0, note=("Interaction corpus 結構化證據已足夠，不查 RAG / 官方網站" if research.corpus_sufficient
                           else "Interaction corpus 沒有這組紀錄（不代表沒有交互作用）：依既有 Researcher policy 補查")))
        return self._finish_interaction(request, rec, bundle, state)

    def _finish_interaction(self, request, rec, bundle, state) -> EvidenceBundle:
        bundle.tools_called = rec.records
        bundle.errors = bundle.errors + self._source_errors(rec)
        bundle.evidence_strength = compute_evidence_strength(bundle)
        research = bundle.interaction_research
        logger.info("interaction_research_done", extra={
            "request_id": request.request_id, "agent": "verification", "planner_mode": bundle.planner_mode,
            "evidence_count": len(research.findings) + len(research.duplicates) if research else 0,
            "product_verification": bundle.product_match.status.value})
        return bundle

    def _interaction_service(self) -> InteractionService | None:
        repo = getattr(self, "_interaction", None)
        if repo is None:
            return None
        try:
            bridge = self._repo.get_ingredient_bridge_reference() if self._repo is not None else {}
            return InteractionService(repo, bridge)
        except Exception as exc:  # noqa: BLE001 - corpus 查詢失敗是「不可用」，不是「沒有交互作用」
            logger.warning("interaction_service_unavailable", extra={"error_code": getattr(exc, "code", type(exc).__name__)})
            return None

    # ------------------------------------------------------------------ subjects

    def _resolve_subject(self, request: NormalizedUserRequest, state: PlannerState, rec: ToolCallRecorder,
                         service: InteractionService, subject: InteractionSubject):
        """→ (SubjectResolution, Side, ProductMatch | None)。"""
        if subject.kind == InteractionSubjectKind.PRODUCT:
            resolution, side, match = self._resolve_product_subject(request, state, rec, service, subject)
            if not (resolution.status == SubjectResolutionStatus.PRODUCT_NEEDS_CONFIRMATION and not match.candidates):
                return resolution, side, match
            # 產品查無任何候選時，若寫法其實是已確認的成分 / 藥品 identity，改當成分 / 藥品（identity 仍由 exact 決定）
            status, entities = service.resolve_named(subject.text, subject.kind)
            if status == SubjectResolutionStatus.RESOLVED:
                return self._named_resolution(service, subject, status, entities), service.side_from_entities(subject.text, entities), None
            return resolution, side, match
        status, entities = service.resolve_named(subject.text, subject.kind)
        return (self._named_resolution(service, subject, status, entities),
                service.side_from_entities(subject.text, entities), None)

    @staticmethod
    def _named_resolution(service, subject, status, entities) -> SubjectResolution:
        return SubjectResolution(text=subject.text, kind=subject.kind, status=status, entities=list(entities))

    def _resolve_product_subject(self, request: NormalizedUserRequest, state: PlannerState, rec: ToolCallRecorder,
                                 service: InteractionService, subject: InteractionSubject):
        products, _corrected, mentions = product_mentions(request.original_text, [subject.text])
        permits = [subject.pinned_permit_number] if subject.pinned_permit_number else []
        sub_request = request.model_copy(update={
            "product_candidates": [subject.pinned_product_name or subject.text] if permits else products,
            "corrected_product_candidates": [], "product_mentions": [] if permits else mentions,
            "permit_number_candidates": permits, "brand_candidates": [], "company_candidates": [],
            "needs_product_lookup": True, "claims": []})
        sub_state = PlannerState(request=sub_request, bundle=EvidenceBundle(
            request_id=request.request_id, product_match=ProductMatch(status=ProductVerificationStatus.NOT_APPLICABLE)),
            repo=self._repo, graph=self._graph, rag=self._rag, llm=self._llm)
        self._guardrail_product_lookup(sub_state, rec)
        match = self._resolve_product(sub_state, rec)
        state.failed |= sub_state.failed
        base = dict(text=subject.text, kind=subject.kind)
        if not match.lookup_completed:
            return SubjectResolution(**base, status=SubjectResolutionStatus.LOOKUP_FAILED), Side(subject.text), match
        verified = (match.status == ProductVerificationStatus.VERIFIED and match.best_match is not None
                    and not match.is_ambiguous and not match.needs_confirmation)
        if not verified:
            return SubjectResolution(**base, status=SubjectResolutionStatus.PRODUCT_NEEDS_CONFIRMATION,
                                     note=match.status.value), Side(subject.text), match
        product = match.best_match.product
        state.known_product_ids.add(product.product_id)
        state.confirmed_product_ids[product.product_id] = product.product_name_zh
        self._call_tool(state, rec, EVIDENCE_TOOLS["lookup_product_ingredients"],
                        ProductIngredientsArgs(product_ids=[product.product_id], ingredient_queries=[]), "guardrail")
        evidence = state.ingredient_evidence_by_product.get(product.product_id)
        if evidence is None:
            return SubjectResolution(**base, status=SubjectResolutionStatus.LOOKUP_FAILED if "lookup_product_ingredients" in state.failed
                                     else SubjectResolutionStatus.PRODUCT_NO_CONFIRMED_INGREDIENTS,
                                     product_id=product.product_id, product_name=product.product_name_zh,
                                     product_confirmed=True), Side(subject.text), match
        side, via_forms = service.side_from_product(subject.text, evidence)
        entities = [e for e in (service.entity_ref(h.entity_id) for h in side.best_handles().values()) if e]
        status = SubjectResolutionStatus.RESOLVED if entities else SubjectResolutionStatus.PRODUCT_NO_CONFIRMED_INGREDIENTS
        return SubjectResolution(
            **base, status=status, entities=entities, product_id=product.product_id, product_name=product.product_name_zh,
            product_confirmed=True, via_forms=via_forms, official_ingredient_count=evidence.coverage.official_count,
            unresolved_ingredient_count=evidence.coverage.unresolved_count,
            ingredient_mappings=[_mapping_out(c) for c in evidence.confirmed_ingredients]), side, match

    # ------------------------------------------------------------------ research

    def _run_interaction_tools(self, request, rec, state: PlannerState, service: InteractionService,
                               research: InteractionResearch, sides: list[Side]) -> None:
        product_ids = [r.product_id for r in research.subjects if r.product_id and r.product_confirmed]
        want_pairwise = research.mode != InteractionMode.DUPLICATE_COMPARISON or len(product_ids) < 2
        if research.mode == InteractionMode.DUPLICATE_COMPARISON and len(set(product_ids)) < 2:
            research.status = InteractionResearchStatus.INSUFFICIENT_SUBJECTS
            research.clarification_reason = "DUPLICATE_COMPARISON_NEEDS_TWO_PRODUCTS"
            return
        if want_pairwise:
            args = LookupInteractionsArgs(sides=[
                InteractionSideArg(label=s.label, handles=[
                    InteractionHandleArg(entity_id=h.entity_id, via_entity_id=h.via_entity_id if h.match_basis == VIA_FORM else None)
                    for h in s.best_handles().values()]) for s in sides])
            self._call_tool(state, rec, INTERACTION_TOOLS["lookup_interactions"], args, "guardrail")
            research.findings = sorted(state.interaction_findings.values(), key=lambda f: f.interaction_id)
            research.pairs = list(state.interaction_pairs)
        if len(set(product_ids)) >= 2:
            focus = []
            for text in request.ingredient_candidates:
                status, entities = service.resolve_named(text, InteractionSubjectKind.INGREDIENT)
                if status == SubjectResolutionStatus.RESOLVED:
                    focus += [e.entity_id for e in entities]
            self._call_tool(state, rec, INTERACTION_TOOLS["lookup_duplicate_ingredients"],
                            DuplicateIngredientsArgs(product_ids=list(dict.fromkeys(product_ids)), focus_entity_ids=focus), "guardrail")
            research.duplicates = list(state.duplicate_findings)
            research.duplicate_coverage = list(state.duplicate_coverage)
        failed = {"lookup_interactions", "lookup_duplicate_ingredients"} & state.failed
        # 有任何紀錄（含衝突 / 待審 / 重複存在）就是結構化證據；查詢失敗不當成「查無」
        research.corpus_sufficient = bool(research.findings or research.duplicates) and not failed
        if failed:
            research.status = InteractionResearchStatus.CORPUS_UNAVAILABLE

    def _interaction_fallback(self, state: PlannerState, rec: ToolCallRecorder, research: InteractionResearch) -> None:
        """資料不足才依既有 Researcher policy：官方文件 RAG 一次 → Relevance Gate → 仍沒有才查台灣官方網站一次。"""
        request, bundle = state.request, state.bundle
        research.fallback_used = True
        self._guardrail_call(state, rec, "search_rag", QueryArgs(query=request.question or request.normalized_text))
        links = self._assess_and_gate(state, rec)
        if self._enable_official_web and not bundle.rag_evidence and not state.failed:
            self._official_web_then_reassess(state, rec, links)
