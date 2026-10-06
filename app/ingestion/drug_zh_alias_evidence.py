"""Reviewed Taiwan Drug Chinese Alias Overlay：證據稽核（offline；不是 runtime）。

目的：只用台灣 TFDA 官方藥品許可證資料，為「繁體中文成分名稱 → 英文有效成分 identity」找出可重現的官方證據。
不使用 AI 翻譯、不使用相似度、不使用 substring 比對來決定 identity；這裡的字串比對只用來「在官方資料裡找到成對的官方寫法」。

官方證據（只認同一張許可證內「中文品名 / 英文品名」成對出現的寫法）：

* EXACT_PRODUCT_NAME_PAIR：中文品名（去空白）= 該中文名稱，且英文品名 = 英文通用名（例如原料藥許可證：品名本身就是通用名）。
* PARENTHETICAL_GENERIC_PAIR：中文品名的括號裡是該中文名稱，英文品名的括號裡是英文通用名（例如「…錠（中文名）」＋「… TABLETS (GENERIC)」）。

不計入證據（只是資訊）：中文名稱夾在中文商品名裡（品牌名稱 / 劑型字樣）、只出現在已註銷藥證。

可成為 VERIFIED_OFFICIAL 的條件（`evaluate_candidate`）：
1. 至少 MIN_SUPPORT_PERMITS 張不同的未註銷許可證支持；
2. 同一個中文名稱在官方資料裡沒有對應到其他英文通用名（歧義 / 母體-鹽類衝突 → NEEDS_REVIEW）；
3. 英文通用名與目標 identity 的英文名稱「正規化後完全相同」（官方成分欄位的「(EQ TO …)」註記視為官方同義註記，鹽類 / 水合物 / 酯類不去除）。
"""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

EXACT_PRODUCT_NAME_PAIR = "EXACT_PRODUCT_NAME_PAIR"
PARENTHETICAL_GENERIC_PAIR = "PARENTHETICAL_GENERIC_PAIR"
MIN_SUPPORT_PERMITS = 2

_CJK_ONLY = re.compile(r"^[一-鿿]+$")
_PAREN = re.compile(r"[（(]([^()（）]+)[）)]")
_EQ_ANNOTATION = re.compile(r"\s*\(\s*eq(?:uivalent)?\s+to\b[^)]*\)\s*$", re.IGNORECASE)


def zh_key(text: str | None) -> str:
    """中文名稱比對鍵：NFKC、移除所有空白、casefold。只消除格式差異；不轉簡繁、不去鹽類字樣。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).casefold()


def en_key(text: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip().casefold()


def strip_official_eq_annotation(name: str | None) -> str:
    """官方成分欄位的尾端「(EQ TO …)」同義註記（例如 ACETAMINOPHEN (EQ TO PARACETAMOL)）；不處理鹽類 / 水合物。"""
    return _EQ_ANNOTATION.sub("", en_key(name)).strip()


def _names_an_ingredient(en: str, items: list[str]) -> bool:
    """英文括號 / 品名確實是這張許可證的主成分（或其母體名稱開頭）；沒有主成分欄位（原料藥許可證）時只靠品名成對。"""
    if not items:
        return True
    return any(item == en or item.startswith(en + " ") or item.startswith(en + "(") for item in items)


@dataclass
class PairEvidence:
    permits: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    applicants: set[str] = field(default_factory=set)  # 申請商：通用名通常由多家藥廠使用，品牌名通常只有一家


def collect_official_pairs(permit_rows: Iterable[dict[str, str]], cancelled: set[str] = frozenset()
                           ) -> dict[str, dict[str, PairEvidence]]:
    """未註銷藥證 → {中文名稱 key: {英文通用名 key: PairEvidence}}（只收同一張許可證內成對出現的寫法）。"""
    pairs: dict[str, dict[str, PairEvidence]] = defaultdict(lambda: defaultdict(PairEvidence))
    for row in permit_rows:
        permit = (row.get("許可證字號") or "").strip()
        if not permit or permit in cancelled:
            continue
        zh_name = unicodedata.normalize("NFKC", row.get("中文品名") or "")
        en_name = unicodedata.normalize("NFKC", row.get("英文品名") or "")
        items = [en_key(i) for i in (row.get("主成分略述") or "").split(";;") if i.strip()]
        whole_zh = re.sub(r"\s+", "", zh_name)
        if _CJK_ONLY.match(whole_zh) and en_key(en_name) and _names_an_ingredient(en_key(en_name), items):
            ev = pairs[zh_key(whole_zh)][en_key(en_name)]
            ev.permits.add(permit)
            ev.types.add(EXACT_PRODUCT_NAME_PAIR)
            ev.applicants.add((row.get("申請商統一編號") or row.get("申請商名稱") or "").strip())
        en_groups = [en_key(m.group(1)) for m in _PAREN.finditer(en_name)]
        for m in _PAREN.finditer(zh_name):
            candidate = re.sub(r"\s+", "", m.group(1))
            if not _CJK_ONLY.match(candidate):
                continue
            for en in en_groups:
                if en and _names_an_ingredient(en, items):
                    ev = pairs[zh_key(candidate)][en]
                    ev.permits.add(permit)
                    ev.types.add(PARENTHETICAL_GENERIC_PAIR)
                    ev.applicants.add((row.get("申請商統一編號") or row.get("申請商名稱") or "").strip())
    return pairs


@dataclass
class EvidenceResult:
    passed: bool
    support_permits: list[str]
    evidence_types: list[str]
    conflicts: dict[str, int]  # 其他英文通用名 → 支持藥證數
    reasons: list[str]
    support_permit_count_total: int = 0
    applicant_count: int = 0


def official_equivalents(official_name: str | None) -> set[str]:
    """官方成分欄位自己標示的同義名稱：「X (EQ TO Y)」→ {X, Y}。鹽類 / 水合物 / 酯類不在此列。"""
    key = en_key(official_name)
    head = strip_official_eq_annotation(key)
    out = {head}
    m = re.search(r"\(\s*eq(?:uivalent)?\s+to\s+([^)]*)\)\s*$", key, re.IGNORECASE)
    if m and m.group(1).strip():
        out.add(m.group(1).strip())
    return out


def _only_accepted_names(en: str, accepted: set[str]) -> bool:
    """「A (B)」「B( A )」這類官方英文寫法：括號內外的每一段都是已接受名稱 → 同一個 identity 的官方同義寫法（不是另一個成分）。"""
    parts = [re.sub(r"^eq(?:uivalent)? to\s+", "", p.strip()) for p in re.split(r"[()]", en) if p.strip()]
    return len(parts) > 1 and all(p in accepted for p in parts)


def evaluate_candidate(alias_zh: str, target_en: str, pairs: dict[str, dict[str, PairEvidence]],
                       *, equivalents: Iterable[str] = (), min_support: int = MIN_SUPPORT_PERMITS) -> EvidenceResult:
    """某中文名稱 → 英文通用名 target_en（及官方標示的同義名稱 equivalents）是否有足夠且無歧義的官方證據。"""
    by_en = pairs.get(zh_key(alias_zh), {})
    accepted = {en_key(target_en), *(en_key(e) for e in equivalents)}
    support: set[str] = set()
    types: set[str] = set()
    applicants: set[str] = set()
    for en, ev in by_en.items():
        if en in accepted or _only_accepted_names(en, accepted):
            support |= ev.permits
            types |= ev.types
            applicants |= ev.applicants
    conflicts = {en: len(ev.permits) for en, ev in by_en.items() if en not in accepted and not _only_accepted_names(en, accepted)}
    reasons: list[str] = []
    if len(support) < min_support:
        reasons.append(f"官方成對證據不足（{len(support)} 張未註銷藥證；至少需要 {min_support} 張）")
    if conflicts:
        reasons.append("同一個中文名稱在官方資料中也對應其他英文名稱：" + "；".join(f"{en}（{n}）" for en, n in sorted(conflicts.items())))
    return EvidenceResult(passed=not reasons, support_permits=sorted(support), evidence_types=sorted(types),
                          conflicts=conflicts, reasons=reasons, support_permit_count_total=len(support),
                          applicant_count=len(applicants - {""}))


# ----------------------------------------------------------------------------- overlay 建構（reviewer 候選 + 官方快照 → 稽核過的 overlay 列）

STATUS_VERIFIED = "VERIFIED_OFFICIAL"
STATUS_SUPPORTED = "SUPPORTED_OFFICIAL"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_REJECTED = "REJECTED"
MATCH_EXACT = "EXACT_NFKC_CASEFOLD_WHITESPACE"
MATCH_NONE = "NO_AUTO_MATCH"
TARGET_DRUG = "DRUG_SUBSTANCE"
TARGET_CATALOG = "TAIWAN_DRUG_INGREDIENT"
OFFICIAL_SOURCE = "TFDA 未註銷藥品許可證資料集（data.gov.tw 9123；data.fda.gov.tw InfoId 37）"
OFFICIAL_SOURCE_URL = "https://data.fda.gov.tw/data/opendata/export/37/csv"
OVERLAY_COLUMNS = [
    "alias_id", "alias_text_zh_tw", "target_kind", "canonical_entity_id", "taiwan_drug_ingredient_code", "canonical_name_en",
    "taiwan_drug_ingredient_official_name", "identity_level", "official_source", "source_url", "source_locator", "evidence_types",
    "support_permit_count", "review_status", "match_policy", "review_reason", "retrieved_at", "overlay_version",
    "candidate_origin", "evidence_source_name", "evidence_source_url", "identity_statement", "active_ingredient_in_source", "conflict_check",
]
ORIGIN_REVIEWER = "REVIEWER"
ORIGIN_AI = "AI_TRANSLATION"  # AI / LLM 翻譯只能產生候選；不是證據
OVERLAY_VERSION = "1.1"
_LOCATOR_SAMPLE = 5


def _alias_id(alias: str, target: str) -> str:
    import hashlib

    return "DZA_" + hashlib.sha1(f"{zh_key(alias)}|{target}".encode("utf-8")).hexdigest()[:12]


def _locator(result: EvidenceResult) -> str:
    if not result.support_permit_count_total:
        return "官方資料中沒有成對證據（9123 欄位：中文品名 / 英文品名）"
    sample = "、".join(result.support_permits[:_LOCATOR_SAMPLE])
    more = f" 等共 {result.support_permit_count_total} 張" if result.support_permit_count_total > _LOCATOR_SAMPLE else ""
    return f"9123 欄位：中文品名 / 英文品名；許可證字號：{sample}{more}"


def _is_taiwan_gov_url(url: str | None) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url or "").hostname or "").lower()
    return host == "gov.tw" or host.endswith(".gov.tw")


def validate_official_source(src: dict[str, str], alias: str, target_en: str, accepted: set[str]) -> list[str]:
    """台灣官方「直接說明 identity」的來源（仿單 / 官方查詢頁 / 官方公告…）是否能支持這個 alias；回傳問題清單（空 = 通過）。

    * 必須是台灣政府網域（*.gov.tw）；
    * 來源引文必須同時含有該中文名稱與英文有效成分（機械檢查，不靠人工宣稱）；
    * 來源標示的有效成分必須與目標 identity 在同一個 declared level（正規化後完全相同；鹽類 / 水合物 / 酯類不同則不通過）。
    """
    problems: list[str] = []
    if not _is_taiwan_gov_url(src.get("source_url")):
        problems.append("來源不是台灣政府網域（*.gov.tw）")
    statement = unicodedata.normalize("NFKC", src.get("identity_statement") or "")
    if zh_key(alias) not in zh_key(statement):
        problems.append("來源引文中找不到該中文名稱")
    active = en_key(src.get("active_ingredient_in_source"))
    if not active or active not in accepted:
        problems.append("來源標示的有效成分與目標 identity 不在同一個層級（鹽類 / 形式不同或未標示）")
    elif active not in en_key(statement):
        problems.append("來源引文中找不到該英文有效成分")
    if not (src.get("source_locator") or "").strip() or not (src.get("retrieved_at") or "").strip():
        problems.append("缺少 source_locator / retrieved_at")
    return problems


def build_overlay_rows(candidates: Iterable[dict[str, str]], pairs: dict[str, dict[str, PairEvidence]],
                       drugs: dict[str, str], catalog: dict[str, str], retrieved_at: str,
                       official_sources: Iterable[dict[str, str]] = ()) -> list[dict[str, str]]:
    """reviewer 候選 → overlay 列。VERIFIED_OFFICIAL 只在 reviewer 接受且官方證據通過（足量、無歧義、identity 名稱相同）時產生。

    drugs：drug_substance_id → canonical 英文名；catalog：ingredient_code → 官方成分名稱（原樣）。
    """
    out: list[dict[str, str]] = []
    sources: dict[tuple[str, str], dict[str, str]] = {}
    for src in official_sources:
        key = (src.get("canonical_entity_id") or src.get("taiwan_drug_ingredient_code") or "").strip()
        sources[(zh_key(src["alias_text_zh_tw"]), key)] = src
    for c in candidates:
        alias, kind = (c["alias_text_zh_tw"] or "").strip(), c["target_kind"]
        entity_id, code = (c.get("canonical_entity_id") or "").strip(), (c.get("taiwan_drug_ingredient_code") or "").strip()
        target_en = (c.get("target_english_name") or "").strip()
        decision = (c.get("reviewer_decision") or "").strip().upper()
        official_name = canonical_en = ""
        equivalents: set[str] = set()
        reasons: list[str] = []
        if kind == TARGET_DRUG:
            canonical_en = drugs.get(entity_id, "")
            if not canonical_en:
                reasons.append(f"找不到已審核的 DrugSubstance：{entity_id}")
            elif en_key(canonical_en) != en_key(target_en):
                reasons.append("目標英文名稱與 DrugSubstance canonical 名稱不同")
        elif kind == TARGET_CATALOG:
            official_name = catalog.get(code, "")
            if not official_name:
                reasons.append(f"找不到台灣官方藥品成分名錄項目：{code}")
            else:
                equivalents = official_equivalents(official_name)
                if en_key(target_en) not in equivalents:
                    reasons.append("目標英文名稱與名錄官方成分名稱不同（鹽類 / 形式不自動視為相同）")
        else:
            reasons.append(f"未知的 target_kind：{kind}")
        result = evaluate_candidate(alias, target_en, pairs, equivalents=equivalents - {en_key(target_en)})
        note = (c.get("reviewer_note") or "").strip()
        origin = (c.get("candidate_origin") or ORIGIN_REVIEWER).strip()
        src = sources.get((zh_key(alias), entity_id or code))
        src_problems = (validate_official_source(src, alias, target_en, {en_key(target_en), *equivalents}) if src else [])
        evidence_name, evidence_url, statement, active_src = OFFICIAL_SOURCE, OFFICIAL_SOURCE_URL, "", ""
        if decision == "REJECT":
            status, reasons = STATUS_REJECTED, [note or "reviewer 拒絕"]
        elif decision == "ACCEPT" and not reasons and result.passed:
            status = STATUS_VERIFIED
            reasons = [f"官方成對證據通過（{len(result.support_permits)} 張藥證、無歧義）" + (f"；{note}" if note else "")]
        elif decision == "ACCEPT" and not reasons and src and not src_problems and not result.conflicts:
            status = STATUS_SUPPORTED
            evidence_name, evidence_url = src["source_name"], src["source_url"]
            statement, active_src = src["identity_statement"], src["active_ingredient_in_source"]
            reasons = [f"台灣官方來源直接說明 identity（{src['source_name']}）；9123 成對證據 {len(result.support_permits)} 張、無衝突"
                       + (f"；{note}" if note else "")]
        else:
            status = STATUS_NEEDS_REVIEW
            reasons = reasons + result.reasons + [f"官方來源未通過：{p}" for p in src_problems] + ([note] if note else [])
            if origin == ORIGIN_AI:
                reasons.append("AI 翻譯只能產生候選，沒有台灣官方來源支持就不啟用")
            if decision not in ("ACCEPT", "HOLD"):
                reasons.append("reviewer 尚未決定")
        out.append({
            "alias_id": _alias_id(alias, entity_id or code), "alias_text_zh_tw": alias, "target_kind": kind,
            "canonical_entity_id": entity_id, "taiwan_drug_ingredient_code": code, "canonical_name_en": canonical_en,
            "taiwan_drug_ingredient_official_name": official_name, "identity_level": c.get("identity_level", ""),
            "official_source": evidence_name, "source_url": evidence_url,
            "source_locator": (src["source_locator"] if status == STATUS_SUPPORTED else _locator(result)),
            "evidence_types": "|".join(result.evidence_types), "support_permit_count": str(len(result.support_permits)),
            "review_status": status, "match_policy": MATCH_EXACT if status in (STATUS_VERIFIED, STATUS_SUPPORTED) else MATCH_NONE,
            "review_reason": "；".join(r for r in reasons if r), "retrieved_at": (src["retrieved_at"] if status == STATUS_SUPPORTED else retrieved_at),
            "overlay_version": OVERLAY_VERSION, "candidate_origin": origin, "evidence_source_name": evidence_name,
            "evidence_source_url": evidence_url, "identity_statement": statement, "active_ingredient_in_source": active_src,
            "conflict_check": "NONE" if not result.conflicts else "CONFLICT",
        })
    return out


def harvest_catalog_candidates(pairs: dict[str, dict[str, PairEvidence]], catalog_rows: Iterable[dict], *, min_support: int = 3,
                               min_chars: int = 3, min_applicants: int = 2, exclude_aliases: Iterable[str] = ()) -> list[dict[str, str]]:
    """名錄（官方成分名稱）→ 官方成對證據充足的中文名稱候選（機械收集；仍須通過 `build_overlay_rows` 的證據規則）。

    只用官方資料：中文名稱 = 同一張藥證內與該成分英文名稱成對的寫法；不翻譯、不相似度。
    """
    skip = {zh_key(a) for a in exclude_aliases}
    out: list[dict[str, str]] = []
    for row in catalog_rows:
        code, name = row.get("ingredient_code"), row["official_name"]
        if not code:
            continue
        equivalents = official_equivalents(name)
        head = strip_official_eq_annotation(name)
        for alias, by_en in pairs.items():
            if len(alias) < min_chars or alias in skip or not any(e in by_en for e in equivalents):
                continue
            res = evaluate_candidate(alias, head, pairs, equivalents=equivalents - {head}, min_support=min_support)
            if res.passed and res.applicant_count >= min_applicants:  # 品牌名多半只有單一申請商；通用名由多家使用
                out.append({"alias_text_zh_tw": alias, "target_kind": TARGET_CATALOG, "canonical_entity_id": "",
                            "taiwan_drug_ingredient_code": code, "target_english_name": head, "identity_level": "CATALOG_OFFICIAL_NAME",
                            "reviewer_decision": "ACCEPT", "reviewer_note": "官方成對證據機械收集（需通過證據規則）",
                            "candidate_origin": ORIGIN_REVIEWER})
    return out
