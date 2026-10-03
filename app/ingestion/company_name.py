"""申請商（公司名稱）正規化：只移除有充分規則依據的行政附註。

背景：TFDA 申請商欄位偶爾把行政註記寫進括號（許可證移轉、核准日期等），
但括號也可能是正式公司名稱的一部分（例如「某某(台灣)股份有限公司」）。
因此不能「刪除所有括號」。

規則（皆為一般性規則，不針對特定公司）：
1. 只處理頂層、配對完整的括號（全形 / 半形）；括號不配對 → AMBIGUOUS，保留原文。
2. 括號內容依「行政證據」分類：
   - 日期（民國或西元 年/月/日）
   - 行政關鍵詞（核准、轉移、移轉、讓與、承受、變更、更名、許可證…）
   強證據 = 日期 + 至少 1 個關鍵詞，或至少 2 個不同關鍵詞。
3. 只有同時符合以下條件才移除（CLEANED）：
   - 強證據
   - 位於名稱尾端（之後沒有其他文字）
   - 移除後剩餘名稱以公司組織型態結尾（公司、分公司、商行…），確保仍是完整名稱
4. 有行政訊號但不符合第 3 點（證據不足、不在尾端、剩餘名稱不完整）→ AMBIGUOUS，保留原文。
5. 沒有行政訊號的括號視為正式名稱的一部分，保留。

原始申請商字串一律保留在 raw_import_records.raw_data，本模組只決定 normalized 名稱。
"""

import re
from dataclasses import dataclass, field
from enum import Enum

_OPEN, _CLOSE = "(（", ")）"
_DATE_RE = re.compile(r"\d{2,4}\s*[/.\-年]\s*\d{1,2}\s*[/.\-月]\s*\d{1,2}\s*日?")
_ADMIN_KEYWORDS = (
    "核准", "核備", "轉移", "移轉", "讓與", "讓渡", "承受", "承接",
    "變更", "更名", "改名", "原名", "許可證", "註銷", "合併", "併購",
)
_ORG_SUFFIX_RE = re.compile(r"(公司|商行|企業社|工廠|藥廠|合作社|農會|基金會|研究所)$")


class CompanyNameStatus(str, Enum):
    UNCHANGED = "unchanged"   # 沒有可移除的行政附註
    CLEANED = "cleaned"       # 已移除行政附註
    AMBIGUOUS = "ambiguous"   # 有行政訊號但無法可靠判斷，保留原文


@dataclass
class CompanyNameResult:
    raw: str
    name: str
    status: CompanyNameStatus
    removed_notes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class _Group:
    start: int
    end: int  # exclusive，含右括號
    content: str


def _top_level_groups(text: str) -> list[_Group] | None:
    """回傳頂層括號群組；括號不配對時回傳 None。"""
    groups, depth, start = [], 0, -1
    for i, ch in enumerate(text):
        if ch in _OPEN:
            if depth == 0:
                start = i
            depth += 1
        elif ch in _CLOSE:
            if depth == 0:
                return None
            depth -= 1
            if depth == 0:
                groups.append(_Group(start, i + 1, text[start + 1 : i]))
    return None if depth else groups


def _admin_evidence(content: str) -> tuple[bool, int]:
    """(是否有日期, 不同行政關鍵詞數)"""
    return bool(_DATE_RE.search(content)), sum(1 for kw in _ADMIN_KEYWORDS if kw in content)


def normalize_company_name(raw: str) -> CompanyNameResult:
    text = (raw or "").strip()
    groups = _top_level_groups(text)
    if groups is None:
        return CompanyNameResult(text, text, CompanyNameStatus.AMBIGUOUS, reasons=["unbalanced_parentheses"])

    # 由尾端往前收集連續的強證據行政附註
    cut, removed, reasons = len(text), [], []
    for g in reversed(groups):
        has_date, kw_count = _admin_evidence(g.content)
        strong = (has_date and kw_count >= 1) or kw_count >= 2
        if strong and text[g.end : cut].strip() == "":
            removed.insert(0, g.content.strip())
            cut = g.start
            continue
        break
    remaining = groups[: len(groups) - len(removed)]
    for g in remaining:
        has_date, kw_count = _admin_evidence(g.content)
        if has_date or kw_count:
            strong = (has_date and kw_count >= 1) or kw_count >= 2
            reasons.append("admin_note_not_trailing" if strong else "admin_signal_weak")

    name = text[:cut].rstrip()
    if removed and not _ORG_SUFFIX_RE.search(name):
        reasons.append("remainder_not_company_name")
    if reasons:
        # 無法可靠判斷時整筆保留原文
        return CompanyNameResult(text, text, CompanyNameStatus.AMBIGUOUS, reasons=reasons)
    if removed:
        return CompanyNameResult(text, name, CompanyNameStatus.CLEANED, removed_notes=removed, reasons=["admin_note_removed"])
    return CompanyNameResult(text, text, CompanyNameStatus.UNCHANGED)
