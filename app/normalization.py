"""通用的文字正規化與比對規則。

這裡只放一般性規則（Unicode 正規化、字元比對、字號格式），
不得放入任何特定產品、特定問句或特定答案。
"""

import re
import unicodedata
import uuid

# canonical ID 命名空間：Supabase 與 Neo4j 用同一套 uuid5 產生 ID
_ID_NAMESPACE = uuid.UUID("7b0f7c52-3f43-4d0e-9a59-6f1b3f1e2a10")

# 官方登記字號：健康食品「英文字母＋5 碼」，或一段完整的數字（健康食品 6 碼、錠狀膠囊類查驗登記 / 核備 10 碼…）。
# 數字段必須完整取出（前後不能再接數字或英文字母）：不截斷、不轉 integer、保留前導零。
_PERMIT_CODE_RE = re.compile(r"([A-Z]\d{5}(?!\d)|(?<![0-9A-Z])\d{6,}(?!\d))")
_PERMIT_TYPE_RE = re.compile(r"(健食規字|健食字)")


def stable_id(kind: str, natural_key: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"{kind}:{natural_key}"))


def normalize_name(text: str) -> str:
    """NFKC、轉小寫、移除空白 / 標點 / 符號（含 ®、™）。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    kept = []
    for ch in text:
        category = unicodedata.category(ch)
        if category[0] in ("P", "S", "Z", "C"):
            continue
        kept.append(ch)
    return "".join(kept)


_TRAILING_QUALIFIER_RE = re.compile(r"\s*[(（\[【][^()（）\[\]【】]*[)）\]】]\s*$")


def base_product_name(name: str) -> str:
    """去掉品名尾端的括號規格（口味、包裝、版本等），例如「某產品(原味)」→「某產品」。

    一般性規則：只移除「尾端」成對括號；括號在中間或去掉後為空時保留原名。
    """
    base = name or ""
    while True:
        stripped = _TRAILING_QUALIFIER_RE.sub("", base)
        if stripped == base or not stripped.strip():
            return base.strip()
        base = stripped


def search_terms(text: str, max_terms: int = 40) -> list[str]:
    """產生給資料庫做片段比對的詞：完整名稱 + 二字片段。"""
    norm = normalize_name(text)
    if not norm:
        return []
    terms = [norm]
    if len(norm) > 2:
        terms.extend(norm[i : i + 2] for i in range(len(norm) - 1))
    seen: set[str] = set()
    unique = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            unique.append(term)
    return unique[:max_terms]


def _lcs_length(a: str, b: str) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ch_a in a:
        cur = [0]
        for j, ch_b in enumerate(b, start=1):
            cur.append(prev[j - 1] + 1 if ch_a == ch_b else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def name_match_score(query: str, candidate: str) -> float:
    """0~1 的名稱相似度。

    coverage：查詢字串有多少比例依序出現在候選名稱中
    precision：候選名稱有多少比例被查詢涵蓋
    完全相同 = 1.0；查詢完整包含於候選名稱時，依 precision 給分。
    """
    q, c = normalize_name(query), normalize_name(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    lcs = _lcs_length(q, c)
    coverage = lcs / len(q)
    precision = lcs / len(c)
    if coverage < 0.5:
        return round(coverage * precision, 4)
    return round(0.6 * coverage + 0.4 * precision, 4)


def extract_permit_code(text: str) -> str | None:
    """從「衛部健食字第A00235號」、「A00235」或完整數字字號（如 10 碼查驗登記證號）取出正規化代碼。

    回傳字串原樣（含前導零）；數字段不截斷。
    """
    norm = unicodedata.normalize("NFKC", text or "").upper()
    match = _PERMIT_CODE_RE.search(norm)
    return match.group(1) if match else None


def extract_permit_type(text: str) -> str | None:
    match = _PERMIT_TYPE_RE.search(unicodedata.normalize("NFKC", text or ""))
    return match.group(1) if match else None


def _normalized_with_positions(text: str) -> tuple[str, list[int]]:
    """normalize_name 逐字版本：回傳正規化字串與每個字對應的原文位置。"""
    chars, positions = [], []
    for i, ch in enumerate(text or ""):
        for out in normalize_name(ch):
            chars.append(out)
            positions.append(i)
    return "".join(chars), positions


def locate_in_text(text: str, mention: str) -> tuple[str | None, int]:
    """在使用者輸入中找出 mention 對應的原文片段（近似子字串比對，只看格式正規化後的字元）。

    回傳 (原文片段, 編輯距離)。完全相同（只差大小寫 / 全半形 / 空白 / 標點）→ 距離 0；
    差異超過 mention 長度的三分之一（至少容忍 2 字）→ (None, 距離)，代表輸入中沒有這個名稱。
    """
    target = normalize_name(mention)
    norm, positions = _normalized_with_positions(text)
    if not target or not norm:
        return None, len(target)
    start = norm.find(target)
    if start >= 0:
        return text[positions[start]:positions[start + len(target) - 1] + 1], 0
    m, n = len(target), len(norm)
    # Sellers 近似子字串：dp[i][j] = target[:i] 對齊到結束於 norm[j-1] 的子字串的最小編輯距離
    prev = [0] * (n + 1)
    starts_prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur, starts = [i] + [0] * n, [0] + [0] * n
        for j in range(1, n + 1):
            options = [(prev[j - 1] + (target[i - 1] != norm[j - 1]), starts_prev[j - 1]),
                       (prev[j] + 1, starts_prev[j]), (cur[j - 1] + 1, starts[j - 1])]
            cur[j], starts[j] = min(options)
        prev, starts_prev = cur, starts
    distance, end = min((prev[j], -j) for j in range(1, n + 1))
    end = -end  # 同距離時取較長的片段（替換字優先於刪字）
    if distance > max(2, m // 3):
        return None, distance
    begin = min(starts_prev[end], end - 1)
    return text[positions[begin]:positions[end - 1] + 1], distance
