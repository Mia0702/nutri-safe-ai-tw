"""Agent C — Reporter。

輸入 NormalizedUserRequest + EvidenceBundle + RiskAssessment，輸出結構化內容（不輸出 Flex JSON）：
* compose()：每個 request 唯一的 primary ResponsePayload（deterministic，不呼叫 LLM，回覆快且可測試）。
  依 intent / 證據選擇 response_type；只有 RISK 才帶風險等級。
* detail()：使用者按下按鈕後，依同一個 request_id 保存的證據產生 DetailPayload（on-demand）。
  「查看完整分析」才由 LLM 把證據寫成白話正文（report()）。
* 轉成 LINE Flex Message 由 app.response.line_renderer.AdaptiveLineRenderer 負責。

不重新查資料、不新增 EvidenceBundle 以外的官方結論。風險標題、必要免責聲明、資料來源由程式產生；
LLM 只寫完整分析正文，因此不可能捏造來源或改變風險等級。OpenAI 失敗時改用固定模板。
"""

import json
import logging
import os
import re

from pydantic import BaseModel

from app.agents import interaction_reporter as ir
from app.agents.prompts import REPORTER_INSTRUCTIONS
from app.schemas.analysis import ReporterOutput
from app.risk.assessment_rules import QUESTION_MEDICAL_PROPOSITION
from app.risk.claim_policy import NOTE_CLAIM_LEVEL_ONLY
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimOrigin,
    ClaimPolicyResult,
    Intent,
    PolicyEvidenceUse,
    ProductVerificationStatus,
    ResponseAction,
    ResponseType,
    RiskLevel,
)
from app.normalization import extract_permit_code, normalize_name
from app.product_coverage import (
    CATEGORY_LABELS,
    COVERAGE_LIST_TEXT,
    NOT_FOUND_NOT_FAKE_TEXT,
    NO_HEALTH_CLAIM_APPROVAL_TEXT,
    NOT_FOUND_COVERAGE_TEXT,
    REGISTRATION_RECORD_TEXT,
    STATUS_NOT_PROVIDED_TEXT,
    expiry_date_text,
    has_health_claim_approval,
    is_officially_inactive,
)
from app.schemas.enums import FunctionEvidenceStatus, IngredientIdentityStatus, IngredientPresence, IngredientQueryResolution, ProductNotFoundReason
from app.schemas.evidence import (
    ApprovedClaimEvidence,
    ClaimAssessment,
    ConfirmedIngredientEntry,
    EvidenceBundle,
    IngredientEntityRef,
    IngredientQueryAnswer,
    ProductIngredientEvidence,
    SourceRef,
)
from app.schemas.response import CLAIM_RISK_LABEL, RISK_EMOJI, DetailPayload, ProductChoice, ResponsePayload
from app.schemas.enums import SemanticTone
from app.schemas.risk import RiskAssessment
from app.schemas.user_request import NormalizedUserRequest
from app.response.sources import display_source_url
from app.services.errors import ServiceError
from app.services.image_fusion import bundle_stop_message
from app.services.openai_service import LLMClient

logger = logging.getLogger("nutri_safe_ai.agents.reporter")

MAX_SOURCES = 3
MAX_WARNINGS_IN_SUMMARY = 4

RISK_TITLES = {
    RiskLevel.LOW: "目前看起來屬於低風險",
    RiskLevel.MEDIUM: "目前看起來屬於中風險，建議多加留意",
    RiskLevel.HIGH: "高風險，請特別小心",
}
INCONCLUSIVE_TITLE = "目前資訊不足，暫時無法確認"
VERIFIED_LOW_TITLE = "查有官方紀錄，目前沒有發現不當宣稱"  # 只描述查證結果，不推論產品整體安全性
RISK_HEADERS = {level: f"{RISK_EMOJI[level]} {title}" for level, title in RISK_TITLES.items()}
INCONCLUSIVE_HEADER = f"{RISK_EMOJI[RiskLevel.MEDIUM]} {INCONCLUSIVE_TITLE}"
VERIFIED_LOW_HEADER = f"{RISK_EMOJI[RiskLevel.LOW]} {VERIFIED_LOW_TITLE}"
INFERRED_LINK_NOTE = "註：品牌與公司之間的對應是系統依產品名稱推測的，並非官方確認。"
# 有推測關聯時，正文不得把推測說成官方確認（否定語境如「非官方確認」「不是官方確認」除外）；出現就改用固定模板
OFFICIAL_CONFIRMATION_RE = re.compile(r"(?<!非)(?<!不是)(?<!未經)官方(?:已)?(?:確認|證實|認定)")
# 推測關聯也不得寫成「擁有 / 旗下 / 品牌屬於某公司」等擁有關係
OWNERSHIP_CLAIM_RE = re.compile(r"旗下|(?<![不非沒])擁有[^，。；\n]{0,8}品牌|品牌(?:是|為)?(?:屬於|歸屬於?|為[^，。；\n]{0,10}所有)")
OUT_OF_SCOPE_TEXT = "呷安心目前專門協助查證保健食品的資訊，例如產品是否有官方登記、廣告說法是否可信。"


def risk_title(risk: RiskAssessment) -> str:
    if risk.risk_level == RiskLevel.MEDIUM and not risk.is_conclusive:
        return INCONCLUSIVE_TITLE
    if risk.risk_level == RiskLevel.LOW and risk.product_verification == ProductVerificationStatus.VERIFIED:
        return VERIFIED_LOW_TITLE
    return RISK_TITLES[risk.risk_level]


def build_header(risk: RiskAssessment) -> str:
    return f"{RISK_EMOJI[risk.risk_level]} {risk_title(risk)}"


def format_sources(sources: list[SourceRef]) -> str:
    official = [s for s in sources if s.is_official][:MAX_SOURCES]
    if not official:
        return ""
    lines = ["資料來源："]
    for s in official:
        shown = display_source_url(s.source_url, s.display_url)  # raw 匯出網址不顯示（手機會直接下載）
        lines.append(f"・{s.source_name}" + (f"\n  {shown}" if shown else ""))
    return "\n".join(lines)


def build_evidence_summary(
    request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment
) -> dict:
    """給 LLM 的精簡證據摘要（只包含 EvidenceBundle 內已有的內容）。"""
    match = bundle.product_match
    best = match.best_match.product if match.best_match else None
    return {
        "user_request": {
            "question": request.question or request.normalized_text,
            "intent": request.intent.value,
            "product_candidates": request.product_candidates,
            "product_reference_status": request.product_reference_status.value,
            "product_from_previous_messages": [e.name for e in request.context_entities_used],
            "claims": [{"claim_text": c.claim_text, "origin": c.origin.value} for c in request.claims],
        },
        "product_verification": {
            "status": match.status.value,
            "lookup_completed": match.lookup_completed,
            "matched_product": None if best is None else {
                "name": best.product_name_zh,
                "company": best.company_name,
                "permit_number": best.permit_number,
                "permit_status": best.status.value,
                "approval_date": best.approval_date,
                # 類別決定官方紀錄的意義：非健康食品類別只是查驗登記／核備資料，沒有核准保健功效
                "product_category": CATEGORY_LABELS[best.product_category],
                "has_health_claim_approval": has_health_claim_approval(best.product_category),
                "registration_status_text": product_status_text(best),
            },
            "similar_candidates": [c.product.product_name_zh for c in match.candidates[:3]]
            if match.status.value == "PARTIAL_MATCH" else [],
            "notes": match.notes,
        },
        "inferred_entity_links": [
            {
                "user_mentioned": link.query,
                "possibly_related_company": link.resolved_company_name,
                "confidence": link.confidence,
                "is_official_confirmation": False,
                "how_to_describe": "只能說「系統推測可能有關」，不可說官方確認，也不可說公司擁有此品牌或是其旗下品牌",
            }
            for link in bundle.inferred_links
        ],
        "approved_claims": [{"functions": line.function_names, "text": line.claim_text}
                            for line in dedupe_approved_claims(bundle.approved_claims)],
        "official_warnings": [w.text for w in bundle.warnings[:MAX_WARNINGS_IN_SUMMARY]],
        "ingredients": sorted({i.name_zh for i in bundle.ingredients}),  # 已確認產品的官方登記成分
        "standalone_ingredient_names": _standalone_ingredient_names(request, bundle),  # 成分名稱查詢（不是產品成分）
        # 獨立成分 / 營養素的 canonical identity 與功能證據狀態（不是產品核准保健功效；沒有證據不可編造生理功能）
        "standalone_entities": [{"name": e.display_name_zh or e.canonical_name_en, "type": _entity_type_label(e.entity_type)}
                                for e in bundle.standalone_entities],
        "ingredient_identity_status": bundle.ingredient_identity_status.value if bundle.ingredient_identity_status else None,
        "function_evidence_status": bundle.function_evidence_status.value if bundle.function_evidence_status else None,
        # 成分三層（官方列有 / 已確認身分 / 尚未確認）與「有沒有某成分」的結構化答案
        "product_ingredients": ingredient_evidence_summary(bundle.product_ingredients),
        "graph_relations": [
            f"{g.subject_name} -[{g.relation}]-> {g.object_name}"
            + (f"（{g.properties.get('product_count')} 項產品）" if g.properties.get("product_count") else "")
            for g in bundle.graph_evidence[:8]
        ],
        "official_documents": [{"title": r.title, "excerpt": r.content[:300]} for r in bundle.rag_evidence[:3]],
        "official_web": [{"title": w.title, "excerpt": w.snippet[:300]} for w in bundle.official_web_evidence[:3]],
        "illegal_ad_records": [
            {"product": a.product_name, "content": a.violation_content[:200]} for a in bundle.illegal_ad_evidence[:3]
        ],
        "claim_assessment": [
            {
                "claim_text": c.claim_text,
                "origin": c.origin.value,
                "status": c.status.value,
                "explanation": c.explanation,
                "evidence_rationales": [l.rationale for l in c.evidence_links[:3]],
                "claim_policy": None if c.policy is None else {
                    "policy_result": c.policy.policy_result.value,
                    "policy_type": c.policy.policy_type.value,
                    "reason": c.policy.reason,
                    "matched_official_approved_claim": c.policy.matched_approved_claim_text,
                    "matched_function": c.policy.matched_function_name,
                    "difference": c.policy.difference,
                    "product_identity_unresolved": c.policy.product_identity_unresolved,
                    "sources": [src.source_name for src in c.policy.sources],
                    "matched_official_example": c.policy.matched_example_text,
                    "policy_rules": [
                        {"article": r.article_or_section, "summary": r.rule_summary_zh,
                         "evidence_use": r.evidence_use.value}
                        for r in bundle.claim_policy_rules if r.rule_id in c.policy.policy_rule_ids
                    ],
                },
            }
            for c in bundle.claim_assessment
        ],
        "response_type": select_response_type(request, bundle, risk).value,
        "claim_verdicts": [
            {"claim_text": c.claim_text, "claim_role": c.origin.value,
             "claim_risk": c.claim_risk.value if c.claim_risk else None, "verdict": claim_verdict_line(c)}
            for c in bundle.claim_assessment
        ],
        "risk": {
            "risk_level": risk.risk_level.value,
            "reasons": [h.reason for h in risk.triggered_rules],
            "is_conclusive": risk.is_conclusive,
            "data_incomplete": risk.data_incomplete,
        },
        "missing_information": bundle.missing_information,
    }


def fallback_body(request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment) -> str:
    """OpenAI 無法使用時的固定模板：只列出規則理由與缺少的資訊。"""
    lines = []
    best = bundle.product_match.best_match
    if best and bundle.product_match.status.value == "VERIFIED":
        lines.append(f"官方紀錄中有「{best.product.product_name_zh}」（{best.product.permit_number or '無字號'}）。")
    if request.missing_product_context:
        lines.append("目前不知道你指的是哪一項產品。")
    if bundle.approved_claims:
        functions = "、".join(sorted({e.function_name for e in bundle.approved_claims}))
        lines.append(f"官方核准的保健功效：{functions}。")
    lines.extend(f"・{note}" for note in bundle.product_match.notes)
    lines.extend(f"・{h.reason}" for h in risk.triggered_rules)
    if not risk.is_conclusive:
        lines.append("如果可以，請提供產品全名、包裝照片或許可證字號，我可以再幫你查。")
    return "\n".join(lines)


class ReporterAgent:
    def __init__(self, llm: LLMClient | None):
        self._llm = llm

    def report(
        self, request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment
    ) -> ReporterOutput:
        if request.intent == Intent.OUT_OF_SCOPE and not request.claims and not request.has_product_identifiers:
            return ReporterOutput(text=OUT_OF_SCOPE_TEXT + "\n如果你有想查證的保健食品或廣告說法，可以直接傳給我。")
        if ir.is_interaction_request(request, bundle) and ir.interaction_response_type(bundle) == ResponseType.INTERACTION_RESULT:
            return self._report_interaction(request, bundle)

        used_fallback = False
        body = ""
        if self._llm is not None:
            try:
                body = self._llm.generate_text(
                    instructions=REPORTER_INSTRUCTIONS,
                    input_text=json.dumps(build_evidence_summary(request, bundle, risk), ensure_ascii=False),
                )
            except ServiceError as exc:
                logger.warning(
                    "reporter_llm_failed",
                    extra={"request_id": request.request_id, "agent": "reporter", "error_code": exc.code.value},
                )
        if body and bundle.inferred_links and (OFFICIAL_CONFIRMATION_RE.search(body) or OWNERSHIP_CLAIM_RE.search(body)):
            logger.warning(
                "reporter_inferred_described_as_official",
                extra={"request_id": request.request_id, "agent": "reporter"},
            )
            body = ""
        if body and (verdict_contradiction(body, bundle)
                     or product_info_overclaims(body, select_response_type(request, bundle, risk))):
            logger.warning("reporter_verdict_contradiction", extra={"request_id": request.request_id, "agent": "reporter"})
            body = ""
        if not body:
            used_fallback = True
            body = fallback_body(request, bundle, risk)

        verdict = verdict_block(bundle)
        product = _verified_product(bundle)
        approved = _approved_claim_lines(bundle, product) if product is not None and bundle.claim_assessment else []
        approved_block = "官方核准內容：\n" + "\n".join(f"・{line}" for line in approved) if approved else ""
        parts = [full_analysis_header(request, bundle, risk), verdict, approved_block, body.strip()]
        disclaimers = [d for d in risk.required_disclaimers if d not in body]
        if bundle.inferred_links and INFERRED_LINK_NOTE not in body:
            disclaimers.append(INFERRED_LINK_NOTE)
        if disclaimers:
            parts.append("\n".join(disclaimers))
        sources_text = format_sources(bundle.all_sources())
        if sources_text:
            parts.append(sources_text)
        cited = sources_text.count("・")
        return ReporterOutput(text="\n\n".join(p for p in parts if p), used_fallback_template=used_fallback, cited_source_count=cited)

    def _report_interaction(self, request: NormalizedUserRequest, bundle: EvidenceBundle) -> ReporterOutput:
        """交互作用完整分析：LLM 只收到 interaction_research 的 structured facts（不重新查資料）；
        標題、限制 / 劑量未評估說明、來源由程式附上。"""
        def warn(event: str, code: str | None) -> None:
            logger.warning(event, extra={"request_id": request.request_id, "agent": "reporter", "error_code": code})

        body, used_fallback = ir.generate_body(self._llm, request, bundle, warn)
        sources_text = format_sources(bundle.all_sources())
        parts = [ir.headline_block(request, bundle), body, ir.evidence_digest(bundle), ir.limitation_block(bundle), sources_text]
        return ReporterOutput(text="\n\n".join(p for p in parts if p), used_fallback_template=used_fallback,
                              cited_source_count=sources_text.count("・"))

    def compose(self, request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment) -> ResponsePayload:
        return compose_payload(request, bundle, risk)

    def detail(
        self, action: ResponseAction, request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment
    ) -> DetailPayload:
        if action in (ResponseAction.INTERACTION_EVIDENCE, ResponseAction.INTERACTION_EVIDENCE_ALL,
                      ResponseAction.INTERACTION_MAPPING) and ir.is_interaction_request(
                request, bundle):  # 交互作用的證據 / 成分對應：只整理已保存的 interaction_research（deterministic，不查任何資料）
            if action == ResponseAction.INTERACTION_MAPPING:
                body, follow_ups = ir.mapping_detail(request, bundle), []
            elif action == ResponseAction.INTERACTION_EVIDENCE:  # 預設：簡短摘要（漸進揭露）
                body = ir.evidence_summary(request, bundle)
                follow_ups = ([ResponseAction.INTERACTION_EVIDENCE_ALL] if ir.evidence_total(bundle) > ir.EVIDENCE_SUMMARY_MAX_ITEMS
                              else []) + ([ResponseAction.SOURCES] if bundle.all_sources() else [])
            else:
                body, follow_ups = ir.evidence_detail(request, bundle), ([ResponseAction.SOURCES] if bundle.all_sources() else [])
            return DetailPayload(request_id=request.request_id, action=action, title=DETAIL_TITLES[action],
                                 query_text=request.normalized_text, body=body, available_actions=follow_ups)
        if action == ResponseAction.WHY and ir.is_interaction_request(request, bundle):  # 交互作用的「為什麼？」：deterministic
            return DetailPayload(request_id=request.request_id, action=action, title=DETAIL_TITLES[action],
                                 query_text=request.normalized_text, body=ir.why_detail(request, bundle))
        if action == ResponseAction.DETAIL:  # 只有完整分析需要 LLM；其他都是證據的 deterministic 整理
            out = self.report(request, bundle, risk)
            return DetailPayload(request_id=request.request_id, action=action, title=DETAIL_TITLES[action],
                                 query_text=request.normalized_text, body=[p for p in out.text.split("\n\n") if p.strip()],
                                 used_fallback_template=out.used_fallback_template)
        return detail_payload(action, request, bundle, risk)


# ---------------------------------------------------------------------------
# Primary ResponsePayload（deterministic）
# ---------------------------------------------------------------------------

TYPE_TITLES = {
    ResponseType.PRODUCT_INFO: "產品官方資訊",
    ResponseType.INGREDIENT_LIST: "成分資訊",
    ResponseType.CLAIM_RESULT: "說法查證結果",
    ResponseType.SOURCE_SUMMARY: "官方資料摘要",
    ResponseType.MISSING_INFORMATION: "需要更多資訊",
    ResponseType.OUT_OF_SCOPE: "呷安心可以幫你查什麼",
    ResponseType.ERROR: "這次沒有完成查詢",
}
EFFICACY_TITLE = "核准功效查詢"  # 功效詢問：第一層先回答功效（Intent-first）
DETAIL_TITLES = {
    ResponseAction.REGISTRATION_INFO: "產品登記資料",
    ResponseAction.DETAIL: "完整分析",
    ResponseAction.SOURCES: "資料來源",
    ResponseAction.INGREDIENTS: "完整成分",
    ResponseAction.OFFICIAL_INFO: "官方資訊",
    ResponseAction.WHY: "為什麼是這個結果？",
    ResponseAction.FULL_EFFICACY: "完整核准功效",
    ResponseAction.WARNINGS: "警語與注意事項",
    ResponseAction.POLICY_BASIS: "法規依據",
    ResponseAction.INTERACTION_EVIDENCE: "證據摘要",
    ResponseAction.INTERACTION_EVIDENCE_ALL: "全部證據來源",
    ResponseAction.INTERACTION_MAPPING: "成分對應",
}
MAX_EFFICACY_SUMMARY_CHARS = 40
MAX_RAW_INGREDIENT_CHARS = 900  # 明細段落上限 1000 字內
MAX_INGREDIENT_DETAIL_ITEMS = 40
INGREDIENT_DETAIL_LINES_PER_PARAGRAPH = 15
MAX_POLICY_BASIS_RULES = 5
MAX_POLICY_TEXT_CHARS = 80
POLICY_USE_NOTE = {
    PolicyEvidenceUse.CORROBORATION_REQUIRED: "（需其他官方資料佐證，不單獨作為結論）",
    PolicyEvidenceUse.CONTEXT_ONLY: "（僅供參考，仍待官方資料複核，不單獨作為結論）",
}
CLAIM_STATUS_SUMMARY = {
    ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL: "這個說法與官方核准的保健功效一致。",
    ClaimAssessmentStatus.UNSUPPORTED: "目前找不到官方資料支持這個說法。",
    ClaimAssessmentStatus.POTENTIALLY_EXAGGERATED: "這個說法可能誇大了官方核准的功效。",
    ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM: "這個說法涉及治療疾病，保健食品不能這樣宣稱。",
    ClaimAssessmentStatus.CONFLICT_WITH_OFFICIAL: "這個說法與官方資料不一致。",
    ClaimAssessmentStatus.UNKNOWN: "目前無法確認這個說法。",
    ClaimAssessmentStatus.NOT_APPLICABLE: "這個說法不需要查證官方功效。",
}
PRODUCT_STATUS_TEXT = {
    "ACTIVE": "許可證有效", "EXPIRED": "許可證已失效", "MERGED": "許可證已併證",
    "CANCELLED": "許可證已註銷", "REVOKED": "許可證已廢止", "UNKNOWN": "許可證狀態不明",
}


def product_status_text(product) -> str:
    """登記狀態文字。官方資料集未提供狀態的非健康食品類別：只說「未提供」＋有效日期（日期比較，非官方狀態）。"""
    if product.status.value == "UNKNOWN" and not has_health_claim_approval(product.product_category):
        return "；".join(t for t in (STATUS_NOT_PROVIDED_TEXT, expiry_date_text(product.expiry_date)) if t)
    return PRODUCT_STATUS_TEXT.get(product.status.value, "")


def registration_number_label(product) -> str:
    """健康食品 →「許可證字號」；其他類別依官方字號類型（查驗登記證 →「查驗登記證號」、核備函 →「核備函字號」）。"""
    if has_health_claim_approval(product.product_category) or not product.permit_type:
        return "許可證字號"
    return product.permit_type + ("號" if product.permit_type.endswith("證") else "字號")


def _flagged_inactive(product) -> bool:
    """候選要標示的非有效狀態：健康食品維持原規則（非 ACTIVE 皆標示）；其他類別只標示官方明確給出的非有效狀態。"""
    if has_health_claim_approval(product.product_category):
        return product.status.value != "ACTIVE"
    return is_officially_inactive(product.status)


NO_FUNCTION_EVIDENCE_TEXT = "目前沒有足夠的官方證據可以說明它的生理功能，所以不下結論。"
STANDALONE_ASK_FUNCTION = "想知道它的功能，可以直接問「{name}有什麼功能？」；想查含有它的產品，請提供產品全名。"
ASK_MORE_INFO = "可以提供產品全名、包裝照片或許可證字號，我再幫你查。"
REGISTRATION_NOT_SAFETY_NOTE = "官方紀錄代表產品有登記，不代表對每個人都安全；有疾病、懷孕或正在用藥，請先諮詢醫師或藥師。"
ASK_IMAGE_MORE_INFO = "可以補充許可證字號，或拍清楚包裝正面的產品名稱，我再幫你查。"
NOT_FOUND_NOT_FAKE = NOT_FOUND_COVERAGE_TEXT  # 說明目前查詢涵蓋的官方類別（不是所有一般食品）
ASK_WHICH_PRODUCT = "請回覆你要查的是哪一項（可直接傳完整品名或許可證字號）。"
NOT_LISTED_NOTE = "如果都不是，可能目前資料中沒有這項產品的官方紀錄（查不到不代表是假貨）。"
PICK_BELOW = "請直接點選下方你要查的品項。"
COLLISION_SUMMARY = "你輸入的名稱同時是一種成分，也是官方登記的產品名稱，我不確定你想查哪一個。"
COLLISION_ASK = "請告訴我你想查的是「產品」還是「成分」（例如在名稱後面加上「產品」或「成分」）。"
MAX_CHOICE_LABEL_CHARS = 16
_LABEL_TRIM = " ()（）[]【】-－_／/・"
ERROR_SUMMARY = "系統暫時無法完成這次查詢，請稍後按「重新查詢」。"


CLAIM_LEVEL_HIGH_NOTE = "目前還不能確認是哪一項產品；這裡的高風險是針對廣告／包裝上的宣稱內容，不是判定產品真偽。"
CLAIMS_NEED_PRODUCT = "包裝／廣告上的宣稱要先確認是哪一項產品，才能和該產品的官方核准功效逐條比對。"
_POLICY_ROLE_LABEL = {
    ClaimOrigin.PRODUCT_LABEL_CLAIM: "包裝宣稱", ClaimOrigin.ADVERTISEMENT_CLAIM: "廣告宣稱",
    ClaimOrigin.THIRD_PARTY_CLAIM: "流傳說法", ClaimOrigin.USER_ASSERTION: "這種說法", ClaimOrigin.OFFICIAL_CLAIM: "引述說法",
}


def claim_policy_line(c: ClaimAssessment) -> str:
    """單一宣稱的固定模板說明：原始宣稱、claim role、policy 結果、對應的官方核准功效 / 超出的部分。
    只使用 ClaimPolicyDecision 中已有的內容，不自行創造法律結論。"""
    policy = c.policy
    label = _POLICY_ROLE_LABEL.get(c.origin, "說法")
    if policy is None:
        return f"{label}「{c.claim_text}」：{c.explanation}" if c.explanation else ""
    approved = policy.matched_approved_claim_text
    result = policy.policy_result
    if result in (ClaimPolicyResult.APPROVED_MATCH, ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT) and approved:
        line = f"{label}「{c.claim_text}」：{policy.reason}（官方核准：{approved}）"
    elif result == ClaimPolicyResult.EXCEEDS_APPROVED_SCOPE and approved:
        line = f"{label}「{c.claim_text}」：官方核准的是「{approved}」，但這項宣稱擴大了範圍。"
        if policy.difference:
            line += f"（{policy.difference}）"
    else:
        line = f"{label}「{c.claim_text}」：{policy.reason}"
    if policy.product_identity_unresolved and result == ClaimPolicyResult.MEDICAL_TREATMENT_CLAIM:
        line += NOTE_CLAIM_LEVEL_ONLY
    return line


def _asserted_claim_lines(bundle: EvidenceBundle) -> list[str]:
    return [claim_policy_line(c) for c in bundle.claim_assessment if c.origin != ClaimOrigin.USER_QUESTION]


_RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


def claim_verdict_line(c: ClaimAssessment) -> str:
    """單一命題的判定文字（primary card / 為什麼 / 完整分析共用，只讀 ClaimAssessment，不重新推論）。

    USER_QUESTION 寫成「你問的…」；醫療效能命題只說「若當成產品功效宣稱屬高風險說法」，不說使用者違法、不說產品危險。
    """
    mark = f"{RISK_EMOJI[c.claim_risk]} " if c.claim_risk else ""
    if c.origin != ClaimOrigin.USER_QUESTION:
        return mark + claim_policy_line(c)
    policy = c.policy
    if c.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM:
        return f"{mark}你問的「{c.claim_text}」：{QUESTION_MEDICAL_PROPOSITION}"
    reason = (policy.reason if policy else c.explanation) or CLAIM_STATUS_SUMMARY[c.status]
    line = f"{mark}你問的「{c.claim_text}」：{reason}"
    if c.status == ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL and policy is not None:
        if policy.matched_function_name:  # 摘要只標功效類別；完整核准原文在完整分析 / 完整功效明細
            line += f"（官方核准功效：{policy.matched_function_name}）"
        elif policy.matched_approved_claim_text:
            line += f"（官方核准：{policy.matched_approved_claim_text}）"
    return line


def worst_claim(bundle: EvidenceBundle) -> ClaimAssessment | None:
    rated = [c for c in bundle.claim_assessment if c.claim_risk is not None]
    return max(rated, key=lambda c: _RISK_ORDER[c.claim_risk], default=None)


def claim_verdict_summary(bundle: EvidenceBundle, product) -> str:
    """說法查證的一句結論：取風險最高的命題（與重點列、為什麼、完整分析使用同一份 ClaimAssessment）。"""
    worst = worst_claim(bundle)
    if worst is None:
        return "目前無法確認這個說法。"
    if worst.claim_risk == RiskLevel.HIGH:
        if worst.origin == ClaimOrigin.USER_QUESTION:
            return "你問的這句話若當成產品功效宣稱，屬高風險說法（不是指你違法，也不是指產品本身危險）。"
        return "這項宣稱屬高風險說法，保健食品不能這樣宣稱。"
    if worst.claim_risk == RiskLevel.LOW:
        policy = worst.policy
        if product is not None and policy is not None and policy.matched_approved_evidence_id:
            return f"這個說法在「{product.product_name_zh}」官方核准的保健功效範圍內。"
        return "這個說法與官方資料一致。"
    return CLAIM_STATUS_SUMMARY[worst.status]


def verdict_block(bundle: EvidenceBundle) -> str:
    if not bundle.claim_assessment:
        return ""
    return "查證結論：\n" + "\n".join(claim_verdict_line(c) for c in bundle.claim_assessment)


def full_analysis_header(request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment) -> str:
    """完整分析標題與 primary card 一致：說法查證顯示宣稱風險，其他型態顯示原本的風險標題。"""
    worst = worst_claim(bundle)
    rtype = select_response_type(request, bundle, risk)
    if rtype == ResponseType.CLAIM_RESULT and worst is not None:
        return f"{TYPE_TITLES[ResponseType.CLAIM_RESULT]}｜{CLAIM_RISK_LABEL[worst.claim_risk]}"
    if rtype == ResponseType.RISK:
        return build_header(risk)
    return TYPE_TITLES.get(rtype, build_header(risk))


# 完整分析（LLM）不得與 deterministic verdict 相反；矛盾時改用固定模板
_SAYS_UNSUPPORTED_RE = re.compile(r"找不到[^。，\n]{0,6}官方[^。，\n]{0,4}支持|沒有[^。，\n]{0,4}官方[^。，\n]{0,4}支持|不在[^。，\n]{0,4}核准[^。，\n]{0,2}範圍")
_SAYS_APPROVED_RE = re.compile(r"(?<!不)(?:在|屬於)[^。，\n]{0,6}核准[^。，\n]{0,4}範圍|與[^。，\n]{0,8}核准[^。，\n]{0,6}一致")


_SAYS_PRODUCT_SAFE_RE = re.compile(r"(?<!不是)(?<!不算)低風險|風險(?:很|較|偏)低|(?:很|相當|非常|十分)安全")


def product_info_overclaims(body: str, rtype: ResponseType) -> bool:
    """產品資訊 / 成分類回答只能描述官方紀錄，不可推論產品本身低風險或安全。"""
    return rtype in (ResponseType.PRODUCT_INFO, ResponseType.INGREDIENT_LIST) and bool(_SAYS_PRODUCT_SAFE_RE.search(body))


def verdict_contradiction(body: str, bundle: EvidenceBundle) -> bool:
    risks = {c.claim_risk for c in bundle.claim_assessment if c.claim_risk is not None}
    if risks == {RiskLevel.LOW}:
        return bool(_SAYS_UNSUPPORTED_RE.search(body))
    if risks == {RiskLevel.HIGH}:
        return bool(_SAYS_APPROVED_RE.search(body))
    return False


def identity_unresolved(request: NormalizedUserRequest, bundle: EvidenceBundle) -> bool:
    """還不知道要查的是哪一項產品（不是產品本身有風險）：
    指稱無法解析、多個 / 低信心候選、只找到名稱相似或只對應到公司、只傳圖片但沒有確認產品、
    問產品是否合法卻完全沒有產品資訊。這些情況回 MISSING_INFORMATION，不顯示風險燈號。"""
    match = bundle.product_match
    if (request.identity_collision or request.image_bundle_blocked or request.missing_product_context
            or match.is_ambiguous or match.needs_confirmation):
        return True
    if request.image_identification:
        return match.status != ProductVerificationStatus.VERIFIED
    if match.status == ProductVerificationStatus.PARTIAL_MATCH:
        return True
    return (match.status == ProductVerificationStatus.NOT_APPLICABLE and request.intent == Intent.PRODUCT_VERIFICATION
            and not request.ingredient_candidates)


def product_not_found(request: NormalizedUserRequest, bundle: EvidenceBundle) -> bool:
    """使用者有給產品識別資訊，但查無目前涵蓋的官方紀錄（或查詢失敗）。"""
    return request.has_product_identifiers and bundle.product_match.status == ProductVerificationStatus.NOT_FOUND


def select_response_type(request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment) -> ResponseType:
    """一般化規則：依 intent 與證據決定 template；高風險或產品方宣稱一律用風險卡，不會被其他 template 隱藏。

    MEDIUM 風險卡只用於「已有明確查證對象、但證據不完整或不確定」；查證對象本身還沒確定時用 MISSING_INFORMATION。
    """
    if request.intent == Intent.OUT_OF_SCOPE and not request.claims and not request.has_product_identifiers:
        return ResponseType.OUT_OF_SCOPE
    if ir.is_interaction_request(request, bundle):
        # 交互作用：不經風險卡（產品風險與 interaction finding 分開）；對象未全部確認時先請使用者確認
        return ir.interaction_response_type(bundle)
    if risk.risk_level == RiskLevel.HIGH:
        return ResponseType.RISK
    if any(c.is_product_side_assertion for c in request.claims) and not (
        bundle.product_match.is_ambiguous or bundle.product_match.needs_confirmation
    ):
        return ResponseType.RISK
    if request.missing_product_context or request.identity_collision or request.image_bundle_blocked:
        return ResponseType.MISSING_INFORMATION
    if (bundle.product_match.is_ambiguous or bundle.product_match.needs_confirmation) and request.claims:
        # 產品有多個 / 低信心候選、宣稱沒有高風險：先請使用者確認產品，再逐條比對核准功效（不直接給風險燈）
        return ResponseType.MISSING_INFORMATION
    if identity_unresolved(request, bundle) and not request.claims:
        # 多個合理候選、低信心候選、圖片無法確認產品、只有相似名稱：不猜是哪一項，請使用者確認或補充
        return ResponseType.MISSING_INFORMATION
    if not request.claims and product_not_found(request, bundle):
        # 查無目前涵蓋的官方紀錄（或查詢失敗）：產品身份未確認，不是風險判定 → 不顯示風險燈號，請使用者補充
        return ResponseType.MISSING_INFORMATION
    if bundle.ingredient_identity_status is not None and not request.has_product_identifiers and not request.claims:
        return ResponseType.INGREDIENT_LIST  # 獨立成分 / 營養素：entity identity 已確認（不是產品卡，也不是 NOT_FOUND）
    if request.image_identification and not request.claims:
        # 只傳圖片且產品已確認：無風險 → 產品資訊；有風險（例如許可證失效）→ 風險卡
        return ResponseType.PRODUCT_INFO if risk.risk_level == RiskLevel.LOW else ResponseType.RISK
    if request.intent in (Intent.PRODUCT_VERIFICATION, Intent.SAFETY_INQUIRY):
        # 查有官方紀錄不等於「產品本身低風險」：沒有命題、也沒有任何風險發現（LOW）時只呈現官方紀錄；
        # 許可證失效、違規廣告紀錄、資訊衝突、查無紀錄等實際發現才用風險卡
        if (not request.claims and risk.risk_level == RiskLevel.LOW
                and bundle.product_match.status == ProductVerificationStatus.VERIFIED):
            return ResponseType.PRODUCT_INFO
        return ResponseType.RISK
    if request.claims:
        return ResponseType.CLAIM_RESULT
    verified = bundle.product_match.status == ProductVerificationStatus.VERIFIED
    if request.intent == Intent.INGREDIENT_INQUIRY and (bundle.ingredients or request.ingredient_candidates or verified):
        return ResponseType.INGREDIENT_LIST  # 問成分 → 第一層先回答成分（沒有已整理的成分也直接說明）
    if verified:
        return ResponseType.PRODUCT_INFO
    if request.has_product_identifiers:  # 有指定產品但無法確認：請使用者補充，不給風險燈號
        return ResponseType.MISSING_INFORMATION
    if request.ingredient_candidates and _standalone_ingredient_names(request, bundle):
        return ResponseType.INGREDIENT_LIST
    if bundle.rag_evidence or bundle.official_web_evidence:
        return ResponseType.SOURCE_SUMMARY
    return ResponseType.MISSING_INFORMATION


def _verified_product(bundle: EvidenceBundle):
    best = bundle.product_match.best_match
    return best.product if best and bundle.product_match.status == ProductVerificationStatus.VERIFIED else None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i.strip() for i in items if i and i.strip()))


def _product_ingredients(bundle: EvidenceBundle) -> list[str]:
    """已確認產品的官方登記成分（不含不針對產品的成分名稱查詢結果）。"""
    return _dedupe([f"{i.name_zh}（{i.name_en}）" if i.name_en else i.name_zh for i in bundle.ingredients])


def _ingredient_key(text: str | None) -> str:
    return normalize_name((text or "").replace("維他命", "維生素"))


def _entity_type_label(graph_label: str) -> str:
    return {"NutrientGroup": "營養素", "IngredientForm": "成分（型態）"}.get(graph_label, "成分")


def _standalone_entity_names(bundle: EvidenceBundle) -> list[str]:
    return _dedupe([f"{e.display_name_zh}（{e.canonical_name_en}）" if e.display_name_zh else e.canonical_name_en
                    for e in bundle.standalone_entities])


def _standalone_entity_overview(bundle: EvidenceBundle) -> tuple[str, list[str]]:
    """獨立成分 / 營養素的精簡概覽（只依結構化 identity 與功能證據狀態；不是產品卡）。
    生理功能只在有官方證據時才說；不稱為產品核准保健功效。"""
    names = _standalone_entity_names(bundle)
    kinds = _dedupe([_entity_type_label(e.entity_type) for e in bundle.standalone_entities])
    summary = f"「{'、'.join(names)}」是目前證據庫中已確認的{'、'.join(kinds)}（這不是指任何一項產品）。"
    status = bundle.function_evidence_status
    if status == FunctionEvidenceStatus.SUPPORTED:
        count = len(bundle.rag_evidence) + len(bundle.official_web_evidence)
        points = [f"找到 {count} 份相關的官方資料，可以按「查看完整分析」了解。"]
    elif status == FunctionEvidenceStatus.NO_FUNCTION_EVIDENCE:
        points = [NO_FUNCTION_EVIDENCE_TEXT]
    else:
        points = [STANDALONE_ASK_FUNCTION.format(name=bundle.standalone_entities[0].query_text)]
    return summary, points


def _identity_not_in_corpus_overview(request: NormalizedUserRequest, bundle: EvidenceBundle) -> tuple[str, list[str]]:
    """結構化 identity 資料沒有收錄這個名稱：不說「不存在 / 找不到成分」，只說識別資料尚未收錄；功能證據另外交代。"""
    name = "、".join(request.ingredient_candidates)
    summary = f"目前的成分識別資料還沒有收錄「{name}」，這不代表它不存在或有問題。"
    if bundle.function_evidence_status == FunctionEvidenceStatus.SUPPORTED:
        count = len(bundle.rag_evidence) + len(bundle.official_web_evidence)
        return summary, [f"不過找到 {count} 份相關的官方資料，可以按「查看完整分析」了解。"]
    return summary, [NO_FUNCTION_EVIDENCE_TEXT]


def _standalone_ingredient_names(request: NormalizedUserRequest, bundle: EvidenceBundle) -> list[str]:
    """不針對產品的成分查詢：只列官方成分名稱與使用者所問「完全相同」者（相似但不同的成分不列，例如問 A 不列 E）。"""
    asked = {_ingredient_key(t) for t in request.ingredient_candidates} - {""}
    return _dedupe([f"{i.name_zh}（{i.name_en}）" if i.name_en else i.name_zh for i in bundle.standalone_ingredients
                    if asked & {_ingredient_key(i.name_zh), _ingredient_key(i.name_en)}])


# ---------------------------------------------------------------------------
# 成分回答：只由 ProductIngredientEvidence 的結構化事實組成（官方列有 / 已確認 identity / 尚未確認分開）。
# 依使用者問的是「有哪些」或「有沒有某成分」選擇要說的事實；不顯示內部 entity ID，不新增成分、不做翻譯合併。
# ---------------------------------------------------------------------------


def _ingredient_evidence(bundle: EvidenceBundle) -> ProductIngredientEvidence | None:
    evidence = bundle.product_ingredients
    return evidence if evidence is not None and evidence.official_ingredients else None


def _entity_name(entity: IngredientEntityRef | None) -> str:
    return "" if entity is None else (entity.display_name_zh or entity.canonical_name_en)


def _ingredient_scope(evidence: ProductIngredientEvidence) -> str:
    """官方成分欄位的範圍：健康食品登記的是「保健功效相關成分」，查驗登記／核備類別是官方成分欄位。"""
    return "保健功效相關成分" if has_health_claim_approval(evidence.product_category) else "成分"


def _confirmed_note(entry: ConfirmedIngredientEntry, raw: str) -> str:
    """已確認成分的補充說明：成分形式 → 所屬營養素（CONTRIBUTES_TO_NUTRIENT，不是同一種成分）。"""
    name = _entity_name(entry.entity)
    if entry.parent_nutrient is not None:
        return f"{_entity_name(entry.parent_nutrient)}的來源形式"
    return "" if normalize_name(name) == normalize_name(raw) else name


def _direct_confirmed_by_raw(evidence: ProductIngredientEvidence) -> dict[str, ConfirmedIngredientEntry]:
    return {raw: c for c in evidence.confirmed_ingredients if c.is_direct for raw in c.raw_texts}


def _ingredient_display_lines(evidence: ProductIngredientEvidence) -> list[str]:
    """成分清單：已確認身分的放前面（附所屬營養素），其餘依官方順序；只用官方原文與 entity 顯示名稱。"""
    confirmed = _direct_confirmed_by_raw(evidence)
    first, rest = [], []
    for o in evidence.official_ingredients:
        # 只顯示官方登記的成分名稱（原樣）；成分身分 / 來源形式等 mapping metadata 是內部資料，不給使用者看
        (first if o.raw_text in confirmed else rest).append(o.display_text)
    return _dedupe(first + rest)


def _answer_sentence(answer: IngredientQueryAnswer, product_name: str, scope: str) -> tuple[str, list[str]]:
    """一個「有沒有」問題的直接回答＋必要補充；只依 presence 與其附帶的官方原文 / entity 名稱。"""
    asked = answer.query_text
    extra: list[str] = []
    if answer.presence == IngredientPresence.CONFIRMED:
        direct = next((m for m in answer.matched if m.is_direct), None)
        if direct is not None:
            raw = "、".join(direct.raw_texts)
            note = _confirmed_note(direct, raw)
            return f"有。「{product_name}」官方成分中列有「{raw}」" + (f"（{note}）" if note else "") + "。", extra
        via = answer.matched[0]
        raw = "、".join(via.raw_texts)
        form = _entity_name(via.via_entity)
        same_name = normalize_name(form) == normalize_name(raw)
        return (f"有。「{product_name}」官方成分中的「{raw}」" + ("" if same_name else f"（{form}）")
                + f"是{_entity_name(answer.query_entity) or asked}的來源形式，不是單獨列出{asked}。"), extra
    if answer.presence == IngredientPresence.LISTED_IN_OFFICIAL_TEXT:
        listed = "、".join(answer.listed_raw_texts)
        summary = f"「{product_name}」官方登記的{scope}中列有「{listed}」。"
        if answer.query_entity is not None:
            extra.append("這項成分的身分目前尚未完成確認，只能確定官方資料列有這個名稱。")
        return summary, extra
    if answer.presence == IngredientPresence.CANDIDATE_ONLY:
        candidates = "、".join(answer.candidate_raw_texts[:3])
        extra.append(f"官方成分中的「{candidates}」可能與{asked}有關，但尚未完成確認，不能視為含有{asked}。")
        return f"目前無法確認「{product_name}」含有{asked}。", extra
    for related in answer.related_confirmed[:2]:
        raw = "、".join(related.raw_texts)
        note = _confirmed_note(related, raw)
        extra.append(f"已確認的相關成分是「{raw}」" + (f"（{note}）" if note else "") + f"，和{asked}不是同一種成分。")
    if answer.resolution == IngredientQueryResolution.AMBIGUOUS:
        extra.append(f"「{asked}」可能指不同的成分，可以說得更具體一點（例如完整成分名稱）。")
    if answer.unresolved_remaining:
        extra.append(f"另有 {answer.unresolved_remaining} 項官方成分尚未完成識別，因此不能完全排除。")
    return f"「{product_name}」目前已確認的{scope}中，沒有找到{asked}。", extra


def _ingredient_answer(evidence: ProductIngredientEvidence, product) -> tuple[str, list[str]]:
    """成分詢問的第一層：有問特定成分 → 先回答有沒有；問全部成分 → 官方列有的成分（已確認的先列）。"""
    scope = _ingredient_scope(evidence)
    if evidence.query_answers:
        summary, points = _answer_sentence(evidence.query_answers[0], product.product_name_zh, scope)
        for answer in evidence.query_answers[1:]:
            line, extra = _answer_sentence(answer, product.product_name_zh, scope)
            points = [line] + points + extra
        return summary, _dedupe(points)
    cov = evidence.coverage
    summary = f"「{product.product_name_zh}」官方登記的{scope}共 {cov.official_count} 項。"
    return summary, _ingredient_display_lines(evidence)


def _ingredient_status(evidence: ProductIngredientEvidence) -> tuple[str, SemanticTone]:
    if not evidence.query_answers:
        return "官方登記成分", SemanticTone.GREEN
    presence = evidence.query_answers[0].presence
    if presence == IngredientPresence.CONFIRMED:
        return "已確認含有", SemanticTone.GREEN
    if presence == IngredientPresence.LISTED_IN_OFFICIAL_TEXT:
        return "官方成分列有", SemanticTone.GREEN
    return "目前未能確認", SemanticTone.ORANGE


def ingredient_detail_lines(evidence: ProductIngredientEvidence) -> list[str]:
    """「查看完整成分」：只列官方登記的成分名稱（原樣，不換成營養素類別、不標示成分身分 / 來源形式 / 未識別數量）。

    每段多行（renderer 一段一個區塊）：成分很多時分成幾段，不會被明細段落上限截掉；
    清單已完整列出時不再重複官方成分欄位原文；只有超過 MAX_INGREDIENT_DETAIL_ITEMS 而被截斷時才附上原文。"""
    items = [f"・{o.display_text}" for o in evidence.official_ingredients]
    shown = items[:MAX_INGREDIENT_DETAIL_ITEMS]
    # 標題反映來源範圍：健康食品登記的是「保健功效相關成分」（不是完整配方），其餘是官方成分欄位；不推論超出來源的完整性
    header = (f"保健功效相關成分（{evidence.coverage.official_count} 項）"
              if _ingredient_scope(evidence) == "保健功效相關成分" else f"官方登記成分（{evidence.coverage.official_count} 項）")
    chunks = [shown[i:i + INGREDIENT_DETAIL_LINES_PER_PARAGRAPH]
              for i in range(0, len(shown), INGREDIENT_DETAIL_LINES_PER_PARAGRAPH)] or [[]]
    paragraphs = ["\n".join(([header] if i == 0 else []) + chunk) for i, chunk in enumerate(chunks)]
    raw = evidence.official_ingredients_raw
    if len(items) > len(shown):
        paragraphs.append(f"另有 {len(items) - len(shown)} 項，可參考下方官方成分欄位原文。")
        if raw:
            paragraphs.append(f"官方成分欄位原文：{_clip_text(raw, MAX_RAW_INGREDIENT_CHARS)}")
    return paragraphs


def ingredient_evidence_summary(evidence: ProductIngredientEvidence | None) -> dict | None:
    """完整分析（LLM）的成分輸入：只有官方原文、entity 顯示名稱與狀態，不含內部 ID。"""
    if evidence is None:
        return None
    confirmed = [{
        "official_text": "、".join(c.raw_texts), "ingredient": _entity_name(c.entity),
        "nutrient_of_this_form": _entity_name(c.parent_nutrient) or None,
        "derived_from_form": _entity_name(c.via_entity) or None,
    } for c in evidence.confirmed_ingredients]
    return {
        "ingredient_scope": _ingredient_scope(evidence),
        "official_ingredients": [o.display_text for o in evidence.official_ingredients],
        "confirmed_ingredients": confirmed,
        "unresolved_ingredients": [u.display_text for u in evidence.unresolved_ingredients],
        "coverage": evidence.coverage.model_dump(),
        "ingredient_answers": [{
            "asked": a.query_text, "presence": a.presence.value,
            "asked_ingredient": _entity_name(a.query_entity) or None,
            "matched_official_text": [t for m in a.matched for t in m.raw_texts],
            "related_confirmed_not_same": ["、".join(r.raw_texts) for r in a.related_confirmed],
            "unconfirmed_candidates": a.candidate_raw_texts, "listed_official_text": a.listed_raw_texts,
            "unresolved_remaining": a.unresolved_remaining,
        } for a in evidence.query_answers],
    }


class ApprovedClaimLine(BaseModel):
    """畫面上的一條核准功效：同一份官方敘述只出現一次，所有功效類別與證據來源都保留。"""

    function_names: list[str]
    claim_text: str
    evidence_ids: list[str]
    sources: list[SourceRef]

    def display(self, template: str = "{functions}：{text}") -> str:
        functions = "、".join(self.function_names)
        return template.format(functions=functions, text=self.claim_text) if self.claim_text else functions


def dedupe_approved_claims(claims: list[ApprovedClaimEvidence]) -> list[ApprovedClaimLine]:
    """Response 層的 deterministic 去重（不改 DB / EvidenceBundle）。

    1. canonical claim ID（approved_claim_id）相同且官方敘述相同 → 同一條（多筆來源合併）
    2. 不同 ID 但正規化後官方敘述完全相同 → 敘述只顯示一次，功效類別合併列出
    敘述字面不同（即使意思可能相近）一律分開，不推測語意。沒有敘述時只以 ID 合併。
    """
    lines: dict[str, ApprovedClaimLine] = {}
    for e in claims:
        text_key = normalize_name(e.claim_text)
        key = f"text:{text_key}" if text_key else f"id:{e.approved_claim_id}"
        line = lines.get(key)
        if line is None:
            lines[key] = ApprovedClaimLine(function_names=[e.function_name], claim_text=e.claim_text.strip(),
                                           evidence_ids=[e.evidence_id], sources=[e.source])
            continue
        if e.function_name not in line.function_names:
            line.function_names.append(e.function_name)
        if e.evidence_id not in line.evidence_ids:
            line.evidence_ids.append(e.evidence_id)
        if all((x.source_name, x.source_url) != (e.source.source_name, e.source.source_url) for x in line.sources):
            line.sources.append(e.source)
    return list(lines.values())


def _approved_function_names(bundle: EvidenceBundle, product) -> list[str]:
    return _dedupe([f for line in dedupe_approved_claims(
        [e for e in bundle.approved_claims if e.product_id == product.product_id]) for f in line.function_names])


def _approved_claim_lines(bundle: EvidenceBundle, product) -> list[str]:
    """已確認產品的官方核准功效（功效類別：核准敘述），只取自 EvidenceBundle；相同敘述只出現一次。"""
    return _dedupe([line.display() for line in dedupe_approved_claims(
        [e for e in bundle.approved_claims if e.product_id == product.product_id])])


def _short_labels(names: list[str]) -> list[str | None]:
    """一般性規則：去掉候選品名的共同前綴，剩下的部分（規格 / 口味）就是能區分品項的短名稱。
    品名完全相同或無法區分時回傳 None（改用許可證字號區分）。"""
    if len(names) < 2 or len(set(names)) < len(names):
        return [None] * len(names)
    prefix = os.path.commonprefix(names)
    labels = [name[len(prefix):].strip(_LABEL_TRIM) for name in names]
    if not all(labels) or len(set(labels)) < len(labels):
        return [None] * len(names)
    return labels


def product_choices(bundle: EvidenceBundle) -> list[ProductChoice]:
    """產品確認卡的候選按鈕：只取自該 request 的 ProductMatch.candidates。"""
    match = bundle.product_match
    if not (match.is_ambiguous or match.needs_confirmation):
        return []
    products = list({c.product.product_id: c.product for c in match.candidates}.values())
    shorts = _short_labels([p.product_name_zh for p in products])
    choices = []
    for product, short in zip(products, shorts):
        code = extract_permit_code(product.permit_number or "")
        label = short or (code if len(set(p.product_name_zh for p in products)) < len(products) and code else None)
        label = label or product.product_name_zh
        active = not _flagged_inactive(product)
        health_food = has_health_claim_approval(product.product_category)
        choices.append(ProductChoice(
            product_id=product.product_id, label=label[:MAX_CHOICE_LABEL_CHARS], full_name=product.product_name_zh,
            permit_number=product.permit_number, is_active=active,
            status_text=None if active else PRODUCT_STATUS_TEXT.get(product.status.value, "許可證狀態不明"),
            number_label=registration_number_label(product),
            category_label=None if health_food else CATEGORY_LABELS[product.product_category],
        ))
    return choices


def _candidate_option(product) -> str:
    """候選項目：品名＋（非健康食品類別）＋字號；非有效狀態一併標示，讓同名不同字號 / 跨類別的紀錄可以分辨。"""
    category = None if has_health_claim_approval(product.product_category) else CATEGORY_LABELS[product.product_category]
    details = [d for d in (category, product.permit_number,
                           PRODUCT_STATUS_TEXT.get(product.status.value) if _flagged_inactive(product) else None) if d]
    return f"{product.product_name_zh}（{'，'.join(details)}）" if details else product.product_name_zh


NOT_FOUND_SUMMARY = "目前在系統涵蓋的台灣官方資料中，沒有找到相符的產品。"
NOT_FOUND_ASK = "提供完整品名、包裝照片或證號，可以再次查詢。"
LOOKUP_FAILED_RETRY = "可以稍後按「重新查詢」，或提供完整品名、包裝照片或證號。"
NO_EFFICACY_REGISTRATION_TEMPLATE = ("「{name}」查有官方查驗登記／核備紀錄，但這類登記不等於健康食品許可，"
                                     "因此不能由這筆資料認定有核准保健功效。")


def _identity_support_note(bundle: EvidenceBundle) -> str:
    """NOT_FOUND 的結構化原因 NO_PRODUCT_IDENTITY_SUPPORT：說明為什麼沒有列出名稱相似的產品。"""
    match = bundle.product_match
    if match.not_found_reason != ProductNotFoundReason.NO_PRODUCT_IDENTITY_SUPPORT:
        return ""
    terms = "、".join(f"「{t}」" for t in match.unsupported_identity_terms[:2])
    if not terms:
        return "名稱相近的官方紀錄都對不上你寫的產品名稱，因此不列為候選。"
    return (f"名稱相近的官方紀錄中，沒有任何一項對得上你寫的{terms}；只是品牌相同或成分、劑型字樣相同的產品"
            "不會列為候選。")


def _efficacy_answer(bundle: EvidenceBundle, product) -> tuple[str, list[str]]:
    """功效詢問（Intent-first）：第一層只回答功效；申請商 / 字號 / 日期放下一層按鈕。"""
    if not has_health_claim_approval(product.product_category):
        # 只說「這筆登記沒有健康食品核准功效」，不說產品沒有任何功效
        return NO_EFFICACY_REGISTRATION_TEMPLATE.format(name=product.product_name_zh), [
            f"資料類別：{CATEGORY_LABELS[product.product_category]}"]
    names = _approved_function_names(bundle, product)
    if not names:
        return f"官方資料中沒有登記「{product.product_name_zh}」的核准保健功效。", []
    return f"「{product.product_name_zh}」的官方核准保健功效：", names


def status_for(rtype: ResponseType, request: NormalizedUserRequest, bundle: EvidenceBundle) -> tuple[str, SemanticTone] | None:
    """直接回答使用者所問內容的關鍵短語與固定語意顏色（GREEN 官方確認 / ORANGE 資料不足或登記≠核准 / RED 高風險）。

    查無紀錄一律 ORANGE（不是違法或假貨）；風險卡與說法查證卡已有自己的風險標示，不另加。"""
    product = _verified_product(bundle)
    if rtype == ResponseType.MISSING_INFORMATION:
        if product_not_found(request, bundle):
            if not bundle.product_match.lookup_completed:
                return "暫時無法完成產品查詢", SemanticTone.ORANGE
            return "未查到對應官方紀錄", SemanticTone.ORANGE
        return None
    if product is None:
        return None
    health_food = has_health_claim_approval(product.product_category)
    if rtype == ResponseType.PRODUCT_INFO and request.intent == Intent.EFFICACY_INQUIRY:
        if not health_food:
            return "未有健康食品核准保健功效資料", SemanticTone.ORANGE
        if _approved_function_names(bundle, product):
            return "官方核准功效", SemanticTone.GREEN
        return "官方資料未列核准保健功效", SemanticTone.ORANGE
    if rtype == ResponseType.PRODUCT_INFO:
        if not health_food:
            return "查有官方查驗登記／核備紀錄", SemanticTone.GREEN
        if product.status.value == "ACTIVE":
            return "查有健康食品許可（許可證有效）", SemanticTone.GREEN
        return "查有健康食品紀錄，許可狀態需確認", SemanticTone.ORANGE
    if rtype == ResponseType.INGREDIENT_LIST:
        evidence = _ingredient_evidence(bundle)
        if evidence is not None and evidence.product_id == product.product_id:
            return _ingredient_status(evidence)
        if _product_ingredients(bundle):
            return "官方登記成分", SemanticTone.GREEN
        return "未有已整理的成分資料", SemanticTone.ORANGE
    return None


def _registration_only_product_info(request: NormalizedUserRequest, product) -> tuple[str, list[str]]:
    """非健康食品類別（查驗登記／核備資料）：只說查有官方登記，不提核准保健功效；沒有核准功效不是異常。"""
    category = CATEGORY_LABELS[product.product_category]
    summary = (f"官方紀錄中有「{product.product_name_zh}」（{category}），{REGISTRATION_RECORD_TEXT}，"
               f"{registration_number_label(product)} {product.permit_number or '無'}。")
    points = [f"申請商：{product.company_name}" if product.company_name else "", product_status_text(product)]
    if request.intent == Intent.EFFICACY_INQUIRY:
        points.insert(0, NO_HEALTH_CLAIM_APPROVAL_TEXT)
    else:
        points.append(NO_HEALTH_CLAIM_APPROVAL_TEXT)
    if request.observed_package_claims:
        points.append(f"包裝上另有 {len(request.observed_package_claims)} 項宣稱文字，需要的話可按「檢視包裝宣稱」。")
    if request.intent == Intent.SAFETY_INQUIRY:
        points.insert(0, REGISTRATION_NOT_SAFETY_NOTE)
    return summary, _dedupe(points)


def _summary_and_points(
    rtype: ResponseType, request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment
) -> tuple[str, list[str]]:
    product = _verified_product(bundle)
    if rtype == ResponseType.RISK:
        claim_level_high = (risk.risk_level == RiskLevel.HIGH and product is None and any(
            c.policy and c.policy.product_identity_unresolved for c in bundle.claim_assessment))
        if claim_level_high:
            summary = CLAIM_LEVEL_HIGH_NOTE
        elif product is not None:
            category = ("" if has_health_claim_approval(product.product_category)
                        else CATEGORY_LABELS[product.product_category] + "，")
            summary = f"官方紀錄中有「{product.product_name_zh}」（{category}{product.permit_number or '無字號'}）。"
        elif not risk.is_conclusive:
            summary = "目前資訊不足，暫時無法確認。"
        else:
            summary = risk.triggered_rules[0].reason if risk.triggered_rules else risk_title(risk) + "。"
        points = [claim_verdict_line(c) for c in bundle.claim_assessment] + [h.reason for h in risk.triggered_rules]
        if not risk.is_conclusive:
            points.append(ASK_MORE_INFO)
        return summary, _dedupe(points)
    if rtype == ResponseType.PRODUCT_INFO and product is not None and request.intent == Intent.EFFICACY_INQUIRY:
        return _efficacy_answer(bundle, product)
    if rtype == ResponseType.PRODUCT_INFO and product is not None and not has_health_claim_approval(product.product_category):
        return _registration_only_product_info(request, product)
    if rtype == ResponseType.PRODUCT_INFO and product is not None:
        functions = "、".join(_dedupe([e.function_name for e in bundle.approved_claims if e.product_id == product.product_id]))
        points = [f"申請商：{product.company_name}" if product.company_name else "",
                  PRODUCT_STATUS_TEXT.get(product.status.value, ""),
                  f"官方核准的保健功效：{functions}" if functions else ""]
        if request.observed_package_claims:
            points.append(f"包裝上另有 {len(request.observed_package_claims)} 項宣稱文字，需要的話可按「檢視包裝宣稱」。")
        if request.intent == Intent.SAFETY_INQUIRY:
            warning_count = sum(1 for w in bundle.warnings if w.product_id == product.product_id)
            points[:0] = [REGISTRATION_NOT_SAFETY_NOTE] + (
                [f"官方登記的警語與注意事項有 {warning_count} 則，可在「查看產品資訊」中查看。"] if warning_count else [])
        return f"官方紀錄中有「{product.product_name_zh}」，許可證字號 {product.permit_number or '無'}。", _dedupe(points)
    if rtype == ResponseType.INGREDIENT_LIST:
        names = _product_ingredients(bundle)
        evidence = _ingredient_evidence(bundle)
        if product is not None and evidence is not None and evidence.product_id == product.product_id:
            return _ingredient_answer(evidence, product)
        if product is not None and names:
            return f"「{product.product_name_zh}」官方登記的保健功效相關成分如下。", names
        if product is not None:
            return (f"官方資料中目前沒有「{product.product_name_zh}」已整理的成分資料。",
                    ["可以按「" + ("查看產品資訊" if has_health_claim_approval(product.product_category)
                                 else "查看產品登記資料") + "」查看這筆官方紀錄。"])
        if bundle.standalone_entities and not request.has_product_identifiers:
            return _standalone_entity_overview(bundle)
        standalone = _standalone_ingredient_names(request, bundle)  # 沒有確認產品：不是任何產品的成分
        if standalone:
            return "官方成分資料中有這個成分名稱（這不是指任何一項產品的成分）。", standalone
        if bundle.ingredient_identity_status == IngredientIdentityStatus.IDENTITY_NOT_IN_CORPUS:
            return _identity_not_in_corpus_overview(request, bundle)
        return "官方成分資料中沒有找到名稱完全相同的成分。", [ASK_MORE_INFO]
    if rtype == ResponseType.CLAIM_RESULT:
        summary = claim_verdict_summary(bundle, product)
        names = _approved_function_names(bundle, product) if product is not None else []
        claims = bundle.claim_assessment
        if names and claims and all(
                c.origin == ClaimOrigin.USER_QUESTION and c.status == ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL
                and c.policy is not None and c.policy.matched_function_name in names for c in claims):
            # 所問的功效都在核准範圍內：結論已在摘要，重點只列官方核准功效一次（逐條判定在「為什麼」/ 完整分析）
            return summary, ["官方核准功效：" + "、".join(names)]
        points = [claim_verdict_line(c) for c in claims]
        if names:
            # 只列功效類別名稱；完整核准原文（含 1. 2. 3. 項目）放完整分析，不在卡片上重複整段原文
            points.append("官方核准功效：" + "、".join(names))
        return summary, _dedupe(points)
    if rtype == ResponseType.SOURCE_SUMMARY:
        titles = [r.title for r in bundle.rag_evidence] + [w.title for w in bundle.official_web_evidence]
        return f"找到 {len(_dedupe(titles))} 份相關的官方資料。", _dedupe(titles)
    if rtype == ResponseType.OUT_OF_SCOPE:
        return OUT_OF_SCOPE_TEXT, ["如果你有想查證的保健食品或廣告說法，可以直接傳給我。"]
    if request.identity_collision:
        return COLLISION_SUMMARY, [COLLISION_ASK]
    if request.image_bundle_blocked:
        return bundle_stop_message(request.image_bundle)
    if bundle.product_match.is_ambiguous or bundle.product_match.needs_confirmation:
        options = _dedupe([_candidate_option(c.product) for c in bundle.product_match.candidates])
        claims_note = [CLAIMS_NEED_PRODUCT] if any(c.is_asserted for c in request.claims) else []
        if bundle.product_match.is_ambiguous:
            return (f"官方紀錄中有 {len(options)} 項名稱相近的產品，還不確定你指的是哪一項：",
                    options + claims_note + [PICK_BELOW])
        return ("官方紀錄中沒有完全相同的名稱，最接近的是：",
                options + claims_note + [PICK_BELOW, NOT_LISTED_NOTE])
    if request.image_identification:
        if request.product_candidates or request.permit_number_candidates:
            return "圖片中的產品目前在官方資料中找不到對應的紀錄。", [NOT_FOUND_NOT_FAKE, ASK_IMAGE_MORE_INFO]
        return "這張圖片中沒有辨識到產品名稱或許可證字號。", [ASK_IMAGE_MORE_INFO]
    if request.missing_product_context:
        return "目前不知道你指的是哪一項產品。", [ASK_MORE_INFO]
    if product_not_found(request, bundle):
        if not bundle.product_match.lookup_completed:
            return "產品資料暫時查不到，無法確認是否有官方紀錄。", [LOOKUP_FAILED_RETRY]
        return NOT_FOUND_SUMMARY + _identity_support_note(bundle), [COVERAGE_LIST_TEXT, NOT_FOUND_NOT_FAKE_TEXT, NOT_FOUND_ASK]
    if request.product_candidates and bundle.product_match.status == ProductVerificationStatus.PARTIAL_MATCH:
        return f"目前無法確認「{request.product_candidates[0]}」是哪一項官方登記的產品。", [ASK_MORE_INFO]
    return "目前資訊不夠，還沒辦法開始查證。", [ASK_MORE_INFO]


MAX_RECOGNIZED_ITEMS = 4
MAX_RECOGNIZED_CHARS = 30


def recognized_image_content(request: NormalizedUserRequest, bundle: EvidenceBundle) -> list[str]:
    """只傳圖片、產品尚未確認時，簡短列出圖片中辨識到的識別資訊（不是整張 OCR）。只取自 Agent A 的抽取結果。"""
    if not request.image_identification or bundle.product_match.status == ProductVerificationStatus.VERIFIED:
        return []
    items = [("產品名稱", request.product_candidates), ("品牌", request.brand_candidates),
             ("許可證字號", request.permit_number_candidates), ("公司", request.company_candidates)]
    lines = [f"{label}：{_clip_text(values[0], MAX_RECOGNIZED_CHARS)}" for label, values in items if values and values[0].strip()]
    return lines[:MAX_RECOGNIZED_ITEMS]


def _clip_text(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _standalone_has_analysis(bundle: EvidenceBundle) -> bool:
    """獨立成分詢問是否有可分析的證據：已確認的 identity（canonical / 官方成分名稱）或通過 Gate 的功能證據。"""
    if bundle.ingredient_identity_status is None:
        return False
    return bool(bundle.standalone_entities or bundle.function_evidence_status == FunctionEvidenceStatus.SUPPORTED
                or bundle.ingredient_identity_status == IngredientIdentityStatus.LEGACY_OFFICIAL_NAME)


def _available_actions(rtype: ResponseType, bundle: EvidenceBundle, risk: RiskAssessment, source_count: int,
                       request: NormalizedUserRequest | None = None) -> list[ResponseAction]:
    product = _verified_product(bundle)
    registration_only = product is not None and not has_health_claim_approval(product.product_category)
    efficacy = request is not None and request.intent == Intent.EFFICACY_INQUIRY
    has = {
        ResponseAction.FULL_EFFICACY: bool(efficacy and product is not None and not registration_only
                                           and _approved_function_names(bundle, product)),
        ResponseAction.REGISTRATION_INFO: registration_only,
        ResponseAction.CHECK_PACKAGE_CLAIMS: bool(request is not None and request.observed_package_claims
                                                  and _verified_product(bundle) is not None),
        # 獨立成分 / 營養素：有可用證據（已確認 identity 或通過 Gate 的功能證據）才有完整分析；產品類回覆維持原樣
        ResponseAction.DETAIL: ((product is None and _standalone_has_analysis(bundle))
                                if rtype == ResponseType.INGREDIENT_LIST else True),
        ResponseAction.WHY: bool(risk.triggered_rules or bundle.claim_assessment),
        ResponseAction.OFFICIAL_INFO: product is not None and not registration_only,
        # 功效詢問卡固定為 完整功效 / 產品資訊（登記資料）/ 資料來源，不讓成分按鈕擠掉資料來源
        ResponseAction.INGREDIENTS: bool((bundle.ingredients and product is not None) or _ingredient_evidence(bundle))
        and not (efficacy and rtype == ResponseType.PRODUCT_INFO),
        ResponseAction.SOURCES: source_count > 0,
    }
    if rtype == ResponseType.MISSING_INFORMATION:
        # 還沒有可分析的對象：不放完整分析 / 為什麼 / 資料來源；候選按鈕由 product_choices 提供，
        # 只有資料來源暫時失敗時才提供「重新查詢」（其餘請使用者補充資訊即可）
        return [ResponseAction.RETRY] if risk.data_incomplete else []
    order = {
        # 卡片最多顯示 3 個按鈕：「查看完整分析」「查看資料來源」優先
        ResponseType.RISK: [ResponseAction.DETAIL, ResponseAction.SOURCES, ResponseAction.OFFICIAL_INFO,
                            ResponseAction.REGISTRATION_INFO, ResponseAction.WHY],
        ResponseType.CLAIM_RESULT: [ResponseAction.DETAIL, ResponseAction.SOURCES, ResponseAction.WHY],
        ResponseType.PRODUCT_INFO: [ResponseAction.CHECK_PACKAGE_CLAIMS, ResponseAction.FULL_EFFICACY,
                                    ResponseAction.OFFICIAL_INFO, ResponseAction.REGISTRATION_INFO,
                                    ResponseAction.INGREDIENTS, ResponseAction.SOURCES],
        ResponseType.INGREDIENT_LIST: [ResponseAction.DETAIL, ResponseAction.INGREDIENTS, ResponseAction.OFFICIAL_INFO,
                                       ResponseAction.REGISTRATION_INFO, ResponseAction.SOURCES],
        ResponseType.SOURCE_SUMMARY: [ResponseAction.DETAIL, ResponseAction.SOURCES],
    }.get(rtype, [])
    return [a for a in order if has[a]]


def compose_payload(request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment) -> ResponsePayload:
    rtype = select_response_type(request, bundle, risk)
    if ir.is_interaction_request(request, bundle):
        card = ir.build_card(request, bundle)
        if not card.defer_to_generic:  # 產品候選確認以外：交互作用卡 / 補充資訊卡（deterministic）
            official = [s for s in bundle.all_sources() if s.is_official] if rtype == ResponseType.INTERACTION_RESULT else []
            return ResponsePayload(
                request_id=request.request_id, response_type=card.response_type,
                is_conclusive=bool(rtype == ResponseType.INTERACTION_RESULT and bundle.interaction_research.conclusion_eligible),
                title=card.title, summary=card.summary, key_points=card.key_points, warnings=card.warnings,
                available_actions=card.actions, source_count=0, sources=official,  # 來源筆數不放在 primary（在「查看證據」）
                status_line=card.status_line, status_tone=card.status_tone, entity_choices=card.entity_choices)
    summary, points = _summary_and_points(rtype, request, bundle, risk)
    # 使用者確認的目標已說明差異（字號來自使用者確認，不是圖片）；否則才說明圖片文字辨識差異
    note = user_confirmed_target_note(request) or image_text_difference_note(request, bundle)
    if note is not None:
        points = [note] + points
    sources = [s for s in bundle.all_sources() if s.is_official]
    warnings = [] if rtype == ResponseType.OUT_OF_SCOPE else list(risk.required_disclaimers)
    if NOT_FOUND_NOT_FAKE_TEXT in points:  # 查無紀錄卡的內容已列出涵蓋類別與「不代表假貨」，不重複放在附註
        warnings = [w for w in warnings if w != NOT_FOUND_NOT_FAKE]
    status = status_for(rtype, request, bundle)
    title = (risk_title(risk) if rtype == ResponseType.RISK
             else EFFICACY_TITLE if rtype == ResponseType.PRODUCT_INFO and request.intent == Intent.EFFICACY_INQUIRY
             else TYPE_TITLES[rtype])
    if bundle.inferred_links:
        warnings.append(INFERRED_LINK_NOTE)
    return ResponsePayload(
        request_id=request.request_id,
        response_type=rtype,
        risk_level=risk.risk_level if rtype == ResponseType.RISK else None,
        claim_risk_level=(worst.claim_risk if rtype == ResponseType.CLAIM_RESULT and (worst := worst_claim(bundle))
                          else None),
        is_conclusive=risk.is_conclusive,
        title=title,
        summary=summary,
        key_points=points,
        warnings=_dedupe(warnings),
        available_actions=_available_actions(rtype, bundle, risk, len(sources), request),
        source_count=len(sources),
        sources=sources,
        **_choice_fields(rtype, bundle),
        recognized_content=recognized_image_content(request, bundle) if rtype == ResponseType.MISSING_INFORMATION else [],
        status_line=status[0] if status else None,
        status_tone=status[1] if status else None,
    )


def image_text_difference_note(request: NormalizedUserRequest, bundle: EvidenceBundle) -> str | None:
    """產品已由許可證字號確認，但圖片讀到的品名與官方名稱不同：說明差異，不把它當成產品真偽問題。"""
    match = bundle.product_match
    if not match.identity_flags or match.best_match is None:
        return None
    official = match.best_match.product.product_name_zh
    seen = "、".join(f"「{n}」" for n in request.product_candidates[:2])
    return (f"圖片文字辨識與官方名稱有差異（圖片讀到{seen}，官方品名「{official}」）；"
            "產品是依許可證字號確認，差異可能來自辨識誤差。")


def user_confirmed_target_note(request: NormalizedUserRequest) -> str | None:
    """查詢目標由使用者確認：清楚說明這是使用者的確認，差異仍保留（不是官方查證結論）。"""
    target = request.user_confirmed_target
    if target is None:
        return None
    if target.discrepancy:
        return f"你確認是同一產品，但{target.discrepancy}；這是你的確認，不是官方比對結果。"
    return f"依你的確認，以「{target.official_product_name}」的官方紀錄回答。"


def _choice_fields(rtype: ResponseType, bundle: EvidenceBundle) -> dict:
    if rtype != ResponseType.MISSING_INFORMATION:
        return {}
    choices = product_choices(bundle)
    if not choices:
        return {}
    return {"product_choices": choices,
            "choice_note": NOT_LISTED_NOTE if bundle.product_match.needs_confirmation else None}


def error_payload(request_id: str, summary: str = ERROR_SUMMARY) -> ResponsePayload:
    """查詢失敗 / 逾時：一張簡潔的 ERROR card，只提供「重新查詢」。"""
    return ResponsePayload(
        request_id=request_id, response_type=ResponseType.ERROR, is_conclusive=False,
        title=TYPE_TITLES[ResponseType.ERROR], summary=summary, available_actions=[ResponseAction.RETRY],
    )


# ---------------------------------------------------------------------------
# On-demand DetailPayload（除完整分析外皆 deterministic）
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """警語分點：依句號 / 分號切開（一般性規則），去掉空白與只有標籤的片段。"""
    parts = [p.strip(" 　\n") for p in re.split(r"(?<=[。；;])", text or "")]
    return [p for p in parts if p and p.rstrip("：:") and len(p.rstrip("：:。；;")) > 1]


def has_policy_basis(bundle: EvidenceBundle) -> bool:
    return bool(bundle.claim_policy_rules or bundle.claim_policy_examples or bundle.health_functions)


def policy_basis_lines(bundle: EvidenceBundle) -> list[str]:
    """「法規依據」明細：只整理 EvidenceBundle 中的結構化規則 / 例示 / 保健功效定義（摘要 + 條次 + 來源名稱）。

    判定本身沒有用到的規則排在後面；待複核 / 需佐證的資料一律標示「不單獨作為結論」。
    """
    lines: list[str] = []
    cited: list[str] = []
    for c in bundle.claim_assessment:
        policy = c.policy
        if policy is None:
            continue
        cited += [r for r in policy.policy_rule_ids if r not in cited]
        if policy.matched_example_text:
            lines.append(f"「{c.claim_text}」與官方例示詞句「{policy.matched_example_text}」相同。")
    rules = sorted(bundle.claim_policy_rules,
                   key=lambda r: (r.rule_id not in cited, cited.index(r.rule_id) if r.rule_id in cited else 0, r.rule_id))
    for r in rules[:MAX_POLICY_BASIS_RULES]:
        summary = r.rule_summary_zh or _clip_text(r.official_text, MAX_POLICY_TEXT_CHARS)
        lines.append(f"【{r.article_or_section}】{summary}{POLICY_USE_NOTE.get(r.evidence_use, '')}\n{r.source.source_name}")
    pending = [e for e in bundle.claim_policy_examples if e.evidence_use != PolicyEvidenceUse.DECISION_ELIGIBLE]
    if pending:
        lines.append(f"另有 {len(pending)} 筆相關官方例示仍待複核或需佐證，只作參考，不單獨作為結論。")
    for hf in bundle.health_functions:
        if hf.official_definition:
            lines.append(f"保健功效「{hf.official_function_name}」官方說明：{_clip_text(hf.official_definition, MAX_POLICY_TEXT_CHARS)}"
                         f"{POLICY_USE_NOTE.get(hf.evidence_use, '')}")
    return _dedupe(lines)


def detail_payload(
    action: ResponseAction, request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment
) -> DetailPayload:
    product = _verified_product(bundle)
    if action == ResponseAction.SOURCES:
        body = [s.source_name + (f"\n{shown}" if (shown := display_source_url(s.source_url, s.display_url)) else "")
                for s in bundle.all_sources() if s.is_official]
        body = body or ["這次沒有可列出的官方資料來源。"]
    elif action == ResponseAction.INGREDIENTS:
        evidence = _ingredient_evidence(bundle)
        if evidence is not None:
            # 完整成分（官方列有 / 已確認 / 尚未識別）→ 下一層：資料來源（provenance）
            return DetailPayload(request_id=request.request_id, action=action, title=DETAIL_TITLES[action],
                                 query_text=request.normalized_text, body=ingredient_detail_lines(evidence),
                                 available_actions=[ResponseAction.SOURCES] if bundle.all_sources() else [])
        body = ((_product_ingredients(bundle) if product is not None
                 else _standalone_ingredient_names(request, bundle) or _standalone_entity_names(bundle))
                or ["官方資料中沒有登記成分。"])
    elif action in (ResponseAction.OFFICIAL_INFO, ResponseAction.REGISTRATION_INFO):
        if product is None:
            body = ["這次沒有確認到官方產品紀錄。"]
        else:
            # 摘要式：第一層只放識別資訊與功效摘要；完整功效 / 警語 / 來源由下一層按鈕載入
            lines = dedupe_approved_claims([e for e in bundle.approved_claims if e.product_id == product.product_id])
            functions = "、".join(_dedupe([f for line in lines for f in line.function_names]))
            first_text = next((line.claim_text for line in lines if line.claim_text), "")
            health_food = has_health_claim_approval(product.product_category)
            category = CATEGORY_LABELS[product.product_category]
            body = _dedupe([
                f"官方品名：{product.product_name_zh}",
                "" if health_food else f"資料類別：{category}（{REGISTRATION_RECORD_TEXT}）",
                f"{registration_number_label(product)}：{product.permit_number}" if product.permit_number else "",
                f"{'許可狀態' if health_food else '登記狀態'}：{product_status_text(product)}",
                f"申請商：{product.company_name}" if product.company_name else "",
                f"核准功效：{functions}" if functions and health_food else "",
                _clip_text(first_text, MAX_EFFICACY_SUMMARY_CHARS) if first_text and health_food else "",
                "" if health_food else NO_HEALTH_CLAIM_APPROVAL_TEXT,
            ])
            follow_ups = {
                ResponseAction.FULL_EFFICACY: bool(lines),
                ResponseAction.WARNINGS: any(w.product_id == product.product_id for w in bundle.warnings),
                ResponseAction.SOURCES: bool(bundle.all_sources()),
            }
            return DetailPayload(request_id=request.request_id, action=action, title=DETAIL_TITLES[action],
                                 query_text=request.normalized_text, body=body,
                                 available_actions=[a for a, ok in follow_ups.items() if ok])
    elif action == ResponseAction.FULL_EFFICACY:
        lines = dedupe_approved_claims([e for e in bundle.approved_claims if product and e.product_id == product.product_id])
        body = [f"【{'、'.join(line.function_names)}】\n{line.claim_text}" if line.claim_text
                else f"【{'、'.join(line.function_names)}】" for line in lines] or ["官方資料中沒有登記核准功效。"]
    elif action == ResponseAction.WARNINGS:
        body = _dedupe([f"・{part}" for w in bundle.warnings if product and w.product_id == product.product_id
                        for part in _split_sentences(w.text)]) or ["官方資料中沒有登記警語。"]
    elif action == ResponseAction.WHY:
        body = _dedupe([claim_verdict_line(c) for c in bundle.claim_assessment]
                       + [h.reason for h in risk.triggered_rules]
                       + bundle.product_match.notes
                       + (["有資料來源查詢失敗，結果可能不完整。"] if risk.data_incomplete else []))
        body = body or ["這次沒有觸發特別的判斷規則。"]
        if has_policy_basis(bundle):  # 法規詳細內容放下一層，不塞進卡片
            follow_ups = [ResponseAction.POLICY_BASIS] + ([ResponseAction.SOURCES] if bundle.all_sources() else [])
            return DetailPayload(request_id=request.request_id, action=action, title=DETAIL_TITLES[action],
                                 query_text=request.normalized_text, body=body, available_actions=follow_ups)
    elif action == ResponseAction.POLICY_BASIS:
        body = policy_basis_lines(bundle) or ["這次沒有使用結構化的官方法規資料。"]
        return DetailPayload(request_id=request.request_id, action=action, title=DETAIL_TITLES[action],
                             query_text=request.normalized_text, body=body,
                             available_actions=[ResponseAction.SOURCES] if bundle.all_sources() else [])
    else:
        raise ValueError(f"{action.value} 不是 detail action")
    return DetailPayload(request_id=request.request_id, action=action, title=DETAIL_TITLES[action],
                         query_text=request.normalized_text, body=body)
