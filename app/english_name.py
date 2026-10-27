"""官方英文品名（TFDA product_name_en）exact 比對的純函式。

只做 identity 判斷需要的規則，不含 I/O：
* conservative key：NFKC → casefold → trim → 連續空白折成一個空格（與 D2 審核資料集的 normalize_alias_text 相同）。
  不移除標點、不 stemming、不翻譯、不做相似度。
* runtime key：既有的 normalize_name（會移除空白 / 標點）。只用來「偵測碰撞」，不用來決定身份。
* narrowing pattern：給資料庫 ilike 縮小範圍用的 pattern（保證是 exact 候選的超集合，不是比對結果）。

不得放入任何特定產品、品牌或問句。
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.normalization import normalize_name

# 與 D2 INVALID_OR_GENERIC 的結構規則一致：空值標記 / 英文字母少於 4 個的名稱不是可用的官方英文品名
MIN_LATIN_LETTERS = 4
_NULL_MARKERS = frozenset({"", "null", "none", "n/a", "na", "-"})
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_RUN_RE = re.compile(r"[^\W_]+")
# 縮小範圍用的 key 至少要有這麼長，pattern 才夠有選擇性；其餘情況不做英文取回（維持沒有英文取回時的行為）
MIN_NARROWING_RUN = 4
MAX_NARROWING_CHARS = 40
# 縮小範圍的資料庫查詢最多取回幾列；超過代表無法保證看完所有可能的碰撞對象 → 不確認
NARROW_LIMIT = 2000


def conservative_key(text: str | None) -> str:
    value = unicodedata.normalize("NFKC", text or "").casefold().strip()
    return re.sub(r"\s+", " ", value)


def runtime_key(text: str | None) -> str:
    return normalize_name(text or "")


def is_single_token(text: str | None) -> bool:
    """conservative key 中沒有空白 = 單一詞。標點不拆詞（連字號 / 加號相連者視為一個詞，不做標點移除的判斷）。

    單詞的官方英文品名可能同時是成分 / 類別 / 品牌用語，所以唯一 exact 也不自動確認（身份安全規則，非詞彙清單）。
    """
    key = conservative_key(text)
    return bool(key) and not re.search(r"\s", key)


def is_usable_english_name(text: str | None) -> bool:
    """通用結構規則（不針對任何產品）：不是空值標記，且至少有 4 個英文字母。"""
    if conservative_key(text) in _NULL_MARKERS:
        return False
    return len(_LATIN_LETTER_RE.findall(unicodedata.normalize("NFKC", text or ""))) >= MIN_LATIN_LETTERS


def narrowing_pattern(query: str | None) -> str | None:
    """資料庫 ilike 縮小範圍用 pattern：查詢 runtime key 的字元依序出現（字元之間允許任意字元）。

    允許任意字元是為了讓「只差空白 / 標點」的名稱（runtime key 碰撞對象）也在範圍內；
    只含 ASCII 的名稱，其 runtime key 相同就一定符合這個子序列 pattern（超集合，不是比對結果）。
    key 太短（選擇性不足）→ None（無法縮小範圍就不做英文取回）。
    """
    key = "".join(_RUN_RE.findall(runtime_key(query)))
    if len(key) < MIN_NARROWING_RUN:
        return None
    return "%" + "%".join(key[:MAX_NARROWING_CHARS]) + "%"


# NFKC / casefold 可能改變的只有含非 ASCII 字元（相容字元，例如羅馬數字、全形字）的名稱；
# 這些列一律併入縮小範圍，之後由 conservative key / runtime key 精確比對（純 ASCII 名稱的 NFKC 是恆等轉換）
NON_ASCII_REGEX = "[^ -~]"


@dataclass(frozen=True)
class EnglishNameMatches:
    """query 在已縮小範圍的列中的比對結果（product_id 依序、不重複）。"""

    exact_ids: tuple[str, ...]   # conservative key 相同
    runtime_ids: tuple[str, ...]  # runtime key 相同

    @property
    def collision_ids(self) -> tuple[str, ...]:
        """exact 命中時，任一個 key 對到的全部產品；只有 runtime key 相同（沒有 exact）時不構成取回。"""
        return tuple(dict.fromkeys(self.exact_ids + self.runtime_ids)) if self.exact_ids else ()


def match_english_name(query: str, rows: list[dict[str, Any]]) -> EnglishNameMatches:
    """rows 需含 product_id 與 product_name_en。只比對 key，不做相似度 / 子字串。"""
    q_cons, q_run = conservative_key(query), runtime_key(query)
    exact: list[str] = []
    runtime: list[str] = []
    if not q_cons or not q_run:
        return EnglishNameMatches((), ())
    for row in rows:
        name = row.get("product_name_en")
        if not name or not is_usable_english_name(name):
            continue
        pid = str(row["product_id"])
        if conservative_key(name) == q_cons and pid not in exact:
            exact.append(pid)
        if runtime_key(name) == q_run and pid not in runtime:
            runtime.append(pid)
    return EnglishNameMatches(tuple(exact), tuple(runtime))
