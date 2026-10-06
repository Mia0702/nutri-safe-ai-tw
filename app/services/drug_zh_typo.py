"""已審核繁中藥品別名的錯字候選建議（deterministic；只是「候選」，一律需要使用者確認）。

* 候選池只有 runtime-active 的已審核別名（VERIFIED_OFFICIAL / SUPPORTED_OFFICIAL）；不搜尋任意字典詞、不用模型。
* 方法：字元編輯距離（Levenshtein）與拼音相似度（既有 pypinyin；同拼音、僅差 zh/z ch/c sh/s in/ing en/eng 口音混淆）。
* 沒有任何個別藥名 / 錯字的寫死對照；所有規則只看長度、編輯距離、拼音。
* 安全：
  - 查詢與候選互為子字串（前後多 / 少字：劑型字尾、鹽類前綴…）→ 不產生候選（不做 substring 比對）；
  - 鹽類 / 形式字樣（鹽酸、鈉、酯…）的集合必須相同，否則不跨越；
  - 不同 canonical identity 不合併；相同 canonical 的多個別名只留信心最高的一個；
  - 超過 MAX_CANDIDATES 個 canonical、或信心不足 → 不給候選（請使用者提供有效成分 / 英文名稱）。
"""

import re
from dataclasses import dataclass
from typing import Iterable

from pypinyin import Style, lazy_pinyin

from app.services.drug_zh_alias import ZhAliasHit, alias_key

MAX_CANDIDATES = 3
MIN_CONFIDENCE = 0.6
SCORE_SAME_PINYIN = 0.9
SCORE_FUZZY_PINYIN = 0.75
SCORE_SCRIPT_VARIANT = 0.95
SIMILARITY_FLOOR_LONG = 0.8  # 長度 >= 5：編輯距離 > 1 時需要的最低相似度（且距離最多 2）
_CJK_ONLY = re.compile(r"^[一-鿿]+$")
# 鹽類 / 形式字樣：只用來確認「查詢與候選的形式字樣相同」（一般性規則，不是任何藥名的對照）
_FORM_TOKENS = ("鹽酸", "硫酸", "磷酸", "醋酸", "乙酸", "檸檬酸", "縮蘋酸", "氫溴酸", "草酸", "丙酸", "苯磺酸", "延胡索酸",
                "鹽", "鈉", "鉀", "鈣", "鎂", "鋅", "水合物", "酯")
_INITIAL_MERGE = (("zh", "z"), ("ch", "c"), ("sh", "s"))
_FINAL_MERGE = (("ing", "in"), ("eng", "en"))


@dataclass(frozen=True)
class TypoCandidate:
    alias_text: str
    alias_id: str
    target_kind: str
    target_id: str  # DrugSubstance ID 或名錄 ingredient_code
    official_name: str | None
    score: float
    method: str  # edit_distance | pinyin | edit_distance+pinyin

    @property
    def key(self) -> tuple[str, str]:
        return (self.target_kind, self.target_id)


def normalize_for_comparison(text: str | None) -> str:
    """只用於候選比對：既有簡 → 繁字形正規化（若可用）＋ NFKC / 去空白標點；不改使用者原文。"""
    raw = text or ""
    try:
        from app.services.text_normalization import normalize_transcript

        raw = normalize_transcript(raw).normalized
    except Exception:  # noqa: BLE001 - 沒有簡繁轉換時只做格式正規化
        pass
    return alias_key(raw)


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _form_signature(text: str) -> frozenset[str]:
    return frozenset(t for t in _FORM_TOKENS if t in text)


def _fuzzy(syllable: str) -> str:
    for old, new in _INITIAL_MERGE:
        if syllable.startswith(old):
            syllable = new + syllable[len(old):]
            break
    for old, new in _FINAL_MERGE:
        if syllable.endswith(old):
            syllable = syllable[: -len(old)] + new
            break
    return syllable


def _edit_score(query: str, alias: str) -> float:
    length = len(alias)
    if length <= 2 or abs(len(query) - length) > 1:
        return 0.0
    d = edit_distance(query, alias)
    similarity = 1 - d / max(len(query), length)
    if length <= 4:
        return similarity if d <= 1 else 0.0
    return similarity if d <= 1 or (d <= 2 and similarity >= SIMILARITY_FLOOR_LONG) else 0.0


def _pinyin_score(query_syllables: list[str], alias: str) -> float:
    syllables = lazy_pinyin(alias, style=Style.NORMAL)
    if len(syllables) != len(query_syllables):
        return 0.0
    if syllables == query_syllables:
        return SCORE_SAME_PINYIN
    if len(alias) >= 3 and [_fuzzy(s) for s in syllables] == [_fuzzy(s) for s in query_syllables]:
        return SCORE_FUZZY_PINYIN
    return 0.0


def typo_candidates(text: str | None, entries: Iterable[ZhAliasHit]) -> list[TypoCandidate]:
    """exact 比對失敗後，從已審核別名找「可能是打錯 / 同音錯字」的 canonical 候選（至多 MAX_CANDIDATES 個；否則空）。"""
    query = normalize_for_comparison(text)
    if len(query) < 2 or not _CJK_ONLY.match(query):
        return []
    raw_key = alias_key(text)
    query_syllables = lazy_pinyin(query, style=Style.NORMAL)
    query_signature = _form_signature(query)
    scored: list[TypoCandidate] = []
    for hit in entries:
        alias = alias_key(hit.alias_text)
        if alias == query and raw_key != query and _CJK_ONLY.match(alias):  # 只差簡繁字形：仍然只是候選（需確認）
            scored.append(TypoCandidate(hit.alias_text, hit.alias_id, hit.target_kind, hit.target_id, hit.official_name,
                                        SCORE_SCRIPT_VARIANT, "traditional_normalization"))
            continue
        if not _CJK_ONLY.match(alias) or alias == query or alias in query or query in alias:
            continue  # 完全相同（exact 已處理）/ 子字串關係（劑型字尾、鹽類前綴…）：不比對
        if _form_signature(alias) != query_signature:
            continue  # 不跨越鹽類 / 形式
        edit = _edit_score(query, alias)
        phon = _pinyin_score(query_syllables, alias)
        if len(alias) <= 2:  # 短名稱：只有「拼音完全相同」且全池唯一才算
            if phon != SCORE_SAME_PINYIN:
                continue
        score = max(edit, phon)
        if score < MIN_CONFIDENCE:
            continue
        method = "edit_distance+pinyin" if edit and phon else ("edit_distance" if edit else "pinyin")
        scored.append(TypoCandidate(hit.alias_text, hit.alias_id, hit.target_kind, hit.target_id, hit.official_name, score, method))
    best: dict[tuple[str, str], TypoCandidate] = {}
    for c in scored:  # 同一 canonical 的多個別名收斂成一個（信心高者；同分取字典序）
        cur = best.get(c.key)
        if cur is None or (-c.score, c.alias_text) < (-cur.score, cur.alias_text):
            best[c.key] = c
    ranked = sorted(best.values(), key=lambda c: (-c.score, c.target_kind, c.target_id))
    if len(ranked) > 1:  # 短名稱（<= 2 字）只有在整個候選池唯一時才算「唯一強候選」
        ranked = [c for c in ranked if len(alias_key(c.alias_text)) > 2]
    return ranked if 0 < len(ranked) <= MAX_CANDIDATES else []
