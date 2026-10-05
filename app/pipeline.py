"""主流程：Input → Agent A（+ Input Tools）→ Agent B（+ Evidence Tools）→ EvidenceBundle
→ Risk Engine → Agent C → ResponsePayload（唯一 primary response）。

debug endpoint 與 LINE webhook 共用同一條 pipeline。
"""

import logging
import time
import uuid
from typing import Protocol

from app.agents.intake_agent import IntakeAgent, needs_identity_reinspection
from app.agents.reporter_agent import ReporterAgent, error_payload
from app.agents.verification_agent import VerificationAgent, stage_timings_ms
from app.risk.assessment_rules import scope_foreign_signal
from app.risk.risk_engine import RiskEngine
from app.schemas.analysis import AnalysisResult
from app.schemas.enums import EntityType, ErrorCode, Intent, ProductReferenceStatus, ProductVerificationStatus
from app.schemas.evidence import ProductRecord, ToolError
from app.schemas.user_request import ContextEntity, ConversationTurn, NormalizedUserRequest, RawUserInput
from app.services.bare_name_routing import route_bare_name
from app.services.context_binding import bind_confirmed_product
from app.services.errors import ServiceError

logger = logging.getLogger("nutri_safe_ai.pipeline")

UNDERSTANDING_FAILED_TEXT = "抱歉，系統目前暫時無法理解這則訊息，請稍後再按「重新查詢」。"
INTERNAL_FAILED_TEXT = "抱歉，系統目前發生問題，這次無法完成查證，請稍後再按「重新查詢」。"


def failed_result(request_id: str, summary: str, **fields) -> AnalysisResult:
    payload = error_payload(request_id, summary)
    return AnalysisResult(request_id=request_id, response=payload, final_answer=summary, **fields)


class AnalysisLogWriter(Protocol):
    def write(self, result: AnalysisResult, channel: str) -> None: ...


class ConversationStore(Protocol):
    def get(self, conversation_id: str): ...

    def append(self, conversation_id: str, turn: ConversationTurn) -> None: ...


def is_multi_product_comparison(req: NormalizedUserRequest | None, bundle) -> bool:
    """交互作用題中同時有兩個以上產品對象（已確認或待確認）：沒有單一「它」可指，不可從中挑一個寫進 task context。

    以 Researcher 的結構化對象計數（已確認的產品 / 待確認的產品）；沒有研究結果時用 Controller 的對象種類。
    """
    if req is None or req.intent != Intent.INTERACTION_INQUIRY:
        return False
    research = getattr(bundle, "interaction_research", None)
    if research is not None:
        from app.schemas.interaction import SubjectResolutionStatus

        return sum(1 for s in research.subjects if s.product_confirmed
                   or s.status == SubjectResolutionStatus.PRODUCT_NEEDS_CONFIRMATION) >= 2
    from app.schemas.interaction import InteractionSubjectKind

    return sum(1 for s in req.interaction_subjects if s.kind == InteractionSubjectKind.PRODUCT) >= 2


def context_entities_for_turn(result: AnalysisResult) -> list[ContextEntity]:
    """本回合可供之後「它 / 這個」指回的實體：已確認的產品優先（帶 canonical ID）。"""
    req, bundle = result.normalized_request, result.evidence
    if req is None:
        return []
    entities: list[ContextEntity] = []
    if is_multi_product_comparison(req, bundle):  # 兩個以上產品的比較：不留任何產品實體（只留成分 / 品牌 / 公司）
        entities += [ContextEntity(entity_type=EntityType.INGREDIENT, name=n) for n in req.ingredient_candidates]
        return entities
    best = bundle.product_match.best_match if bundle else None
    if best and bundle.product_match.status == ProductVerificationStatus.VERIFIED:
        entities.append(ContextEntity(entity_type=EntityType.PRODUCT, name=best.product.product_name_zh,
                                      entity_id=best.product.product_id))
    else:
        # 已確認產品時，使用者的原始寫法只是同一產品的另一種叫法；另存成無 ID 的實體會讓下一題
        # 「它」被解析成原始寫法而重新比對（可能重新變成 ambiguous），因此只在未確認時保留
        for name in req.product_candidates:
            if all(e.name != name for e in entities):
                entities.append(ContextEntity(entity_type=EntityType.PRODUCT, name=name))
    entities += [ContextEntity(entity_type=EntityType.BRAND, name=n) for n in req.brand_candidates]
    entities += [ContextEntity(entity_type=EntityType.COMPANY, name=n) for n in req.company_candidates]
    entities += [ContextEntity(entity_type=EntityType.INGREDIENT, name=n) for n in req.ingredient_candidates]
    return entities


def request_for_selected_product(
    original: NormalizedUserRequest, product: ProductRecord, request_id: str
) -> NormalizedUserRequest:
    """使用者從確認卡選定產品後的 request：沿用原題的 intent / 問題 / 命題，只把產品識別換成所選的官方紀錄。

    以所選產品的許可證字號與官方品名作為識別，交給 Agent B 既有的 deterministic resolver
    （PERMIT_NUMBER → VERIFIED → 資料足夠時走 structured fast path）；不重新做語意理解。
    """
    if original.interaction_subjects:
        return request_for_selected_interaction_product(original, product, request_id)
    return original.model_copy(deep=True, update={
        "request_id": request_id,
        "product_candidates": [product.product_name_zh],
        "corrected_product_candidates": [],
        "brand_candidates": [],
        "company_candidates": [],
        "permit_number_candidates": [product.permit_number] if product.permit_number else [],
        "product_reference_status": ProductReferenceStatus.EXPLICIT,
        "context_entities_used": [],
        "needs_product_lookup": True,
        "input_tools_called": [],
        "adapter_notes": original.adapter_notes + ["使用者從確認卡選定產品"],
    })


def request_for_selected_interaction_product(
    original: NormalizedUserRequest, product: ProductRecord, request_id: str
) -> NormalizedUserRequest:
    """Interaction inquiry：使用者從確認卡選定產品後，只固定「那一個」產品對象（以所選官方紀錄的字號），其餘對象不變；
    沿用原題（intent / 對象 / 問題型態），不重跑 Agent A。

    確認卡對應的是尚未確認的那個產品對象：在尚未固定的產品對象中，選與所選官方品名最相近者
    （候選本來就是由該對象的寫法比對出來的）；並列時取排序最前者。
    """
    from difflib import SequenceMatcher

    from app.normalization import normalize_name
    from app.schemas.interaction import InteractionSubjectKind

    subjects = [s.model_copy() for s in original.interaction_subjects]
    open_idx = [i for i, s in enumerate(subjects)
                if s.kind == InteractionSubjectKind.PRODUCT and not s.pinned_permit_number]
    if open_idx and product.permit_number:
        target = max(open_idx, key=lambda i: (
            SequenceMatcher(None, normalize_name(subjects[i].text), normalize_name(product.product_name_zh)).ratio(), -i))
        order = max([s.selection_order or 0 for s in subjects] + [0]) + 1
        subjects[target] = subjects[target].model_copy(update={
            "pinned_permit_number": product.permit_number, "pinned_product_name": product.product_name_zh,
            "selection_order": order})
    return original.model_copy(deep=True, update={
        "request_id": request_id, "interaction_subjects": subjects, "input_tools_called": [],
        "adapter_notes": original.adapter_notes + ["使用者從確認卡選定產品"],
    })


def request_for_package_claims(
    original: NormalizedUserRequest, product: ProductRecord | None, request_id: str
) -> NormalizedUserRequest:
    """「查驗包裝宣稱」：使用原 request 保存的 observed package claims（不重新跑 Vision / Agent A），
    產品以該題已確認的官方紀錄（字號＋官方品名）識別，進入 claim verification。"""
    from app.agents.intake_agent import _REGULATORY_CATEGORIES

    claims = [c.model_copy() for c in original.observed_package_claims]
    update = {
        "request_id": request_id,
        "intent": Intent.CLAIM_VERIFICATION,
        "claims": claims,
        "observed_package_claims": [],
        "image_identification": False,
        "claims_from_package": True,
        "needs_claim_check": True,
        "needs_rag": any(c.semantic_category in _REGULATORY_CATEGORIES for c in claims),
        "needs_official_web_search": False,
        "input_tools_called": [],
        "adapter_notes": original.adapter_notes + ["使用者要求查驗原圖片中的包裝宣稱"],
    }
    if product is not None:
        update.update({
            "product_candidates": [product.product_name_zh],
            "corrected_product_candidates": [],
            "brand_candidates": [],
            "company_candidates": [],
            "permit_number_candidates": [product.permit_number] if product.permit_number else [],
            "product_reference_status": ProductReferenceStatus.EXPLICIT,
            "needs_product_lookup": True,
        })
    return original.model_copy(deep=True, update=update)


class AnalysisPipeline:
    def __init__(
        self,
        intake: IntakeAgent,
        verifier: VerificationAgent,
        risk_engine: RiskEngine,
        reporter: ReporterAgent,
        log_writer: AnalysisLogWriter | None = None,
        conversation_store: ConversationStore | None = None,
    ):
        self._intake = intake
        self._verifier = verifier
        self._risk = risk_engine
        self._reporter = reporter
        self._log_writer = log_writer
        self._conversations = conversation_store

    @property
    def reporter(self) -> ReporterAgent:
        return self._reporter

    def run(
        self,
        raw: RawUserInput,
        channel: str = "debug_api",
        request_id: str | None = None,
        conversation_id: str | None = None,
    ) -> AnalysisResult:
        request_id = request_id or str(uuid.uuid4())
        start = time.perf_counter()
        if conversation_id and self._conversations is not None:
            raw = raw.model_copy(update={"context": self._conversations.get(conversation_id)})
        result = self._run(raw, request_id)
        result.input_type = raw.input_type.value
        if conversation_id and self._conversations is not None and result.normalized_request is not None:
            self._conversations.append(
                conversation_id, ConversationTurn(user_text=raw.text, entities=context_entities_for_turn(result))
            )
        result.duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "analysis_done",
            extra={
                "request_id": request_id,
                "channel": channel,
                "input_type": raw.input_type.value,
                "intent": result.normalized_request.intent.value if result.normalized_request else None,
                "evidence_count": result.evidence.evidence_count() if result.evidence else 0,
                "risk_result": result.risk.risk_level.value if result.risk else None,
                "error_code": ",".join(sorted({e.code.value for e in result.errors})) or None,
                "duration_ms": result.duration_ms,
                "planner_mode": result.evidence.planner_mode if result.evidence else None,
                **stage_timings_ms(result.tools_called),
            },
        )
        if self._log_writer is not None:
            try:
                self._log_writer.write(result, channel)
            except Exception as exc:  # noqa: BLE001 - 紀錄失敗不影響回覆
                logger.warning(
                    "analysis_log_write_failed",
                    extra={"request_id": request_id, "error_code": getattr(exc, "code", type(exc).__name__)},
                )
        return result

    def resume_with_request(
        self, request: NormalizedUserRequest, channel: str = "line", input_type: str | None = None
    ) -> AnalysisResult:
        """從已理解好的 NormalizedUserRequest 繼續（Agent B → Risk → Agent C），不重新呼叫 Agent A。"""
        start = time.perf_counter()
        result = self._evaluate(request, request.request_id)
        result.input_type = input_type or request.input_type.value
        result.duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info("analysis_done", extra={
            "request_id": request.request_id, "channel": channel, "input_type": result.input_type,
            "intent": request.intent.value, "stage": "resumed_with_selected_product",
            "evidence_count": result.evidence.evidence_count() if result.evidence else 0,
            "risk_result": result.risk.risk_level.value if result.risk else None,
            "duration_ms": result.duration_ms,
        })
        if self._log_writer is not None:
            try:
                self._log_writer.write(result, channel)
            except Exception as exc:  # noqa: BLE001 - 紀錄失敗不影響回覆
                logger.warning("analysis_log_write_failed", extra={
                    "request_id": request.request_id, "error_code": getattr(exc, "code", type(exc).__name__)})
        return result

    def _run(self, raw: RawUserInput, request_id: str) -> AnalysisResult:
        try:
            request = self._intake.understand(request_id, raw)
        except ServiceError as exc:
            return failed_result(request_id, UNDERSTANDING_FAILED_TEXT, errors=[exc.to_tool_error()])
        # task context 已確認的產品：產品相關 follow-up 直接以 canonical 產品（字號）查證，不重新比對基本品名
        # 整則訊息只有一個成分名稱時，先判成獨立成分詢問（不沿用前一題產品）；名稱同時是官方產品名稱 → collision
        request = self._verifier.route_bare_name(request)
        request = bind_confirmed_product(request, raw.context.confirmed_product)
        return self._evaluate(request, request_id, raw)

    def _evaluate(self, request: NormalizedUserRequest, request_id: str,
                  raw: RawUserInput | None = None) -> AnalysisResult:
        try:
            bundle = self._verifier.verify(request)
            has_image = raw is not None and any(m.kind == "image" for m in raw.media)
            if needs_identity_reinspection(request, bundle, has_image):
                # 圖片產品身份仍無法確認：Agent A 最多一次局部重讀字號 / 品名，讀到新線索才重新查證
                reread = self._intake.reinspect_identity(request, raw)
                if reread is not None:
                    found_new = (reread.permit_number_candidates != request.permit_number_candidates
                                 or reread.product_candidates != request.product_candidates)
                    request = reread
                    if found_new:
                        bundle = self._verifier.verify(request)
            request = scope_foreign_signal(request, bundle.product_match)  # 台灣官方紀錄已確認者不算外國產品
            risk = self._risk.assess(request, bundle)
            payload = self._reporter.compose(request, bundle, risk)
        except Exception:  # noqa: BLE001 - 不讓 pipeline crash，回傳結構化錯誤
            logger.exception("pipeline_internal_error", extra={"request_id": request_id})
            return failed_result(
                request_id, INTERNAL_FAILED_TEXT, normalized_request=request,
                errors=[ToolError(code=ErrorCode.INTERNAL_ERROR, message="pipeline 內部錯誤", source="pipeline")],
            )

        return AnalysisResult(
            request_id=request_id,
            normalized_request=request,
            tools_called=request.input_tools_called + bundle.tools_called,
            product_verification=bundle.product_match,
            evidence=bundle,
            claim_assessment=bundle.claim_assessment,
            risk=risk,
            response=payload,
            final_answer=payload.plain_text(),
            errors=bundle.errors,
        )
