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

from app.agents.prompts import REPORTER_INSTRUCTIONS
from app.schemas.analysis import ReporterOutput
from app.risk.claim_policy import NOTE_CLAIM_LEVEL_ONLY
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimOrigin,
    ClaimPolicyResult,
    Intent,
    ProductVerificationStatus,
    ResponseAction,
    ResponseType,
    RiskLevel,
)
from app.normalization import extract_permit_code, normalize_name
from app.schemas.evidence import ApprovedClaimEvidence, ClaimAssessment, EvidenceBundle, SourceRef
from app.schemas.response import RISK_EMOJI, DetailPayload, ProductChoice, ResponsePayload
from app.schemas.risk import RiskAssessment
from app.schemas.user_request import NormalizedUserRequest
from app.services.errors import ServiceError
from app.services.openai_service import LLMClient

logger = logging.getLogger("nutri_safe_ai.agents.reporter")

MAX_SOURCES = 3
MAX_WARNINGS_IN_SUMMARY = 4

RISK_TITLES = {
    RiskLevel.LOW: "目前看起來屬於低風險",
    RiskLevel.MEDIUM: "目前看起來屬於中風險，建議多加留意",
    RiskLevel.HIGH: "高風險，請特別小心",
}
INCONCLUSIVE_TITLE = "目前資訊不足，暫時無法確認（先以中風險看待）"
VERIFIED_LOW_TITLE = "這項產品有官方紀錄，產品本身目前看起來屬於低風險"
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
        lines.append(f"・{s.source_name}" + (f"\n  {s.source_url}" if s.source_url else ""))
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
        "ingredients": sorted({i.name_zh for i in bundle.ingredients}),
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
                },
            }
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
        if not body:
            used_fallback = True
            body = fallback_body(request, bundle, risk)

        parts = [build_header(risk), body.strip()]
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

    def compose(self, request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment) -> ResponsePayload:
        return compose_payload(request, bundle, risk)

    def detail(
        self, action: ResponseAction, request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment
    ) -> DetailPayload:
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
    ResponseType.MISSING_INFORMATION: "需要多一點資訊",
    ResponseType.OUT_OF_SCOPE: "呷安心可以幫你查什麼",
    ResponseType.ERROR: "這次沒有完成查詢",
}
DETAIL_TITLES = {
    ResponseAction.DETAIL: "完整分析",
    ResponseAction.SOURCES: "資料來源",
    ResponseAction.INGREDIENTS: "完整成分",
    ResponseAction.OFFICIAL_INFO: "官方資訊",
    ResponseAction.WHY: "為什麼是這個結果？",
    ResponseAction.FULL_EFFICACY: "完整核准功效",
    ResponseAction.WARNINGS: "警語與注意事項",
}
MAX_EFFICACY_SUMMARY_CHARS = 40
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
ASK_MORE_INFO = "可以提供產品全名、包裝照片或許可證字號，我再幫你查。"
ASK_IMAGE_MORE_INFO = "可以補充許可證字號，或拍清楚包裝正面的產品名稱，我再幫你查。"
NOT_FOUND_NOT_FAKE = "查不到不代表是假貨或違法。"
ASK_WHICH_PRODUCT = "請回覆你要查的是哪一項（可直接傳完整品名或許可證字號）。"
NOT_LISTED_NOTE = "如果都不是，可能目前資料中沒有這項產品的官方紀錄（查不到不代表是假貨）。"
PICK_BELOW = "請直接點選下方你要查的品項。"
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


def identity_unresolved(request: NormalizedUserRequest, bundle: EvidenceBundle) -> bool:
    """還不知道要查的是哪一項產品（不是產品本身有風險）：
    指稱無法解析、多個 / 低信心候選、只找到名稱相似或只對應到公司、只傳圖片但沒有確認產品、
    問產品是否合法卻完全沒有產品資訊。這些情況回 MISSING_INFORMATION，不顯示風險燈號。"""
    match = bundle.product_match
    if request.missing_product_context or match.is_ambiguous or match.needs_confirmation:
        return True
    if request.image_identification:
        return match.status != ProductVerificationStatus.VERIFIED
    if match.status == ProductVerificationStatus.PARTIAL_MATCH:
        return True
    return (match.status == ProductVerificationStatus.NOT_APPLICABLE and request.intent == Intent.PRODUCT_VERIFICATION
            and not request.ingredient_candidates)


def select_response_type(request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment) -> ResponseType:
    """一般化規則：依 intent 與證據決定 template；高風險或產品方宣稱一律用風險卡，不會被其他 template 隱藏。

    MEDIUM 風險卡只用於「已有明確查證對象、但證據不完整或不確定」；查證對象本身還沒確定時用 MISSING_INFORMATION。
    """
    if request.intent == Intent.OUT_OF_SCOPE and not request.claims and not request.has_product_identifiers:
        return ResponseType.OUT_OF_SCOPE
    if risk.risk_level == RiskLevel.HIGH:
        return ResponseType.RISK
    if any(c.is_product_side_assertion for c in request.claims) and not (
        bundle.product_match.is_ambiguous or bundle.product_match.needs_confirmation
    ):
        return ResponseType.RISK
    if request.missing_product_context:
        return ResponseType.MISSING_INFORMATION
    if (bundle.product_match.is_ambiguous or bundle.product_match.needs_confirmation) and request.claims:
        # 產品有多個 / 低信心候選、宣稱沒有高風險：先請使用者確認產品，再逐條比對核准功效（不直接給風險燈）
        return ResponseType.MISSING_INFORMATION
    if identity_unresolved(request, bundle) and not request.claims:
        # 多個合理候選、低信心候選、圖片無法確認產品、只有相似名稱：不猜是哪一項，請使用者確認或補充
        return ResponseType.MISSING_INFORMATION
    if request.image_identification and not request.claims:
        # 只傳圖片且產品已確認：無風險 → 產品資訊；有風險（例如許可證失效）→ 風險卡
        return ResponseType.PRODUCT_INFO if risk.risk_level == RiskLevel.LOW else ResponseType.RISK
    if request.intent in (Intent.PRODUCT_VERIFICATION, Intent.SAFETY_INQUIRY):
        return ResponseType.RISK
    if request.claims:
        return ResponseType.CLAIM_RESULT
    verified = bundle.product_match.status == ProductVerificationStatus.VERIFIED
    if request.intent == Intent.INGREDIENT_INQUIRY and (bundle.ingredients or request.ingredient_candidates):
        return ResponseType.INGREDIENT_LIST
    if verified:
        return ResponseType.PRODUCT_INFO
    if request.has_product_identifiers:  # 有指定產品但無法確認：NOT_FOUND 不等於假貨，以保守風險卡說明
        return ResponseType.RISK
    if request.ingredient_candidates and bundle.ingredients:
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
    return _dedupe([f"{i.name_zh}（{i.name_en}）" if i.name_en else i.name_zh for i in bundle.ingredients])


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
        active = product.status.value == "ACTIVE"
        choices.append(ProductChoice(
            product_id=product.product_id, label=label[:MAX_CHOICE_LABEL_CHARS], full_name=product.product_name_zh,
            permit_number=product.permit_number, is_active=active,
            status_text=None if active else PRODUCT_STATUS_TEXT.get(product.status.value, "許可證狀態不明"),
        ))
    return choices


def _candidate_option(product) -> str:
    """候選項目：品名＋字號；許可證不是有效狀態時一併標示，讓同名不同字號的紀錄可以分辨。"""
    details = [d for d in (product.permit_number,
                           None if product.status.value == "ACTIVE" else PRODUCT_STATUS_TEXT.get(product.status.value)) if d]
    return f"{product.product_name_zh}（{'，'.join(details)}）" if details else product.product_name_zh


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
            summary = f"官方紀錄中有「{product.product_name_zh}」（{product.permit_number or '無字號'}）。"
        elif not risk.is_conclusive:
            summary = "目前資訊不足，暫時無法確認。"
        else:
            summary = risk.triggered_rules[0].reason if risk.triggered_rules else risk_title(risk) + "。"
        points = _asserted_claim_lines(bundle) + [h.reason for h in risk.triggered_rules]
        if not risk.is_conclusive:
            points.append(ASK_MORE_INFO)
        return summary, _dedupe(points)
    if rtype == ResponseType.PRODUCT_INFO and product is not None:
        approved = _approved_claim_lines(bundle, product)
        if request.intent == Intent.EFFICACY_INQUIRY and approved:
            # 詢問官方核准功效：答案就是官方核准內容本身，不是待查證的說法
            return f"「{product.product_name_zh}」官方核准的保健功效如下。", approved
        functions = "、".join(_dedupe([e.function_name for e in bundle.approved_claims if e.product_id == product.product_id]))
        points = [f"申請商：{product.company_name}" if product.company_name else "",
                  PRODUCT_STATUS_TEXT.get(product.status.value, ""),
                  f"官方核准的保健功效：{functions}" if functions else ""]
        if request.observed_package_claims:
            points.append(f"包裝上另有 {len(request.observed_package_claims)} 項宣稱文字，需要的話可按「查驗包裝宣稱」。")
        return f"官方紀錄中有「{product.product_name_zh}」，許可證字號 {product.permit_number or '無'}。", _dedupe(points)
    if rtype == ResponseType.INGREDIENT_LIST:
        names = _product_ingredients(bundle)
        if product is not None and names:
            return f"「{product.product_name_zh}」官方登記的保健功效相關成分如下。", names
        if names:
            return "官方資料中找到以下成分。", names
        return "官方資料中目前查不到這個成分。", [ASK_MORE_INFO]
    if rtype == ResponseType.CLAIM_RESULT:
        first = bundle.claim_assessment[0] if bundle.claim_assessment else None
        summary = CLAIM_STATUS_SUMMARY[first.status] if first else "目前無法確認這個說法。"
        points = [claim_policy_line(c) if c.origin != ClaimOrigin.USER_QUESTION
                  else (f"「{c.claim_text}」：{c.explanation}" if c.explanation else "") for c in bundle.claim_assessment]
        if product is not None and (approved := _approved_claim_lines(bundle, product)):
            points.append(f"「{product.product_name_zh}」官方核准的保健功效：" + "；".join(approved))
        return summary, _dedupe(points)
    if rtype == ResponseType.SOURCE_SUMMARY:
        titles = [r.title for r in bundle.rag_evidence] + [w.title for w in bundle.official_web_evidence]
        return f"找到 {len(_dedupe(titles))} 份相關的官方資料。", _dedupe(titles)
    if rtype == ResponseType.OUT_OF_SCOPE:
        return OUT_OF_SCOPE_TEXT, ["如果你有想查證的保健食品或廣告說法，可以直接傳給我。"]
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


def _available_actions(rtype: ResponseType, bundle: EvidenceBundle, risk: RiskAssessment, source_count: int,
                       request: NormalizedUserRequest | None = None) -> list[ResponseAction]:
    has = {
        ResponseAction.CHECK_PACKAGE_CLAIMS: bool(request is not None and request.observed_package_claims
                                                  and _verified_product(bundle) is not None),
        ResponseAction.DETAIL: True,
        ResponseAction.WHY: bool(risk.triggered_rules or bundle.claim_assessment),
        ResponseAction.OFFICIAL_INFO: _verified_product(bundle) is not None,
        ResponseAction.INGREDIENTS: bool(bundle.ingredients),
        ResponseAction.SOURCES: source_count > 0,
    }
    if rtype == ResponseType.MISSING_INFORMATION:
        # 還沒有可分析的對象：不放完整分析 / 為什麼 / 資料來源；候選按鈕由 product_choices 提供，
        # 只有資料來源暫時失敗時才提供「重新查詢」（其餘請使用者補充資訊即可）
        return [ResponseAction.RETRY] if risk.data_incomplete else []
    order = {
        # 卡片最多顯示 3 個按鈕：「查看完整分析」「查看資料來源」優先
        ResponseType.RISK: [ResponseAction.DETAIL, ResponseAction.SOURCES, ResponseAction.OFFICIAL_INFO, ResponseAction.WHY],
        ResponseType.CLAIM_RESULT: [ResponseAction.DETAIL, ResponseAction.SOURCES, ResponseAction.WHY],
        ResponseType.PRODUCT_INFO: [ResponseAction.CHECK_PACKAGE_CLAIMS, ResponseAction.OFFICIAL_INFO,
                                    ResponseAction.INGREDIENTS, ResponseAction.SOURCES],
        ResponseType.INGREDIENT_LIST: [ResponseAction.INGREDIENTS, ResponseAction.SOURCES],
        ResponseType.SOURCE_SUMMARY: [ResponseAction.DETAIL, ResponseAction.SOURCES],
    }.get(rtype, [])
    return [a for a in order if has[a]]


def compose_payload(request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment) -> ResponsePayload:
    rtype = select_response_type(request, bundle, risk)
    summary, points = _summary_and_points(rtype, request, bundle, risk)
    # 使用者確認的目標已說明差異（字號來自使用者確認，不是圖片）；否則才說明圖片文字辨識差異
    note = user_confirmed_target_note(request) or image_text_difference_note(request, bundle)
    if note is not None:
        points = [note] + points
    sources = [s for s in bundle.all_sources() if s.is_official]
    warnings = [] if rtype == ResponseType.OUT_OF_SCOPE else list(risk.required_disclaimers)
    if bundle.inferred_links:
        warnings.append(INFERRED_LINK_NOTE)
    return ResponsePayload(
        request_id=request.request_id,
        response_type=rtype,
        risk_level=risk.risk_level if rtype == ResponseType.RISK else None,
        is_conclusive=risk.is_conclusive,
        title=risk_title(risk) if rtype == ResponseType.RISK else TYPE_TITLES[rtype],
        summary=summary,
        key_points=points,
        warnings=_dedupe(warnings),
        available_actions=_available_actions(rtype, bundle, risk, len(sources), request),
        source_count=len(sources),
        sources=sources,
        **_choice_fields(rtype, bundle),
        recognized_content=recognized_image_content(request, bundle) if rtype == ResponseType.MISSING_INFORMATION else [],
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


def detail_payload(
    action: ResponseAction, request: NormalizedUserRequest, bundle: EvidenceBundle, risk: RiskAssessment
) -> DetailPayload:
    product = _verified_product(bundle)
    if action == ResponseAction.SOURCES:
        body = [s.source_name + (f"\n{s.source_url}" if s.source_url else "") for s in bundle.all_sources() if s.is_official]
        body = body or ["這次沒有可列出的官方資料來源。"]
    elif action == ResponseAction.INGREDIENTS:
        body = _product_ingredients(bundle) or ["官方資料中沒有登記成分。"]
    elif action == ResponseAction.OFFICIAL_INFO:
        if product is None:
            body = ["這次沒有確認到官方產品紀錄。"]
        else:
            # 摘要式：第一層只放識別資訊與功效摘要；完整功效 / 警語 / 來源由下一層按鈕載入
            lines = dedupe_approved_claims([e for e in bundle.approved_claims if e.product_id == product.product_id])
            functions = "、".join(_dedupe([f for line in lines for f in line.function_names]))
            first_text = next((line.claim_text for line in lines if line.claim_text), "")
            body = _dedupe([
                f"官方品名：{product.product_name_zh}",
                f"許可證字號：{product.permit_number}" if product.permit_number else "",
                f"許可狀態：{PRODUCT_STATUS_TEXT.get(product.status.value, '')}",
                f"申請商：{product.company_name}" if product.company_name else "",
                f"核准功效：{functions}" if functions else "",
                _clip_text(first_text, MAX_EFFICACY_SUMMARY_CHARS) if first_text else "",
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
        body = _dedupe([h.reason for h in risk.triggered_rules]
                       + [claim_policy_line(c) if c.policy is not None
                          else f"「{c.claim_text}」：{CLAIM_STATUS_SUMMARY[c.status]}{c.explanation}"
                          for c in bundle.claim_assessment]
                       + bundle.product_match.notes
                       + (["有資料來源查詢失敗，結果可能不完整。"] if risk.data_incomplete else []))
        body = body or ["這次沒有觸發特別的判斷規則。"]
    else:
        raise ValueError(f"{action.value} 不是 detail action")
    return DetailPayload(request_id=request.request_id, action=action, title=DETAIL_TITLES[action],
                         query_text=request.normalized_text, body=body)
