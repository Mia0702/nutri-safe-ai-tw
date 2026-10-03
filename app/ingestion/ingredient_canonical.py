"""Ingredient canonicalization 與 alias 品質把關。

原則（不依 LLM 常識判斷同義詞）：
- 自動合併只限兩種：
  1. 格式差異：NFKC／大小寫／空白／標點，以及希臘字母拼寫（beta- → β-）。
  2. 同一官方欄位同時提供中英文：「中文名English name」直接相連 → 一個 canonical，英文為 alias。
- 括號英文（「中文(English)」）只有在「像單一名稱、沒有跨成分衝突、具體程度一致」時才建立
  OFFICIAL_RECORD alias；否則列為 review candidate，不建立 alias。
- 跨紀錄的中英配對（某筆寫「甲(X)」、另一筆只寫「X」）只列為 candidate，不自動合併。
- 原料型名稱（例如某某粉末、某某精）照官方原文保留，不拆成推測成分。
"""

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from app.normalization import normalize_name

_CJK = "一-鿿"
_GREEK_SPELLING_RE = re.compile(r"(?i)\b(alpha|beta|gamma|delta|omega)(?=\s*-|[一-鿿])")
_GREEK = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "omega": "ω"}
# 「中文名English name」：中文在前、英文在後，英文只含字母／空白／連字號／句點
_INLINE_BILINGUAL_RE = re.compile(
    rf"^(?P<zh>[α-ωΑ-Ω\d,\-]*[{_CJK}][{_CJK}α-ω\-]*)\s*(?P<en>[A-Za-z][A-Za-z.\- ]*[A-Za-z.])$"
)
# 括號內容若含這些字元，代表是清單／組合說明，不是單一名稱
_LIST_CHARS_RE = re.compile(r"[、,;+&\n/]|\band\b")
# 英文名最後一個 token 是短代號（Re、Rb1、K、EM1…）→ 指特定成員或菌株
_LAST_TOKEN_RE = re.compile(r"\s([A-Za-z0-9\-]+)$")
_ZH_DESIGNATOR_RE = re.compile(rf"[{_CJK}]\s*-?([A-Za-z0-9]{{1,6}})$")
# 名稱中「英文單字 + 空白 + 中文」或「中文 + 空白 + 英文單字」且未被拆開 → 可能是黏在一起的兩個成分
_MIXED_UNSPLIT_RE = re.compile(rf"[A-Za-z0-9]{{2,}}\s+[{_CJK}]|[{_CJK}]\s+[A-Za-z]{{2,}}")
# 異體字（同一字的常見寫法差異）：只用來列 review candidate，不自動合併
_VARIANT_CHARS = str.maketrans({"醣": "糖", "蔘": "參", "甙": "苷", "氨": "胺", "岐": "歧"})
MIN_CONTAINED_LATIN_KEY = 5


def greek_spelled_to_symbol(name: str) -> str:
    return _GREEK_SPELLING_RE.sub(lambda m: _GREEK[m.group(1).lower()], name)


def canonical_key(name: str) -> str:
    """只做可證明是格式差異的正規化。"""
    return normalize_name(greek_spelled_to_symbol(name))


def split_inline_bilingual(name: str) -> tuple[str, str | None]:
    """「中文名English name」→ (中文名, English name)。

    英文部分含數字（菌株、編號，例如 Rb2、HN019）或太短（單一字母代號）時不拆，
    因為中文部分可能比英文更泛稱，拆開會遺失具體性。
    """
    m = _INLINE_BILINGUAL_RE.match(name.strip())
    if not m:
        return name, None
    en = m.group("en").strip()
    zh = m.group("zh").strip()
    if len(re.sub(r"[^A-Za-z]", "", en)) < 4 or specificity_mismatch(zh, en):
        return name, None
    return zh, en


def is_single_name(text: str) -> bool:
    """括號內容是否像單一名稱（不是清單、組合或計算說明）。"""
    t = text.strip()
    return bool(t) and not _LIST_CHARS_RE.search(t) and len(normalize_name(t)) >= 2 and bool(re.search(r"[A-Za-z]", t))


def _is_designator(token: str) -> bool:
    """菌株／成員代號：含數字（LCW23、Rb1、EM1），或 1~3 字元且含大寫或只有一個字母（K、Re、GG、a）。"""
    return bool(re.search(r"\d", token)) or (len(token) <= 3 and (bool(re.search(r"[A-Z]", token)) or len(token) == 1))


def has_designator(name: str) -> bool:
    name = name.strip()
    m = _LAST_TOKEN_RE.search(name)
    if m and _is_designator(m.group(1)):
        return True
    m = _ZH_DESIGNATOR_RE.search(name)
    if m and _is_designator(m.group(1)):
        return True
    # 單一英數 token 且含數字（例如 AB12CD）也視為代號
    return bool(re.fullmatch(r"[A-Za-z0-9\-]+", name) and re.search(r"\d", name))


def specificity_mismatch(name_a: str, name_b: str) -> bool:
    """一邊帶特定成員／菌株代號、另一邊沒有 → 兩者具體程度不同（例如類別 vs 單一成分、菌種 vs 菌株）。"""
    return has_designator(name_a) != has_designator(name_b)


class IngredientMention(BaseModel):
    """一筆官方紀錄中的一個成分提及（不寫入 DB，只供 canonicalization 使用）。"""

    ingredient_id: str
    surface: str               # parser 取得的名稱原樣
    name_zh: str               # canonical 用名稱（已拆掉同欄位相連的英文）
    english: str | None = None
    english_basis: str | None = None  # official_parenthetical_english / official_inline_bilingual
    source_record_key: str
    source_url: str | None = None


class AliasCandidate(BaseModel):
    kind: str
    names: list[str]
    ingredient_ids: list[str]
    detail: str
    source_record_keys: list[str] = Field(default_factory=list)


class IngredientCanonicalization(BaseModel):
    unique_surfaces: int
    unique_before_key: int        # 只用舊 normalize_name(原樣) 時的 unique 數
    canonical_count: int
    display_names: dict[str, dict[str, Any]]  # ingredient_id -> {name_zh, name_en}
    aliases: list[dict[str, Any]]
    auto_merges: list[dict[str, Any]]
    inferred_candidates: list[AliasCandidate]
    review_candidates: list[AliasCandidate]
    rejected_parentheticals: list[dict[str, Any]]


def _alias(ingredient_id: str, alias: str, alias_type: str, status: str, confidence: float,
           basis: dict[str, Any], m: IngredientMention) -> dict[str, Any]:
    return {
        "ingredient_id": ingredient_id, "alias": alias, "alias_normalized": normalize_name(alias),
        "alias_type": alias_type, "evidence_status": status, "confidence": confidence, "inference_basis": basis,
        "source_record_key": m.source_record_key, "source_url": m.source_url,
    }


def build_ingredient_canonicalization(
    mentions: Iterable[IngredientMention], manual_reviews: Iterable[dict[str, str]] = (),
) -> IngredientCanonicalization:
    """manual_reviews: [{"ingredient_id", "alias", "reviewer_note"}]，人工審核過的 alias。"""
    mentions = list(mentions)
    by_id: dict[str, list[IngredientMention]] = defaultdict(list)
    for m in mentions:
        by_id[m.ingredient_id].append(m)

    # canonical 顯示名稱：出現最多的寫法，同數取字典序最小（可重現）
    display: dict[str, dict[str, Any]] = {}
    for iid, ms in by_id.items():
        counts = Counter(m.name_zh for m in ms)
        name = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        display[iid] = {"name_zh": name, "name_en": None}
    key_owner = {canonical_key(v["name_zh"]): iid for iid, v in display.items()}

    # 自動合併紀錄：同一 canonical 下有多種寫法
    auto_merges = []
    for iid, ms in by_id.items():
        surfaces = sorted({m.surface for m in ms})
        if len(surfaces) > 1:
            rules = set()
            for s in surfaces:
                split_zh, split_en = split_inline_bilingual(s)
                if split_en:
                    rules.add("official_inline_bilingual")
                elif normalize_name(s) != normalize_name(display[iid]["name_zh"]):
                    rules.add("greek_letter_spelling")
                else:
                    rules.add("format_variant")
            auto_merges.append({"ingredient_id": iid, "canonical": display[iid]["name_zh"], "surfaces": surfaces,
                                "rules": sorted(rules)})

    # 英文名稱 → 哪些 canonical 用過；canonical → 用過哪些英文名稱
    en_owners: dict[str, set[str]] = defaultdict(set)
    en_of: dict[str, set[str]] = defaultdict(set)
    rejected: list[dict[str, Any]] = []
    for m in mentions:
        if not m.english:
            continue
        if not is_single_name(m.english):
            rejected.append({"ingredient": display[m.ingredient_id]["name_zh"], "parenthetical": m.english,
                             "reason": "not_single_name", "source_record_key": m.source_record_key})
            continue
        en_owners[canonical_key(m.english)].add(m.ingredient_id)
        en_of[m.ingredient_id].add(canonical_key(m.english))

    aliases: dict[tuple[str, str], dict[str, Any]] = {}
    inferred: list[AliasCandidate] = []
    review: list[AliasCandidate] = []
    review_index: dict[tuple[str, ...], AliasCandidate] = {}

    def add_review(kind: str, ids: Iterable[str], detail: str, keys: Iterable[str] = ()):
        ids = sorted(set(ids))
        sig = (kind, *ids)
        if sig in review_index:
            review_index[sig].source_record_keys = sorted(set(review_index[sig].source_record_keys) | set(keys))
            return
        review_index[sig] = AliasCandidate(kind=kind, names=[display[i]["name_zh"] for i in ids], ingredient_ids=ids,
                                           detail=detail, source_record_keys=sorted(set(keys)))
        review.append(review_index[sig])

    cross_pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for m in mentions:
        iid = m.ingredient_id
        name = display[iid]["name_zh"]
        # 寫法本身與 canonical 名稱正規化後不同（希臘字母拼寫、或「中文English」整串）→ 保存原樣為 alias
        if normalize_name(m.surface) != normalize_name(name):
            if m.english_basis == "official_inline_bilingual":
                aliases.setdefault((iid, normalize_name(m.surface)), _alias(
                    iid, m.surface, "OFFICIAL_NAME", "OFFICIAL_RECORD", 1.0,
                    {"rule": "official_inline_bilingual_full_surface"}, m))
            else:
                aliases.setdefault((iid, normalize_name(m.surface)), _alias(
                    iid, m.surface, "OFFICIAL_NAME", "INFERRED", 0.95,
                    {"rule": "greek_letter_spelling", "canonical": name}, m))
        if not m.english or not is_single_name(m.english):
            continue
        ekey = canonical_key(m.english)
        owners = en_owners[ekey]
        other_canonical = key_owner.get(ekey)
        if len(owners) > 1:
            add_review("english_name_shared_by_multiple_ingredients", owners, f"英文名稱「{m.english}」對應多個中文成分",
                       [x.source_record_key for x in mentions if x.ingredient_id in owners and x.english
                        and canonical_key(x.english) == ekey])
            continue
        if len(en_of[iid]) > 1:
            add_review("multiple_english_names", [iid], f"「{name}」在官方資料中對應多個不同英文名稱", [m.source_record_key])
            continue
        if other_canonical and other_canonical != iid:
            # 另一個 canonical 的名稱就是這個英文 → 跨紀錄配對，稍後判斷 inferred / review
            cross_pairs[(iid, other_canonical)].add(m.source_record_key)
            continue
        if _MIXED_UNSPLIT_RE.search(name):
            continue  # 名稱本身可疑（已列 review），不掛英文 alias
        if specificity_mismatch(name, m.english):
            add_review("specificity_mismatch", [iid], f"「{name}」↔「{m.english}」：只有一邊帶特定成員／菌株代號",
                       [m.source_record_key])
            continue
        if display[iid]["name_en"] is None:
            display[iid]["name_en"] = m.english
        aliases.setdefault((iid, normalize_name(m.english)), _alias(
            iid, m.english, "ENGLISH_NAME", "OFFICIAL_RECORD", 1.0, {"rule": m.english_basis}, m))

    # 跨紀錄配對：A 筆寫「甲(X)」、B 筆只寫「X」→ 不自動合併
    # 中文名含英文字母（可能是類別名，例如「某某抑制劑」系列）或具體程度不同 → manual review；其餘 → inferred candidate
    for (zh_id, latin_id), keys in cross_pairs.items():
        zh_name, latin_name = display[zh_id]["name_zh"], display[latin_id]["name_zh"]
        if re.search(r"[A-Za-z]", zh_name) or specificity_mismatch(zh_name, latin_name):
            add_review("cross_record_pairing_needs_review", [zh_id, latin_id],
                       f"「{zh_name}」的括號英文「{latin_name}」是另一個獨立成分，且名稱型態不一致", keys)
        else:
            inferred.append(AliasCandidate(
                kind="cross_record_english_pairing", names=[zh_name, latin_name], ingredient_ids=[zh_id, latin_id],
                detail="另一筆官方紀錄以「中文(English)」配對；建議人工確認後才合併", source_record_keys=sorted(keys),
            ))

    for iid, v in display.items():
        if _MIXED_UNSPLIT_RE.search(v["name_zh"]):
            add_review("mixed_script_unsplit_name", [iid], f"「{v['name_zh']}」中英文以空白相連且未拆開，可能是多個成分或菌株描述")

    # 異體字（醣/糖、蔘/參…）與英文名稱包含另一個 canonical 名稱 → review
    variant_groups: dict[str, set[str]] = defaultdict(set)
    for iid, v in display.items():
        variant_groups[canonical_key(v["name_zh"].translate(_VARIANT_CHARS))].add(iid)
    for ids in variant_groups.values():
        if len(ids) > 1:
            add_review("variant_characters", ids, "僅異體字不同（例如 醣/糖、蔘/參），需人工確認")
    for key, iid in key_owner.items():
        if len(key) < MIN_CONTAINED_LATIN_KEY or not re.search(r"[a-z]", key):
            continue
        for other_key, other in key_owner.items():
            if other != iid and key in other_key:
                add_review("name_contains_other_ingredient", [iid, other],
                           f"「{display[other]['name_zh']}」包含另一成分名稱「{display[iid]['name_zh']}」")

    for r in manual_reviews:
        iid = r["ingredient_id"]
        if iid in display:
            m = by_id[iid][0]
            aliases[(iid, normalize_name(r["alias"]))] = _alias(
                iid, r["alias"], "COMMON_NAME", "MANUAL_REVIEWED", 1.0,
                {"rule": "manual_review", "reviewer_note": r.get("reviewer_note", "")}, m)

    # alias 與 canonical 名稱本身相同者不需要
    alias_rows = [a for (iid, norm), a in aliases.items() if norm and norm != normalize_name(display[iid]["name_zh"])]
    return IngredientCanonicalization(
        unique_surfaces=len({m.surface for m in mentions}),
        unique_before_key=len({normalize_name(m.surface) for m in mentions}),
        canonical_count=len(by_id),
        display_names=display,
        aliases=alias_rows,
        auto_merges=auto_merges,
        inferred_candidates=inferred,
        review_candidates=review,
        rejected_parentheticals=rejected,
    )


def apply_canonical_names(bundles: Iterable[Any], result: IngredientCanonicalization) -> None:
    """把同一 canonical 的顯示名稱統一（不改 id；原文仍在 product_ingredients.raw_text）。"""
    for b in bundles:
        for ing in b.ingredients:
            d = result.display_names.get(ing["id"])
            if d:
                ing["name_zh"], ing["name_en"] = d["name_zh"], d["name_en"]
                ing["name_normalized"] = canonical_key(d["name_zh"])
