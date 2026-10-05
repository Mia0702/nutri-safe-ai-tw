"""Pending product clarification（deterministic，不呼叫 LLM）。

上一題系統若在問「是不是這一項官方紀錄」（只有一個低信心候選，或字號對應的官方品名與看到的品名不一致），
task context 會保存一筆 PendingClarification。使用者下一句的對話行為：

* CONFIRM（「是」「對」「是同一個」「就是這個」這類短回覆）→ 以該候選繼續上一題（不重跑 Agent A / planner）。
  只代表 USER_CONFIRMED_TARGET：原本的名稱 / 證據衝突保留，不會因使用者確認變成官方 VERIFIED。
* REJECT（「不是」「不對」）→ 清掉候選，請使用者補產品資訊。
* NEW_ENTITY（訊息中出現另一個許可證字號）→ 清掉 pending，新產品優先，走正常流程。
* OTHER → 正常 semantic pipeline。

判定是一般性規則：訊息正規化後必須「完全」由肯定 / 否定詞與指稱 / 語助詞組成才算 CONFIRM / REJECT；
任何其他內容（品名、問題、說明）都交給正常流程，不猜。
"""

import re
import unicodedata

from app.normalization import extract_permit_code, normalize_name
from app.schemas.analysis import AnalysisResult
from app.schemas.enums import (
    DialogueAct,
    InputType,
    PendingClarificationType,
    ProductReferenceStatus,
    ProductVerificationStatus,
)
from app.schemas.evidence import ProductRecord
from app.schemas.user_request import NormalizedUserRequest, PendingClarification, UserConfirmedTarget

MAX_ACT_CHARS = 20  # 超過這個長度的訊息不當作單純的是 / 否回覆

_AFFIRM = ("是的", "對的", "沒錯", "正確", "確定", "就是", "一樣", "是", "對", "嗯", "恩", "好",
           "yes", "yeah", "yep", "ok", "okay", "sure", "y")
_NEGATE = ("並不是", "不正確", "不一樣", "都不是", "不是", "不對", "錯了", "沒有", "不", "錯", "否",
           "nope", "no", "n")
# 指稱 / 語助詞：本身不表態，只能搭配肯定或否定詞
_NEUTRAL = ("同一個", "同一款", "同一項", "同一罐", "同一瓶", "同一盒", "同一種", "這一個", "這一款", "這一項",
            "同一", "這個", "這款", "這項", "這罐", "這瓶", "這盒", "這種", "那個", "那款", "它", "牠",
            "產品", "商品", "就", "也", "啊", "阿", "呀", "喔", "哦", "唷", "耶", "啦", "囉", "的", "了")
_TOKENS = sorted({*_AFFIRM, *_NEGATE, *_NEUTRAL}, key=len, reverse=True)
_STRIP_RE = re.compile(r"[^\w]|_", re.UNICODE)  # 去掉標點、空白、emoji


def _segment(text: str) -> list[str] | None:
    """完全由已知詞組成時回傳詞序列，否則 None（有任何其他內容都不是單純的是 / 否回覆）。"""
    norm = _STRIP_RE.sub("", unicodedata.normalize("NFKC", text or "").lower())
    if not norm or len(norm) > MAX_ACT_CHARS:
        return None
    out, i = [], 0
    while i < len(norm):
        token = next((t for t in _TOKENS if norm.startswith(t, i)), None)
        if token is None:
            return None
        out.append(token)
        i += len(token)
    return out


def classify_dialogue_act(text: str, pending: PendingClarification | None) -> DialogueAct:
    if pending is None:
        return DialogueAct.OTHER
    code = extract_permit_code(text)
    if code and code != extract_permit_code(pending.permit_number or ""):
        return DialogueAct.NEW_ENTITY
    tokens = _segment(text)
    if tokens is None:
        return DialogueAct.OTHER
    affirm = any(t in _AFFIRM for t in tokens)
    negate = any(t in _NEGATE for t in tokens)
    if affirm and not negate:
        return DialogueAct.CONFIRM
    if negate and not affirm:
        return DialogueAct.REJECT
    return DialogueAct.OTHER  # 只有指稱詞、或同時肯定又否定（「對不對」）：不猜


def is_plain_text(raw) -> bool:
    return raw.input_type == InputType.TEXT and not raw.media and not raw.urls


def pending_clarification_for(result: AnalysisResult, request_id: str) -> PendingClarification | None:
    """這一題是否正在請使用者確認「是不是這一項」（只有一個具體候選時才成立；多候選用確認卡按鈕選）。"""
    req, bundle = result.normalized_request, result.evidence
    if req is None or bundle is None or req.user_confirmed_target is not None:
        return None
    match = bundle.product_match
    best = match.best_match
    if best is None:
        return None
    if match.status == ProductVerificationStatus.CONFLICT:
        kind, reason = PendingClarificationType.PRODUCT_IDENTITY_CONFLICT, match.status.value
    elif match.needs_confirmation and {c.product.product_id for c in match.candidates} <= {best.product.product_id}:
        kind, reason = PendingClarificationType.PRODUCT_CANDIDATE_CONFIRMATION, match.ambiguity_reason.value
    else:
        return None
    product = best.product
    return PendingClarification(
        clarification_type=kind, candidate_product_id=product.product_id,
        candidate_product_name=product.product_name_zh, permit_number=product.permit_number,
        previous_request_id=request_id, unresolved_reason=reason,
        discrepancy=_discrepancy(req, product, kind),
    )


def _discrepancy(req: NormalizedUserRequest, product: ProductRecord, kind: PendingClarificationType) -> str | None:
    official = normalize_name(product.product_name_zh)
    observed = next((n for n in req.product_candidates if normalize_name(n) and normalize_name(n) != official), None)
    if observed is None:
        return None
    where = "圖片上的品名" if req.input_type == InputType.IMAGE or req.image_identification else "你提供的品名"
    if kind == PendingClarificationType.PRODUCT_IDENTITY_CONFLICT:
        return f"{where}「{observed}」與許可證字號對應的官方品名「{product.product_name_zh}」不一致"
    return f"{where}「{observed}」與官方品名「{product.product_name_zh}」不完全相同"


def request_for_confirmed_target(
    original: NormalizedUserRequest, product: ProductRecord, pending: PendingClarification, request_id: str
) -> NormalizedUserRequest:
    """CONFIRM：沿用上一題已理解的問題，加上候選的許可證字號繼續（不重新做語意理解）。

    保留上一題看到的品名，讓 deterministic resolver 照原規則判定：字號與品名不一致仍是 CONFLICT，
    使用者確認不會把衝突改成官方 VERIFIED；差異另以 user_confirmed_target 標示給 Agent C。
    """
    if original.interaction_subjects:  # 交互作用題：只固定這一個產品對象（與確認卡選擇相同），不改寫其他對象
        from app.pipeline import request_for_selected_interaction_product

        return request_for_selected_interaction_product(original, product, request_id)
    names = list(original.product_candidates) or [product.product_name_zh]
    return original.model_copy(deep=True, update={
        "request_id": request_id,
        "product_candidates": names,
        "corrected_product_candidates": [],
        "permit_number_candidates": [product.permit_number] if product.permit_number else [],
        "product_reference_status": ProductReferenceStatus.EXPLICIT,
        "context_entities_used": [],
        "needs_product_lookup": True,
        "input_tools_called": [],
        "adapter_notes": original.adapter_notes + ["使用者回覆確認系統詢問的候選產品"],
        "user_confirmed_target": UserConfirmedTarget(
            product_id=product.product_id, official_product_name=product.product_name_zh,
            permit_number=product.permit_number, discrepancy=pending.discrepancy),
    })
