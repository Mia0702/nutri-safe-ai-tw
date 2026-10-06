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

        if request.interaction_overview:  # 目前產品的泛指交互作用總覽：不需要第二個對象
            return self._verify_overview(request, rec, bundle, state, research, service)
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
        gaps = [r for r in unresolved if r.status == SubjectResolutionStatus.IDENTITY_NOT_IN_INTERACTION_CORPUS]
        blocking = [r for r in unresolved if r not in gaps]  # 真的無法辨識 / 產品未確認 / 歧義：維持 clarification，不上網猜 identity
        if blocking or len(request.interaction_subjects) < 2:
            research.status = (InteractionResearchStatus.NEEDS_CLARIFICATION if blocking
                               else InteractionResearchStatus.INSUFFICIENT_SUBJECTS)
            research.clarification_reason = blocking[0].status.value if blocking else "FEWER_THAN_TWO_SUBJECTS"
            bundle.planner_mode = CLARIFICATION_MODE
            bundle.fast_path_reason = f"interaction_clarification:{research.clarification_reason}"
            bundle.planner_trace.append(PlannerStep(
                round=0, note=f"interaction 對象身份尚未確定（{research.clarification_reason}），先請使用者確認，不進行 interaction 研究"))
            return self._finish_interaction(request, rec, bundle, state)

        research.coverage_gap_subjects = [r.text for r in gaps]
        research.catalog_recognized = {r.text: r.catalog_official_name for r in gaps if r.catalog_official_name}
        if len(sides) >= 2:  # 至少兩個已確認 identity 的對象才有 corpus 配對可查；其餘只有 coverage gap
            self._run_interaction_tools(request, rec, state, service, research, sides)
        if not research.corpus_sufficient and not state.failed:
            # 補查的 RAG / 官方網站（各最多一次）只是參考資訊：不產生 CONCLUSION_ELIGIBLE、不建立 interaction、不補 identity
            self._interaction_fallback(state, rec, research)
        reason = "coverage_gap" if gaps and not research.corpus_sufficient else (
            "sufficient" if research.corpus_sufficient else "no_record")
        bundle.fast_path_reason = f"interaction_corpus:{reason}"
        bundle.planner_trace.append(PlannerStep(
            round=0, note=("Interaction corpus 結構化證據已足夠，不查 RAG / 官方網站" if research.corpus_sufficient
                           else "Interaction corpus 沒有這組紀錄（不代表沒有交互作用）：依既有 Researcher policy 補查")))
        return self._finish_interaction(request, rec, bundle, state)

    def _verify_overview(self, request, rec, bundle, state, research, service) -> EvidenceBundle:
        """CURRENT_PRODUCT_INTERACTION_OVERVIEW：已確認的目前產品 → 官方成分 → 已確認的 Bridge mapping → corpus 已知的交互作用對象。
        不做產品 ↔ 產品比對、不查 RAG / 官方網站（corpus 沒有紀錄也只如實說明）。"""
        research.mode = InteractionMode.CURRENT_PRODUCT_OVERVIEW
        if not request.interaction_subjects:  # 沒有已確認的目前產品可以綁定
            research.status = InteractionResearchStatus.INSUFFICIENT_SUBJECTS
            research.clarification_reason = "OVERVIEW_NEEDS_CURRENT_PRODUCT"
            bundle.planner_mode = CLARIFICATION_MODE
            bundle.fast_path_reason = "interaction_overview:needs_current_product"
            return self._finish_interaction(request, rec, bundle, state)
        subject = request.interaction_subjects[0]
        try:
            resolution, side, match = self._resolve_subject(request, state, rec, service, subject)
        except Exception as exc:  # noqa: BLE001 - 查詢失敗不是「沒有紀錄」
            bundle.errors.append(exc.to_tool_error() if isinstance(exc, ServiceError) else ToolError(
                code=ErrorCode.INTERNAL_ERROR, message=type(exc).__name__, source="interaction_corpus"))
            resolution, side, match = SubjectResolution(text=subject.text, kind=subject.kind,
                                                        status=SubjectResolutionStatus.LOOKUP_FAILED), Side(subject.text), None
        research.subjects.append(resolution)
        if match is not None:
            bundle.product_match = match
        if resolution.status == SubjectResolutionStatus.LOOKUP_FAILED:
            research.status = InteractionResearchStatus.CORPUS_UNAVAILABLE
            return self._finish_interaction(request, rec, bundle, state)
        if resolution.status != SubjectResolutionStatus.RESOLVED:
            research.status = InteractionResearchStatus.NEEDS_CLARIFICATION
            research.clarification_reason = resolution.status.value
            bundle.planner_mode = CLARIFICATION_MODE
            bundle.fast_path_reason = f"interaction_overview:{resolution.status.value}"
            return self._finish_interaction(request, rec, bundle, state)
        try:
            research.findings = service.overview(side)
        except Exception as exc:  # noqa: BLE001
            bundle.errors.append(exc.to_tool_error() if isinstance(exc, ServiceError) else ToolError(
                code=ErrorCode.INTERNAL_ERROR, message=type(exc).__name__, source="interaction_corpus"))
            research.status = InteractionResearchStatus.CORPUS_UNAVAILABLE
            return self._finish_interaction(request, rec, bundle, state)
        research.corpus_sufficient = bool(research.findings)
        bundle.fast_path_reason = f"interaction_overview:{'records' if research.findings else 'no_record'}"
        bundle.planner_trace.append(PlannerStep(
            round=0, note="目前產品的交互作用總覽：只列 corpus 已收錄的對象，不查 RAG / 官方網站"))
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
        if subject.pinned_entity_id and subject.pinned_entity_id.startswith(service.CATALOG_ENTITY_PREFIX):
            row = service.catalog_by_code(subject.pinned_entity_id[len(service.CATALOG_ENTITY_PREFIX):])  # 繁中別名候選（名錄項目）確認
            if row is not None:
                logger.info("drug_zh_alias_recovery_confirmed", extra={
                    "request_id": request.request_id, "agent": "verification", "recovery_confirmed": True, "alias_target_kind": "TAIWAN_DRUG_INGREDIENT"})
                return (*self._catalog_row_resolution(service, subject, row), None)
        if subject.pinned_entity_id and (pinned := service.entity_ref(subject.pinned_entity_id)) is not None:
            if pinned.kind == "DrugSubstance":
                logger.info("drug_zh_alias_recovery_confirmed", extra={
                    "request_id": request.request_id, "agent": "verification", "recovery_confirmed": True, "alias_target_kind": "DRUG_SUBSTANCE"})
            # 使用者在語音候選按鈕上確認的 canonical entity（系統只會固定「那一題提供過的候選」）
            resolution = SubjectResolution(text=subject.text, kind=subject.kind, status=SubjectResolutionStatus.RESOLVED,
                                           entities=[pinned])
            return resolution, service.side_from_entities(subject.text, [pinned]), None
        if subject.kind == InteractionSubjectKind.PRODUCT:
            resolution, side, match = self._resolve_product_subject(request, state, rec, service, subject)
            if not (resolution.status == SubjectResolutionStatus.PRODUCT_NEEDS_CONFIRMATION and not match.candidates):
                return resolution, side, match
            # 產品查無任何候選時，若寫法其實是已確認的成分 / 藥品 identity，改當成分 / 藥品（identity 仍由 exact 決定）
            status, entities = service.resolve_named(subject.text, subject.kind)
            if status == SubjectResolutionStatus.RESOLVED:
                return self._named_resolution(service, subject, status, entities), service.side_from_entities(subject.text, entities), None
            if status == SubjectResolutionStatus.UNRESOLVED and (recovered := self._recover_asr_entity(request, service, subject)):
                return recovered, Side(subject.text), None
            return resolution, side, match
        status, entities = service.resolve_named(subject.text, subject.kind)
        if status == SubjectResolutionStatus.RESOLVED and (hit := service.zh_alias_hit(subject.text)) is not None:
            self._log_alias_exact(request, hit)
        if (status in (SubjectResolutionStatus.UNRESOLVED, SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED)
                and subject.kind != InteractionSubjectKind.PRODUCT and (via_catalog := self._catalog_resolution(service, subject))):
            if (hit := service.zh_alias_hit(subject.text)) is not None:
                self._log_alias_exact(request, hit)
            return via_catalog[0], via_catalog[1], None
        if status == SubjectResolutionStatus.UNRESOLVED and (recovered := self._recover_asr_entity(request, service, subject)):
            return recovered, Side(subject.text), None
        if (status in (SubjectResolutionStatus.UNRESOLVED, SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED)
                and subject.kind != InteractionSubjectKind.PRODUCT and (typo := self._recover_alias_typo(request, service, subject))):
            return typo, Side(subject.text), None
        return (self._named_resolution(service, subject, status, entities),
                service.side_from_entities(subject.text, entities), None)

    @staticmethod
    def _catalog_resolution(service: InteractionService, subject: InteractionSubject):
        """台灣官方藥品成分名錄（exact）：有已審核對應 → 既有 DrugSubstance 流程；沒有 → 資料涵蓋缺口（官方成分已辨識）。

        名錄成員資格不是 interaction 證據：不建立 finding，也不產生 CONCLUSION_ELIGIBLE。
        """
        row = service.catalog_lookup(subject.text)
        if not row:
            return None
        return InteractionResearchMixin._catalog_row_resolution(service, subject, row)

    @staticmethod
    def _catalog_row_resolution(service: InteractionService, subject: InteractionSubject, row: dict):
        drug = service.drug_ref(row["mapped_drug_substance_id"]) if row.get("mapped_drug_substance_id") else None
        if drug is not None:
            return (SubjectResolution(text=subject.text, kind=subject.kind, status=SubjectResolutionStatus.RESOLVED, entities=[drug]),
                    service.side_from_entities(subject.text, [drug]))
        return (SubjectResolution(text=subject.text, kind=subject.kind, status=SubjectResolutionStatus.IDENTITY_NOT_IN_INTERACTION_CORPUS,
                                  catalog_official_name=row["official_name"]), Side(subject.text))

    @staticmethod
    def _log_alias_exact(request: NormalizedUserRequest, hit) -> None:
        logger.info("drug_zh_alias_exact_match", extra={
            "request_id": request.request_id, "agent": "verification", "alias_id": hit.alias_id, "alias_target_kind": hit.target_kind})

    def _recover_alias_typo(self, request: NormalizedUserRequest, service: InteractionService, subject: InteractionSubject
                            ) -> SubjectResolution | None:
        """已審核繁中別名的錯字 / 同音字候選（文字與語音皆可）：只是建議，一律需要使用者確認；不默默更正原文。"""
        found, refs = service.zh_typo_candidates(subject.text)
        if not found:
            return None
        logger.info("drug_zh_alias_recovery_used", extra={
            "request_id": request.request_id, "agent": "verification", "recovery_method": "+".join(sorted({c.method for c in found})),
            "recovery_candidate_count": len(found), "recovery_candidate_ids": [r.entity_id for r in refs],
            "recovery_best_score": round(found[0].score, 3)})
        return SubjectResolution(text=subject.text, kind=subject.kind, status=SubjectResolutionStatus.ENTITY_CANDIDATE_NEEDS_CONFIRMATION,
                                 candidates=refs, recovery_confidence=found[0].score, recovery_method=found[0].method)

    def _recover_asr_entity(self, request: NormalizedUserRequest, service: InteractionService, subject: InteractionSubject
                            ) -> SubjectResolution | None:
        """語音專用：無法辨識的 interaction 對象 → 以讀音找已審核 identity 作為「候選建議」（一律需使用者確認，不自動採用）。"""
        if request.raw_transcript is None:
            return None  # 文字輸入 / 其他語音以外的輸入：行為不變
        candidates = service.phonetic_entity_candidates(subject.text)
        if not candidates:
            return None
        refs = [r for r in (service.entity_ref(c.entity_id) for c in candidates[:3]) if r is not None]
        best = candidates[0].confidence
        logger.info("asr_entity_recovery", extra={
            "request_id": request.request_id, "agent": "verification", "asr_entity_recovery_used": True,
            "recovery_candidate_count": len(candidates), "recovery_confidence": best,
            "recovery_proposed_entity": refs[0].canonical_name_en if len(candidates) == 1 and refs else "AMBIGUOUS"})
        return SubjectResolution(text=subject.text, kind=subject.kind, status=SubjectResolutionStatus.ENTITY_CANDIDATE_NEEDS_CONFIRMATION,
                                 candidates=refs, recovery_confidence=best)

    @staticmethod
    def _named_resolution(service, subject, status, entities) -> SubjectResolution:
        if (status == SubjectResolutionStatus.UNRESOLVED and subject.concept_recognized
                and subject.kind != InteractionSubjectKind.PRODUCT):
            # 語意層認得這是合理概念，只是 Interaction corpus 沒有 canonical identity：資料涵蓋缺口，不是拼錯字
            status = SubjectResolutionStatus.IDENTITY_NOT_IN_INTERACTION_CORPUS
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
