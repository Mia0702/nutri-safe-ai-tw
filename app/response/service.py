"""Adaptive response orchestration（LINE webhook 之後直接使用；本身不處理 LINE 簽章或 API 呼叫）。

保證
* 每個 user request 只產生「一則」primary Flex Message（respond() 回傳一則訊息，由呼叫端以 reply API 送出）。
* 不自動補發第二、第三則訊息；詳細內容只在使用者按按鈕時產生（handle_action()）。
* pipeline 超過時限：立即回一張 ERROR card（附「重新查詢」）；背景晚完成的結果直接丟棄，
  不會之後再送出，也不會覆蓋該 request_id 已保存的結果。
* 按鈕一律依 postback 中的 request_id 取回那一題的資料，並確認是同一位使用者。
* 產品確認卡的「選擇」：candidate_id 必須是那一題當初產生的候選；沿用那一題已理解的問題直接繼續
  （不重新跑 Agent A），結果必須確實是所選產品才回答，否則不猜、回提示。
* 有 TaskContextStore 時（LINE）：開始時取 task context 快照帶入 Agent A；只有「準時完成」且
  session 未被重設的結果才寫回 context（逾時的晚到結果、按過「開始新問題」之前的結果都不會寫入）。
* Pending clarification：上一題在問「是不是這一項」時，「是 / 不是」這類短回覆以 deterministic 規則判定
  （app/services/clarification.py）：CONFIRM 以該候選繼續上一題（不重跑 Agent A / planner），只代表使用者確認目標，
  原本的衝突保留；REJECT 清掉候選並請使用者補產品資訊；其他訊息走正常流程。
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from app.agents.reporter_agent import ReporterAgent, error_payload
from app.pipeline import (
    AnalysisPipeline,
    context_entities_for_turn,
    is_multi_product_comparison,
    request_for_package_claims,
    request_for_selected_product,
)
from app.response.actions import parse_postback_data, reachable_actions
from app.response.line_renderer import AdaptiveLineRenderer
from app.response.query_context import image_query_label, is_image_input
from app.response.sources import source_links
from app.response.store import RequestRecord, RequestStore
from app.schemas.enums import (
    DialogueAct,
    ProductConfirmationSource,
    ProductReferenceStatus,
    ProductVerificationStatus,
    ResponseAction,
)
from app.schemas.user_request import ConfirmedProduct, PendingClarification, RawUserInput
from app.services.clarification import (
    classify_dialogue_act,
    is_plain_text,
    pending_clarification_for,
    request_for_confirmed_target,
)
from app.services.context_binding import is_bound_from_context
from app.services.context_store import TaskContext, TaskContextStore

logger = logging.getLogger("nutri_safe_ai.response")

DEFAULT_TIMEOUT_SECONDS = 45.0
TIMEOUT_SUMMARY = "這次查詢花太久時間，系統先停止了。請按「重新查詢」再試一次。"
INVALID_ACTION_TEXT = "無法辨識這個按鈕，請重新傳送你的問題。"
EXPIRED_ACTION_TEXT = "這個查詢已經過期或找不到了，請重新傳送你的問題。"
UNAVAILABLE_ACTION_TEXT = "這一題沒有這項內容。"
DETAIL_FAILED_TEXT = "這項內容暫時無法產生，請稍後再按一次。"
INVALID_CHOICE_TEXT = "這個選項不屬於那一題的候選產品，請重新傳送你的問題。"
CHOICE_NOT_CONFIRMED_TEXT = "所選產品這次無法確認，請改傳完整品名或許可證字號。"
REJECTED_CANDIDATE_TEXT = "好的，那就不是這一項。請傳產品完整名稱、許可證字號，或清楚的包裝正面照片，我再幫你查。"


def confirmed_product_update(result, request_id: str, source: ProductConfirmationSource,
                             ) -> tuple[ConfirmedProduct | None, bool]:
    """這一題對 task context 的 confirmed product 有什麼影響：(新的 confirmed product, 是否清除舊的)。

    * 該題產品 VERIFIED → 成為新的 confirmed product（使用者選定 / resolver 確認）。
    * 使用者在本次訊息明確提了產品但沒有確認（多候選 / 查無）→ 清除舊的（不可再把「它」當成舊產品）。
    * 其餘（例如一般問題）→ 不變。
    """
    req, bundle = result.normalized_request, result.evidence
    if req is None or bundle is None:
        return None, False
    if is_multi_product_comparison(req, bundle):
        return None, True  # 兩個以上產品的比較：不從中挑一個當「已確認產品」，也不沿用更早的
    if is_bound_from_context(req):
        return None, False  # 沿用 context 中已確認的產品：保留原本的確認來源與時間
    match = bundle.product_match
    if match.status == ProductVerificationStatus.VERIFIED and match.best_match is not None:
        p = match.best_match.product
        return ConfirmedProduct(
            product_id=p.product_id, permit_id=p.permit_id, permit_number=p.permit_number,
            official_product_name=p.product_name_zh, confirmation_source=source,
            confirmed_at=datetime.now(timezone.utc), confirmed_request_id=request_id,
        ), False
    explicit = req.product_reference_status == ProductReferenceStatus.EXPLICIT and req.has_product_identifiers
    return None, explicit


class AdaptiveResponseService:
    def __init__(
        self,
        pipeline: AnalysisPipeline,
        reporter: ReporterAgent,
        store: RequestStore,
        renderer: AdaptiveLineRenderer | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        executor: ThreadPoolExecutor | None = None,
        context_store: TaskContextStore | None = None,
    ):
        self._pipeline = pipeline
        self._reporter = reporter
        self._store = store
        self._renderer = renderer or AdaptiveLineRenderer()
        self._timeout = timeout_seconds
        self._executor = executor or ThreadPoolExecutor(max_workers=8, thread_name_prefix="analysis")
        self._contexts = context_store

    def _await(self, future: Future, request_id: str, stage: str):
        try:
            return future.result(timeout=self._timeout)
        except FutureTimeout:
            future.add_done_callback(lambda _: logger.info(
                "late_result_discarded", extra={"request_id": request_id, "stage": stage}))
            return None

    def respond(
        self, raw: RawUserInput, owner_id: str | None, conversation_id: str | None = None, channel: str = "line",
        request_id: str | None = None, keep_raw_context: bool = False, received_at: datetime | None = None,
    ) -> dict[str, Any]:
        """跑完整條 pipeline，回傳唯一一則 primary Flex Message。

        received_at：使用者送出這則輸入的時間（LINE event timestamp）；圖片的 query label 以它與這一題的結果產生。
        """
        request_id = request_id or str(uuid.uuid4())
        received_at = received_at or datetime.now(timezone.utc)
        image = is_image_input(raw)
        session_id = None
        if self._contexts is not None and owner_id:
            task = self._contexts.snapshot(owner_id)
            session_id = task.session_id
            if task.pending_clarification is not None and not keep_raw_context and is_plain_text(raw):
                handled = self._answer_pending(task, raw, owner_id, channel, request_id, received_at)
                if handled is not None:
                    return handled
            if not keep_raw_context:  # 「重新查詢」沿用那一題當時的 context
                raw = raw.model_copy(update={"context": task.conversation_context()})
            conversation_id = None  # context 由 task store 管理，pipeline 不再自行讀寫
        started = time.monotonic()
        logger.info("line_stage", extra={"request_id": request_id, "stage": "pipeline_started",
                                         "input_type": raw.input_type.value})
        future = self._executor.submit(self._pipeline.run, raw, channel, request_id, conversation_id)
        result = self._await(future, request_id, "pipeline")
        logger.info("line_stage", extra={
            "request_id": request_id, "stage": "pipeline_done" if result is not None else "pipeline_timeout",
            "error_code": None if result is not None else "TIMEOUT",
            "duration_ms": int((time.monotonic() - started) * 1000)})
        label = image_query_label(received_at, result) if image else None
        if result is None:
            payload = error_payload(request_id, TIMEOUT_SUMMARY)
            record = RequestRecord(request_id=request_id, owner_id=owner_id, raw_input=raw, response=payload,
                                   query_label=label, received_at=received_at)
        else:
            payload = result.response
            record = RequestRecord(
                request_id=request_id, owner_id=owner_id, raw_input=raw, response=payload,
                normalized_request=result.normalized_request, evidence=result.evidence, risk=result.risk,
                query_label=label, received_at=received_at,
            )
            if session_id is not None and result.normalized_request is not None:
                req = result.normalized_request
                confirmed, clear = confirmed_product_update(result, request_id, ProductConfirmationSource.RESOLVER_VERIFIED)
                self._contexts.record(owner_id, session_id, request_id, req.normalized_text or raw.text,
                                      req.intent, context_entities_for_turn(result),
                                      confirmed=confirmed, clear_confirmed=clear,
                                      pending=pending_clarification_for(result, request_id),
                                      clear_products=is_multi_product_comparison(req, result.evidence))
        self._store.save(record)
        return self._renderer.render_primary(payload, query_label=label)

    def _answer_pending(self, task: TaskContext, raw: RawUserInput, owner_id: str, channel: str, request_id: str,
                        received_at: datetime) -> dict[str, Any] | None:
        """上一題在等「是不是這一項」：CONFIRM / REJECT 直接處理；NEW_ENTITY / OTHER 回傳 None 走正常流程。"""
        pending = task.pending_clarification
        act = classify_dialogue_act(raw.text, pending)
        logger.info("line_stage", extra={"request_id": request_id, "stage": "pending_clarification_act",
                                         "dialogue_act": act.value, "match_type": pending.clarification_type.value})
        if act == DialogueAct.REJECT:
            self._contexts.clear_pending(owner_id, task.session_id)
            return self._renderer.render_notice(REJECTED_CANDIDATE_TEXT)
        if act == DialogueAct.CONFIRM:
            out = self._resume_confirmed_target(pending, task.session_id, owner_id, channel, request_id)
            if out is not None:
                return out
        # NEW_ENTITY（新產品優先）/ 上一題已無法取回：清掉 pending，走正常流程；OTHER 由下一次 record 取代
        if act != DialogueAct.OTHER:
            self._contexts.clear_pending(owner_id, task.session_id)
        return None

    def _resume_confirmed_target(self, pending: PendingClarification, session_id: str, owner_id: str, channel: str,
                                 request_id: str) -> dict[str, Any] | None:
        """CONFIRM：以上一題保存的候選與已理解的問題繼續（Agent B → Risk → Agent C），不重跑 Agent A。"""
        record = self._store.get(pending.previous_request_id)
        if record is None or record.owner_id != owner_id or record.normalized_request is None or record.evidence is None:
            return None
        match = record.evidence.product_match
        products = {c.product.product_id: c.product
                    for c in match.candidates + ([match.best_match] if match.best_match else [])}
        product = products.get(pending.candidate_product_id)
        if product is None:
            return None
        request = request_for_confirmed_target(record.normalized_request, product, pending, request_id)
        started = time.monotonic()
        future = self._executor.submit(self._pipeline.resume_with_request, request, channel,
                                       record.raw_input.input_type.value)
        result = self._await(future, request_id, "confirm_pending_product")
        logger.info("line_stage", extra={
            "request_id": request_id,
            "stage": "confirm_pending_product_done" if result is not None else "confirm_pending_product_timeout",
            "duration_ms": int((time.monotonic() - started) * 1000)})
        if result is None:
            payload = error_payload(request_id, TIMEOUT_SUMMARY).model_copy(update={"available_actions": []})
            self._store.save(RequestRecord(request_id=request_id, owner_id=owner_id, raw_input=record.raw_input,
                                           response=payload, query_label=record.query_label,
                                           received_at=record.received_at))
            return self._renderer.render_primary(payload, query_label=record.query_label)
        self._store.save(RequestRecord(
            request_id=request_id, owner_id=owner_id, raw_input=record.raw_input, response=result.response,
            normalized_request=result.normalized_request, evidence=result.evidence, risk=result.risk,
            query_label=record.query_label, received_at=record.received_at,  # 沿用上一題（原圖片）的 context
        ))
        # 目標由使用者確認（不是 resolver 確認）：保留差異，後續「它」沿用時也會帶著差異說明
        confirmed = ConfirmedProduct(
            product_id=product.product_id, permit_id=product.permit_id, permit_number=product.permit_number,
            official_product_name=product.product_name_zh,
            confirmation_source=ProductConfirmationSource.USER_CONFIRMED_TARGET,
            confirmed_at=datetime.now(timezone.utc), confirmed_request_id=request_id, discrepancy=pending.discrepancy)
        multi = is_multi_product_comparison(result.normalized_request, result.evidence)
        self._contexts.record(owner_id, session_id, request_id, request.normalized_text, request.intent,
                              context_entities_for_turn(result), confirmed=None if multi else confirmed,
                              clear_products=multi)
        return self._renderer.render_primary(result.response, query_label=record.query_label)

    def select_product(self, record: RequestRecord, candidate_id: str, owner_id: str | None,
                       channel: str = "line", request_id: str | None = None) -> dict[str, Any]:
        """確認卡選擇：驗證候選屬於那一題 → 沿用原題（intent / 問題）以所選產品繼續 → 設為目前產品。"""
        offered = {c.product_id for c in record.response.product_choices}
        candidates = {c.product.product_id: c.product
                      for c in (record.evidence.product_match.candidates if record.evidence else [])}
        product = candidates.get(candidate_id)
        if candidate_id not in offered or product is None or record.normalized_request is None:
            logger.info("line_stage", extra={"request_id": record.request_id, "stage": "select_product_rejected"})
            return self._renderer.render_notice(INVALID_CHOICE_TEXT, query_label=record.query_label)
        request_id = request_id or str(uuid.uuid4())
        request = request_for_selected_product(record.normalized_request, product, request_id)
        session_id = self._contexts.snapshot(owner_id).session_id if self._contexts is not None and owner_id else None
        started = time.monotonic()
        future = self._executor.submit(self._pipeline.resume_with_request, request, channel, record.raw_input.input_type.value)
        result = self._await(future, request_id, "select_product")
        logger.info("line_stage", extra={
            "request_id": request_id, "stage": "select_product_done" if result is not None else "select_product_timeout",
            "duration_ms": int((time.monotonic() - started) * 1000)})
        if result is None:
            payload = error_payload(request_id, TIMEOUT_SUMMARY).model_copy(update={"available_actions": []})
            self._store.save(RequestRecord(request_id=request_id, owner_id=owner_id, raw_input=record.raw_input,
                                           response=payload, query_label=record.query_label,
                                           received_at=record.received_at))
            return self._renderer.render_primary(payload, query_label=record.query_label)
        match = result.evidence.product_match if result.evidence else None
        if (match is None or match.status != ProductVerificationStatus.VERIFIED or match.best_match is None
                or match.best_match.product.product_id != candidate_id):
            # 解析結果不是使用者選的那一項：不回答別的產品
            return self._renderer.render_notice(CHOICE_NOT_CONFIRMED_TEXT, query_label=record.query_label)
        self._store.save(RequestRecord(
            request_id=request_id, owner_id=owner_id, raw_input=record.raw_input, response=result.response,
            normalized_request=result.normalized_request, evidence=result.evidence, risk=result.risk,
            query_label=record.query_label, received_at=record.received_at,  # 沿用原題（原圖片）的 context
        ))
        if session_id is not None:  # 所選產品成為目前 task context 的已確認產品（文字 / 圖片 / 字號選擇皆同）
            confirmed, _ = confirmed_product_update(result, request_id, ProductConfirmationSource.USER_SELECTED)
            self._contexts.record(owner_id, session_id, request_id, request.normalized_text, request.intent,
                                  context_entities_for_turn(result), confirmed=confirmed,
                                  clear_products=is_multi_product_comparison(result.normalized_request, result.evidence))
        return self._renderer.render_primary(result.response, query_label=record.query_label)

    def check_package_claims(self, record: RequestRecord, owner_id: str | None, channel: str = "line",
                             request_id: str | None = None) -> dict[str, Any]:
        """「查驗包裝宣稱」：使用原 request 保存的 observed claims 與已確認產品（不重新跑 Vision），進入 claim verification。
        結果沿用原圖片的 query label。"""
        original = record.normalized_request
        if original is None or not original.observed_package_claims:
            return self._renderer.render_notice(UNAVAILABLE_ACTION_TEXT, query_label=record.query_label)
        match = record.evidence.product_match if record.evidence else None
        product = (match.best_match.product
                   if match is not None and match.status == ProductVerificationStatus.VERIFIED and match.best_match
                   else None)
        request_id = request_id or str(uuid.uuid4())
        request = request_for_package_claims(original, product, request_id)
        session_id = self._contexts.snapshot(owner_id).session_id if self._contexts is not None and owner_id else None
        started = time.monotonic()
        future = self._executor.submit(self._pipeline.resume_with_request, request, channel,
                                       record.raw_input.input_type.value)
        result = self._await(future, request_id, "check_package_claims")
        logger.info("line_stage", extra={
            "request_id": request_id,
            "stage": "check_package_claims_done" if result is not None else "check_package_claims_timeout",
            "duration_ms": int((time.monotonic() - started) * 1000)})
        if result is None:
            payload = error_payload(request_id, TIMEOUT_SUMMARY).model_copy(update={"available_actions": []})
            self._store.save(RequestRecord(request_id=request_id, owner_id=owner_id, raw_input=record.raw_input,
                                           response=payload, query_label=record.query_label,
                                           received_at=record.received_at))
            return self._renderer.render_primary(payload, query_label=record.query_label)
        self._store.save(RequestRecord(
            request_id=request_id, owner_id=owner_id, raw_input=record.raw_input, response=result.response,
            normalized_request=result.normalized_request, evidence=result.evidence, risk=result.risk,
            query_label=record.query_label, received_at=record.received_at,
        ))
        if session_id is not None:  # 同一項產品：已確認產品（來源 / 時間）維持不變
            self._contexts.record(owner_id, session_id, request_id, request.normalized_text, request.intent,
                                  context_entities_for_turn(result))
        return self._renderer.render_primary(result.response, query_label=record.query_label)

    def handle_action(self, postback_data: str, owner_id: str | None, channel: str = "line",
                      request_id: str | None = None) -> dict[str, Any]:
        """按鈕 postback：依 request_id 回傳那一題的 on-demand 內容（一則訊息）。"""
        act = parse_postback_data(postback_data)
        if act is None:
            return self._renderer.render_notice(INVALID_ACTION_TEXT)
        record = self._store.get(act.request_id)
        if record is None or record.owner_id != owner_id:  # 不洩漏他人的查詢，也不猜是哪一題
            return self._renderer.render_notice(EXPIRED_ACTION_TEXT)
        if act.action == ResponseAction.SELECT_PRODUCT:
            return self.select_product(record, act.candidate_id, owner_id, channel=channel, request_id=request_id)
        if act.action == ResponseAction.RETRY:  # 同一張圖片：沿用原本的收到時間
            return self.respond(record.raw_input, owner_id, channel=channel, keep_raw_context=True,
                                received_at=record.received_at)
        if act.action not in reachable_actions(record.response.available_actions) or not record.has_evidence:
            return self._renderer.render_notice(UNAVAILABLE_ACTION_TEXT, query_label=record.query_label)
        if act.action == ResponseAction.CHECK_PACKAGE_CLAIMS:
            return self.check_package_claims(record, owner_id, channel=channel, request_id=request_id)
        if act.action == ResponseAction.SOURCES:
            # 來源卡只用這一題保存的 evidence / source metadata（含可點擊的來源網址），不重新查詢
            return self._renderer.render_sources(
                source_links(record), record.query_label or record.normalized_request.normalized_text)
        future = self._executor.submit(
            self._reporter.detail, act.action, record.normalized_request, record.evidence, record.risk
        )
        detail = self._await(future, act.request_id, f"detail:{act.action.value}")
        if detail is None:
            return self._renderer.render_notice(DETAIL_FAILED_TEXT, query_label=record.query_label)
        if record.query_label:
            detail = detail.model_copy(update={"query_text": record.query_label})
        return self._renderer.render_detail(detail)
