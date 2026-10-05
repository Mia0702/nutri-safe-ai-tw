"""語音逐字稿的字形正規化（簡體 → 繁體字形；最小範圍，只處理字形）。

OpenAI 語音轉錄偶爾回傳簡體字（即使說的是台灣中文），會讓之後的產品比對全部失敗。
這裡只做「簡體字形 → 繁體字形」的確定性轉換（OpenCC `s2t`：字元與詞彙層級的簡繁對照，不含台灣慣用語改寫）：

* 不做詞意改寫 / 同義詞替換（例如不把「軟件」換成「軟體」）、不猜產品名稱、不修正成資料庫中的任何產品。
* 英文、數字、許可證字號、標點原樣保留。
* 已經是繁體（沒有任何「只存在於簡體」的字）→ no-op，原文不變（不會把「台灣」「裏面」等繁體寫法改寫）。
* 只在逐字稿確實含有簡體字時才轉換；「台」一律保留（台灣慣用寫法）。
* 不影響 resolver 的 identity 判斷：轉換後的文字仍然只是「使用者說的話」，仍走同一套 exact / 候選確認規則。
"""

import os
from dataclasses import dataclass
from functools import lru_cache

KEEP_AS_IS = "台"  # 台灣慣用寫法：簡繁轉換不改動
_PLACEHOLDER = ""


@dataclass(frozen=True)
class NormalizedTranscript:
    raw: str
    normalized: str

    @property
    def changed(self) -> bool:
        return self.raw != self.normalized


def _load_chars(filename: str) -> dict[str, list[str]]:
    import opencc

    path = os.path.join(os.path.dirname(opencc.__file__), "dictionary", filename)
    table: dict[str, list[str]] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or "\t" not in line:
                continue
            key, values = line.rstrip("\n").split("\t", 1)
            table[key] = values.split(" ")
    return table


@lru_cache(maxsize=1)
def _simplified_only_chars() -> frozenset[str]:
    """只存在於簡體的字：簡繁對照表的 key，且本身不是任何字的繁體寫法（例如 鱼 软 锌 请；不含 台 干 后 这類兩邊都可能出現的字）。"""
    table = _load_chars("STCharacters.txt")
    traditional_forms = {c for values in table.values() for c in values}
    return frozenset(k for k in table if k not in traditional_forms)


@lru_cache(maxsize=1)
def _converter():
    from opencc import OpenCC

    return OpenCC("s2t")


def has_simplified_chars(text: str) -> bool:
    chars = _simplified_only_chars()
    return any(ch in chars for ch in text or "")


def normalize_transcript(text: str) -> NormalizedTranscript:
    raw = text or ""
    if not has_simplified_chars(raw):
        return NormalizedTranscript(raw=raw, normalized=raw)
    protected = raw.replace(KEEP_AS_IS, _PLACEHOLDER)
    return NormalizedTranscript(raw=raw, normalized=_converter().convert(protected).replace(_PLACEHOLDER, KEEP_AS_IS))
