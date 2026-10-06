"""Agent A — Multimodal Intake / Understanding。

1. 非文字輸入先用 Input Tools 轉成文字：analyze_image / transcribe_audio / fetch_url_content
   （文字輸入不需要 Tool）。
2. 所有內容走同一段語意理解，輸出唯一的 NormalizedUserRequest。

Modality semantic parity：語音轉錄的內容就是使用者親口說的話，語意上等同使用者打字。
因此轉錄完成後，送進語意理解的 payload 與直接輸入同一段文字時完全相同
（同樣的 content 格式、input_type、adapter_notes），不會有 audio 專用的命題分類。
圖片 / 網頁內容是第三方（包裝、廣告、網頁）的文字，才保留來源標示。

只做理解與抽取，不回答問題、不查資料庫（Evidence Tools 屬於 Agent B）、不判斷風險。
"""

import json
import logging
import re
import time

from app.agents.prompts import INTAKE_INSTRUCTIONS
from app.risk.candidate_relevance import FORM_TERMS
from app.risk.claim_policy import is_asserted_disease_claim
from app.normalization import extract_permit_code, locate_in_text, normalize_name
from app.schemas.enums import (
    ClaimContentSource,
    ClaimOrigin,
    ClaimSemanticCategory,
    ContextReference,
    EntityType,
    ErrorCode,
    EvidenceScope,
    InputType,
    Intent,
    ProductMatchType,
    ProductMentionProvenance,
    ProductReferenceStatus,
    ProductVerificationStatus,
    UrlSourceRole,
)
from app.schemas.user_request import (
    ContextEntity,
    ExtractedClaim,
    IntakeExtraction,
    NormalizedUserRequest,
    OfficialSourceStatement,
    ProductMention,
    RawUserInput,
    UrlSource,
)
from app.schemas.evidence import EvidenceBundle, ToolCallRecord, utc_now
from app.schemas.interaction import (
    InteractionMode,
    InteractionSubject,
    InteractionSubjectExtraction,
    InteractionSubjectKind,
)
from app.services.errors import ServiceError
from app.services.openai_service import LLMClient, MediaClient
from app.tools.base import ToolCallRecorder
from app.schemas.image_bundle import (
    MAX_BUNDLE_IMAGES,
    MIN_BUNDLE_IMAGES,
    FusedImageBundle,
    ImageBundleMetrics,
    ImageConflictCode,
)
from app.services.image_fusion import candidate_product_names, fuse_image_bundle, render_fused_content
from app.tools.input_tools import (
    analyze_image,
    analyze_image_bundle,
    fetch_url_content,
    reinspect_image_identity,
    transcribe_audio,
)
from app.tools.url_source import classify_url_source, url_domain

logger = logging.getLogger("nutri_safe_ai.agents.intake")

MAX_INPUT_CHARS = 6000
MAX_CONTEXT_TURNS = 5
MAX_CONTEXT_ENTITIES = 10

# 這兩類宣稱需要官方法規 / 指引文字來說明，因此一定要查 RAG
_REGULATORY_CATEGORIES = {ClaimSemanticCategory.MEDICAL_EFFICACY, ClaimSemanticCategory.EXAGGERATED_EFFICACY}


def _clean_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for value in values:
        value = (value or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


_INPUT_TOOL_STAGES = {"analyze_image": "image_analysis", "transcribe_audio": "transcription",
                      "fetch_url_content": "url_fetch", "reinspect_image_identity": "image_reinspection",
                      "analyze_image_bundle": "image_bundle_analysis"}
REINSPECTED_NOTE = "產品身份無法確認，已局部重讀圖片的許可證字號 / 產品名稱區域一次"
_UNRESOLVED_STATUSES = frozenset({ProductVerificationStatus.NOT_FOUND, ProductVerificationStatus.PARTIAL_MATCH,
                                  ProductVerificationStatus.NOT_APPLICABLE})


def needs_identity_reinspection(request: NormalizedUserRequest, bundle: EvidenceBundle, has_image: bool) -> bool:
    """只有圖片中的產品身份仍無法確認時，才允許一次局部重讀（deterministic）。

    * 許可證字號已命中（PERMIT_NUMBER，包括已 VERIFIED 或真正的字號 / 品名衝突）→ 不重讀；
      不會為了標章文字或品名小差異再做第二次 Vision。
    * 查詢失敗（不是辨識問題）、已重讀過、與產品無關的問題 → 不重讀。
    """
    match = bundle.product_match
    return (
        has_image
        and REINSPECTED_NOTE not in request.adapter_notes
        and match.status in _UNRESOLVED_STATUSES
        and match.match_type != ProductMatchType.PERMIT_NUMBER
        and match.lookup_completed
        and (request.image_identification or request.needs_product_lookup)
        and request.intent not in (Intent.OUT_OF_SCOPE, Intent.GENERAL_QUESTION)
    )
_SECTION_LABELS = {"analyze_image": "圖片內容", "analyze_image_bundle": "圖片內容", "transcribe_audio": "語音內容",
                   "fetch_url_content": "網頁內容"}
USER_TEXT_LABEL = "使用者文字"
OFFICIAL_URL_LABEL = "官方網頁內容"  # 台灣官方監管網域的頁面：內容是官方來源，不是廣告宣稱
URL_EXCERPT_CHARS = 1500
# 內容就是使用者自己說的話（不是第三方文字）的 Input Tool：轉換後與使用者文字走完全相同的語意契約
USER_VOICED_TOOLS = frozenset({"transcribe_audio"})


def semantic_input_type(raw: RawUserInput) -> InputType:
    """語意理解看到的輸入型態：只有文字 / 語音（都是使用者自己的話）時一律視為 TEXT。"""
    if raw.urls or any(m.kind != "audio" for m in raw.media):
        return raw.input_type
    return InputType.TEXT


class IntakeAgent:
    def __init__(self, llm: LLMClient, media: MediaClient | None = None, url_fetcher=fetch_url_content,
                 vocabulary_provider=None):
        self._vocabulary = vocabulary_provider  # 語音轉錄的領域詞彙提示（動態由 Interaction Corpus 建立；None = 不使用）
        self._llm = llm
        self._media = media if media is not None else llm  # OpenAIService 同時實作兩個介面
        self._fetch_url = url_fetcher

    def _vocabulary_prompt(self) -> str | None:
        if self._vocabulary is None:
            return None
        try:
            return self._vocabulary() or None
        except Exception:  # noqa: BLE001 - 詞彙提示只是輔助
            return None

    def prepare_input(
        self, request_id: str, raw: RawUserInput
    ) -> tuple[RawUserInput, list[ToolCallRecord], list[str], list[UrlSource], FusedImageBundle | None,
               list[tuple[str, str]]]:
        """呼叫 Input Tools，把 media / urls 轉成文字併入 text（沒有非文字輸入時原樣返回）。

        回傳 (給語意理解的輸入, Input Tool 紀錄, 只記錄在 request 上、不送進語意理解的 notes,
        每個網址的來源角色 / provenance, multi-image 融合結果)。

        2 張以上圖片 → bundle path：一次 Vision（analyze_image_bundle）+ deterministic fusion，
        融合後的觀察（每項標示來源圖片）取代逐張 analyze_image 的 OCR 全文；單張圖片完全沿用原流程。
        """
        if not raw.media and not raw.urls:
            return raw, [], [], [], None, []
        rec = ToolCallRecorder(request_id, agent="intake")
        images = [m for m in raw.media if m.kind == "image"]
        bundle_mode = len(images) >= MIN_BUNDLE_IMAGES
        if bundle_mode and len(images) > MAX_BUNDLE_IMAGES:
            raise ServiceError(ErrorCode.INVALID_INPUT,
                               f"一次最多只能比對 {MAX_BUNDLE_IMAGES} 張照片", "analyze_image_bundle")
        fused: FusedImageBundle | None = None
        transcripts: list[tuple[str, str]] = []
        calls = [
            ("analyze_image" if m.kind == "image" else "transcribe_audio",
             {"mime_type": m.mime_type, "size_bytes": len(m.content)},
             (lambda m=m: analyze_image(self._media, m.content, m.mime_type)) if m.kind == "image"
             else (lambda m=m: transcribe_audio(self._media, m.content, m.filename or "audio.m4a", self._vocabulary_prompt())))
            for m in raw.media if not (bundle_mode and m.kind == "image")
        ]
        if bundle_mode:
            pairs = [(m.content, m.mime_type) for m in images]
            calls.insert(0, ("analyze_image_bundle",
                             {"image_count": len(images), "size_bytes": sum(len(c) for c, _ in pairs)},
                             lambda: analyze_image_bundle(self._media, pairs)))
        calls += [("fetch_url_content", {"url": u}, lambda u=u: self._fetch_url(u)) for u in raw.urls]
        url_iter = iter(raw.urls)
        url_sources: list[UrlSource] = []

        sections = [(USER_TEXT_LABEL, raw.text.strip())] if raw.text.strip() else []
        notes = list(raw.adapter_notes)
        record_only_notes: list[str] = []
        source_url = raw.source_url
        for name, args, fn in calls:
            stage = _INPUT_TOOL_STAGES[name]
            logger.info("line_stage", extra={"request_id": request_id, "stage": f"{stage}_started"})
            result = rec.call(name, args, fn, planned_by="intake")
            logger.info("line_stage", extra={
                "request_id": request_id, "stage": f"{stage}_done" if result.ok else f"{stage}_failed",
                "error_code": None if result.ok else result.error.code.value,
                "duration_ms": rec.records[-1].duration_ms if rec.records else None})
            if name == "fetch_url_content":
                url_sources.append(_url_source(next(url_iter), result.data if result.ok else None))
            if result.ok and name == "analyze_image_bundle":
                fused = fuse_image_bundle(result.data.observation, result.data.image_count)
                fused.metrics = ImageBundleMetrics(image_count=result.data.image_count,
                                                   vision_bundle_ms=rec.records[-1].duration_ms)
                fused.image_source_ids = {i: m.source_id for i, m in enumerate(images, start=1) if m.source_id}
                sections.append((_SECTION_LABELS[name], render_fused_content(fused)))
                notes.extend(result.data.notes)
            elif result.ok and name in USER_VOICED_TOOLS:
                # 使用者親口說的話 = 使用者文字；轉錄附註只記錄，不影響語意判斷
                sections.append((USER_TEXT_LABEL, result.data.text))  # 已是字形正規化後的繁體逐字稿
                record_only_notes.extend(result.data.notes)
                transcripts.append((result.data.raw_text or result.data.text, result.data.text))
            elif result.ok:
                label = (OFFICIAL_URL_LABEL if name == "fetch_url_content" and url_sources[-1].is_official
                         else _SECTION_LABELS[name])
                sections.append((label, result.data.text))
                notes.extend(result.data.notes)
                source_url = source_url or result.data.source_url
            else:
                notes.append(f"{_SECTION_LABELS[name]}無法讀取（{result.error.code.value}）")
        if not sections:  # 所有非文字輸入都失敗、也沒有文字：如實回報錯誤，不假裝理解
            first = rec.errors[0]
            raise ServiceError(first.code, first.message, first.source)
        if all(label == USER_TEXT_LABEL for label, _ in sections):
            text = "\n\n".join(body for _, body in sections)  # 與直接打字的格式完全相同
        else:
            text = "\n\n".join(f"【{label}】\n{body}" for label, body in sections)
        prepared = raw.model_copy(update={"text": text, "adapter_notes": notes, "source_url": source_url})
        return prepared, rec.records, record_only_notes, url_sources, fused, transcripts

    def reinspect_identity(self, request: NormalizedUserRequest, raw: RawUserInput) -> NormalizedUserRequest | None:
        """局部重讀圖片的許可證字號 / 產品名稱（最多一次）；讀到新的識別資訊才回傳更新後的 request。

        新讀到的字號 / 品名只是「加入」識別線索，是否確認產品仍由 Agent B 的 deterministic resolver 決定。
        """
        image = next((m for m in raw.media if m.kind == "image"), None)
        if image is None:
            return None
        rec = ToolCallRecorder(request.request_id, agent="intake")
        logger.info("line_stage", extra={"request_id": request.request_id, "stage": "image_reinspection_started"})
        result = rec.call("reinspect_image_identity", {"mime_type": image.mime_type, "size_bytes": len(image.content)},
                          lambda: reinspect_image_identity(self._media, image.content, image.mime_type),
                          planned_by="intake")
        logger.info("line_stage", extra={
            "request_id": request.request_id, "stage": "image_reinspection_done" if result.ok else "image_reinspection_failed",
            "error_code": None if result.ok else result.error.code.value,
            "duration_ms": rec.records[-1].duration_ms if rec.records else None})
        notes = request.adapter_notes + [REINSPECTED_NOTE]
        tools = request.input_tools_called + rec.records
        if not result.ok:
            return request.model_copy(update={"adapter_notes": notes, "input_tools_called": tools})
        reading = result.data
        mark_norms = {normalize_name(m) for m in request.certification_marks}
        known_codes = {extract_permit_code(p) for p in request.permit_number_candidates}
        known_names = {normalize_name(n) for n in request.product_candidates}
        permits = [reading.permit_number] if (code := extract_permit_code(reading.permit_number)) and code not in known_codes else []
        name_norm = normalize_name(reading.product_name)
        names = ([reading.product_name] if name_norm and name_norm not in known_names and name_norm not in mark_norms
                 else [])
        if not (permits or names):
            return request.model_copy(update={"adapter_notes": notes, "input_tools_called": tools})
        return request.model_copy(deep=True, update={
            "permit_number_candidates": request.permit_number_candidates + permits,
            "product_candidates": request.product_candidates + names,
            "product_reference_status": ProductReferenceStatus.EXPLICIT,
            "needs_product_lookup": True,
            "adapter_notes": notes,
            "input_tools_called": tools,
        })

    def understand(self, request_id: str, raw: RawUserInput) -> NormalizedUserRequest:
        semantic_type = semantic_input_type(raw)
        only_url_content = not raw.text.strip() and not raw.media
        has_image = any(m.kind == "image" for m in raw.media)
        # 只有圖片：沒有使用者文字、語音（= 使用者的話）或網址
        only_image = has_image and not raw.text.strip() and not raw.urls and all(m.kind == "image" for m in raw.media)
        started_total = time.perf_counter()
        raw, input_tool_records, record_only_notes, url_sources, fused, transcripts = self.prepare_input(request_id, raw)
        text = (raw.text or "").strip()
        if fused is not None and fused.blocks_verification:
            # 身份衝突 / 可能不是同一產品：不做語意理解、不查證，直接請使用者確認（不任選其中一個）
            fused.metrics.total_ms = int((time.perf_counter() - started_total) * 1000)
            logger.info("intake_done", extra={
                "request_id": request_id, "agent": "intake", "input_type": raw.input_type.value,
                "image_count": fused.metrics.image_count, "vision_bundle_ms": fused.metrics.vision_bundle_ms,
                "semantic_ms": None, "bundle_status": fused.status.value})
            return NormalizedUserRequest(
                request_id=request_id, input_type=raw.input_type, original_text=text, normalized_text=text,
                intent=Intent.PRODUCT_VERIFICATION, source_url=raw.source_url,
                adapter_notes=raw.adapter_notes + record_only_notes, input_tools_called=input_tool_records,
                image_bundle=fused)
        if not text:
            return NormalizedUserRequest(
                request_id=request_id,
                input_type=raw.input_type,
                original_text="",
                normalized_text="",
                intent=Intent.UNCLEAR,
                source_url=raw.source_url,
                adapter_notes=raw.adapter_notes,
                url_sources=url_sources,
            )

        known = raw.context.known_entities()[:MAX_CONTEXT_ENTITIES]
        payload = {
            "input_type": semantic_type.value,
            "source_url": raw.source_url,
            "adapter_notes": raw.adapter_notes,
            "previous_user_messages": [t.user_text for t in raw.context.turns if t.user_text][-MAX_CONTEXT_TURNS:],
            "known_context_entities": [{"type": e.entity_type.value, "name": e.name} for e in known],
            "content": text[:MAX_INPUT_CHARS],
        }
        if raw.context.last_visual_request_id:  # 只有前文有已完成的圖片時才加（其餘 payload 維持不變）
            payload["last_visual_request_available"] = True
        if url_sources:  # 只有 URL 輸入才加（文字 / 語音的 payload 維持不變）
            payload["url_sources"] = [{"source_url": u.source_url, "source_role": u.source_role.value}
                                      for u in url_sources]
        started = time.perf_counter()
        extraction = self._llm.parse_structured(
            instructions=INTAKE_INSTRUCTIONS,
            input_text=json.dumps(payload, ensure_ascii=False),
            output_model=IntakeExtraction,
        )
        semantic_ms = int((time.perf_counter() - started) * 1000)
        if fused is not None:
            extraction = merge_bundle_into_extraction(extraction, fused)
        request = self._to_request(request_id, raw, text, extraction, known)
        resolve_context_reference(request, extraction.context_reference, raw, has_image=has_image)
        apply_url_source_roles(request, url_sources, only_url_content=only_url_content)
        apply_image_routing(request, has_image=has_image, only_image=only_image)
        apply_product_phrase_routing(request)
        if fused is not None:
            fused.metrics.semantic_ms = semantic_ms
            fused.metrics.total_ms = int((time.perf_counter() - started_total) * 1000)
            apply_image_bundle(request, fused)
        request.input_tools_called = input_tool_records
        request.adapter_notes = request.adapter_notes + record_only_notes
        if transcripts:  # 語音：保留轉錄原文與正規化逐字稿（log / debug）；Controller 與後續使用 normalized
            request.raw_transcript = "\n".join(r for r, _ in transcripts)
            request.normalized_transcript = "\n".join(n for _, n in transcripts)
        transcriptions = [r for r in input_tool_records if r.tool_name == "transcribe_audio"]
        logger.info(
            "intake_done",
            extra={
                "request_id": request_id,
                "agent": "intake",
                "input_type": raw.input_type.value,
                "semantic_input_type": semantic_type.value,
                "parity_path_used": semantic_type != raw.input_type,
                "transcription_completed": any(r.ok for r in transcriptions) if transcriptions else None,
                "transcript_normalized": (request.raw_transcript != request.normalized_transcript) if transcripts else None,
                "intent": request.intent.value,
                "claim_count": len(request.claims),
                "claim_roles": ",".join(c.origin.value for c in request.claims) or "NONE",
                "url_source_roles": ",".join(u.source_role.value for u in url_sources) or None,
                "official_statement_count": len(request.official_source_statements) if url_sources else None,
                "semantic_extraction_ms": semantic_ms,
                "claim_count_observed": len(request.observed_package_claims) if has_image else None,
                "image_count": fused.metrics.image_count if fused else None,
                "vision_bundle_ms": fused.metrics.vision_bundle_ms if fused else None,
                "semantic_ms": semantic_ms if fused else None,
            },
        )
        return request

    @staticmethod
    def _to_request(
        request_id: str, raw: RawUserInput, text: str, ex: IntakeExtraction, known: list[ContextEntity]
    ) -> NormalizedUserRequest:
        # 標章是視覺證據：弧形 / 小字不必讀對；只保留看得出的標章，文字只在清楚可讀時保留
        mark_observations = [
            o.model_copy(update={"confidence": min(max(o.confidence, 0.0), 1.0),
                                 "text": o.text.strip() if o.text_readable else ""})
            for o in ex.certification_mark_observations if o.visually_detected or (o.text_readable and o.text.strip())
        ]
        certification_marks = _clean_list(
            ex.certification_marks + [o.text for o in mark_observations if o.text_readable and o.text])
        mark_norms = {normalize_name(m) for m in certification_marks} - {""}
        # 標章文字不是產品識別：不可當成品名 / 品牌（標章只能輔助，不能單獨確認產品）
        extracted = [n for n in _clean_list(ex.product_candidates) if normalize_name(n) not in mark_norms]
        # provenance：使用者原文 vs Agent A 改字。只有格式差異的寫法才是使用者自己的寫法（可走 exact）
        products, agent_corrected, mentions = product_mentions(text, extracted)
        raw_norms = {normalize_name(n) for n in products}
        corrected = [n for n in _clean_list(ex.corrected_product_candidates + agent_corrected)
                     if products and normalize_name(n) and normalize_name(n) not in raw_norms]
        brands = [n for n in _clean_list(ex.brand_candidates) if normalize_name(n) not in mark_norms]
        companies = _clean_list(ex.company_candidates)
        ingredients = _clean_list(ex.ingredient_candidates)
        permits = _clean_list(ex.permit_number_candidates)
        claims = [c for c in ex.claims if c.claim_text.strip() and not _is_open_efficacy_question(ex.intent, c)]
        claims, identity_text = separate_identity_text(
            claims, identifiers=products + agent_corrected + brands + companies + permits + certification_marks)
        other_visible_text = _clean_list(ex.other_visible_text + identity_text)

        # 產品指稱解析：LLM 只能從系統提供的前文實體中挑選，清單外的名稱一律丟棄
        explicit = bool(products or brands or companies or permits)
        resolved = _resolve_context_names(ex.context_resolved_names, known) if not explicit else []
        for entity in resolved:
            target = {EntityType.PRODUCT: products, EntityType.BRAND: brands,
                      EntityType.COMPANY: companies, EntityType.INGREDIENT: ingredients}[entity.entity_type]
            if entity.name not in target:
                target.append(entity.name)
        if explicit:
            reference = ProductReferenceStatus.EXPLICIT
        elif any(e.entity_type != EntityType.INGREDIENT for e in resolved):
            reference = ProductReferenceStatus.RESOLVED_FROM_CONTEXT
        elif ex.refers_to_unspecified_product:
            reference = ProductReferenceStatus.MISSING_CONTEXT
        else:
            reference = ProductReferenceStatus.NOT_NEEDED

        # 一般性一致化規則：有抽到對應實體就一定要查，避免 LLM 旗標漏設
        needs_product_lookup = ex.needs_product_lookup or bool(products or brands or companies or permits)
        needs_claim_check = ex.needs_claim_check or bool(claims)
        needs_graph_lookup = ex.needs_graph_lookup or bool(ingredients) or (
            ex.intent in (Intent.EFFICACY_INQUIRY, Intent.INGREDIENT_INQUIRY) and bool(products or ingredients)
        )
        needs_rag = ex.needs_rag or any(c.semantic_category in _REGULATORY_CATEGORIES for c in claims)

        subjects, interaction_mode = interaction_subjects_for(ex, text, products, ingredients)
        overview = interaction_mode == InteractionMode.CURRENT_PRODUCT_OVERVIEW
        if subjects or overview:
            # interaction 的產品對象只放 interaction_subjects（逐一由 Agent B 確認）；不放 product_candidates，避免當成單一產品比對
            products, corrected, mentions = [], [], []
            needs_product_lookup = needs_claim_check = needs_graph_lookup = needs_rag = False
            claims = []
            reference = ProductReferenceStatus.NOT_NEEDED

        return NormalizedUserRequest(
            request_id=request_id,
            input_type=raw.input_type,
            original_text=text,
            normalized_text=ex.normalized_text.strip() or text,
            language=ex.language or "zh-TW",
            intent=ex.intent,
            product_candidates=products,
            corrected_product_candidates=corrected,
            product_mentions=mentions,
            brand_candidates=brands,
            company_candidates=companies,
            ingredient_candidates=ingredients,
            permit_number_candidates=permits,
            certification_marks=certification_marks,
            certification_mark_observations=mark_observations,
            claims=claims,
            other_visible_text=other_visible_text,
            question=ex.question.strip(),
            refers_to_unspecified_product=ex.refers_to_unspecified_product,
            product_reference_status=reference,
            context_entities_used=resolved,
            mentioned_country_codes=[c.strip().upper() for c in ex.mentioned_country_codes if c.strip()],
            needs_product_lookup=needs_product_lookup,
            needs_claim_check=needs_claim_check,
            needs_graph_lookup=needs_graph_lookup,
            needs_rag=needs_rag,
            needs_official_web_search=ex.needs_official_web_search,
            source_url=raw.source_url,
            adapter_notes=raw.adapter_notes,
            interaction_subjects=subjects,
            interaction_overview=overview,
            interaction_mode=interaction_mode,
            interaction_question=ex.interaction_question if subjects else None,
        )


def interaction_subjects_for(ex: IntakeExtraction, text: str, products: list[str],
                             ingredients: list[str]) -> tuple[list[InteractionSubject], InteractionMode | None]:
    """intent = interaction_inquiry 的對象與問題型態（deterministic 一致化；LLM 只提供線索）。

    * 對象名稱必須能在使用者原文中找到（只差格式 / 小幅錯字）；找不到的一律丟棄——LLM 不能把名稱翻譯或改寫成別的 identity。
    * identity（成分 / 藥品 / 產品）一律由 Agent B 的 deterministic resolver 決定；kind 只是線索。
    * LLM 沒給對象時，退回已抽到的產品 / 成分名稱（保持原文）。
    """
    if ex.intent != Intent.INTERACTION_INQUIRY:
        return [], None
    # 泛指的交互作用總覽（Controller 的結構化判斷：沒有指定第二個對象）：沒有任何具體對象，或只有「這個產品」本身的指稱
    # （唯一的 product 對象 + 本次有未指明產品的指稱）→ 對象由 pipeline 以已確認的目前產品綁定；不把泛稱當成產品名稱查詢
    lone_reference = (len(ex.interaction_subjects) == 1 and ex.interaction_subjects[0].kind == InteractionSubjectKind.PRODUCT
                      and ex.refers_to_unspecified_product)
    if ex.interaction_partner_unspecified and (not ex.interaction_subjects or lone_reference):
        return [], InteractionMode.CURRENT_PRODUCT_OVERVIEW
    candidates = list(ex.interaction_subjects) or (
        [InteractionSubjectExtraction(text=n, kind=InteractionSubjectKind.PRODUCT) for n in products]
        + [InteractionSubjectExtraction(text=n, kind=InteractionSubjectKind.INGREDIENT, concept_recognized=True)
           for n in ingredients])  # Controller 已把它抽成成分：視為可辨識的成分概念
    subjects: list[InteractionSubject] = []
    seen: set[str] = set()
    for c in candidates:
        raw, _distance = locate_in_text(text, c.text)
        if raw is None or not raw.strip() or normalize_name(raw) in seen:
            continue
        seen.add(normalize_name(raw))
        subjects.append(InteractionSubject(text=raw.strip(), kind=c.kind, concept_recognized=c.concept_recognized))
    mode = ex.interaction_mode or InteractionMode.PAIRWISE
    return subjects, mode


# 「功效」本身的泛稱：不是任何具體的生理功能或疾病（一般性詞彙，不針對特定產品或問句）
GENERIC_EFFICACY_TERMS = frozenset(normalize_name(t) for t in (
    "功效", "保健功效", "核准功效", "官方核准功效", "健康功效", "效果", "效用", "功能", "作用", "好處",
))


# 泛稱的組合（例如「核准保健功效」「官方核准的保健功效」「保健效果」）：只由修飾詞＋泛稱核心詞組成，沒有指定具體功效
GENERIC_EFFICACY_QUALIFIERS = tuple(normalize_name(t) for t in (
    "官方", "核准", "核可", "許可", "登記", "保健", "健康", "產品", "這個", "它", "的",
))
GENERIC_EFFICACY_CORES = frozenset(normalize_name(t) for t in ("功效", "效果", "效用", "功能", "作用", "好處"))


def _is_generic_efficacy_term(norm: str) -> bool:
    if norm in GENERIC_EFFICACY_TERMS:
        return True
    rest = norm
    for qualifier in GENERIC_EFFICACY_QUALIFIERS:
        rest = rest.replace(qualifier, "")
    return rest in GENERIC_EFFICACY_CORES


def _is_specific_target(value: str | None) -> bool:
    norm = normalize_name(value or "")
    return bool(norm) and not _is_generic_efficacy_term(norm)


def _is_open_efficacy_question(intent: Intent, claim: ExtractedClaim) -> bool:
    """「它的核准功效是什麼？」這類開放式詢問：使用者在問官方核准了什麼，沒有提出任何待查證的具體效果。

    一般化判斷：功效詢問（efficacy_inquiry）中的 USER_QUESTION，既沒有疾病 / 症狀，也沒有指定具體生理功能
    （「功效」「效果」等泛稱不算指定）。
    這種「命題」無法被證實或否定，若保留會被判為 UNSUPPORTED（「找不到官方資料支持這個說法」），
    因此不當成命題；答案直接來自官方核准功效。
    被說出來的宣稱（斷言、包裝、廣告、轉述）一律保留。
    """
    return (
        intent == Intent.EFFICACY_INQUIRY
        and claim.origin == ClaimOrigin.USER_QUESTION
        and not _is_specific_target(claim.target_condition)
        and not _is_specific_target(claim.target_function)
    )


def separate_identity_text(
    claims: list[ExtractedClaim], identifiers: list[str]
) -> tuple[list[ExtractedClaim], list[str]]:
    """產品身份文字不是宣稱（deterministic）：命題文字（正規化後）與品名 / 品牌 / 公司 / 字號 / 標章相同，
    或整段只是其中某個識別名稱的一部分 → 移出 claims，改存為其他可見文字。

    使用者提問不受影響（提問本身不會是識別名稱）。一般性規則，不針對任何特定產品或字詞。
    """
    norms = [n for n in (normalize_name(i) for i in identifiers) if n]
    kept: list[ExtractedClaim] = []
    moved: list[str] = []
    for claim in claims:
        text = normalize_name(claim.claim_text)
        if claim.origin != ClaimOrigin.USER_QUESTION and text and any(text == n or text in n for n in norms):
            moved.append(claim.claim_text.strip())
            continue
        kept.append(claim)
    return kept, moved


def _resolve_context_names(names: list[str], known: list[ContextEntity]) -> list[ContextEntity]:
    by_norm = {normalize_name(e.name): e for e in known}
    out: list[ContextEntity] = []
    for name in names:
        entity = by_norm.get(normalize_name(name))
        if entity is not None and entity not in out:
            out.append(entity)
    return out


# ================================================================== URL source role


def _url_source(requested_url: str, output) -> UrlSource:
    """依實際讀取的網址（含重新導向）判斷來源角色；讀取失敗時以原網址判斷，且沒有內容可用。"""
    source_url = (output.source_url if output is not None else None) or requested_url
    return UrlSource(
        requested_url=requested_url,
        source_url=source_url,
        source_domain=url_domain(source_url),
        source_role=classify_url_source(source_url),
        title=getattr(output, "title", None) if output is not None else None,
        fetched=output is not None,
        retrieved_at=utc_now() if output is not None else None,
        excerpt=(output.text[:URL_EXCERPT_CHARS] if output is not None else ""),
    )


_USER_ORIGINS = frozenset({ClaimOrigin.USER_QUESTION, ClaimOrigin.USER_ASSERTION})


def apply_url_source_roles(
    request: NormalizedUserRequest, url_sources: list[UrlSource], *, only_url_content: bool = False
) -> None:
    """依 URL source role 決定網頁內容中的命題是「官方來源的陳述」還是「待查證的外部宣稱」（deterministic）。

    * 官方監管網域（且本次所有讀到的網址都是官方）：頁面上的內容是官方資料，移到 official_source_statements，
      不進 claims——不會被當成廣告 / 流傳說法，也不會拿官方資料去驗證官方資料自己。
    * 非官方 / 無法判斷：頁面內容仍是外部宣稱（待查證）；頁面自稱「官方認證」不會讓它變成官方，
      OFFICIAL_CLAIM 改為 THIRD_PARTY_CLAIM（對方「說」官方怎麼樣，仍是對方的說法）。
    * 使用者自己的文字（提問 / 斷言）不受影響。

    命題是否來自網頁：Agent A 標記的 content_source；本次只有網頁內容（沒有使用者文字 / 圖片）時，
    非使用者角色的命題一定出自網頁。
    """
    request.url_sources = list(url_sources)
    fetched = [u for u in url_sources if u.fetched]
    if not fetched:
        return
    roles = {u.source_role for u in fetched}
    all_official = roles == {UrlSourceRole.OFFICIAL_REGULATORY_SOURCE}
    page = fetched[0] if len(fetched) == 1 else None
    # 多個網址角色不同時無法判斷命題出自哪一頁：取最保守的角色
    if page is not None and not all_official:
        external_role = page.source_role
    elif UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE in roles:
        external_role = UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE
    else:
        external_role = UrlSourceRole.NON_OFFICIAL_EXTERNAL_SOURCE

    kept: list[ExtractedClaim] = []
    moved = False
    for claim in request.claims:
        from_page = claim.content_source == ClaimContentSource.URL_CONTENT or (
            only_url_content and claim.origin not in _USER_ORIGINS
        )
        if not from_page:
            kept.append(claim)
            continue
        if all_official:
            src = page or fetched[0]
            request.official_source_statements.append(OfficialSourceStatement(
                claim_text=claim.claim_text, source_url=src.source_url, source_domain=src.source_domain,
                title=src.title, evidence_scope=EvidenceScope.GENERAL_BACKGROUND,
            ))
            moved = True
            continue
        update = {"source_role": external_role, "source_url": page.source_url if page is not None else None,
                  "content_source": ClaimContentSource.URL_CONTENT}
        if claim.origin == ClaimOrigin.OFFICIAL_CLAIM:
            update["origin"] = ClaimOrigin.THIRD_PARTY_CLAIM
        kept.append(claim.model_copy(update=update))
    request.claims = kept
    if moved:
        # 官方頁面的內容不需要再查證：旗標依剩下的（使用者自己的）命題重新計算，不再搜尋同一官方資訊
        request.needs_claim_check = bool(kept)
        request.needs_rag = any(c.semantic_category in _REGULATORY_CATEGORIES for c in kept)
        request.needs_official_web_search = False


# ================================================================== 圖片輸入：product-first routing


def is_asserted_medical_claim(claim: ExtractedClaim) -> bool:
    """被說出來（包裝 / 廣告等）的疾病宣稱（明確治療類或疾病預防 / 改善類）：需要自動查證。

    是否真的是 MEDICAL_TREATMENT_CLAIM 由 claim policy 決定（已確認產品先與官方核准功效比對）。
    """
    return is_asserted_disease_claim(claim)


def apply_image_routing(request: NormalizedUserRequest, *, has_image: bool, only_image: bool) -> None:
    """圖片中的宣稱預設是「觀察到的包裝內容」，不自動進完整 claim verification（deterministic）。

    只有兩種情況自動查證：
    A. 使用者自己的文字明確要求查證（intent = claim_verification，且有使用者文字）→ 不改動。
    B. 圖片上有被宣稱的醫療效能（治療 / 預防疾病）→ 只把這些高風險宣稱留在 claims。
    其餘圖片宣稱移到 observed_package_claims（可按「查驗包裝宣稱」再查）。
    只傳圖片（沒有使用者文字）且沒有高風險宣稱 → 目標是辨識產品（image_identification）。
    """
    if not has_image:
        return
    image_claims = [c for c in request.claims if c.content_source == ClaimContentSource.IMAGE_CONTENT
                    or (only_image and c.origin not in _USER_ORIGINS)]
    if not image_claims:
        if only_image and request.intent != Intent.OUT_OF_SCOPE:
            _route_to_identification(request)
        return
    if not only_image and request.intent == Intent.CLAIM_VERIFICATION:
        request.claims_from_package = True  # A. 使用者明確要求查驗
        return
    image_ids = {id(c) for c in image_claims}
    high_risk = [c for c in image_claims if is_asserted_medical_claim(c)]
    request.claims = [c for c in request.claims if id(c) not in image_ids] + high_risk
    request.observed_package_claims = [c for c in image_claims if not is_asserted_medical_claim(c)]
    if high_risk:  # B. 高風險醫療宣稱：自動查證
        request.claims_from_package = True
        if only_image:
            request.intent = Intent.CLAIM_VERIFICATION
        request.needs_claim_check = True
        return
    request.needs_claim_check = bool(request.claims)
    request.needs_rag = any(c.semantic_category in _REGULATORY_CATEGORIES for c in request.claims)
    if only_image and request.intent != Intent.OUT_OF_SCOPE:
        _route_to_identification(request)


def _route_to_identification(request: NormalizedUserRequest) -> None:
    request.intent = Intent.PRODUCT_VERIFICATION
    request.image_identification = True
    request.needs_official_web_search = False
    request.needs_claim_check = bool(request.claims)


def resolve_context_reference(request: NormalizedUserRequest, reference: ContextReference, raw: RawUserInput,
                              *, has_image: bool) -> None:
    """Controller 的 context_reference（語意判斷）→ request（deterministic 守門，不用任何關鍵字）。

    PREVIOUS_IMAGE 只在：前文有可用的圖片觀察、這一題沒有自己的圖片 / 網址、而且文字沒有給出新的產品 / 品牌 / 公司 / 字號
    （明確的新對象優先）時成立；否則視為 NONE。CURRENT_PRODUCT 且已有已確認產品 → 產品綁定仍由 context_binding 決定；
    指稱「目前產品」卻沒有已確認產品、前文又有圖片觀察 → 改為 PREVIOUS_IMAGE（不是關鍵字規則，只看結構化欄位）。
    """
    visual_id = raw.context.last_visual_request_id
    own_input = has_image or bool(raw.urls)
    no_new_identity = not request.has_product_identifiers  # 明確的新產品 / 品牌 / 公司 / 字號優先
    if reference == ContextReference.PREVIOUS_IMAGE and not (visual_id and not own_input and no_new_identity):
        reference = ContextReference.NONE
    elif (reference != ContextReference.PREVIOUS_IMAGE and visual_id and not own_input and no_new_identity
          and raw.context.confirmed_product is None and request.intent != Intent.INTERACTION_INQUIRY
          and (reference == ContextReference.CURRENT_PRODUCT or request.missing_product_context)):
        # 指的是「目前產品」但沒有已確認的產品、而前文有可用的圖片觀察：不進通用 Researcher / planner / 網路，
        # 改由圖片觀察回答（依既有 intent 決定顯示哪一類；沒有相關內容就請使用者補充）
        reference = ContextReference.PREVIOUS_IMAGE
    request.context_reference = reference
    if reference == ContextReference.PREVIOUS_IMAGE:
        request.visual_request_id = visual_id


# ================================================================== 圖片輸入：multi-image bundle


def merge_bundle_into_extraction(ex: IntakeExtraction, fused: FusedImageBundle) -> IntakeExtraction:
    """用融合後的觀察校正語意抽取（deterministic；LLM 只負責語意，不能弄丟 / 弄混 identity 欄位）。

    * 融合觀察中的「品名 / 許可證字號」一定進入候選（LLM 漏抽也不會掉）。
    * 成分表上的成分只能是成分：被 LLM 放進 product_candidates 的成分名稱一律移除（不會變成另一個產品候選）。
    """
    ex = ex.model_copy(deep=True)
    name_keys = {normalize_name(v.value) for v in fused.product_names}
    ingredient_keys = {normalize_name(v.value) for v in fused.ingredients} - name_keys - {""}
    names = [n for n in ex.product_candidates if normalize_name(n) not in ingredient_keys]
    have = {normalize_name(n) for n in names}
    names += [n for n in candidate_product_names(fused) if normalize_name(n) not in have]
    ex.product_candidates = names
    have_codes = {extract_permit_code(p) for p in ex.permit_number_candidates}
    ex.permit_number_candidates = ex.permit_number_candidates + [
        v.value for v in fused.permit_numbers if extract_permit_code(v.value) not in have_codes]
    return ex


def apply_image_bundle(request: NormalizedUserRequest, fused: FusedImageBundle) -> None:
    """融合結果存到 request（保留每項來源圖片）；宣稱標示來源圖片；非關鍵的辨識不一致只留紀錄。"""
    request.image_bundle = fused
    for c in request.claims + request.observed_package_claims:
        c.source_images = fused.sources_of(fused.claims, c.claim_text)
    for conflict in fused.conflicts:
        if conflict.code == ImageConflictCode.OCR_UNCERTAIN:
            request.adapter_notes = request.adapter_notes + [
                f"多張圖片的{conflict.field}辨識不一致（OCR_UNCERTAIN），依既有證據優先序判斷"]


# 「成分＋劑型」片語（例如某營養素加上膠囊 / 錠）：中間最多夾幾個字（規格、口味等），不跨標點 / 空白
_PHRASE_GAP = r"[^\s，,。？?！!、；;：:]{0,6}?"
_FORM_ALTERNATION = "|".join(re.escape(t) for t in sorted(FORM_TERMS, key=len, reverse=True))
# 問句 / 敘述線索：有這些字就不是「只有一個名稱」的訊息（一般性語言規則，不是產品詞表）
_QUESTION_CUES = ("?", "？", "嗎", "呢", "吗", "什麼", "甚麼", "什么", "怎麼", "怎么", "如何", "哪", "可以", "能不能", "是否",
                  "有沒有", "有没有", "請問", "请问", "幫我", "帮我", "為什麼", "為何", "是不是", "要不要")
MAX_BARE_NAME_CHARS = 40
PRODUCT_PHRASE_NOTE = "含劑型的成分片語視為產品名稱 / 產品類別，先做產品查詢"
BARE_NAME_NOTE = "整則訊息只有一個名稱，先當成產品名稱查詢"


def apply_product_phrase_routing(request: NormalizedUserRequest) -> None:
    """產品 vs 成分的一般化路由（deterministic，Agent A 語意抽取之後）。

    A. 「成分＋劑型」片語（膠囊 / 錠 / 粉 / 飲…）指的是產品或產品類別，不是單一成分：
       移到 product_candidates，走產品查詢 / 候選確認；不自動變成成分查詢。
    B. intent 不明、沒有任何實體 / 命題、整則訊息只是一個名稱（沒有問句線索）→ 當成產品名稱查詢。
    只在沒有任何產品識別、沒有命題、不是圖片 / 網址 / 代名詞時套用；明確的成分問題（「X 是什麼？」）不受影響。
    """
    if (request.has_product_identifiers or request.claims or request.url_sources or request.image_identification
            or request.refers_to_unspecified_product or request.context_entities_used
            or request.input_type not in (InputType.TEXT, InputType.AUDIO)):
        return
    text = request.original_text or ""
    for ingredient in list(request.ingredient_candidates):
        m = re.search(re.escape(ingredient) + _PHRASE_GAP + f"(?:{_FORM_ALTERNATION})", text, re.IGNORECASE)
        if m is None:
            continue
        request.product_candidates = [m.group(0).strip()]
        request.ingredient_candidates = [i for i in request.ingredient_candidates if i != ingredient]
        if request.intent in (Intent.UNCLEAR, Intent.GENERAL_QUESTION):
            request.intent = Intent.PRODUCT_VERIFICATION
        _mark_product_lookup(request, PRODUCT_PHRASE_NOTE)
        return
    bare = text.strip().strip("。.!！~～ ")
    if (request.intent == Intent.UNCLEAR and not request.ingredient_candidates and bare
            and len(normalize_name(bare)) >= 2 and len(bare) <= MAX_BARE_NAME_CHARS
            and not any(cue in bare for cue in _QUESTION_CUES) and not re.search(r"[，,。；;\n]", bare)):
        request.product_candidates = [bare]
        request.intent = Intent.PRODUCT_VERIFICATION
        _mark_product_lookup(request, BARE_NAME_NOTE)


def _mark_product_lookup(request: NormalizedUserRequest, note: str) -> None:
    request.needs_product_lookup = True
    request.product_reference_status = ProductReferenceStatus.EXPLICIT
    request.adapter_notes = request.adapter_notes + [note]


def product_mentions(text: str, extracted: list[str]) -> tuple[list[str], list[str], list[ProductMention]]:
    """Agent A 抽出的產品名稱 → (產品名稱＝使用者寫法, Agent A 修正寫法, provenance)。

    * 在使用者輸入中逐字（只差大小寫 / 全半形 / 空白 / 標點）找得到 → USER_TEXT，保留原寫法。
    * 只找得到改了字的對應片段（錯字修正、同音修正、增刪字）→ AGENT_CORRECTED：產品名稱改用使用者原文片段，
      Agent A 的寫法移到修正寫法（只是查詢線索，不能直接確認產品）。
    * 輸入中找不到 → NOT_IN_USER_TEXT：仍用來查詢，但 resolver 不把它當成使用者寫法。
    不禁止 Agent A 改字；只把 provenance 分開。
    """
    products, corrected, mentions = [], [], []
    for name in extracted:
        raw, distance = locate_in_text(text, name)
        if raw is not None and distance == 0:
            mentions.append(ProductMention(extracted_text=name, raw_text=raw, normalized_mention=normalize_name(raw),
                                           provenance=ProductMentionProvenance.USER_TEXT))
            products.append(name)
        elif raw is not None:
            mentions.append(ProductMention(extracted_text=name, raw_text=raw, normalized_mention=normalize_name(raw),
                                           corrected_name=name, correction_applied=True, edit_count=distance,
                                           provenance=ProductMentionProvenance.AGENT_CORRECTED))
            products.append(raw.strip())
            corrected.append(name)
        else:
            mentions.append(ProductMention(extracted_text=name, corrected_name=name, correction_applied=True,
                                           edit_count=distance, provenance=ProductMentionProvenance.NOT_IN_USER_TEXT))
            products.append(name)
    return list(dict.fromkeys(products)), list(dict.fromkeys(corrected)), mentions
