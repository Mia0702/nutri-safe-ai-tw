"""語音輸入的 Interaction 領域詞彙：ASR vocabulary hint ＋ 同音錯字的候選建議。

兩件事都只用「已審核的 Interaction Corpus identity」（成分 / 營養素 entity 與已審核 alias、DrugSubstance 名稱），
不放任何產品名稱，也沒有任何個別字詞的寫死規則：

1. `build_vocabulary_prompt`：轉錄時給 OpenAI 的 `prompt`（詞彙提示），讓 ASR 更容易寫對領域名稱。
   詞彙動態由 corpus 建立、字數有上限（優先中文名稱，再放英文正式名稱）。
2. `phonetic_candidates`：ASR 仍然聽錯（同音 / 近音字）、導致 interaction 對象無法辨識時，依「拼音相同或僅差台灣口音常見混淆」
   找出語料中的 identity 作為「候選建議」。只是建議：唯一候選也要使用者確認，多個候選要使用者選，不會默默改成任何 entity。
   拼音比對：toneless pinyin 相同 → 較高信心；只在 zh/z、ch/c、sh/s、-ing/-in、-eng/-en 合併後相同 → 較低信心。
"""

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from pypinyin import Style, lazy_pinyin

MAX_VOCABULARY_CHARS = 400
VOCABULARY_PREFIX = "可能出現的成分與藥品名稱："
CONFIDENCE_SAME_PINYIN = 0.9
CONFIDENCE_FUZZY_PINYIN = 0.75
MAX_PROPOSED_CANDIDATES = 3
_CJK_RE = re.compile(r"^[一-鿿]+$")
DISPLAY_ONLY_POLICY = "DISPLAY_ONLY_NOT_AUTOMERGE"
CANDIDATE_POLICY = "CANDIDATE_ONLY_NOT_AUTOMERGE"
FORMAL_ALIAS_STATUSES = ("VERIFIED_OFFICIAL", "SUPPORTED_AUTHORITATIVE")
EXACT_POLICY_PREFIX = "EXACT"


@dataclass(frozen=True)
class DomainTerm:
    text: str
    entity_id: str
    kind: str  # ingredient | drug


@dataclass(frozen=True)
class PhoneticCandidate:
    entity_id: str
    term: str  # 命中的語料名稱（給使用者看）
    confidence: float


def interaction_terms(bridge_reference: dict[str, Any] | None, drug_reference: dict[str, Any] | None) -> list[DomainTerm]:
    """已審核的 Interaction Corpus 名稱：entity 正式 / 顯示名稱、已審核 alias、DrugSubstance 名稱（與 resolve_query 的採用條件一致）。"""
    terms: list[DomainTerm] = []
    bridge = bridge_reference or {}
    entities = {e["entity_id"]: e for e in bridge.get("entities") or []}
    for eid, e in entities.items():
        for name in (e.get("display_name_zh"), e.get("canonical_name_en")):
            if name and name.strip():
                terms.append(DomainTerm(name.strip(), eid, "ingredient"))
    for a in bridge.get("aliases") or []:
        eid, text = a.get("entity_id"), (a.get("alias_text") or "").strip()
        policy = a.get("match_policy")
        if not text or eid not in entities or policy == CANDIDATE_POLICY:  # 候選型 alias 尚未審核，不採用
            continue
        reviewed = (policy == DISPLAY_ONLY_POLICY
                    or a.get("alias_origin") != "REVIEWED_OVERLAY" or a.get("alias_status") in FORMAL_ALIAS_STATUSES)
        if reviewed:
            terms.append(DomainTerm(text, eid, "ingredient"))
    drugs = {d["drug_substance_id"]: d for d in (drug_reference or {}).get("drugs") or []}
    for did, d in drugs.items():
        if d.get("canonical_name_en"):
            terms.append(DomainTerm(d["canonical_name_en"].strip(), did, "drug"))
    for a in (drug_reference or {}).get("aliases") or []:
        if a.get("match_policy", "").startswith(EXACT_POLICY_PREFIX) and a.get("drug_substance_id") in drugs and a.get("alias_text"):
            terms.append(DomainTerm(a["alias_text"].strip(), a["drug_substance_id"], "drug"))
    return terms


def build_vocabulary_prompt(terms: list[DomainTerm], max_chars: int = MAX_VOCABULARY_CHARS) -> str | None:
    """ASR 詞彙提示：中文名稱優先（最容易被聽錯），再放英文正式名稱；去重、字數有上限。"""
    ordered = sorted(terms, key=lambda t: (not _CJK_RE.match(t.text), t.kind != "ingredient", t.text))
    chosen, seen, size = [], set(), len(VOCABULARY_PREFIX)
    for term in ordered:
        key = term.text.casefold()
        if key in seen:
            continue
        cost = len(term.text) + 1
        if size + cost > max_chars:
            break
        seen.add(key)
        chosen.append(term.text)
        size += cost
    return VOCABULARY_PREFIX + "、".join(chosen) if chosen else None


# ------------------------------------------------------------------ 拼音候選

_INITIAL_MERGE = (("zh", "z"), ("ch", "c"), ("sh", "s"))
_FINAL_MERGE = (("ing", "in"), ("eng", "en"))


def _syllables(text: str) -> list[str] | None:
    if not _CJK_RE.match(text or ""):
        return None
    return lazy_pinyin(text, style=Style.NORMAL)


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


def phonetic_candidates(text: str, terms: list[DomainTerm]) -> list[PhoneticCandidate]:
    """與 ASR 文字「讀音相同或僅差口音混淆」的語料 identity（每個 entity 一筆，信心較高者優先）。
    只比對中文名稱、長度必須相同；原文本身就是語料名稱時不是錯字，不產生候選。"""
    heard = _syllables(text)
    if not heard:
        return []
    heard_fuzzy = [_fuzzy(s) for s in heard]
    best: dict[str, PhoneticCandidate] = {}
    for term in terms:
        if term.text == text:
            continue
        syllables = _syllables(term.text)
        if not syllables or len(syllables) != len(heard):
            continue
        if syllables == heard:
            confidence = CONFIDENCE_SAME_PINYIN
        elif [_fuzzy(s) for s in syllables] == heard_fuzzy:
            confidence = CONFIDENCE_FUZZY_PINYIN
        else:
            continue
        cur = best.get(term.entity_id)
        if cur is None or confidence > cur.confidence:
            best[term.entity_id] = PhoneticCandidate(entity_id=term.entity_id, term=term.text, confidence=confidence)
    return sorted(best.values(), key=lambda c: (-c.confidence, c.entity_id))[:MAX_PROPOSED_CANDIDATES + 1]


class InteractionVocabularyProvider:
    """依 Interaction Corpus 動態產生 ASR 詞彙提示（有快取；corpus 不可用 / 失敗 → None，不影響語音轉錄）。"""

    def __init__(self, bridge_loader: Callable[[], dict[str, Any]], drug_loader: Callable[[], dict[str, Any]],
                 ttl_seconds: float = 600.0, clock: Callable[[], float] = time.monotonic):
        self._bridge_loader, self._drug_loader, self._ttl, self._clock = bridge_loader, drug_loader, ttl_seconds, clock
        self._cached: tuple[float, str | None] | None = None

    def __call__(self) -> str | None:
        now = self._clock()
        if self._cached is not None and now - self._cached[0] < self._ttl:
            return self._cached[1]
        try:
            prompt = build_vocabulary_prompt(interaction_terms(self._bridge_loader(), self._drug_loader()))
        except Exception:  # noqa: BLE001 - 沒有詞彙提示只是少一個輔助，不得影響語音輸入
            prompt = None
        self._cached = (now, prompt)
        return prompt
