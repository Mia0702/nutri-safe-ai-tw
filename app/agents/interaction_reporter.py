"""Agent C — Interaction Reporter（Phase 3）：把 `bundle.interaction_research` 安全地呈現給使用者。

* Primary card（`build_card`）完全 deterministic：只依 InteractionResearch 的結構化欄位，不呼叫 LLM、不查資料、不新增證據。
  最多 3 個重點；每個重點 ≤ 78 字（Renderer 每點只顯示 80 字，限制 / 劑量未評估等關鍵語句不可被截斷）。
* 只有 `CONCLUSION_ELIGIBLE` 可以說「有可用的交互作用證據」，而且一律說明「是否適用於你的劑量、用法、族群」目前尚未評估。
  衝突 / lineage 不繼承 / 待審 / 證據不足 / 資料庫沒有收錄，都只能表達為證據與不確定性。
* NO_CORPUS_RECORD 只代表「資料庫沒有收錄」，不可寫成沒有交互作用或可以放心一起使用。
* 重複成分只說「兩項產品都含有 X」＋「尚未完成劑量評估」，不推論過量 / 超過上限 / 毒性 / 不能一起吃。
* 內部 ID / enum（N_*、F_*、CONCLUSION_ELIGIBLE …）不會出現在使用者可見文字；顯示一律用成分 / 藥品名稱與固定白話。
* 產品風險（Risk Engine）與 interaction finding 分開：本卡片沒有風險燈號，也不改變任何產品風險。
* 詳細內容（證據來源 / 成分對應 / 完整分析）走 on-demand detail；完整分析只把結構化 facts 交給 LLM，
  回傳後再過 guard（禁止詞 / 內部代碼），不合格改用固定模板；限制說明一律由程式附上，不由 LLM 決定。
"""

import json
import re
from dataclasses import dataclass, field

from app.agents.prompts import INTERACTION_REPORTER_INSTRUCTIONS
from app.product_coverage import COVERAGE_LIST_TEXT, NOT_FOUND_NOT_FAKE_TEXT
from app.schemas.enums import Intent, ResponseAction, ResponseType, SemanticTone
from app.schemas.evidence import EvidenceBundle
from app.schemas.interaction import (
    EntityRefOut,
    EvidenceDirectionRole,
    InteractionConclusionStatus as Status,
    InteractionEvidenceItem,
    InteractionFinding,
    InteractionMode,
    InteractionQuestion,
    InteractionResearch,
    InteractionResearchStatus,
    InteractionSubjectKind,
    PairStatus,
    SubjectResolution,
    SubjectResolutionStatus,
)
from app.schemas.user_request import NormalizedUserRequest
from app.services.errors import ServiceError
from app.services.interaction_service import VIA_FORM

MAX_POINT_CHARS = 78  # Renderer 每個重點只顯示 80 字：關鍵限制不可被截斷
MAX_EVIDENCE_LISTED = 20
MAX_MAPPING_LINES = 12
MAX_NAMES_IN_POINT = 2

# --------------------------------------------------------------------------- 固定白話

TITLE_ELIGIBLE = "有可用的交互作用證據"
TITLE_LINEAGE = "目前不能直接套用交互作用結論"
TITLE_CONFLICT = "目前證據不一致"
TITLE_INSUFFICIENT = "目前證據不足以確定"
TITLE_NO_RECORD = "目前交互作用資料庫沒有收錄這組關係"
TITLE_DUPLICATE = "重複成分提醒"
TITLE_NO_DUPLICATE = "重複成分比對結果"
TITLE_CLARIFY = "需要更多資訊"

STATUS_NEEDS_USAGE = "需要確認實際用量與使用方式"
STATUS_NO_DUPLICATE = "未找到重複成分"
STATUS_LINEAGE = "不能自動套用"
STATUS_CONFLICT = "現有證據不一致"
STATUS_INSUFFICIENT = "資料不足"
STATUS_NO_RECORD = "資料庫未收錄這組關係"
STATUS_DUPLICATE = "尚未評估實際攝取量"

APPLICABILITY_NOTE = "是否適用於你的實際劑量、用法與族群，目前無法自動判斷，需另外確認適用條件。"
APPLICABILITY_NOT_EVALUATED = "是否適用於你的實際劑量、用法與族群，目前尚未評估，需另外確認適用條件。"
DOSE_NOT_EVALUATED = "尚未完成劑量評估"
NO_RECORD_MEANING = "未收錄只代表資料庫尚無紀錄，不能當作可以放心一起使用的依據。"
STATUS_COVERAGE_GAP = "資料庫尚未收錄此成分／藥物"
GAP_EVIDENCE_FOUND = "找到一些相關資料，但目前不足以形成結構化交互作用結論。"
GAP_NO_EVIDENCE = "目前也沒有足夠證據可下結論。"


def coverage_gap_sentence(names: list[str]) -> str:
    """資料涵蓋缺口（不是拼錯字）：結構化交互作用資料尚未收錄這些成分 / 藥物。"""
    shown = "、".join(f"『{n}』" for n in names[:2]) + (f"等 {len(names)} 項" if len(names) > 2 else "")
    return f"目前結構化交互作用資料尚未收錄{shown}，因此無法直接用知識圖譜判定。"


def _gap_evidence_point(bundle: EvidenceBundle) -> str:
    """補查（RAG / 官方網站）只是參考：找到相關資料也不構成結構化交互作用結論；沒找到就如實說證據不足。"""
    return GAP_EVIDENCE_FOUND if (bundle.rag_evidence or bundle.official_web_evidence) else GAP_NO_EVIDENCE
ASK_PROFESSIONAL = "用藥相關問題請詢問醫師或藥師。"
DISCLAIMER = f"本資訊整理自資料庫收錄的證據，不是醫療建議；{ASK_PROFESSIONAL}"
NOT_PRODUCT_RISK = "這裡只整理成分與藥品之間的交互作用資料，不是對產品本身的風險判定。"
DUPLICATE_MEANING = "只表示成分重複；要評估攝取量，需要每份含量與每日份數，目前資料尚未評估。"

KIND_LABELS = {"NutrientGroup": "營養素類別", "IngredientForm": "成分形式", "DrugSubstance": "藥品有效成分"}

# 完整分析各結論資格的白話（不輸出 enum 名稱）
STATUS_PHRASES = {
    Status.CONCLUSION_ELIGIBLE: "有可用的證據支持這個交互作用（適用條件尚待確認）",
    Status.EVIDENCE_CONFLICT: "證據不一致，不下肯定結論",
    Status.LINEAGE_NOT_INHERITED: "只能經成分形式追到營養素類別，資料不允許自動套用該類別的交互作用",
    Status.NEEDS_REVIEW: "資料庫尚待審核，證據不足以確定",
    Status.INSUFFICIENT_FOR_CONCLUSION: "只有背景或輔助性的證據，不足以確定",
}
STATUS_RANK = {Status.CONCLUSION_ELIGIBLE: 0, Status.EVIDENCE_CONFLICT: 1, Status.LINEAGE_NOT_INHERITED: 2,
               Status.NEEDS_REVIEW: 3, Status.INSUFFICIENT_FOR_CONCLUSION: 4}

# supports_direction 原值 → 使用者看得懂、且不誇大的說法。NO_SIGNIFICANT_EFFECT / MIXED / CONTEXT_NOT_PAIR_SPECIFIC
# 絕對不是「支持有交互作用」；未知的新值保守表達為「方向尚未分類，不當作支持」。
SUPPORTS_LABEL = "支持此交互作用紀錄"
DIRECTION_LABELS = {
    "SUPPORTS": SUPPORTS_LABEL,
    "SUPPORTS_ABSORPTION_NOT_TREATMENT_NECESSITY": "支持影響吸收，但不代表需要改變治療",
    "CLASS_LEVEL_ASSOCIATION": "只是類別層級的相關資料，不是這一組的直接證據",
    "PRODUCT_SPECIFIC_CAUTION": "只是特定產品的注意事項，不是這一組的直接證據",
    "NO_SIGNIFICANT_EFFECT": "未發現明顯影響（不支持此交互作用紀錄）",
    "NO_ADDED_CLINICAL_BENEFIT_IN_STUDIED_CONTEXT": "在研究情境中未增加臨床效益（不支持此交互作用紀錄）",
    "MIXED": "結果不一致（不能視為支持）",
    "MIXED_ENDPOINTS": "不同觀察指標結果不一致（不能視為支持）",
    "MIXED_POPULATION_CONTEXT": "不同族群結果不一致（不能視為支持）",
    "CONTEXT_NOT_PAIR_SPECIFIC": "背景資料，不是這一組的直接證據",
}
ROLE_FALLBACK_LABELS = {
    EvidenceDirectionRole.SUPPORTS: SUPPORTS_LABEL,
    EvidenceDirectionRole.SUPPORTS_QUALIFIED: "支持，但有限定條件",
    EvidenceDirectionRole.INDIRECT: "間接資料，不是這一組的直接證據",
    EvidenceDirectionRole.NOT_SUPPORTING: "不支持此交互作用紀錄",
    EvidenceDirectionRole.MIXED: "結果不一致（不能視為支持）",
    EvidenceDirectionRole.CONTEXT_ONLY: "背景資料，不是這一組的直接證據",
    EvidenceDirectionRole.UNCLASSIFIED: "方向尚未分類，不當作支持",
}
# 重複成分規則的證據：「支持」是支持重複攝取的說明，不是有害交互作用
DUPLICATE_SUPPORT_LABEL = "支持這項重複成分的說明"


def direction_label(item: InteractionEvidenceItem, *, duplicate_rule: bool = False) -> str:
    if duplicate_rule and item.direction_role in (EvidenceDirectionRole.SUPPORTS, EvidenceDirectionRole.SUPPORTS_QUALIFIED):
        return DUPLICATE_SUPPORT_LABEL
    return DIRECTION_LABELS.get(item.supports_direction) or ROLE_FALLBACK_LABELS[item.direction_role]


# --------------------------------------------------------------------------- 名稱（不顯示內部 ID）


def entity_label(entity: EntityRefOut | None) -> str:
    if entity is None:
        return ""
    return (entity.display_name_zh or entity.canonical_name_en or "").strip()


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fit(text: str) -> str:
    return _clip(text, MAX_POINT_CHARS)


class _Names:
    """entity_id → 使用者可讀名稱；只用 InteractionResearch 內已有的 entity（不查資料）。"""

    def __init__(self, research: InteractionResearch):
        self.by_id: dict[str, EntityRefOut] = {}
        self.subjects = {s.text: s for s in research.subjects}

        def add(e: EntityRefOut | None):
            if e is not None and e.entity_id not in self.by_id:
                self.by_id[e.entity_id] = e

        for s in research.subjects:
            for e in s.entities:
                add(e)
            for m in s.ingredient_mappings:
                add(m.entity), add(m.parent), add(m.via)
        for f in research.findings:
            for p in f.participants:
                add(p.entity)
        for d in research.duplicates:
            add(d.nutrient)
            for p in d.products:
                for e in p.via_entities:
                    add(e)

    def name(self, entity_id: str | None) -> str:
        return entity_label(self.by_id.get(entity_id or ""))

    def subject(self, text: str) -> SubjectResolution | None:
        return self.subjects.get(text)


def _quote(name: str) -> str:
    return f"「{name}」"


def _side_phrase(names: _Names, subject_text: str, entity_id: str) -> str:
    """一個 interaction 參與者對應到使用者哪個對象：產品 →「產品」所含的「成分」；其餘直接用成分 / 藥品名稱。"""
    entity = names.name(entity_id) or subject_text
    res = names.subject(subject_text)
    if res is not None and res.kind == InteractionSubjectKind.PRODUCT and res.product_confirmed:
        return f"{_quote(res.product_name or res.text)}所含的{_quote(entity)}"
    return _quote(entity)


def _finding_sides(names: _Names, f: InteractionFinding) -> tuple[str, str]:
    if len(f.subject_matches) == 2:
        a, b = f.subject_matches
        return _side_phrase(names, a.subject_text, a.entity_id), _side_phrase(names, b.subject_text, b.entity_id)
    labels = [_quote(entity_label(p.entity)) for p in f.participants if entity_label(p.entity)]
    return (labels + ["", ""])[0], (labels + ["", ""])[1]


def _pair(a: str, b: str) -> str:
    return f"{a}與{b}"


def _list_names(items: list[str]) -> str:
    shown = [_quote(i) for i in items[:MAX_NAMES_IN_POINT]]
    out = "、".join(shown)
    return out + (f"等 {len(items)} 項" if len(items) > MAX_NAMES_IN_POINT else "")


# --------------------------------------------------------------------------- Primary card


@dataclass
class InteractionCard:
    response_type: ResponseType
    title: str
    summary: str
    key_points: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status_line: str | None = None
    status_tone: SemanticTone | None = None
    actions: list[ResponseAction] = field(default_factory=list)
    defer_to_generic: bool = False  # 產品候選確認：沿用既有確認卡（候選按鈕）


def is_interaction_request(request: NormalizedUserRequest, bundle: EvidenceBundle) -> bool:
    return request.intent == Intent.INTERACTION_INQUIRY and bundle.interaction_research is not None


def interaction_response_type(bundle: EvidenceBundle) -> ResponseType:
    research = bundle.interaction_research
    return (ResponseType.INTERACTION_RESULT if research is not None and research.status == InteractionResearchStatus.COMPLETE
            else ResponseType.MISSING_INFORMATION)


def _ranked_findings(research: InteractionResearch) -> list[InteractionFinding]:
    return sorted(research.findings, key=lambda f: (STATUS_RANK[f.conclusion_status], f.interaction_id))


def _unresolved_total(research: InteractionResearch) -> int:
    return sum(c.unresolved_count for c in research.duplicate_coverage)


def _duplicate_point(research: InteractionResearch, names: _Names) -> str | None:
    if not research.duplicates:
        return None
    nutrients = [entity_label(d.nutrient) for d in research.duplicates if entity_label(d.nutrient)]
    base = f"兩項產品都含有{_list_names(nutrients)}；{DOSE_NOT_EVALUATED}"
    if any(not d.same_identity_level_across_products for d in research.duplicates):
        base = f"兩項產品都含有{_list_names(nutrients)}（成分形式不同）；{DOSE_NOT_EVALUATED}"
    unresolved = _unresolved_total(research)
    with_note = f"{base}；仍有 {unresolved} 項成分尚未完成辨識" if unresolved else base
    return with_note if len(with_note) <= MAX_POINT_CHARS else _fit(base)


def _evidence_counts(f: InteractionFinding) -> str:
    return f"證據：{f.supporting_evidence_count} 筆支持，{f.non_supporting_evidence_count} 筆不支持、結果不一致或僅為背景資料"


def _no_record_pairs(research: InteractionResearch) -> list:
    return [p for p in research.pairs if p.status == PairStatus.NO_CORPUS_RECORD]


def _fallback_note(bundle: EvidenceBundle, research: InteractionResearch) -> str | None:
    if not research.fallback_used:
        return None
    if bundle.rag_evidence or bundle.official_web_evidence:
        return "另外補查到的官方文件只供參考，不能當作這組的交互作用結論。"
    return "另外補查官方文件，也沒有找到可作為交互作用結論的資料。"


def _has_product_subject(research: InteractionResearch) -> bool:
    return any(s.kind == InteractionSubjectKind.PRODUCT and s.product_confirmed for s in research.subjects)


def _has_evidence(research: InteractionResearch) -> bool:
    return any(f.evidence for f in research.findings) or any(d.duplicate_rule_evidence for d in research.duplicates)


def _has_mapping(research: InteractionResearch) -> bool:
    return any(s.ingredient_mappings for s in research.subjects)


def _actions(research: InteractionResearch) -> list[ResponseAction]:
    """Primary 按鈕（最多 3）：為什麼？/ 查看證據 /（有已確認產品的成分對應才有）查看成分對應。"""
    actions = [ResponseAction.WHY]
    if _has_evidence(research):
        actions.append(ResponseAction.INTERACTION_EVIDENCE)
    if _has_mapping(research):
        actions.append(ResponseAction.INTERACTION_MAPPING)
    return actions


def _lineage_summary(names: _Names, f: InteractionFinding) -> tuple[str, str]:
    """→ (summary, point)：產品成分形式可追到某營養素群，但資料不允許自動繼承該群的 interaction。"""
    via = next((m for m in f.subject_matches if m.match_basis == VIA_FORM), None)
    other = next((m for m in f.subject_matches if m is not via), None)
    if via is None:
        a, b = _finding_sides(names, f)
        return (f"{_pair(a, b)}的紀錄只能經成分形式對應，目前資料不允許自動套用，所以不能直接下結論。",
                "資料庫的紀錄不能視為這項產品的結論")
    group = names.name(via.entity_id)
    form = names.name(via.via_entity_id)
    res = names.subject(via.subject_text)
    owner = _quote(res.product_name or res.text) if res is not None and res.product_confirmed else _quote(via.subject_text)
    traced = f"含有{_quote(form)}，可追到{_quote(group)}類成分" if form else f"的成分可追到{_quote(group)}類成分"
    summary = f"{owner}{traced}；但目前資料不允許自動繼承{_quote(group)}類的交互作用，所以不能直接套用到這項產品。"
    if other is None:
        other_name = ""
    elif other.match_basis == VIA_FORM:   # 另一邊也是由成分形式對應：以營養素類別表達，不重複產品名稱
        other_name = f"{_quote(names.name(other.entity_id))}類成分"
    else:
        other_name = _side_phrase(names, other.subject_text, other.entity_id)
    point = (f"資料庫收錄了{_quote(group)}類成分與{other_name}的紀錄，但不能視為這項產品的結論" if other_name
             else f"資料庫收錄了{_quote(group)}類成分的紀錄，但不能視為這項產品的結論")
    return summary, point


# 直接回答（卡片標題）：依「使用者實際在問什麼」(InteractionQuestion) 與結構化結論資格產生；
# 結論資格不是 CONCLUSION_ELIGIBLE、或適用條件尚未評估（applicability.evaluated = False）時，一律不說「可以」或「不可以」一起使用。
ANSWER_ELIGIBLE, ANSWER_CONFLICT, ANSWER_LINEAGE, ANSWER_INSUFFICIENT, ANSWER_NO_RECORD = (
    "eligible", "conflict", "lineage", "insufficient", "no_record")
TOGETHER_ANSWERS = {
    ANSWER_ELIGIBLE: "目前不能直接判定是否適合一起使用。",
    ANSWER_LINEAGE: "目前不能直接判定是否適合一起使用。",
    ANSWER_CONFLICT: "目前無法確定是否適合一起使用，因為現有證據不一致。",
    ANSWER_INSUFFICIENT: "目前資料不足，無法確定是否適合一起使用。",
    ANSWER_NO_RECORD: "目前資料不足，不能判定兩者是否可以一起使用。",
}
EXISTENCE_ANSWERS = {
    ANSWER_LINEAGE: "目前不能直接判定這項產品是否有該交互作用。",
    ANSWER_CONFLICT: "目前無法確定是否有交互作用，因為現有證據不一致。",
    ANSWER_INSUFFICIENT: "目前資料不足，無法確定是否有交互作用。",
    ANSWER_NO_RECORD: "目前資料庫沒有收錄這組關係，無法判定是否有交互作用。",
}
STATUS_BY_ANSWER = {
    ANSWER_ELIGIBLE: STATUS_NEEDS_USAGE, ANSWER_LINEAGE: STATUS_NEEDS_USAGE, ANSWER_CONFLICT: STATUS_CONFLICT,
    ANSWER_INSUFFICIENT: STATUS_INSUFFICIENT, ANSWER_NO_RECORD: STATUS_NO_RECORD,
}
LIMIT_BY_ANSWER = {
    ANSWER_ELIGIBLE: "是否適用於你，目前無法自動判斷；需要確認實際劑量、用法與使用情況。",
    ANSWER_LINEAGE: "若要確認實際影響，請詢問醫師或藥師。",
    ANSWER_CONFLICT: f"請不要只依這項資料判斷；{ASK_PROFESSIONAL}",
    ANSWER_INSUFFICIENT: f"請不要把這當成肯定的結論；{ASK_PROFESSIONAL}",
    ANSWER_NO_RECORD: "需要確認時，請詢問醫師或藥師。",
}
ANSWER_BY_STATUS = {
    Status.CONCLUSION_ELIGIBLE: ANSWER_ELIGIBLE, Status.EVIDENCE_CONFLICT: ANSWER_CONFLICT,
    Status.LINEAGE_NOT_INHERITED: ANSWER_LINEAGE, Status.NEEDS_REVIEW: ANSWER_INSUFFICIENT,
    Status.INSUFFICIENT_FOR_CONCLUSION: ANSWER_INSUFFICIENT,
}


def question_of(request: NormalizedUserRequest, research: InteractionResearch) -> InteractionQuestion | None:
    """使用者實際的問題型態：重複成分比較 → None（另行處理）；其餘用 Controller 的結構化欄位，沒有時視為「能不能一起使用」。"""
    if research.mode == InteractionMode.DUPLICATE_COMPARISON:
        return None
    return request.interaction_question or InteractionQuestion.TOGETHER_USE


def _direct_answer(question: InteractionQuestion, answer: str, pair_text: str) -> str:
    if question == InteractionQuestion.TOGETHER_USE:
        return TOGETHER_ANSWERS[answer]
    if answer == ANSWER_ELIGIBLE:
        if question == InteractionQuestion.EXPLANATION:
            return f"資料庫對{pair_text}的說明如下。"
        return f"有，資料庫收錄了{pair_text}的交互作用。"
    return EXISTENCE_ANSWERS[answer]


def _reason(answer: str, names: _Names, top: InteractionFinding | None, pair_text: str, research: InteractionResearch) -> str:
    """一句簡短原因（不放證據筆數 / 來源 / 證據方向 / 技術說明；那些在按鈕裡）。"""
    if answer == ANSWER_ELIGIBLE:
        effect = (top.clinical_effect or "").strip() if top else ""
        return f"資料顯示：{effect}" if effect else f"資料庫收錄了{pair_text}的交互作用。"
    if answer == ANSWER_CONFLICT:
        return f"資料庫收錄了{pair_text}的相關紀錄，但證據互相不一致，目前不下肯定結論。"
    if answer == ANSWER_LINEAGE:
        return _lineage_summary(names, top)[0]
    if answer == ANSWER_INSUFFICIENT:
        return f"資料庫對{pair_text}只有待確認或背景性的紀錄。"
    pair = (_no_record_pairs(research) or [None])[0]
    who = _pair(_quote(pair.subject_a), _quote(pair.subject_b)) if pair else "這組對象"
    return f"{who}：資料庫沒有收錄這組的紀錄。{NO_RECORD_MEANING}"


def _compose_duplicate_answer(research: InteractionResearch, names: _Names) -> tuple[str, str, list[str], str]:
    """「有沒有重複成分」：直接回答是 / 否；依已確認的對應與未辨識覆蓋，不用交互作用句型。→ (title, reason, points, status)"""
    unresolved = _unresolved_total(research)
    if research.duplicates:
        nutrients = [entity_label(d.nutrient) for d in research.duplicates if entity_label(d.nutrient)]
        points = []
        if unresolved:
            points.append(f"仍有 {unresolved} 項成分尚未完成辨識，結果可能不完整。")
        if any(not d.same_identity_level_across_products for d in research.duplicates):
            points.append("兩項產品的成分形式不同，含量不能直接相加。")
        points.append("只表示成分重複；要評估攝取量，需要每份含量與每日份數。")
        return (f"有，兩項產品都含有{_list_names(nutrients)}。", "目前尚未評估兩項產品的實際攝取量。", points[:3],
                STATUS_DUPLICATE)
    reason = (f"但仍有 {unresolved} 項成分尚未完成辨識，所以不能確定沒有重複。" if unresolved
              else "目前只比對已確認的成分對應。")
    return ("以已確認的成分比對，沒有找到重複的成分。", reason, ["尚未辨識的成分不在比對範圍內。"], STATUS_NO_DUPLICATE)


def _compose_result(request: NormalizedUserRequest, bundle: EvidenceBundle) -> InteractionCard:
    """Answer-first：標題 = 直接回答使用者的問題；summary = 一句原因；重點（≤ 3）= 限制 / 還需要的資訊。
    證據筆數、來源、證據方向、技術說明都在按鈕（為什麼？/ 查看證據 / 查看成分對應）。"""
    research = bundle.interaction_research
    names = _Names(research)
    warnings = ([NOT_PRODUCT_RISK] if _has_product_subject(research) else []) + [DISCLAIMER]
    question = question_of(request, research)
    if question is None:
        title, reason, points, tag = _compose_duplicate_answer(research, names)
        return InteractionCard(response_type=ResponseType.INTERACTION_RESULT, title=title, summary=reason, key_points=points,
                               warnings=warnings, status_line=tag, status_tone=SemanticTone.ORANGE, actions=_actions(research))
    findings = _ranked_findings(research)
    if research.coverage_gap_subjects and not findings:
        # IDENTITY_NOT_IN_INTERACTION_CORPUS：資料涵蓋缺口。不請使用者改寫法；補查結果只是參考，不是 KG 結論，也不是「沒有交互作用」
        pair_text = _pair(*[_quote(s.text) for s in research.subjects[:2]]) if len(research.subjects) >= 2 else "這組對象"
        return InteractionCard(
            response_type=ResponseType.INTERACTION_RESULT, title=_direct_answer(question, ANSWER_NO_RECORD, pair_text),
            summary=coverage_gap_sentence(research.coverage_gap_subjects),
            key_points=[_gap_evidence_point(bundle), NO_RECORD_MEANING, LIMIT_BY_ANSWER[ANSWER_NO_RECORD]],
            warnings=warnings, status_line=STATUS_COVERAGE_GAP, status_tone=SemanticTone.ORANGE, actions=_actions(research))
    top = findings[0] if findings else None
    answer = ANSWER_BY_STATUS[top.conclusion_status] if top else ANSWER_NO_RECORD
    if top is not None:
        a, b = _finding_sides(names, top)
        pair_text = _pair(a, b)
    else:
        pair = (_no_record_pairs(research) or [None])[0]
        pair_text = _pair(_quote(pair.subject_a), _quote(pair.subject_b)) if pair else "這組對象"
    title = _direct_answer(question, answer, pair_text)
    reason = _reason(answer, names, top, pair_text, research)
    dup_point = _duplicate_point(research, names)
    points = [p for p in (dup_point, LIMIT_BY_ANSWER[answer]) if p]
    return InteractionCard(response_type=ResponseType.INTERACTION_RESULT, title=title, summary=reason, key_points=points,
                           warnings=warnings, status_line=STATUS_BY_ANSWER[answer], status_tone=SemanticTone.ORANGE,
                           actions=_actions(research))


# --------------------------------------------------------------------------- 需要補充（clarification）


def _clarify(summary: str, points: list[str], actions: list[ResponseAction] | None = None) -> InteractionCard:
    return InteractionCard(response_type=ResponseType.MISSING_INFORMATION, title=TITLE_CLARIFY, summary=summary,
                           key_points=points, actions=actions or [])


def _clarification(request: NormalizedUserRequest, bundle: EvidenceBundle) -> InteractionCard:
    research = bundle.interaction_research
    if research.status == InteractionResearchStatus.CORPUS_UNAVAILABLE:
        # 查詢失敗 / 資料尚未可用：如實說明，不當成「查無」，也不產生任何結論
        return _clarify("交互作用資料暫時查詢不到，這次沒有結果。請稍後再按「重新查詢」。", [],
                        [ResponseAction.RETRY])
    match = bundle.product_match
    if match.candidates and (match.is_ambiguous or match.needs_confirmation):
        return InteractionCard(response_type=ResponseType.MISSING_INFORMATION, title=TITLE_CLARIFY, summary="",
                               defer_to_generic=True)  # 先出既有的產品候選確認卡；確認前不顯示任何 interaction 結論
    unresolved = [s for s in research.subjects if s.status not in (
        SubjectResolutionStatus.RESOLVED, SubjectResolutionStatus.IDENTITY_NOT_IN_INTERACTION_CORPUS)]
    if unresolved:
        s = unresolved[0]
        label = _quote(s.text)
        if s.status == SubjectResolutionStatus.PRODUCT_NEEDS_CONFIRMATION:
            return _clarify(f"目前找不到{label}對應的官方產品紀錄，無法比對它的成分。",
                            [NOT_FOUND_NOT_FAKE_TEXT, COVERAGE_LIST_TEXT, "請提供完整品名、許可證字號，或改用成分名稱查詢。"])
        if s.status == SubjectResolutionStatus.PRODUCT_NO_CONFIRMED_INGREDIENTS:
            return _clarify(f"{_quote(s.product_name or s.text)}是官方登記的產品，但目前沒有已確認的成分對應，所以無法比對交互作用。",
                            ["請改用成分名稱查詢，例如產品標示的主要成分。"])
        if s.status == SubjectResolutionStatus.DRUG_ACTIVE_SUBSTANCE_UNMAPPED:
            return _clarify(f"{label}目前無法對應到藥品的有效成分，所以不能查它的交互作用。",
                            ["目前只能用藥品的有效成分名稱（英文）查詢，還不能從商品名判斷是哪一種藥。",
                             "請補充藥品的有效成分，我再幫你查。"])
        if s.status == SubjectResolutionStatus.AMBIGUOUS:
            return _clarify(f"{label}可能是成分，也可能是藥品，我還不確定你指的是哪一個。",
                            ["請補充說明，例如寫出完整的成分或藥品名稱。"])
        return _clarify(f"目前交互作用資料庫找不到{label}對應的成分或藥品名稱。",
                        ["請確認寫法，或改用常見的成分名稱／藥品的有效成分名稱。"])
    if research.clarification_reason == "DUPLICATE_COMPARISON_NEEDS_TWO_PRODUCTS":
        return _clarify("要比較重複成分，需要兩項都已確認的產品。", ["請提供兩項產品的完整品名或許可證字號。"])
    return _clarify("要查交互作用，至少需要兩個對象（成分、藥品或產品）。", ["請補充另一個要一起比較的對象。"])


def build_card(request: NormalizedUserRequest, bundle: EvidenceBundle) -> InteractionCard:
    """interaction 問題的 primary card 內容（deterministic）。"""
    if interaction_response_type(bundle) == ResponseType.INTERACTION_RESULT:
        return _compose_result(request, bundle)
    return _clarification(request, bundle)


# --------------------------------------------------------------------------- On-demand detail（deterministic）


def user_facing_limitations(item: InteractionEvidenceItem) -> str:
    """使用者來源卡的限制說明：不顯示未解釋的 GRADE 術語（完整的 limitations_zh / certainty 仍保存在結構化 evidence 與完整分析 facts）。

    含 GRADE 字樣的句子改成固定白話；其餘限制照原文保留。"""
    parts = [p.strip() for p in re.split(r"(?<=[；;。])", item.limitations_zh or "") if p.strip()]
    kept = [p for p in parts if not GRADE_RE.search(p)]
    explained = len(kept) < len(parts) or item.evidence_level == "OFFICIAL_SYNTHESIS"
    return "".join(kept + ([SOURCE_LIMITATION] if explained or not kept else []))


def _evidence_paragraph(item: InteractionEvidenceItem, *, duplicate_rule: bool = False) -> str:
    kind = "官方資料" if item.is_official else "非官方資料（例如研究文獻）"
    lines = [f"・{item.source_title or '（未命名來源）'}", f"證據方向：{direction_label(item, duplicate_rule=duplicate_rule)}",
             kind + (f"｜{item.authority}" if item.authority else "")]
    if item.result_summary_zh:
        lines.append(f"摘要：{_clip(item.result_summary_zh, 100)}")
    lines.append(f"限制：{_clip(user_facing_limitations(item), 90)}")
    return "\n".join(lines)


def evidence_detail(request: NormalizedUserRequest, bundle: EvidenceBundle) -> list[str]:
    """「查看證據來源」：逐筆列出證據與 supports_direction 的白話語意（不支持 / 不一致 / 背景資料不會被寫成支持）。"""
    research = bundle.interaction_research
    names = _Names(research)
    body: list[str] = []
    budget = MAX_EVIDENCE_LISTED
    omitted = 0
    for f in _ranked_findings(research):
        a, b = _finding_sides(names, f)
        body.append(f"{_pair(a, b)}：{STATUS_PHRASES[f.conclusion_status]}\n"
                    f"證據 {len(f.evidence)} 筆（支持 {f.supporting_evidence_count} 筆，不支持、不一致或背景資料 {f.non_supporting_evidence_count} 筆）")
        for item in sorted(f.evidence, key=lambda e: (not e.counts_as_support, e.evidence_id)):
            if budget <= 0:
                omitted += 1
                continue
            budget -= 1
            body.append(_evidence_paragraph(item))
    for d in research.duplicates:
        if not d.duplicate_rule_evidence:
            continue
        body.append(f"重複成分說明：{_quote(entity_label(d.nutrient))}")
        for item in sorted(d.duplicate_rule_evidence, key=lambda e: e.evidence_id):
            if budget <= 0:
                omitted += 1
                continue
            budget -= 1
            body.append(_evidence_paragraph(item, duplicate_rule=True))
    if omitted:
        body.append(f"另有 {omitted} 筆證據未列出。")
    return body or ["這次沒有可列出的交互作用證據來源。"]


def _mapping_line(m, raw_forms_listed: set[str]) -> str | None:
    raw = "、".join(m.raw_texts[:2]) if m.raw_texts else ""
    source = f"官方成分「{raw}」" if raw else "官方成分"
    entity = entity_label(m.entity)
    if m.via is not None:   # 由成分形式推導出營養素類別：形式已另列時不重複
        if m.via.entity_id in raw_forms_listed:
            return None
        return f"・{source} → {_quote(entity_label(m.via))}（成分形式）→ 屬於{_quote(entity)}類"
    kind = KIND_LABELS.get(m.entity.kind, "")
    line = f"・{source} → {_quote(entity)}（{kind}）"
    if m.parent is not None:
        line += f"，屬於{_quote(entity_label(m.parent))}類"
    return line


def mapping_detail(request: NormalizedUserRequest, bundle: EvidenceBundle) -> list[str]:
    """「查看成分對應」：官方成分 → 成分形式 → 營養素類別；只列已確認的正式對應。"""
    research = bundle.interaction_research
    body: list[str] = []
    for s in research.subjects:
        if s.status == SubjectResolutionStatus.RESOLVED and not s.ingredient_mappings and s.entities:
            for e in s.entities:
                body.append(f"{_quote(s.text)} → {_quote(entity_label(e))}（{KIND_LABELS.get(e.kind, '')}）")
            continue
        if not s.ingredient_mappings:
            continue
        direct_forms = {m.entity.entity_id for m in s.ingredient_mappings if m.via is None}
        lines = [ln for ln in (_mapping_line(m, direct_forms) for m in s.ingredient_mappings) if ln]
        shown = lines[:MAX_MAPPING_LINES]
        head = f"{_quote(s.product_name or s.text)}的成分對應"
        if len(lines) > len(shown):
            shown.append(f"（另有 {len(lines) - len(shown)} 項已確認對應未列出）")
        if s.unresolved_ingredient_count:
            shown.append(f"另有 {s.unresolved_ingredient_count} 項官方成分尚未完成辨識，未納入比對。")
        body.append("\n".join([head] + shown))
    return body or ["這次沒有可列出的成分對應。"]


def why_detail(request: NormalizedUserRequest, bundle: EvidenceBundle) -> list[str]:
    """「為什麼？」：用白話說明資料庫記載的影響與適用條件（deterministic；只重述 interaction_research 的結構化欄位）。"""
    research = bundle.interaction_research
    names = _Names(research)
    body: list[str] = []
    for f in _ranked_findings(research)[:3]:
        a, b = _finding_sides(names, f)
        lines = [f"{_pair(a, b)}：{STATUS_PHRASES[f.conclusion_status]}"]
        for label, value in (("資料庫記載的影響", f.clinical_effect), ("機制說明", f.mechanism_summary),
                             ("資料庫記載的建議", f.recommendation), ("時間間隔建議", f.applicability.timing_advice)):
            if value:
                lines.append(f"・{label}：{value}")
        if f.conclusion_status == Status.CONCLUSION_ELIGIBLE:
            if f.applicability.conditions:
                lines.append(f"・適用條件（資料庫原文）：{f.applicability.conditions}")
            lines.append(f"・{APPLICABILITY_NOT_EVALUATED}")
        elif f.conclusion_status == Status.LINEAGE_NOT_INHERITED:
            lines.append(f"・{_lineage_summary(names, f)[0]}")
        body.append("\n".join(lines))
    if research.duplicates:
        nutrients = [entity_label(d.nutrient) for d in research.duplicates if entity_label(d.nutrient)]
        lines = [f"重複成分：兩項產品都含有{_list_names(nutrients)}。", f"・{DUPLICATE_MEANING}"]
        unresolved = _unresolved_total(research)
        if unresolved:
            lines.append(f"・仍有 {unresolved} 項成分尚未完成辨識，未納入重複成分比對。")
        body.append("\n".join(lines))
    if research.coverage_gap_subjects and not research.findings:
        body.append("\n".join([coverage_gap_sentence(research.coverage_gap_subjects), f"・{_gap_evidence_point(bundle)}",
                               f"・{NO_RECORD_MEANING}"]))
    elif _no_record_pairs(research) and not research.findings:
        lines = [f"資料庫沒有收錄這組關係。{NO_RECORD_MEANING}"]
        note = _fallback_note(bundle, research)
        if note:
            lines.append(f"・{note}")
        body.append("\n".join(lines))
    body.append(DISCLAIMER)
    return body


# --------------------------------------------------------------------------- 完整分析（LLM 只拿 structured facts）


def interaction_facts(request: NormalizedUserRequest, bundle: EvidenceBundle) -> dict:
    """給 LLM 的 facts：全部來自 InteractionResearch，使用白話名稱與狀態描述（不含內部 ID / enum 名稱）。"""
    research = bundle.interaction_research
    names = _Names(research)
    findings = []
    for f in _ranked_findings(research):
        a, b = _finding_sides(names, f)
        findings.append({
            "對象": [a.strip("「」"), b.strip("「」")],
            "結論資格": STATUS_PHRASES[f.conclusion_status],
            "只有經成分形式對應": any(m.match_basis == VIA_FORM for m in f.subject_matches),
            "資料庫記載的影響": f.clinical_effect, "機制說明": f.mechanism_summary,
            "資料庫記載的建議": f.recommendation, "適用條件原文": f.applicability.conditions,
            "劑量背景": f.applicability.dose_context, "族群背景": f.applicability.population_context,
            "時間間隔建議": f.applicability.timing_advice,
            "適用條件是否已對照使用者的劑量、用法、族群": "尚未評估" if not f.applicability.evaluated else "已評估",
            "證據": [{"來源": e.source_title, "官方來源": e.is_official, "證據方向": direction_label(e),
                      "摘要": e.result_summary_zh, "限制": e.limitations_zh} for e in f.evidence],
        })
    duplicates = [{
        "重複的成分": entity_label(d.nutrient), "產品": [p.product_name for p in d.products],
        "成分形式相同": d.same_identity_level_across_products, "劑量評估": "尚未評估",
        "不能推論": ["過量", "超過上限", "毒性", "不能一起吃", "一定有風險"],
        "還缺少的資訊": ["每份含量", "份量", "每日份數"]} for d in research.duplicates]
    return {
        "使用者問題": request.question or request.normalized_text,
        "對象": [{"名稱": s.text, "類型": s.kind.value} for s in research.subjects],
        "交互作用紀錄": findings,
        "資料庫沒有收錄的組合": [[p.subject_a, p.subject_b] for p in _no_record_pairs(research)],
        "結構化交互作用資料尚未收錄的對象（只是資料涵蓋缺口，不是拼錯字，也不代表沒有交互作用）": list(research.coverage_gap_subjects),
        "重複成分": duplicates,
        "尚有未完成辨識的成分數": {c.product_id: c.unresolved_count for c in research.duplicate_coverage if c.unresolved_count},
    }


GRADE_RE = re.compile(r"GRADE", re.IGNORECASE)
SOURCE_LIMITATION = "這是官方資料摘要，是否適用仍需看實際劑量與使用情境。"
DOSE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|μg|µg|mcg|ug|IU|毫克|微克|公克|g\b)", re.IGNORECASE)
INTERNAL_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")   # N_CA / CONCLUSION_ELIGIBLE / NO_SIGNIFICANT_EFFECT …
FORBIDDEN_RE = re.compile(
    r"不能一起吃|不可以一起吃|不能同時|不可同時|不應同時|禁止併用|過量|超過上限|超標|有毒|中毒|一定(?:會|有)|絕對"
    r"|沒有交互作用|無交互作用|不會有交互作用|可以一起吃|可以放心|安全無虞|沒有風險|不會有風險")
ASSERTS_INTERACTION_RE = re.compile(r"(?<![不未無沒])(?:存在|確實有|會產生|會有|已證實)[^，。；\n]{0,6}交互作用")


def body_is_safe(body: str, research: InteractionResearch) -> bool:
    """LLM 正文的 guard：不得含內部代碼 / 禁止說法；沒有任何可肯定的結論時不得斷言存在交互作用。"""
    if INTERNAL_TOKEN_RE.search(body) or FORBIDDEN_RE.search(body):
        return False
    if DOSE_RE.search(body):  # 劑量數字（以及它是「誰的」攝取量）只引用資料庫原文，不讓 LLM 轉述（避免主語被改錯）
        return False
    if not research.conclusion_eligible and ASSERTS_INTERACTION_RE.search(body):
        return False
    return True


def deterministic_body(request: NormalizedUserRequest, bundle: EvidenceBundle) -> str:
    """OpenAI 無法使用 / 正文不合格時的固定模板：只重述結構化 facts。"""
    research = bundle.interaction_research
    names = _Names(research)
    lines: list[str] = []
    for f in _ranked_findings(research):
        a, b = _finding_sides(names, f)
        lines.append(f"{_pair(a, b)}：{STATUS_PHRASES[f.conclusion_status]}。")
        for label, value in (("資料庫記載的影響", f.clinical_effect), ("機制", f.mechanism_summary),
                             ("資料庫記載的建議", f.recommendation), ("時間間隔建議", f.applicability.timing_advice),
                             ("適用條件（資料庫原文）", f.applicability.conditions)):
            if value:
                lines.append(f"・{label}：{value}")
        lines.append(f"・證據：{f.supporting_evidence_count} 筆支持，{f.non_supporting_evidence_count} 筆不支持、不一致或僅為背景資料。")
    for d in research.duplicates:
        lines.append(f"重複成分：兩項產品都含有{_quote(entity_label(d.nutrient))}。")
    for p in _no_record_pairs(research):
        lines.append(f"{_pair(_quote(p.subject_a), _quote(p.subject_b))}：交互作用資料庫沒有收錄這組的紀錄。")
    if research.coverage_gap_subjects and not research.findings:
        lines.append(coverage_gap_sentence(research.coverage_gap_subjects))
    return "\n".join(lines)


def limitation_block(bundle: EvidenceBundle) -> str:
    """完整分析一律由程式附上的限制說明（不交給 LLM 決定）。"""
    research = bundle.interaction_research
    lines: list[str] = []
    if research.findings:
        lines.append(f"・{APPLICABILITY_NOT_EVALUATED}")
    if research.duplicates:
        lines.append(f"・重複成分：{DUPLICATE_MEANING}")
        unresolved = _unresolved_total(research)
        if unresolved:
            lines.append(f"・仍有 {unresolved} 項成分尚未完成辨識，未納入重複成分比對。")
    if _no_record_pairs(research) or research.coverage_gap_subjects:
        lines.append(f"・{NO_RECORD_MEANING}")
    if _has_product_subject(research):
        lines.append(f"・{NOT_PRODUCT_RISK}")
    lines.append(f"・{DISCLAIMER}")
    return "限制與提醒：\n" + "\n".join(lines)


def evidence_digest(bundle: EvidenceBundle) -> str:
    """完整分析一律附上的證據摘要（資料庫原文；劑量與主語以原文為準，不經 LLM 轉述）。"""
    research = bundle.interaction_research
    lines: list[str] = []
    for f in _ranked_findings(research)[:2]:
        for e in sorted(f.evidence, key=lambda e: (not e.counts_as_support, e.evidence_id))[:4]:
            if e.result_summary_zh:
                lines.append(f"・{e.source_title or '（未命名來源）'}：{_clip(e.result_summary_zh, 120)}（{direction_label(e)}）")
    return ("證據摘要（資料庫原文）：\n" + "\n".join(lines)) if lines else ""


def headline_block(request: NormalizedUserRequest, bundle: EvidenceBundle) -> str:
    card = _compose_result(request, bundle)
    return f"{card.title}\n{card.summary}"


def generate_body(llm, request: NormalizedUserRequest, bundle: EvidenceBundle, log_warning) -> tuple[str, bool]:
    """→ (正文, 是否使用固定模板)。LLM 只收到 structured facts；不合格就用固定模板。"""
    research = bundle.interaction_research
    body = ""
    if llm is not None:
        try:
            body = llm.generate_text(instructions=INTERACTION_REPORTER_INSTRUCTIONS,
                                     input_text=json.dumps(interaction_facts(request, bundle), ensure_ascii=False)).strip()
        except ServiceError as exc:
            log_warning("interaction_reporter_llm_failed", exc.code.value)
        if body and not body_is_safe(body, research):
            log_warning("interaction_reporter_body_rejected", None)
            body = ""
    if body:
        return body, False
    return deterministic_body(request, bundle), True
