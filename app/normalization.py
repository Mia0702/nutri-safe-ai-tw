"""通用的文字正規化與比對規則。

這裡只放一般性規則（Unicode 正規化、字元比對、字號格式），
不得放入任何特定產品、特定問句或特定答案。
"""

import re
import unicodedata
import uuid

# canonical ID 命名空間：Supabase 與 Neo4j 用同一套 uuid5 產生 ID
_ID_NAMESPACE = uuid.UUID("7b0f7c52-3f43-4d0e-9a59-6f1b3f1e2a10")

_PERMIT_CODE_RE = re.compile(r"([A-Z]\d{5}|\d{6})")
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
    """從「衛部健食字第A00235號」或「A00235」取出正規化代碼。"""
    norm = unicodedata.normalize("NFKC", text or "").upper()
    match = _PERMIT_CODE_RE.search(norm)
    return match.group(1) if match else None


def extract_permit_type(text: str) -> str | None:
    match = _PERMIT_TYPE_RE.search(unicodedata.normalize("NFKC", text or ""))
    return match.group(1) if match else None
