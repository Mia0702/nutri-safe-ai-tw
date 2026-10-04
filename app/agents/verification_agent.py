"""Agent B — Evidence / Verification（Hybrid Tool Planning）。

流程
  1. LLM tool planning（OpenAI function calling）：在程式控制的 allowlist 內選擇內部工具，
     可多輪（依前一輪結果決定下一步），受 round / call / 時間上限限制。
  2. Deterministic guardrails：檢查必要的查證是否已完成，沒做的由程式補做（planned_by=guardrail）。
     LLM 無法使用時，整個查證退回 guardrail 流程（planner_mode=deterministic_fallback）。
  3. 解析 Product Verification（固定門檻規則）。
  4. 證據語意評估（LLM）＋ Evidence Relevance Gate（固定門檻）：
     官方但不相關的資料不得進入 EvidenceBundle，只放進 rejected_evidence。
  5. 內部證據不足時（固定規則判斷），才開放 search_tw_official_web 給 planner 決定查詢語句，最多一次。
  6. 依 statement role 判定每個命題的 Claim Assessment，計算 Evidence Strength。

Deterministic product resolution（步驟 0，任何 LLM planning 之前）：以使用者提供的品名 / 字號查結構化資料，
依固定規則（字號 → 官方品名 / 官方別名完全相同 → 基本品名相同 → 名稱相似度）判定產品。
結果為 VERIFIED 或「真正 ambiguous」時鎖定，planner 之後的查詢不得改寫（避免無證據的降級或猜測）。

Structured evidence fast path：產品查證 / 成分 / 核准功效等不含命題的問題，產品 VERIFIED、資料完整、
無衝突、無工具失敗時直接停止（不呼叫 planner / RAG / assess_evidence / 官方網站），否則回到上述完整流程。
產品有兩個以上真正合理的候選時不猜，直接停止並請使用者確認（planner_mode = structured_clarification）。

URL evidence early-stop（planner_mode = url_evidence_early_stop）：使用者貼網址、來源角色已知（非 UNKNOWN）、
產品 VERIFIED、結構化官方資料足以比較時，不跑多輪 planner：
  * 官方監管網頁且頁面對應到已確認產品、沒有使用者自己的命題 → 直接以官方證據回覆。
  * 有外部宣稱（非官方網頁）或使用者命題 → deterministic 查證 + 一次 assess_evidence；
    只有「證據衝突」或「命題沒有任何內部官方資料可對照（且結論不是由規則直接決定）」才查官方網站。
官方網頁本身以 provenance（網址 / 網域 / 標題 / 取得時間）作為官方證據，證據範圍依頁面能否對應到已確認產品決定。

Claim-policy evidence priority（被說出來的宣稱；固定順序，每一層都保留 evidence_use）：
  1. 該產品官方核准功效（product-specific approved claim）——產品 VERIFIED 時永遠先比對，一般例句不能覆蓋。
  2. 結構化 claim-policy 規則（search_claim_policy_rules）
  3. 官方例示詞句（search_claim_policy_examples，宣稱原文與例示文字相同才算對應）
  4. claim-policy RAG（search_rag；只查結構化證據無法決定的宣稱）
  5. 台灣官方網站 fallback（只在證據衝突或宣稱超出內部證據時）
  結構化證據已足以決定的宣稱（policy_decided_claims）不查 RAG、不查官方網站。
  只有 DECISION_ELIGIBLE 的規則 / 例示 / RAG chunk 能參與 deterministic decision；CONTEXT_ONLY /
  CORROBORATION_REQUIRED 只提供脈絡；EXCLUDED_SUPERSEDED 不出現在 current query。

assess_evidence 去重：同一份證據（沒有新的候選、已確認參考資料沒有變化）不重複評估；
官方網站只回傳已知頁面（例如使用者貼的同一官方網址）時不算新增證據，不重新評估。

不可變的 guardrails：台灣 Phase 1 only、官方網域 allowlist、round / call / 時間上限、
ID 必須來自先前工具結果、證據來源驗證、外部失敗必須如實記錄。
"""

import hashlib
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agents.prompts import EVIDENCE_ASSESSMENT_INSTRUCTIONS, PLANNER_INSTRUCTIONS, WEB_PLANNER_INSTRUCTIONS
from app.agents.evidence_tools import (
    EVIDENCE_TOOLS,
    OFFICIAL_WEB_TOOLS,
    ClaimPolicyRulesArgs,
    ClaimTextsArgs,
    GraphArgs,
    HealthFunctionArgs,
    IllegalAdArgs,
    LookupPermitArgs,
    LookupProductArgs,
    NamesArgs,
    PlannerState,
    ProductIdsArgs,
    QueryArgs,
    ToolSpec,
)
from app.normalization import extract_permit_code, normalize_name
from app.product_coverage import has_health_claim_approval
from app.risk.assessment_rules import (
    assess_claim,
    compute_evidence_strength,
    relevance_gate,
    resolve_product_match,
)
from app.risk.claim_policy import (
    EFFECT_PROHIBITED,
    MEDICAL_EXAMPLE_TYPE,
    StructuredPolicyContext,
    exact_approved_match,
    is_asserted_disease_claim,
    is_disease_claim,
    is_explicit_medical_claim,
    match_approved_function,
)
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimEvidenceRelation,
    EntityType,
    ErrorCode,
    EvidenceScope,
    Intent,
    ProductMatchType,
    ProductVerificationStatus,
    SourceType,
    UrlSourceRole,
)
from app.schemas.evidence import (
    ClaimEvidenceLink,
    EvidenceBundle,
    EvidenceRelevance,
    OfficialWebEvidence,
    PlannerStep,
    ProductMatch,
    RejectedEvidence,
    SourceRef,
    ToolCallRecord,
    ToolError,
)
from app.schemas.user_request import NormalizedUserRequest
from app.services.errors import ServiceError
from app.services.openai_service import LLMClient
from app.tools.base import ToolCallRecorder
from app.tools.graph_tools import GraphQueryType, GraphRepository
from app.tools.product_tools import OfficialDataRepository
from app.tools.claim_policy_tools import ClaimPolicyRepository
from app.tools.rag_tools import RagStore

logger = logging.getLogger("nutri_safe_ai.agents.verification")

MAX_PLANNER_ROUNDS = 4
MAX_CALLS_PER_ROUND = 6
MAX_TOTAL_PLANNED_CALLS = 14
PLANNER_TIME_BUDGET_SECONDS = 90.0
MAX_WEB_SEARCHES = 1
MAX_CLAIMS_FOR_RAG = 3
EVIDENCE_TEXT_CHARS = 700

# Structured evidence fast path：只限明確的產品資訊類 intent（不含命題、不含安全性問題）
FAST_PATH_INTENTS = frozenset({Intent.PRODUCT_VERIFICATION, Intent.INGREDIENT_INQUIRY, Intent.EFFICACY_INQUIRY})
FAST_PATH_MODE = "structured_fast_path"
CLARIFICATION_MODE = "structured_clarification"
URL_EVIDENCE_MODE = "url_evidence_early_stop"
IMAGE_IDENTIFICATION_MODE = "image_identification_stop"
PACKAGE_CLAIM_MODE = "package_claim_early_stop"
CLAIM_COMPARISON_MODE = "approved_claim_comparison"  # 已確認產品 + 官方核准功效：宣稱直接與結構化證據比對
CLAIMS_PENDING_PRODUCT_MODE = "claims_pending_product_choice"  # 產品待使用者確認：宣稱先不比對核准功效
URL_EVIDENCE_PREFIX = "url:"  # 使用者提供的官方網頁作為證據時的 evidence_id 前綴
# corpus 的醫療效能規則類別：被說出的疾病宣稱以這類 PROHIBITED 規則作為法規來源
MEDICAL_RULE_CATEGORY = "MEDICAL_EFFICACY"

# Agent 內部的 LLM 步驟（不是外部資料來源）
PLANNER_STEP = "plan_tool_calls"
ASSESSMENT_STEP = "assess_evidence"


class _RelevanceItem(BaseModel):
    evidence_id: str
    relevance_score: float
    evidence_scope: EvidenceScope
    supports_claim: bool | None
    rationale: str


class _ClaimLink(BaseModel):
    claim_index: int
    evidence_id: str
    relation: ClaimEvidenceRelation
    rationale: str


class _EvidenceAssessment(BaseModel):
    relevance: list[_RelevanceItem]
    claim_links: list[_ClaimLink]


def _openai_not_configured():
    raise ServiceError(ErrorCode.NOT_CONFIGURED, "OpenAI 未設定，無法評估證據相關性", "openai")


class VerificationAgent:
    def __init__(
        self,
        official_repo: OfficialDataRepository | None,
        graph_repo: GraphRepository | None,
        rag_store: RagStore | None,
        llm: LLMClient | None,
        rag_top_k: int = 5,
        rag_min_similarity: float = 0.3,
        enable_official_web: bool = True,
        enable_llm_planner: bool = True,
        claim_policy_repo: ClaimPolicyRepository | None = None,
    ):
        self._repo = official_repo
        self._graph = graph_repo
        self._rag = rag_store
        self._llm = llm
        self._rag_top_k = rag_top_k
        self._rag_min_similarity = rag_min_similarity
        self._enable_official_web = enable_official_web
        self._enable_llm_planner = enable_llm_planner
        self._claim_policy = claim_policy_repo

    # ================================================================== main

    def verify(self, request: NormalizedUserRequest) -> EvidenceBundle:
        rec = ToolCallRecorder(request.request_id)
        bundle = EvidenceBundle(
            request_id=request.request_id,
            product_match=ProductMatch(status=ProductVerificationStatus.NOT_APPLICABLE),
        )
        state = PlannerState(
            request=request, bundle=bundle, repo=self._repo, graph=self._graph, rag=self._rag, llm=self._llm,
            rag_top_k=self._rag_top_k, rag_min_similarity=self._rag_min_similarity, claim_policy=self._claim_policy,
        )
        # 前文已查證過的產品 ID 可直接使用
        for entity in request.context_entities_used:
            if entity.entity_type == EntityType.PRODUCT and entity.entity_id:
                state.known_product_ids.add(entity.entity_id)

        # 0. 先以 deterministic resolver 確定產品，再決定是否需要 LLM planning
        self._deterministic_product_resolution(state, rec)
        self._attach_official_url_evidence(state)
        # 0b. Structured evidence fast path：簡單的產品 / 成分 / 核准功效問題，結構化官方資料已足夠就停止
        if self._try_structured_fast_path(state, rec):
            links: list[_ClaimLink] = []
        elif self._try_image_identification_stop(state, rec):
            links = []
        elif (url_links := self._try_url_evidence_path(state, rec)) is not None:
            links = url_links
        elif (package_links := self._try_package_claim_path(state, rec)) is not None:
            links = package_links
        elif (pending_links := self._try_claims_pending_product(state, rec)) is not None:
            links = pending_links
        else:
            links = self._full_verification(state, rec)
        self._attach_official_url_evidence(state)  # 依最終的產品確認結果更新官方網頁的證據範圍

        # 6. 命題評估與摘要
        bundle.tools_called = rec.records
        bundle.errors = self._source_errors(rec)
        self._attach_claim_policy_evidence(state)
        self._assess_claims(request, bundle, links, rec, [self._policy_context(state, c) for c in request.claims])
        bundle.evidence_strength = compute_evidence_strength(bundle)
        bundle.conflicts = self._conflicts(bundle)
        bundle.missing_information = self._missing_information(request, bundle)
        logger.info(
            "verification_done",
            extra={
                "request_id": request.request_id,
                "agent": "verification",
                "evidence_count": bundle.evidence_count(),
                "product_verification": bundle.product_match.status.value,
                "evidence_strength": bundle.evidence_strength.value,
                "planner_mode": bundle.planner_mode,
                "fast_path_eligible": bundle.fast_path_eligible,
                "fast_path_reason": bundle.fast_path_reason,
                "url_source_roles": ",".join(u.source_role.value for u in request.url_sources) or None,
                "assessment_count": state.assessment_count,
                **stage_timings_ms(rec.records),
                **bundle.product_match.resolver_debug(),
            },
        )
        return bundle

    def _full_verification(self, state: PlannerState, rec: ToolCallRecorder) -> list["_ClaimLink"]:
        request, bundle = state.request, state.bundle
        # 1. LLM tool planning（內部工具）
        planner_ok = False
        if self._llm is not None and self._enable_llm_planner:
            planner_ok = self._run_planner(
                state, rec, EVIDENCE_TOOLS, PLANNER_INSTRUCTIONS, self._planner_input(request),
                max_rounds=MAX_PLANNER_ROUNDS, tool_choice="auto",
            )
        bundle.planner_mode = "llm_planner" if planner_ok else "deterministic_fallback"

        # 2~3. guardrails 補齊必要查證，並解析產品
        self._guardrail_product_lookup(state, rec)
        bundle.product_match = self._resolve_product(state, rec)
        self._guardrail_product_details(state, rec)
        self._guardrail_rag(state, rec)
        self._guardrail_ads_and_graph(state, rec)

        # 4. 證據評估 + Relevance Gate
        links = self._assess_and_gate(state, rec)

        # 5. 內部證據不足才查官方網站；官方網站有新增證據才重新評估（最多一次）
        if self._enable_official_web and self._internal_evidence_insufficient(
                request, bundle, links, state.policy_decided_claims):
            links = self._official_web_then_reassess(state, rec, links)
        return links

    def _official_web_then_reassess(self, state: PlannerState, rec: ToolCallRecorder, links: list["_ClaimLink"]):
        self._run_official_web(state, rec)
        self._drop_known_web_candidates(state)
        if state.candidate_web:
            links = self._assess_and_gate(state, rec)
        return links

    # ================================================================== URL evidence early-stop

    # ================================================================== 圖片：product-first

    def _try_image_identification_stop(self, state: PlannerState, rec: ToolCallRecorder) -> bool:
        """只傳圖片、沒有待查證宣稱：辨識產品就是目標。fast path / 確認卡沒處理到的情況
        （查無、只有部分比對、沒有可辨識的產品、結構化查詢失敗）直接停止，請使用者補充資訊，
        不用多輪 planner / RAG / 官方網站硬找（查不到不代表是假貨）。"""
        request, bundle = state.request, state.bundle
        if not request.image_identification or request.claims:
            return False
        self._guardrail_ads_and_graph(state, rec)  # 違規廣告紀錄照查
        bundle.planner_mode = IMAGE_IDENTIFICATION_MODE
        bundle.fast_path_reason = f"image_identification:{bundle.fast_path_reason or 'no_reason'}"
        bundle.planner_trace.append(PlannerStep(
            round=0, note="只傳圖片：以辨識產品為目標，無法確認產品時請使用者補充，停止規劃"))
        return True

    def _try_package_claim_path(self, state: PlannerState, rec: ToolCallRecorder) -> list["_ClaimLink"] | None:
        """宣稱查驗的 early-stop（非 URL 輸入）：產品已確認、結構化官方資料可直接比對時，不跑多輪 planner。

        * 包裝宣稱（高風險醫療宣稱自動觸發、按「查驗包裝宣稱」、使用者明確要求查驗圖片）：有核准功效或警語即可。
        * 其他宣稱 / 命題：該產品有官方核准功效時（product-specific approved claim 可直接比對）。
        deterministic 查證 + 最多一次 assess_evidence（所有被說出的宣稱都與核准功效文字完全相同時不需要語意比對）；
        只有證據衝突或宣稱沒有任何內部證據可對照（且結論不是由規則直接決定）才查官方網站。
        產品未確認、查詢失敗、沒有結構化證據 → None（回到完整流程）。
        """
        request, bundle = state.request, state.bundle
        if not request.claims or request.url_sources:
            return None
        package = request.claims_from_package
        label = "package_claim" if package else "claim_comparison"
        if not self._product_sufficient(state):
            bundle.fast_path_reason = f"{label}_declined:product_{bundle.product_match.status.value.lower()}"
            return None
        self._guardrail_product_details(state, rec)
        has_evidence = bool(bundle.approved_claims or bundle.warnings) if package else bool(bundle.approved_claims)
        if state.failed or not has_evidence:
            bundle.fast_path_reason = f"{label}_declined:structured_evidence_missing"
            return None
        self._guardrail_ads_and_graph(state, rec)
        bundle.planner_mode = PACKAGE_CLAIM_MODE if package else CLAIM_COMPARISON_MODE
        if all(c.is_asserted and not c.uses_absolute_language and exact_approved_match(c.claim_text, bundle.approved_claims)
               for c in request.claims):
            if state.failed:
                bundle.fast_path_reason = f"{label}_declined:structured_lookup_failed"
                return None
            bundle.fast_path_reason = f"{label}s_assessed:approved_exact_text"
            bundle.planner_trace.append(PlannerStep(round=0, note="宣稱與官方核准功效文字完全相同，不需要語意比對"))
            return []
        # 宣稱與使用者提問的命題共用同一條結構化 claim-policy evidence path
        self._claim_policy_stage(state, rec)
        if not state.failed and state.policy_final_claims >= set(range(len(request.claims))):
            bundle.fast_path_reason = f"{label}s_assessed:structured_policy_deterministic"
            bundle.planner_trace.append(PlannerStep(
                round=0, note="所有命題已由核准功效 / 結構化規則決定，不查 RAG / 官方網站、不做語意比對"))
            return []
        self._guardrail_rag(state, rec)
        if state.failed - {"search_rag"}:
            bundle.fast_path_reason = f"{label}_declined:structured_lookup_failed"
            return None
        links = self._assess_and_gate(state, rec)
        need_web, reason = self._url_claims_need_official_web(request, links, state.policy_decided_claims)
        bundle.fast_path_reason = f"{label}s_assessed:{reason}"
        bundle.planner_trace.append(PlannerStep(round=0, note=f"宣稱已用結構化官方資料評估一次（{reason}）"))
        if need_web and self._enable_official_web:
            links = self._official_web_then_reassess(state, rec, links)
        return links

    def _try_claims_pending_product(self, state: PlannerState, rec: ToolCallRecorder) -> list["_ClaimLink"] | None:
        """產品有多個 / 低信心候選（resolver 已鎖定，不猜）且有宣稱：先請使用者確認產品，不跑 planner / 官方網站。

        一般宣稱在產品確認前無法與核准功效比對（INSUFFICIENT_EVIDENCE）；疾病宣稱由規則對「宣稱本身」判定，
        只查一次法規文件作為來源（RAG + 一次 assess_evidence）。使用者選定產品後以原題重跑，再逐條比對核准功效。
        """
        request, bundle = state.request, state.bundle
        match = bundle.product_match
        if not request.claims or not (match.is_ambiguous or match.needs_confirmation) or state.failed:
            return None
        self._guardrail_ads_and_graph(state, rec)
        self._claim_policy_stage(state, rec)  # 宣稱本身的官方例示 / 規則不需要先確認產品
        links: list[_ClaimLink] = []
        if any(is_asserted_disease_claim(c) for c in request.claims):
            self._guardrail_rag(state, rec)
            if state.candidate_rag:
                links = self._assess_and_gate(state, rec)
        bundle.planner_mode = CLAIMS_PENDING_PRODUCT_MODE
        bundle.fast_path_reason = f"claims_pending_product_choice:{match.ambiguity_reason.value}"
        bundle.planner_trace.append(PlannerStep(
            round=0, note=f"產品無法唯一確認（{match.ambiguity_reason.value}），宣稱待產品確認後比對核准功效，停止規劃"))
        return links

    def _try_url_evidence_path(self, state: PlannerState, rec: ToolCallRecorder) -> list["_ClaimLink"] | None:
        """URL 輸入的保守 early-stop；條件不滿足回傳 None（回到完整流程，已查過的工具不會重複呼叫）。"""
        request, bundle = state.request, state.bundle
        if not request.url_sources:
            return None

        def decline(reason: str) -> None:
            bundle.fast_path_reason = f"url_path_declined:{reason}"
            return None

        roles = request.url_source_roles
        if UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE in roles:
            return decline("source_unknown")
        if not any(u.fetched for u in request.url_sources):
            return decline("url_not_fetched")
        if not self._product_sufficient(state):
            match = bundle.product_match
            return decline("structured_lookup_failed" if state.failed else f"product_{match.status.value.lower()}")
        self._guardrail_product_details(state, rec)
        if state.failed:
            return decline("structured_lookup_failed")
        official_only = roles == {UrlSourceRole.OFFICIAL_REGULATORY_SOURCE}

        if not request.claims:
            # 情況 B：官方監管網頁 → 直接以官方證據回覆（不把頁面當外部宣稱、不再搜尋同一官方資訊）
            if not official_only:
                return decline("no_external_claim_extracted")
            if not self._official_page_maps_to_product(state):
                return decline("official_page_not_mapped_to_product")
            self._guardrail_ads_and_graph(state, rec)  # 違規廣告紀錄照查
            if state.failed:
                return decline("structured_lookup_failed")
            bundle.planner_mode = URL_EVIDENCE_MODE
            bundle.fast_path_reason = "official_url_mapped_to_verified_product"
            bundle.planner_trace.append(PlannerStep(
                round=0, note="使用者提供官方監管網頁，已對應到已確認產品，以官方證據回覆，停止規劃"))
            return []

        # 情況 A：外部宣稱（或使用者命題）＋已確認產品＋結構化官方核准功效 / 警語可比較
        if not (bundle.approved_claims or bundle.warnings):
            return decline("structured_evidence_missing")
        self._guardrail_ads_and_graph(state, rec)
        self._guardrail_rag(state, rec)  # 法規文字（醫療 / 誇大類命題）仍要查，RAG 失敗如實記錄
        if state.failed - {"search_rag"}:
            return decline("structured_lookup_failed")
        bundle.planner_mode = URL_EVIDENCE_MODE
        links = self._assess_and_gate(state, rec)
        need_web, reason = self._url_claims_need_official_web(request, links, state.policy_decided_claims)
        bundle.fast_path_reason = f"url_claims_assessed:{reason}"
        bundle.planner_trace.append(PlannerStep(
            round=0, note=f"URL 宣稱已用結構化官方資料評估一次（{reason}），不執行多輪規劃"))
        if need_web and self._enable_official_web:
            links = self._official_web_then_reassess(state, rec, links)
        return links

    @staticmethod
    def _url_claims_need_official_web(request: NormalizedUserRequest, links,
                                      policy_decided: set[int] = frozenset()) -> tuple[bool, str]:
        """URL 宣稱評估後，是否還需要官方網站（只有證據衝突，或命題沒有任何內部官方資料可對照時）。

        被說出來的疾病宣稱：已和核准功效比對過，沒有對應時結論由規則直接決定（MEDICAL_TREATMENT_CLAIM），
        官方網站不會改變結論，因此不查。結構化 claim-policy 已足以決定的宣稱同理（policy_decided）。
        """
        rule_decided = {i for i, claim in enumerate(request.claims) if is_asserted_disease_claim(claim)}
        rule_decided |= set(policy_decided)
        # 規則已決定結論的宣稱，被法規文件「反駁」只是與規則一致，不是需要再查的證據衝突
        if any(link.relation == ClaimEvidenceRelation.CONTRADICTS and link.claim_index not in rule_decided
               for link in links):
            return True, "evidence_conflict"
        linked = {link.claim_index for link in links}
        for i in range(len(request.claims)):
            if i not in linked and i not in rule_decided:
                return True, "claim_beyond_internal_evidence"
        return False, "internal_evidence_sufficient"

    def _official_page_maps_to_product(self, state: PlannerState) -> bool:
        """官方網頁內容是否確實對應到已確認產品（頁面上有該產品的許可證字號或官方品名）。"""
        best = state.bundle.product_match.best_match
        if best is None or state.bundle.product_match.status != ProductVerificationStatus.VERIFIED:
            return False
        permit_code = extract_permit_code(best.product.permit_number or "")
        name = normalize_name(best.product.product_name_zh)
        for src in state.request.url_sources:
            if not (src.is_official and src.fetched):
                continue
            page = f"{src.title or ''}\n{src.excerpt}"
            if permit_code and permit_code in page:
                return True
            if name and name in normalize_name(page):
                return True
        return False

    def _attach_official_url_evidence(self, state: PlannerState) -> None:
        """使用者提供的官方監管網頁 → 官方證據（保留網址 / 網域 / 標題 / 取得時間）。

        官方網域只代表來源角色：頁面能對應到已確認產品才是 PRODUCT_SPECIFIC，否則只是 GENERAL_BACKGROUND。
        """
        bundle = state.bundle
        official = [u for u in state.request.url_sources if u.is_official and u.fetched]
        if not official:
            return
        mapped = self._official_page_maps_to_product(state)
        bundle.official_web_evidence = [
            e for e in bundle.official_web_evidence if not e.evidence_id.startswith(URL_EVIDENCE_PREFIX)
        ]
        for src in official:
            bundle.official_web_evidence.append(OfficialWebEvidence(
                evidence_id=URL_EVIDENCE_PREFIX + hashlib.sha1(src.source_url.encode("utf-8")).hexdigest()[:16],
                title=src.title or src.source_domain or src.source_url,
                url=src.source_url,
                domain=src.source_domain or "",
                snippet=src.excerpt[:EVIDENCE_TEXT_CHARS],
                relevance=EvidenceRelevance(
                    relevance_score=1.0,
                    evidence_scope=EvidenceScope.PRODUCT_SPECIFIC if mapped else EvidenceScope.GENERAL_BACKGROUND,
                    rationale="使用者提供的台灣官方監管網域頁面" + ("，頁面對應到已確認產品" if mapped else ""),
                    assessed_by="source_role",
                ),
                source=SourceRef(
                    source_name=src.title or src.source_domain or src.source_url,
                    source_type=SourceType.OFFICIAL_WEB,
                    source_url=src.source_url,
                    retrieved_at=src.retrieved_at,
                    is_official=True,
                ),
            ))

    def _drop_known_web_candidates(self, state: PlannerState) -> None:
        """官方網站回傳的頁面若是使用者已提供的網址或已評估過的證據，不算新增證據。"""
        known_urls = {u.rstrip("/") for src in state.request.url_sources for u in (src.source_url, src.requested_url)}
        known_ids = state.bundle.evidence_ids() | state.assessed_evidence_ids
        dropped = [eid for eid, e in state.candidate_web.items()
                   if eid in known_ids or e.url.rstrip("/") in known_urls]
        for eid in dropped:
            state.candidate_web.pop(eid, None)
        if dropped:
            state.bundle.planner_trace.append(PlannerStep(
                round=len(state.bundle.planner_trace) + 1, note=f"官方網站結果 {len(dropped)} 筆為已知頁面，不重新評估"))

    # ================================================================== structured fast path

    @staticmethod
    def fast_path_decision(request: NormalizedUserRequest) -> tuple[bool, str | None]:
        """只有「不含任何待查證命題」的明確產品資訊問題才可走 fast path；回傳 (eligible, 不適用原因)。

        有命題（廣告 / 包裝 / 使用者斷言 / 是非題）、安全性問題、外國產品、無法解析的指稱，一律走完整查證。
        """
        if request.intent not in FAST_PATH_INTENTS:
            return False, "intent_not_informational"
        if request.claims:
            return False, "has_claims_to_verify"
        if not request.has_product_identifiers:
            return False, "no_product_identifier"
        if request.missing_product_context:
            return False, "missing_product_context"
        if request.foreign_country_codes:
            return False, "foreign_product"
        return True, None

    @classmethod
    def fast_path_eligible(cls, request: NormalizedUserRequest) -> bool:
        return cls.fast_path_decision(request)[0]

    def _deterministic_product_resolution(self, state: PlannerState, rec: ToolCallRecorder) -> None:
        """LLM planning 前先查使用者提供的產品識別資訊並判定；確定的結果（VERIFIED / ambiguous）鎖定。"""
        req = state.request
        if not (req.needs_product_lookup and req.has_product_identifiers):
            return
        self._guardrail_product_lookup(state, rec)
        match = self._resolve_product(state, rec)
        state.bundle.product_match = match
        if match.lookup_completed and (match.status == ProductVerificationStatus.VERIFIED or match.is_ambiguous):
            state.locked_product_match = match

    def _try_structured_fast_path(self, state: PlannerState, rec: ToolCallRecorder) -> bool:
        """deterministic 查結構化官方資料；證據充分才停止，否則回到完整流程（已查過的工具不會重複呼叫）。"""
        request, bundle = state.request, state.bundle
        eligible, reason = self.fast_path_decision(request)
        name_only = not eligible and self._product_name_only_candidate(request)
        if name_only:
            # intent 不明、沒有命題、整則訊息就是一個產品名稱：只有官方品名 / 官方別名「完全相同」才走 fast path
            if not self._product_name_only_exact(state):
                bundle.fast_path_reason = "product_name_only_not_exact"
                return False
            eligible, reason = True, None
        bundle.fast_path_eligible = eligible
        if not eligible:
            bundle.fast_path_reason = reason
            return False
        match = bundle.product_match
        if (match.is_ambiguous or match.needs_confirmation) and not state.failed:
            # 多個合理候選，或只有名稱相似、未達確認門檻的候選：不猜、不交給 planner
            # （planner 的查詢不能把使用者寫法的分數抬高，跑完也無法確認），直接請使用者確認是哪一項
            self._guardrail_ads_and_graph(state, rec)
            bundle.planner_mode = CLARIFICATION_MODE
            bundle.fast_path_reason = f"product_needs_user_choice:{match.ambiguity_reason.value}"
            bundle.planner_trace.append(PlannerStep(
                round=0, note=f"產品無法唯一確認（{match.ambiguity_reason.value}），請使用者確認，停止規劃"))
            return True
        if not self._product_sufficient(state):
            bundle.fast_path_reason = (
                "structured_lookup_failed" if state.failed else f"product_{match.status.value.lower()}"
            )
            return False
        self._guardrail_product_details(state, rec)
        if not self._details_sufficient(state):
            bundle.fast_path_reason = "structured_lookup_failed" if state.failed else "requested_data_missing"
            return False
        self._guardrail_ads_and_graph(state, rec)  # 違規廣告紀錄照查：fast path 不降低安全檢查
        if state.failed:
            bundle.fast_path_reason = "structured_lookup_failed"
            return False
        bundle.planner_mode = FAST_PATH_MODE
        bundle.fast_path_reason = "product_name_only_exact" if name_only else "verified_structured_evidence_sufficient"
        bundle.planner_trace.append(PlannerStep(round=0, note="結構化官方資料已足夠（產品已確認、無衝突、無待查證命題），停止規劃"))
        return True

    @staticmethod
    def _product_name_only_candidate(request: NormalizedUserRequest) -> bool:
        """intent=unclear 的純產品名稱查詢候選（之後還要確認 resolver exact 且整則文字就是該名稱）。"""
        return (
            request.intent == Intent.UNCLEAR
            and not request.claims
            and bool(request.product_candidates)
            and not (request.permit_number_candidates or request.ingredient_candidates or request.url_sources
                     or request.observed_package_claims or request.missing_product_context
                     or request.foreign_country_codes or request.image_identification)
        )

    @staticmethod
    def _product_name_only_exact(state: PlannerState) -> bool:
        """resolver 以官方品名 / 官方別名完全相同確認（VERIFIED），且使用者原始訊息正規化後就只是這個名稱。"""
        match = state.bundle.product_match
        best = match.best_match
        if (match.status != ProductVerificationStatus.VERIFIED or best is None
                or match.match_type not in (ProductMatchType.CANONICAL_EXACT, ProductMatchType.OFFICIAL_ALIAS_EXACT)):
            return False
        text = normalize_name(state.request.original_text)
        return bool(text) and text in {normalize_name(best.product.product_name_zh), normalize_name(best.query)}

    @staticmethod
    def _product_sufficient(state: PlannerState) -> bool:
        m = state.bundle.product_match
        return m.status == ProductVerificationStatus.VERIFIED and m.lookup_completed and m.best_match is not None and not state.failed

    @staticmethod
    def _details_sufficient(state: PlannerState) -> bool:
        if state.failed:
            return False
        intent, bundle = state.request.intent, state.bundle
        if intent == Intent.EFFICACY_INQUIRY:
            best = bundle.product_match.best_match
            if best is not None and not has_health_claim_approval(best.product.product_category):
                # 查驗登記／核備類別本來就沒有核准保健功效：「沒有」就是結構化答案，不是缺資料
                return True
            return bool(bundle.approved_claims)
        if intent == Intent.INGREDIENT_INQUIRY:
            return bool(bundle.ingredients)
        return True

    # ================================================================== planner

    @staticmethod
    def _planner_input(request: NormalizedUserRequest) -> str:
        payload = {
            "question": request.question or request.normalized_text,
            "intent": request.intent.value,
            "product_candidates": request.product_candidates,
            "corrected_product_candidates": request.corrected_product_candidates,
            "brand_candidates": request.brand_candidates,
            "company_candidates": request.company_candidates,
            "ingredient_candidates": request.ingredient_candidates,
            "permit_number_candidates": request.permit_number_candidates,
            "product_reference_status": request.product_reference_status.value,
            "context_entities": [e.model_dump(mode="json") for e in request.context_entities_used],
            "claims": [
                {"index": i, "text": c.claim_text, "origin": c.origin.value, "category": c.semantic_category.value}
                for i, c in enumerate(request.claims)
            ],
            "hints": {
                "needs_product_lookup": request.needs_product_lookup,
                "needs_claim_check": request.needs_claim_check,
                "needs_graph_lookup": request.needs_graph_lookup,
                "needs_rag": request.needs_rag,
            },
            "scope": "只查台灣（TW）官方資料",
        }
        return json.dumps(payload, ensure_ascii=False)

    def _run_planner(
        self,
        state: PlannerState,
        rec: ToolCallRecorder,
        allowlist: dict[str, ToolSpec],
        instructions: str,
        initial_input: str,
        *,
        max_rounds: int,
        tool_choice: str,
        max_total_calls: int = MAX_TOTAL_PLANNED_CALLS,
    ) -> bool:
        """執行受控的 tool planning 迴圈。回傳 planner 是否正常運作。"""
        tools = [spec.openai_tool() for spec in allowlist.values()]
        started = time.monotonic()
        previous_id: str | None = None
        input_items: Any = initial_input
        total_calls = 0
        trace = state.bundle.planner_trace
        base_round = len(trace)

        for round_no in range(1, max_rounds + 1):
            if time.monotonic() - started > PLANNER_TIME_BUDGET_SECONDS:
                trace.append(PlannerStep(round=base_round + round_no, note="超過 planner 時間上限，停止規劃"))
                break
            plan = rec.call(
                PLANNER_STEP, {"round": base_round + round_no, "allowed_tools": list(allowlist)},
                lambda: self._llm.plan_step(
                    instructions=instructions, input_items=input_items, tools=tools,
                    previous_response_id=previous_id, tool_choice=tool_choice,
                ),
            )
            if not plan.ok:
                trace.append(PlannerStep(round=base_round + round_no, note=f"planner 失敗：{plan.error.code.value}"))
                return False
            step = plan.data
            if not step.calls:
                trace.append(PlannerStep(round=base_round + round_no, note="planner 判斷證據已足夠"))
                break

            outputs, planned, rejected = [], [], []
            for index, call in enumerate(step.calls):
                output = self._execute_planned_call(
                    state, rec, allowlist, call, over_limit=index >= MAX_CALLS_PER_ROUND or total_calls >= max_total_calls
                )
                if output.get("rejected"):
                    rejected.append(f"{call.name}: {output['error']}")
                else:
                    planned.append(call.name)
                    total_calls += 1
                outputs.append({"type": "function_call_output", "call_id": call.call_id,
                                "output": json.dumps(output, ensure_ascii=False, default=str)})
            trace.append(PlannerStep(round=base_round + round_no, planned_calls=planned, rejected_calls=rejected))
            previous_id = step.response_id
            input_items = outputs
            tool_choice = "auto"
            if total_calls >= max_total_calls:
                trace.append(PlannerStep(round=base_round + round_no, note="達到工具呼叫總數上限"))
                break
        return True

    def _execute_planned_call(self, state, rec, allowlist, call, *, over_limit: bool) -> dict[str, Any]:
        """allowlist、參數 schema、guard、重複檢查都通過才真正執行。"""
        spec = allowlist.get(call.name)
        if spec is None:
            return {"rejected": True, "error": f"工具 {call.name} 不在允許清單中"}
        if over_limit:
            return {"rejected": True, "error": "超過工具呼叫上限，未執行"}
        try:
            args = spec.args_model.model_validate(json.loads(call.arguments or "{}"))
        except (json.JSONDecodeError, ValidationError) as exc:
            return {"rejected": True, "error": f"參數格式錯誤：{type(exc).__name__}"}
        if spec.guard and (reason := spec.guard(state, args)):
            return {"rejected": True, "error": reason}
        signature = f"{spec.name}:{args.model_dump_json()}"
        if signature in state.call_signatures:
            return {"rejected": True, "error": "相同查詢已執行過"}
        state.call_signatures.add(signature)
        return self._call_tool(state, rec, spec, args, planned_by="llm_planner")

    @staticmethod
    def _call_tool(state: PlannerState, rec: ToolCallRecorder, spec: ToolSpec, args: BaseModel, planned_by: str) -> dict:
        state.attempted[spec.name] = state.attempted.get(spec.name, 0) + 1
        result = rec.call(spec.name, args.model_dump(mode="json"), lambda: spec.handler(state, args), planned_by=planned_by)
        if not result.ok:
            state.failed.add(spec.name)
            return {"error": result.error.code.value, "message": result.error.message}
        return result.data[1]

    def _guardrail_call(self, state: PlannerState, rec: ToolCallRecorder, name: str, args: BaseModel) -> None:
        spec = {**EVIDENCE_TOOLS, **OFFICIAL_WEB_TOOLS}[name]
        signature = f"{spec.name}:{args.model_dump_json()}"
        if signature in state.call_signatures:
            return
        state.call_signatures.add(signature)
        self._call_tool(state, rec, spec, args, planned_by="guardrail")

    # ================================================================== guardrails

    def _guardrail_product_lookup(self, state: PlannerState, rec: ToolCallRecorder) -> None:
        req = state.request
        if not (req.needs_product_lookup and req.has_product_identifiers):
            return
        if req.permit_number_candidates and not state.attempted.get("lookup_permit"):
            for permit in req.permit_number_candidates:
                self._guardrail_call(state, rec, "lookup_permit", LookupPermitArgs(permit_text=permit))
        if req.product_candidates and not state.attempted.get("lookup_product"):
            # 原始寫法與 Agent A 的修正寫法一起查；修正寫法能否確認產品由 resolver 的佐證規則決定
            self._guardrail_call(state, rec, "lookup_product", LookupProductArgs(
                product_names=req.product_candidates + req.corrected_product_candidates,
                brand_names=req.brand_candidates))
        names = req.company_candidates + req.brand_candidates
        if names and not state.attempted.get("lookup_company"):
            self._guardrail_call(state, rec, "lookup_company", NamesArgs(names=names))

    def _resolve_product(self, state: PlannerState, rec: ToolCallRecorder) -> ProductMatch:
        req = state.request
        if not (req.needs_product_lookup and req.has_product_identifiers):
            return resolve_product_match(req, [], [], [], [], lookup_completed=True)
        if state.locked_product_match is not None:
            # deterministic resolver 已確定：planner 之後的查詢結果不得改寫（不降級、不猜）
            return state.locked_product_match
        product_tools = {"lookup_product", "lookup_permit", "lookup_company"}
        completed = not (product_tools & state.failed)
        queried = req.permit_number_candidates + req.product_candidates + req.company_candidates + req.brand_candidates
        state.bundle.inferred_links = list(state.inferred_links)
        return resolve_product_match(
            req, list(state.name_matches.values()), state.permit_matches, state.company_names_found, queried, completed,
            inferred_links=state.inferred_links,
        )

    def _resolved_product_id(self, bundle: EvidenceBundle) -> str | None:
        m = bundle.product_match
        if m.best_match and m.status in (ProductVerificationStatus.VERIFIED, ProductVerificationStatus.CONFLICT):
            return m.best_match.product.product_id
        return None

    def _guardrail_product_details(self, state: PlannerState, rec: ToolCallRecorder) -> None:
        bundle = state.bundle
        pid = self._resolved_product_id(bundle)
        if pid is None:
            return
        args = ProductIdsArgs(product_ids=[pid])
        if pid not in state.approved_by_product:
            self._guardrail_call(state, rec, "lookup_approved_claim", args)
        if pid not in state.warnings_by_product:
            self._guardrail_call(state, rec, "lookup_product_warnings", args)
        if pid not in state.ingredients_by_product:
            self._guardrail_call(state, rec, "lookup_product_ingredients", args)
        if state.request.needs_graph_lookup and not any(
            e.subject_id == pid and e.relation == "CONTAINS" for e in state.graph_evidence.values()
        ):
            self._guardrail_call(state, rec, "query_knowledge_graph",
                                 GraphArgs(query_type=GraphQueryType.PRODUCT_INGREDIENTS, ids=[pid]))
        # 只有確認的產品資料才進入 EvidenceBundle
        bundle.approved_claims = state.approved_by_product.get(pid, [])
        bundle.warnings = state.warnings_by_product.get(pid, [])
        for item in state.ingredients_by_product.get(pid, []):
            if all(i.ingredient_id != item.ingredient_id for i in bundle.ingredients):
                bundle.ingredients.append(item)

    def _guardrail_rag(self, state: PlannerState, rec: ToolCallRecorder) -> None:
        req = state.request
        if (req.claims or req.needs_rag) and not state.attempted.get("search_rag"):
            self._claim_policy_stage(state, rec)  # evidence priority：結構化 policy 先於 RAG
            claims = [c for i, c in enumerate(req.claims) if i not in state.policy_decided_claims]
            if req.claims and not claims:
                state.bundle.planner_trace.append(PlannerStep(
                    round=len(state.bundle.planner_trace) + 1, note="宣稱已由核准功效 / 結構化 claim policy 決定，不查 RAG"))
                return
            queries = [rag_query_for_claim(c) for c in claims[:MAX_CLAIMS_FOR_RAG]] or [req.question or req.normalized_text]
            for query in queries:
                self._guardrail_call(state, rec, "search_rag", QueryArgs(query=query))
                if "search_rag" in state.failed:
                    break

    def _guardrail_ads_and_graph(self, state: PlannerState, rec: ToolCallRecorder) -> None:
        req, bundle = state.request, state.bundle
        if req.has_product_identifiers and not state.attempted.get("search_illegal_ad_cases"):
            names = list(req.product_candidates)
            companies = list(req.company_candidates)
            best = bundle.product_match.best_match
            if best and bundle.product_match.status == ProductVerificationStatus.VERIFIED:
                names.append(best.product.product_name_zh)
                if best.product.company_name:
                    companies.append(best.product.company_name)
            if names or companies:
                self._guardrail_call(state, rec, "search_illegal_ad_cases",
                                     IllegalAdArgs(product_names=names, company_names=companies))
        # 圖譜證據：只保留已確認產品或成分層級的關係
        pid = self._resolved_product_id(bundle)
        bundle.graph_evidence = [
            e for e in state.graph_evidence.values()
            if e.query_type == GraphQueryType.INGREDIENT_APPROVED_FUNCTIONS or e.subject_id == pid
        ]

    # ================================================================== 結構化 claim policy

    def _claim_policy_stage(self, state: PlannerState, rec: ToolCallRecorder) -> None:
        """所有命題（被說出的宣稱與使用者提問的命題）依固定 evidence priority 查結構化 claim-policy（每個 request 最多一次）。

        1. 產品 VERIFIED：命題與該產品核准功效文字相同，或提問所問的功效就是核准功效類別 → 由核准功效決定。
        2. 其餘命題：官方例示（文字相同）→ 例示所屬規則；疾病命題另查醫療效能禁止規則（法規來源）。
        3. 已確認產品的核准功效類別 → 保健功效目錄（官方定義，供法規依據明細）。
        結構化證據已足以決定的命題記入 policy_decided_claims（之後不查 RAG / 官方網站）；
        其中不需要語意比對的（核准原文 / 核准功效類別、明確醫療效能＋結構化醫療效能規則、沒有核准功效時的決定性例示）
        另記入 policy_final_claims。claim_role 不變：提問仍是 USER_QUESTION，產品風險不因此升級（Risk Engine 規則）。
        corpus 尚未套用（0003 marker 不存在）時略過，不算資料來源失敗。
        """
        req, bundle = state.request, state.bundle
        if state.claim_policy_checked or not req.claims:
            return
        state.claim_policy_checked = True
        propositions = list(enumerate(req.claims))
        verified = bundle.product_match.status == ProductVerificationStatus.VERIFIED
        approved = bundle.approved_claims if verified else []
        decided: set[int] = set()
        final: set[int] = set()
        pending = []
        function_questions = []  # 可用保健功效目錄 deterministic 對應的提問
        for i, c in propositions:
            if approved and not c.uses_absolute_language and exact_approved_match(c.claim_text, approved):
                decided.add(i)  # 1. product-specific approved claim 優先（不需要 corpus）
                final.add(i)
                continue
            plain_question = approved and not c.is_asserted and not is_disease_claim(c) and not c.uses_absolute_language
            if plain_question and match_approved_function(c, approved)[0] is not None:
                decided.add(i)  # 提問的 target 就是核准功效類別名稱（完全相同）
                final.add(i)
                continue
            if plain_question:
                function_questions.append((i, c))
            pending.append((i, c))  # 部分重疊 / 多個功效 / 對應不到 → 保守的 evidence path
        state.policy_decided_claims, state.policy_final_claims = set(decided), set(final)
        if state.claim_policy is None or not pending:
            return
        try:
            available = state.claim_policy.corpus_available()
        except Exception:  # noqa: BLE001 - 交給下面的 Tool 呼叫如實記錄失敗
            available = True
        if not available:
            bundle.planner_trace.append(PlannerStep(
                round=len(bundle.planner_trace) + 1, note="claim policy corpus 尚未套用（0003），略過結構化 policy"))
            return
        if function_questions:
            # 2a. 提問 → 保健功效目錄（問句中逐字出現的官方功效名稱）→ 該產品核准功效：唯一且一致才 deterministic 決定
            texts = {i: self._question_text_without_product(state, c) for i, c in function_questions}
            names = [e.function_name for e in approved if e.function_classified]
            names += [c.target_function for _, c in function_questions if c.target_function]  # target 是否為另一個官方功效
            self._guardrail_call(state, rec, "lookup_health_function", HealthFunctionArgs(
                function_names=list(dict.fromkeys(names)), question_texts=list(dict.fromkeys(texts.values()))))
            approved_names = {normalize_name(e.function_name) for e in approved if e.function_classified}
            catalog = {normalize_name(h.official_function_name) for h in state.health_functions.values()}
            for i, c in function_questions:
                mentioned = state.function_mentions.get(normalize_name(texts[i]), [])
                target = normalize_name(c.target_function or "")
                if (len(mentioned) == 1 and normalize_name(mentioned[0]) in approved_names
                        and not (target in catalog and target != normalize_name(mentioned[0]))):
                    state.question_functions[normalize_name(c.claim_text)] = mentioned[0]
                    decided.add(i)
                    final.add(i)
            pending = [(i, c) for i, c in pending if i not in final]
        if pending:
            self._guardrail_call(state, rec, "search_claim_policy_examples",
                                 ClaimTextsArgs(claim_texts=[c.claim_text for _, c in pending]))
        rule_ids = sorted({e.rule_id for e in state.policy_examples.values()})
        needs_medical = any(is_disease_claim(c) for _, c in pending)
        if rule_ids or needs_medical:
            self._guardrail_call(state, rec, "search_claim_policy_rules", ClaimPolicyRulesArgs(
                rule_ids=rule_ids,
                rule_categories=[MEDICAL_RULE_CATEGORY] if needs_medical else [],
                policy_effects=[EFFECT_PROHIBITED] if needs_medical else [],
            ))
        if needs_medical:
            state.medical_rule_ids = {
                r.rule_id for r in state.policy_rules.values()
                if r.rule_category == MEDICAL_RULE_CATEGORY and r.policy_effect == EFFECT_PROHIBITED
            }
        function_names = [e.function_name for e in approved if e.function_classified]
        function_names += [c.target_function for _, c in propositions if c.target_function]
        if function_names and not state.attempted.get("lookup_health_function"):
            self._guardrail_call(state, rec, "lookup_health_function",
                                 HealthFunctionArgs(function_names=list(dict.fromkeys(function_names)), question_texts=[]))
        for i, c in pending:
            ctx = self._policy_context(state, c)
            decisive = ctx.decisive_example()
            medical_rules = bool(ctx.decision_medical_rules())
            if decisive is not None and (not approved or decisive[0].example_type == MEDICAL_EXAMPLE_TYPE):
                decided.add(i)  # 例示所屬規則已決定（通常可使用例示不覆蓋產品核准功效）
                if not approved:
                    final.add(i)
            elif is_disease_claim(c) and medical_rules:
                decided.add(i)  # 疾病命題由規則決定，法規來源已由結構化規則提供
            if is_explicit_medical_claim(c) and medical_rules:
                decided.add(i)  # 明確治療 / 診斷 / 取代醫療：在核准功效比對之前就由規則決定
                final.add(i)
        state.policy_decided_claims = decided
        state.policy_final_claims = final
        bundle.planner_trace.append(PlannerStep(
            round=len(bundle.planner_trace) + 1,
            note=f"結構化 claim policy：{len(decided)}/{len(propositions)} 則命題已可由核准功效或規則決定"
                 f"（{len(final)} 則不需語意比對）"))

    @staticmethod
    def _question_text_without_product(state: PlannerState, claim) -> str:
        """問句去掉產品 / 品牌名稱（品名中的字不能被當成使用者問的功效）。"""
        text = normalize_name(claim.claim_text)
        best = state.bundle.product_match.best_match
        names = [best.product.product_name_zh] if best else []
        names += state.request.product_candidates + state.request.corrected_product_candidates + state.request.brand_candidates
        for name in sorted({normalize_name(n) for n in names if n}, key=len, reverse=True):
            if name:
                text = text.replace(name, "")
        return text

    @staticmethod
    def _policy_context(state: PlannerState, claim) -> StructuredPolicyContext:
        norm = normalize_name(claim.claim_text)
        examples = [e for e in state.policy_examples.values()
                    if norm and any(normalize_name(t) == norm for t in e.matched_claim_texts)]
        return StructuredPolicyContext(
            example_matches=sorted(examples, key=lambda e: e.example_id),
            rules=dict(state.policy_rules),
            medical_rules=[state.policy_rules[r] for r in sorted(state.medical_rule_ids) if r in state.policy_rules],
            question_function=state.question_functions.get(norm),
        )

    @staticmethod
    def _attach_claim_policy_evidence(state: PlannerState) -> None:
        """查到的結構化 policy 證據放進 EvidenceBundle（evidence_use 原樣保留；superseded 不會被查出）。"""
        bundle = state.bundle
        bundle.claim_policy_rules = sorted(state.policy_rules.values(), key=lambda e: e.rule_id)
        bundle.claim_policy_examples = sorted(state.policy_examples.values(), key=lambda e: e.example_id)
        bundle.health_functions = sorted(state.health_functions.values(), key=lambda e: e.function_id)

    # ================================================================== 官方網站

    @classmethod
    def _internal_evidence_insufficient(cls, request: NormalizedUserRequest, bundle: EvidenceBundle, links,
                                        policy_decided: set[int] = frozenset()) -> bool:
        product_insufficient = request.has_product_identifiers and bundle.product_match.status in (
            ProductVerificationStatus.NOT_FOUND, ProductVerificationStatus.PARTIAL_MATCH,
        )
        linked = {link.claim_index for link in links} | set(policy_decided)
        claims_insufficient = any(i not in linked for i in range(len(request.claims)))
        if request.url_sources and UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE not in request.url_source_roles:
            # URL 輸入（來源角色已知）：只有證據衝突或命題超出內部證據範圍才查官方網站
            claims_insufficient = cls._url_claims_need_official_web(request, links, policy_decided)[0]
        general_insufficient = (
            request.needs_official_web_search
            and not request.claims
            and not bundle.rag_evidence
            and bundle.product_match.status != ProductVerificationStatus.VERIFIED
        )
        return product_insufficient or claims_insufficient or general_insufficient

    def _run_official_web(self, state: PlannerState, rec: ToolCallRecorder) -> None:
        """程式決定「是否」查官方網站；planner 只決定查詢語句，且最多一次。"""
        if state.attempted.get("search_tw_official_web", 0) >= MAX_WEB_SEARCHES:
            return
        req = state.request
        ran = False
        if self._llm is not None and self._enable_llm_planner:
            missing = {
                "product_status": state.bundle.product_match.status.value,
                "claims_without_official_evidence": [c.claim_text for c in req.claims],
            }
            ran = self._run_planner(
                state, rec, OFFICIAL_WEB_TOOLS, WEB_PLANNER_INSTRUCTIONS,
                json.dumps({"request": json.loads(self._planner_input(req)), "missing": missing}, ensure_ascii=False),
                max_rounds=1, tool_choice="required", max_total_calls=MAX_WEB_SEARCHES,
            ) and state.attempted.get("search_tw_official_web", 0) > 0
        if not ran:
            parts = [req.question or req.normalized_text]
            if req.product_candidates:
                parts.append("產品：" + "、".join(req.product_candidates))
            if req.claims:
                parts.append("命題：" + "；".join(c.claim_text for c in req.claims[:MAX_CLAIMS_FOR_RAG]))
            self._guardrail_call(state, rec, "search_tw_official_web", QueryArgs(query="\n".join(parts)))

    # ================================================================== 證據評估 + Relevance Gate

    def _established_references(self, bundle: EvidenceBundle) -> list[dict[str, str]]:
        refs = [
            {"evidence_id": e.evidence_id, "kind": "該產品官方核准的保健功效", "text": f"{e.function_name}：{e.claim_text}"}
            for e in bundle.approved_claims
        ]
        for e in bundle.graph_evidence:
            if e.relation == "PRODUCTS_WITH_INGREDIENT_HAVE_APPROVED_CLAIM":
                refs.append({
                    "evidence_id": e.evidence_id,
                    "kind": "含此成分的健康食品曾獲核准的功效（不是特定產品的核准內容）",
                    "text": f"成分「{e.subject_name}」：{e.object_name}（{e.properties.get('product_count')} 項產品）",
                })
        for e in bundle.rag_evidence:
            kind = f"官方文件：{e.title}" if e.decision_eligible else f"官方文件（僅供脈絡，不可單獨作為結論）：{e.title}"
            refs.append({"evidence_id": e.evidence_id, "kind": kind, "text": e.content[:EVIDENCE_TEXT_CHARS]})
        for e in bundle.official_web_evidence:
            refs.append({"evidence_id": e.evidence_id, "kind": f"官方網站：{e.title}", "text": e.snippet[:EVIDENCE_TEXT_CHARS]})
        return refs

    def _assess_and_gate(self, state: PlannerState, rec: ToolCallRecorder) -> list[_ClaimLink]:
        """LLM 評估相關性與命題關係；固定門檻決定哪些證據能進入 EvidenceBundle。"""
        req, bundle = state.request, state.bundle
        candidates: dict[str, Any] = {
            eid: e for eid, e in {**state.candidate_rag, **state.candidate_web}.items()
            if eid not in state.assessed_evidence_ids  # 已評估過的證據不重複評估
        }
        state.candidate_rag, state.candidate_web = {}, {}
        established = self._established_references(bundle)
        if not candidates and not (req.claims and established):
            return []
        established_key = frozenset(r["evidence_id"] for r in established)
        if not candidates and state.last_assessment_refs == established_key:
            # 沒有新增 material evidence：沿用上一次的評估結果，不重複呼叫 assess_evidence
            return list(state.last_assessment_links)

        assessment: _EvidenceAssessment | None = None
        if self._llm is not None:
            best = bundle.product_match.best_match
            payload = {
                "request": {
                    "question": req.question or req.normalized_text,
                    "verified_product": best.product.product_name_zh
                    if best and bundle.product_match.status == ProductVerificationStatus.VERIFIED else None,
                    "claims": [{"claim_index": i, "claim_text": c.claim_text, "origin": c.origin.value}
                               for i, c in enumerate(req.claims)],
                },
                "candidates_to_assess": [
                    {"evidence_id": eid, "kind": "官方文件" if eid.startswith("rag:") else "官方網站",
                     "title": e.title, "text": (getattr(e, "content", None) or getattr(e, "snippet", ""))[:EVIDENCE_TEXT_CHARS],
                     **({"evidence_use": e.policy_metadata.evidence_use.value}
                        if getattr(e, "policy_metadata", None) is not None else {})}
                    for eid, e in candidates.items()
                ],
                "established_references": established,
            }
            result = rec.call(
                ASSESSMENT_STEP, {"candidates": len(candidates), "references": len(established)},
                lambda: self._llm.parse_structured(
                    instructions=EVIDENCE_ASSESSMENT_INSTRUCTIONS,
                    input_text=json.dumps(payload, ensure_ascii=False),
                    output_model=_EvidenceAssessment,
                ),
            )
            state.assessment_count += 1
            if result.ok:
                assessment = result.data
        else:
            rec.call(ASSESSMENT_STEP, {}, _openai_not_configured)
        state.assessed_evidence_ids.update(candidates)

        relevance = {r.evidence_id: r for r in (assessment.relevance if assessment else [])}
        for eid, evidence in candidates.items():
            item = relevance.get(eid)
            rel = None if item is None else EvidenceRelevance(
                relevance_score=min(1.0, max(0.0, item.relevance_score)),
                supports_claim=item.supports_claim,
                evidence_scope=item.evidence_scope,
                rationale=item.rationale,
                assessed_by="llm",
            )
            passed, reason = relevance_gate(rel)
            if passed:
                evidence.relevance = rel
                target = bundle.rag_evidence if eid.startswith("rag:") else bundle.official_web_evidence
                target.append(evidence)
            else:
                bundle.rejected_evidence.append(RejectedEvidence(
                    evidence_id=eid, evidence_kind="rag" if eid.startswith("rag:") else "official_web",
                    title=evidence.title, source=evidence.source, relevance=rel, reason=reason,
                ))

        accepted_ids = {r["evidence_id"] for r in self._established_references(bundle)}
        links = [
            link for link in (assessment.claim_links if assessment else [])
            if link.evidence_id in accepted_ids
            and 0 <= link.claim_index < len(req.claims)
            and link.relation != ClaimEvidenceRelation.UNRELATED
        ]
        state.last_assessment_refs = frozenset(r["evidence_id"] for r in self._established_references(bundle))
        state.last_assessment_links = list(links)
        return links

    def _assess_claims(self, request: NormalizedUserRequest, bundle: EvidenceBundle, links, rec,
                       policy_contexts: list[StructuredPolicyContext] | None = None) -> None:
        if not request.claims:
            bundle.claim_assessment = []
            return
        approved_ids = {e.evidence_id for e in bundle.approved_claims}
        # CONTEXT_ONLY / CORROBORATION_REQUIRED 的 corpus chunk 只提供脈絡：不能成為 final legal evidence
        decision_rag = [e for e in bundle.rag_evidence if e.decision_eligible]
        official_ids = (
            approved_ids
            | {e.evidence_id for e in decision_rag if e.source.is_official}
            | {e.evidence_id for e in bundle.official_web_evidence}
            | {e.evidence_id for e in bundle.graph_evidence if e.source.is_official}
        )
        source_records = [r for r in rec.records if r.tool_name not in (PLANNER_STEP, ASSESSMENT_STEP)]
        any_ok = any(r.ok for r in source_records) or not source_records
        # 語意比對步驟必須成功完成，「沒有比對結果」才能解讀為「不在核准範圍」
        comparison_completed = any(r.ok for r in rec.records if r.tool_name == ASSESSMENT_STEP)
        regulation_sources = {
            e.evidence_id: e.source for e in decision_rag
            if e.source.is_official and (
                (e.relevance is not None and e.relevance.evidence_scope == EvidenceScope.CLAIM_REGULATION)
                or e.document_type == "regulation")
        }
        match = bundle.product_match
        product_unresolved = match.status == ProductVerificationStatus.PARTIAL_MATCH or match.is_ambiguous
        bundle.claim_assessment = [
            assess_claim(
                claim_index=i,
                claim=claim,
                links=[
                    ClaimEvidenceLink(evidence_id=l.evidence_id, relation=l.relation, rationale=l.rationale)
                    for l in links
                    if l.claim_index == i
                ],
                approved_claim_evidence_ids=approved_ids,
                official_reference_ids=official_ids,
                any_source_succeeded=any_ok,
                approved_claims=bundle.approved_claims,
                regulation_sources=regulation_sources,
                product_unresolved=product_unresolved,
                comparison_completed=comparison_completed,
                structured_policy=policy_contexts[i] if policy_contexts else None,
            )
            for i, claim in enumerate(request.claims)
        ]

    # ================================================================== 摘要

    @staticmethod
    def _source_errors(rec: ToolCallRecorder) -> list[ToolError]:
        """資料來源與證據評估的失敗都要如實回報；planner 失敗由 guardrail 補做，不算資料缺漏。"""
        return [r.error for r in rec.records if r.error is not None and r.tool_name != PLANNER_STEP]

    @staticmethod
    def _conflicts(bundle: EvidenceBundle) -> list[str]:
        conflicts: list[str] = []
        if bundle.product_match.status == ProductVerificationStatus.CONFLICT:
            conflicts.extend(bundle.product_match.notes)
        for ca in bundle.claim_assessment:
            if ca.status == ClaimAssessmentStatus.CONFLICT_WITH_OFFICIAL:
                conflicts.append(f"「{ca.claim_text}」與官方資料矛盾")
        return conflicts

    @staticmethod
    def _missing_information(request: NormalizedUserRequest, bundle: EvidenceBundle) -> list[str]:
        missing: list[str] = []
        if request.missing_product_context:
            missing.append("missing product context：無法從本次訊息或對話前文確認是哪一項產品")
        if bundle.product_match.status == ProductVerificationStatus.NOT_FOUND:
            missing.append(
                "官方產品紀錄查詢失敗" if not bundle.product_match.lookup_completed else "目前資料中查無此產品的官方紀錄"
            )
        for ca in bundle.claim_assessment:
            if not ca.evidence_links:
                missing.append(f"「{ca.claim_text}」缺少可對照的官方資料")
        if request.foreign_country_codes:
            missing.append("目前只查證台灣官方資料，無法比對外國產品")
        for err in bundle.errors:
            missing.append(f"資料來源 {err.source} 無法使用（{err.code.value}）")
        return missing


# 被說出來的宣稱要查的是「這類宣稱能不能這樣說」的規範文字（一般性模板，不含任何特定產品或字詞）；
# 直接用宣稱原文查詢時與法規文件的相似度通常低於門檻
CLAIM_POLICY_RAG_TEMPLATE = "健康食品標示或廣告宣稱是否涉及醫療效能或超過許可範圍：{claim}"


def rag_query_for_claim(claim) -> str:
    return CLAIM_POLICY_RAG_TEMPLATE.format(claim=claim.claim_text) if claim.is_asserted else claim.claim_text


# ================================================================== stage timing（debug / log 用）

_STAGE_OF_TOOL = {
    "lookup_product": "product_resolution_ms",
    "lookup_permit": "product_resolution_ms",
    "lookup_company": "product_resolution_ms",
    "resolve_entity_alias": "product_resolution_ms",
    "search_rag": "rag_ms",
    "search_claim_policy_rules": "claim_policy_ms",
    "search_claim_policy_examples": "claim_policy_ms",
    "lookup_health_function": "claim_policy_ms",
    PLANNER_STEP: "planner_ms",
    ASSESSMENT_STEP: "assessment_ms",
    "search_tw_official_web": "official_web_ms",
}
_INPUT_STAGE_OF_TOOL = {"fetch_url_content": "fetch_ms", "analyze_image": "vision_ms",
                        "transcribe_audio": "transcription_ms", "reinspect_image_identity": "vision_ms"}


def stage_timings_ms(records: list[ToolCallRecord]) -> dict[str, int]:
    """把 Agent B 的工具紀錄依階段加總（DB / KG 查詢歸在 db_kg_ms）。"""
    out = {"product_resolution_ms": 0, "db_kg_ms": 0, "rag_ms": 0, "planner_ms": 0,
           "assessment_ms": 0, "official_web_ms": 0, "claim_policy_ms": 0}
    for r in records:
        if (stage := _INPUT_STAGE_OF_TOOL.get(r.tool_name)) is not None:
            out[stage] = out.get(stage, 0) + r.duration_ms
        else:
            out[_STAGE_OF_TOOL.get(r.tool_name, "db_kg_ms")] += r.duration_ms
    return out
