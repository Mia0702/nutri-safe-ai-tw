"""Candidate Relevance Gate / Product-Core Relevance Gate（deterministic、無 LLM）。

搜尋可以寬，但「名稱相似、未確認」的候選在顯示給使用者 / 參與判定之前，必須支持使用者寫法的「產品身分」：
品牌 / 公司相同只代表「可能是同品牌」，不代表「可能是同產品」。

使用者寫法（正規化後）逐字分成三類：
* FORM：劑型 / 包裝 / 規格 / 數量、「X牌」的牌、「食品」等一般標示 —— 不影響身分，忽略。
* NUTRIENT：營養素 / 成分寫法（維生素寫法、單字礦物質、固定類別用語、資料庫官方成分名稱與 ingredient entity 名稱）——
  是產品核心的一部分，但只靠它不能建立身分；候選必須「完整」含有（維生素 A ≠ 維生素 C，不容錯字）。
* DISTINCTIVE：其餘字元（品牌、品名特有字樣、產品核心描述）。

候選（官方品名｜申請商）逐字對齊（LCS）後：
* NUTRIENT 字元有任何沒對上 → 不符（成分核心衝突）
* DISTINCTIVE 有連續 >= 2 字沒對上 → 不符（缺一整段品牌 / 核心描述，例如同品牌但不同產品、未知品牌＋一般營養素）
* 沒對上的核心字元超過 MAX_MISSED_RATIO → 不符；單一零星錯字容忍
* 使用者寫法依序完整包含於官方品名（只有省略字）→ 符合
核心字元少於 2 個（例如只有劑型）→ 不擋。只用字元比較，不判斷語意；不針對任何產品、品牌或問句。
"""

import re
from typing import Iterable

from app.normalization import normalize_name

MIN_CORE_CHARS = 2
MAX_MISSED_RUN = 1          # 允許的連續未對上 DISTINCTIVE 字數（單一錯字 / 同音字）
MAX_MISSED_RATIO = 0.2      # 未對上的核心字元比例上限（至少容忍 1 字）
MAX_COINCIDENTAL_CHARS = 2  # 顯示缺少片段時，夾在缺字之間的巧合對上字數上限

FORM, NUTRIENT, DISTINCTIVE = "F", "N", "D"

# 劑型 / 包裝 / 規格 / 一般標示（不影響產品身分）
FORM_TERMS = (
    "軟膠囊", "硬膠囊", "植物膠囊", "膠囊", "膜衣錠", "糖衣錠", "口含錠", "咀嚼錠", "嚼錠", "發泡錠", "錠劑", "錠",
    "顆粒", "粉末", "粉包", "粉", "口服液", "飲品", "飲", "液", "果凍", "軟糖", "滴劑", "膏",
    "保健食品", "營養補充品", "食品",
    "capsules", "capsule", "softgels", "softgel", "tablets", "tablet", "powder",
)
# 一般營養素 / 成分類別寫法（產品核心的一部分，但不是身分識別）
NUTRIENT_CATEGORY_TERMS = (
    "綜合維生素", "維生素", "維他命", "礦物質", "魚油", "益生菌", "乳酸菌",
    "omega3", "fishoil", "multivitamin", "vitamin", "probiotics", "probiotic",
)
SINGLE_CHAR_NUTRIENTS = "鈣鐵鋅鎂銅硒鉻碘錳鉀"
BRAND_MARKERS = "牌"
_VITAMIN_RE = re.compile(r"(?:維生素|維他命|vitamin)(?:[a-z]\d{0,2})?群?|b群")
_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?(?:mg|mcg|μg|µg|g|iu|ml|毫克|微克|公克|國際單位|粒|顆|錠|入|包|瓶|盒|%)")


def _terms(values: Iterable[str], min_len: int = 1) -> list[str]:
    return sorted({t for t in (normalize_name(v) for v in values) if len(t) >= min_len}, key=len, reverse=True)


def char_classes(text: str, generic_terms: Iterable[str] = ()) -> list[str]:
    """正規化後文字逐字分類（FORM / NUTRIENT / DISTINCTIVE）。資料庫來的成分名稱至少 2 字才採用。"""
    classes = [DISTINCTIVE] * len(text)

    def mark(spans, cls):
        for start, end in spans:
            for i in range(start, end):
                if classes[i] == DISTINCTIVE:
                    classes[i] = cls

    def finds(terms):
        return [(m.start(), m.end()) for t in terms for m in re.finditer(re.escape(t), text)]

    mark([(m.start(), m.end()) for m in _AMOUNT_RE.finditer(text)], FORM)
    mark(finds(_terms(FORM_TERMS)), FORM)
    mark([(m.start(), m.end()) for m in _VITAMIN_RE.finditer(text)], NUTRIENT)
    mark(finds(_terms([*NUTRIENT_CATEGORY_TERMS, *_terms(generic_terms, 2)])), NUTRIENT)
    for i, ch in enumerate(text):
        if classes[i] == DISTINCTIVE and ch in SINGLE_CHAR_NUTRIENTS:
            classes[i] = NUTRIENT
        elif classes[i] == DISTINCTIVE and ch in BRAND_MARKERS:
            classes[i] = FORM
    return classes


def core_text(text: str, generic_terms: Iterable[str] = ()) -> str:
    norm = normalize_name(text)
    return "".join(ch for ch, c in zip(norm, char_classes(norm, generic_terms)) if c != FORM)


def distinctive_segments(names: Iterable[str], generic_terms: Iterable[str] = ()) -> list[str]:
    """使用者寫法中 DISTINCTIVE 的連續片段（>= 2 字；去重，被其他片段包含者省略）。"""
    segments: list[str] = []
    for name in names:
        text = normalize_name(name)
        classes = char_classes(text, generic_terms)
        segments += [s for s in "".join(ch if c == DISTINCTIVE else "|" for ch, c in zip(text, classes)).split("|")
                     if len(s) >= MIN_CORE_CHARS]
    unique = list(dict.fromkeys(segments))
    return [s for s in unique if not any(s != o and s in o for o in unique)]


def _aligned_positions(query: str, target: str) -> set[int]:
    """LCS 對齊：query 中與 target 依序對上的字元位置。"""
    n, m = len(query), len(target)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if query[i] == target[j] else max(dp[i + 1][j], dp[i][j + 1])
    out, i, j = set(), 0, 0
    while i < n and j < m:
        if query[i] == target[j]:
            out.add(i)
            i, j = i + 1, j + 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


def _ordered_subsequence(query: str, name: str) -> bool:
    it = iter(name)
    return bool(query) and all(ch in it for ch in query)


def _single_edit(a: str, b: str) -> int | None:
    """a 與 b 只差一個字（替換 / 多一字 / 少一字）時回傳 a 中被改動的位置（少一字時為 -1），否則 None。"""
    if a == b or abs(len(a) - len(b)) > 1:
        return None
    if len(a) == len(b):
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        return diff[0] if len(diff) == 1 else None
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    for i in range(len(longer)):
        if longer[:i] + longer[i + 1:] == shorter:
            return i if longer is a else -1
    return None


def missed_core_terms(query: str, product_name: str, company_name: str | None,
                      generic_terms: Iterable[str] = ()) -> tuple[bool, list[str]]:
    """(候選是否支持此寫法的產品核心, 候選缺少的核心片段：以包含缺字的完整 DISTINCTIVE 片段表示)。"""
    q = normalize_name(query)
    name = normalize_name(product_name)
    if _ordered_subsequence(q, name):
        return True, []  # 使用者寫法只是官方品名省略字（截斷 / 簡稱）
    classes = char_classes(q, generic_terms)
    # 與官方品名（或去掉劑型字樣的核心）只差一個字：真正的單字錯字 / 同音字 → 保留為候選（只會請使用者確認），
    # 但改動的字若是營養素 / 成分字樣（例如維生素 A → C）仍是核心衝突
    for target in (name, core_text(product_name, generic_terms)):
        edited = _single_edit(q, target) if target else None
        if edited is not None and (edited < 0 or classes[edited] != NUTRIENT):
            return True, []
    core = [i for i, c in enumerate(classes) if c != FORM]
    if len(core) < MIN_CORE_CHARS:
        return True, []
    aligned = _aligned_positions(q, f"{name}|{normalize_name(company_name or '')}")
    runs, current = [], []
    for i in core:  # 依核心字元順序找連續未對上的片段（中間的 FORM 字不算斷開）
        if i in aligned:
            if current:
                runs.append(current)
            current = []
        else:
            current.append(i)
    if current:
        runs.append(current)
    missed = [i for run in runs for i in run]
    terms = _segments_containing(q, classes, missed)
    nutrient_conflict = any(classes[i] == NUTRIENT for i in missed)
    long_run = any(len(run) > MAX_MISSED_RUN for run in runs)
    too_many = len(missed) > max(1, int(len(core) * MAX_MISSED_RATIO))
    return not (nutrient_conflict or long_run or too_many), terms


def _segments_containing(q: str, classes: list[str], missed: list[int]) -> list[str]:
    """缺字所在的完整連續片段（同一類別的連續核心字；不顯示對齊造成的半個詞）。"""
    out, start = [], 0
    for i in range(1, len(q) + 1):
        if i == len(q) or classes[i] != classes[start]:
            hit = [m for m in missed if start <= m < i]
            if classes[start] == NUTRIENT and hit:
                out.append(q[start:i])  # 營養素 / 成分字樣整段顯示
            elif classes[start] == DISTINCTIVE and hit:
                # 缺字片段；中間只有 <= 2 個巧合對上的字時視為同一段（例如未知品牌中剛好有字母對上）
                lo = prev = hit[0]
                for m in hit[1:] + [None]:
                    if m is not None and m - prev - 1 <= MAX_COINCIDENTAL_CHARS:
                        prev = m
                        continue
                    out.append(q[lo:prev + 1])
                    if m is not None:
                        lo = prev = m
            start = i
    return out


def supports_product_core(queries: list[str], product_name: str, company_name: str | None,
                          generic_terms: Iterable[str] = ()) -> bool:
    return any(missed_core_terms(q, product_name, company_name, generic_terms)[0] for q in queries)
