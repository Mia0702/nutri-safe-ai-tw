"""從官方資料推導 entity aliases（一般化規則，不含任何特定品牌）。

規則
1. COMPANY / SHORT_NAME：去除公司組織型態（股份有限公司、有限公司、分公司、外商前綴等）。
2. INGREDIENT：由 ingredient_canonical 產生（官方英文名、同欄位中英相連、希臘字母拼寫）。
3. BRAND / DERIVED_BRAND_PREFIX：同一申請商的多項產品共用的名稱前綴，作為「推測品牌」候選。
   TFDA 健康食品資料沒有品牌欄位，因此推導出的品牌一律是 INFERRED（不會是 OFFICIAL_RECORD）。
   Brand inference quality gate（皆為一般化規則，不含任何品牌名稱清單）：
   - 共用數以「不同產品名稱」計算；同名重複紀錄（多張許可證同一品名）不算多項產品
   - 前綴長度 2~4 字元、不可切在英數單字中間，且占該公司不同品名 >= 30%
   - 一般描述詞：片段出現在 >= 3 家其他公司的品名中，且多數不在品名開頭 → 前綴本身是描述詞 → REJECTED
   - 前綴尾段（>=2 字）是一般描述詞（例如成分、劑型字樣）→ 前綴切進產品描述 → REJECTED
   - 前綴被 >= 3 家公司當品名開頭，且沒有任何申請商名稱含此字樣 → 通用詞 → REJECTED
   - 邊界：共用產品在前綴後分歧、接一般描述詞、或前綴以「牌」結尾，才是自然的品牌邊界；
     否則（例如兩項產品後面接同一串字）無法判斷品牌在哪裡結束 → REVIEW_CANDIDATE
   - 前綴被 >= 2 家公司當品名開頭（可能是跨公司授權品牌）→ REVIEW_CANDIDATE，不自動歸給單一公司
   - confidence 由不同品名共用數、覆蓋率、與公司簡稱的關聯、「牌」字標記合成；
     >= AUTO_ACCEPT_CONFIDENCE 且無 gate 問題才自動採用（ACCEPTED），否則 REVIEW_CANDIDATE
   - 只有 ACCEPTED（或人工審核 MANUAL_REVIEWED）會成為 brands / product_brands / alias；
     其他候選保留在 brand_candidates 供 dry-run 與人工審核，不刪除、不寫入品牌表
   - 一家公司可以有 0..N 個品牌候選：每條「前綴鏈」（彼此互為前綴，如 X、XY、XYZ）只是同一品牌的
     不同邊界，取其中最佳者；彼此不互為前綴的候選各自獨立通過 gate，可同時被採用
4. 公司與品牌的關係（company_relation）：
   - OWNS_BRAND：只限 OFFICIAL_RECORD / MANUAL_REVIEWED，且有 ownership_evidence（例如人工審核附上官方商標或公司資料）
   - ASSOCIATED_WITH_BRAND：其他情況，包含所有由產品名稱推導的品牌；只代表「有關聯」，不代表官方確認擁有
"""

import re
from collections import Counter, defaultdict
from enum import StrEnum
from typing import Any, NamedTuple

from app.ingestion.ingredient_canonical import build_ingredient_canonicalization
from app.ingestion.tfda_health_food import HealthFoodBundle
from app.normalization import normalize_name, stable_id
from app.schemas.enums import AliasType, BrandCompanyRelation, EntityType, EvidenceStatus

_ORG_PREFIX_RE = re.compile(r"^(香港商|美商|日商|英屬維京群島商|英屬開曼群島商|新加坡商|荷蘭商|瑞士商|德商|法商|韓商|澳商|加拿大商)")
_ORG_SUFFIX_RE = re.compile(r"(股份有限公司|有限公司|公司)?(台灣分公司|臺灣分公司|分公司)?$")
_TRAILING_ORG_RE = re.compile(r"(股份有限公司|有限公司|公司)$")

MIN_PREFIX, MAX_PREFIX = 2, 4
MIN_SHARED_PRODUCTS = 2
MIN_SHARE = 0.3
GENERIC_PREFIX_COMPANY_COUNT = 3  # 前綴被幾家公司當品名開頭且無公司名稱支持 → 通用詞
GENERIC_FRAGMENT_COMPANY_COUNT = 3  # 片段出現在幾家「其他」公司的產品名稱中 → 一般描述詞
AUTO_ACCEPT_CONFIDENCE = 0.65
BRAND_MARKER = "牌"  # 中文「X牌」本身就是品牌標記（一般語言規則）


class BrandGateStatus(StrEnum):
    ACCEPTED = "ACCEPTED"  # 通過 quality gate，自動採用（仍為 INFERRED）
    REVIEW_CANDIDATE = "REVIEW_CANDIDATE"  # 不確定：不自動採用，等待人工審核
    REJECTED = "REJECTED"  # quality gate 判定不像品牌：不採用，但保留候選紀錄


_GATE_RANK = {BrandGateStatus.ACCEPTED: 2, BrandGateStatus.REVIEW_CANDIDATE: 1, BrandGateStatus.REJECTED: 0}

def company_short_name(name: str) -> str | None:
    short = _ORG_PREFIX_RE.sub("", name.strip())
    short = _ORG_SUFFIX_RE.sub("", short)
    short = _TRAILING_ORG_RE.sub("", short).strip()
    return short if len(short) >= 2 and short != name else None


def _is_latin_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _cuts_latin_word(prefix: str, names: list[str]) -> bool:
    if not _is_latin_alnum(prefix[-1]):
        return False
    return any(n.startswith(prefix) and len(n) > len(prefix) and _is_latin_alnum(n[len(prefix)]) for n in names)


def _related_to_company(prefix: str, short_name: str | None) -> bool:
    if not short_name:
        return False
    short = normalize_name(short_name)
    return bool(short) and (prefix in short or short in prefix)


def brand_company_relation(evidence_status: EvidenceStatus, inference_basis: dict[str, Any]) -> BrandCompanyRelation:
    """只有非推測、且帶 ownership evidence 的品牌才能說公司「擁有」品牌。"""
    if evidence_status != EvidenceStatus.INFERRED and inference_basis.get("ownership_evidence"):
        return BrandCompanyRelation.OWNS_BRAND
    return BrandCompanyRelation.ASSOCIATED_WITH_BRAND


def derive_brand_candidates(
    company_products: dict[str, list[str]], company_short_names: dict[str, str | None] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """company_id -> 品牌候選清單（0..N，最佳者在前；含 gate_status / gate_reasons / confidence）。

    輸入為正規化產品名稱。同一條前綴鏈只保留最佳邊界；不同前綴鏈各自獨立評估，不因去重而只留一個。
    """
    company_short_names = company_short_names or {}
    distinct = {cid: sorted(set(names)) for cid, names in company_products.items()}

    prefix_companies: dict[str, set[str]] = defaultdict(set)
    for cid, names in distinct.items():
        for n in names:
            for length in range(MIN_PREFIX, MAX_PREFIX + 1):
                if len(n) > length:
                    prefix_companies[n[:length]].add(cid)

    fragment_cache: dict[tuple[str, str], tuple[set[str], set[str]]] = {}

    def fragment_spread(fragment: str, exclude: str = "") -> tuple[set[str], set[str]]:
        """(出現該片段的公司, 只在品名開頭出現的公司)；exclude：忽略含有此字串的品名（避免把前綴自己算進去）。"""
        key = (fragment, exclude)
        if key not in fragment_cache:
            seen: set[str] = set()
            mid: set[str] = set()
            for c, names in distinct.items():
                for n in names:
                    if fragment in n and not (exclude and exclude in n):
                        seen.add(c)
                        if n.find(fragment, 1) != -1 or not n.startswith(fragment):
                            mid.add(c)
            fragment_cache[key] = (seen, seen - mid)
        return fragment_cache[key]

    def is_descriptor(fragment: str, cid: str, exclude: str = "") -> bool:
        """一般描述詞：出現在多家其他公司品名中，且多數不是當作品名開頭（品牌通常在開頭）。"""
        seen, start_only = fragment_spread(fragment, exclude)
        others, start_others = seen - {cid}, start_only - {cid}
        return len(others) >= GENERIC_FRAGMENT_COMPANY_COUNT and len(start_others) * 2 <= len(others)

    all_short_names = [normalize_name(v) for v in company_short_names.values() if v]

    def evaluate(cid: str, prefix: str, names: list[str], records: int) -> dict[str, Any]:
        shared_names = [n for n in names if n.startswith(prefix) and len(n) > len(prefix)]
        shared = len(shared_names)
        share = shared / len(names)
        reasons: list[str] = []
        status = BrandGateStatus.ACCEPTED
        if shared < MIN_SHARED_PRODUCTS:
            status = BrandGateStatus.REJECTED
            reasons.append("single_distinct_product")  # 只有同名重複紀錄支持
        sharing = len(prefix_companies[prefix])
        named_after = any(prefix in short for short in all_short_names)  # 某申請商名稱就含此字樣
        if is_descriptor(prefix, cid) or (sharing >= GENERIC_PREFIX_COMPANY_COUNT and not named_after):
            status = BrandGateStatus.REJECTED
            reasons.append("generic_descriptor")
        tail = next((prefix[i:] for i in range(1, len(prefix) - 1) if is_descriptor(prefix[i:], cid, prefix)), None)
        if tail:
            status = BrandGateStatus.REJECTED
            reasons.append("prefix_extends_into_descriptor")
        marker = prefix.endswith(BRAND_MARKER)
        continuations = {n[len(prefix):] for n in shared_names}
        natural_boundary = (
            marker or len({c[0] for c in continuations}) >= 2 or any(is_descriptor(c[:2], cid) for c in continuations)
        )
        if not natural_boundary:
            reasons.append("ambiguous_boundary")  # 共用產品後面接同一串字，無法判斷品牌在哪裡結束
        if sharing >= 2:
            reasons.append("prefix_shared_with_other_company")  # 可能是跨公司授權品牌，不能自動歸給單一公司
        related = _related_to_company(prefix, company_short_names.get(cid))
        confidence = 0.4 + min(0.2, 0.1 * max(0, shared - 2)) + 0.2 * share
        confidence += 0.15 if related else 0.0
        confidence += 0.1 if marker else 0.0
        confidence = round(min(0.95, confidence), 3)  # 推測永遠 < 1
        if status == BrandGateStatus.ACCEPTED and (reasons or confidence < AUTO_ACCEPT_CONFIDENCE):
            status = BrandGateStatus.REVIEW_CANDIDATE
            if confidence < AUTO_ACCEPT_CONFIDENCE:
                reasons.append("below_auto_accept_confidence")
        return {
            "prefix": prefix, "shared": shared, "total": len(names), "share": round(share, 3),
            "records_total": records, "confidence": confidence, "gate_status": status, "gate_reasons": reasons,
            "natural_boundary": natural_boundary, "related_to_company_name": related, "brand_marker": marker,
            "descriptor_tail": tail,
        }

    result: dict[str, list[dict[str, Any]]] = {}
    for cid, names in distinct.items():
        records = len(company_products[cid])
        if records < MIN_SHARED_PRODUCTS:
            continue
        scored: list[tuple[tuple, dict[str, Any]]] = []
        counts: Counter[str] = Counter()
        for n in company_products[cid]:  # 以紀錄數篩選候選，讓同名重複紀錄也能被回報（並被 gate 擋下）
            for length in range(MIN_PREFIX, MAX_PREFIX + 1):
                if len(n) > length:
                    counts[n[:length]] += 1
        for prefix, rec_shared in counts.items():
            if rec_shared < MIN_SHARED_PRODUCTS or rec_shared / records < MIN_SHARE:
                continue
            if _cuts_latin_word(prefix, names):
                continue
            info = evaluate(cid, prefix, names, records)
            key = (_GATE_RANK[info["gate_status"]], info["shared"], info["natural_boundary"], info["confidence"],
                   len(prefix), prefix)
            scored.append((key, info))
        chosen: list[dict[str, Any]] = []
        for _, info in sorted(scored, key=lambda s: s[0], reverse=True):
            p = info["prefix"]
            # 與已選候選互為前綴 → 同一品牌的另一種邊界，不另立品牌；否則是獨立候選
            if not any(p.startswith(c["prefix"]) or c["prefix"].startswith(p) for c in chosen):
                chosen.append(info)
        if chosen:
            result[cid] = chosen
    return result


def derive_brand_prefixes(
    company_products: dict[str, list[str]], company_short_names: dict[str, str | None] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """只回傳通過 quality gate（ACCEPTED）的品牌候選（每家公司 0..N 個）。"""
    result: dict[str, list[dict[str, Any]]] = {}
    for cid, infos in derive_brand_candidates(company_products, company_short_names).items():
        accepted = [i for i in infos if i["gate_status"] == BrandGateStatus.ACCEPTED]
        if accepted:
            result[cid] = accepted
    return result


class AliasBuild(NamedTuple):
    brands: list[dict[str, Any]]
    product_brands: list[dict[str, Any]]
    aliases: list[dict[str, Any]]  # 含 entity_type / entity_id；寫入 DB 前由 loader 轉成對應 FK 欄位
    brand_candidates: list[dict[str, Any]] = []  # 每家公司 0..N 個品牌候選（含未採用者），供 dry-run / 人工審核


def build_aliases(
    bundles: list[HealthFoodBundle], source_id: str, manual_brand_reviews: list[dict[str, str]] = (),
) -> AliasBuild:
    """bundles 應涵蓋完整資料集，推導才有統計意義。

    manual_brand_reviews: [{"company_id", "brand", "reviewer_note", "ownership_evidence"?}]，人工審核確認的品牌候選；
    只有附 ownership_evidence 時，公司與品牌的關係才是 OWNS_BRAND。
    """
    aliases: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(entity_type: EntityType, entity_id: str, alias: str, alias_type: AliasType, confidence: float,
            status: EvidenceStatus, inference_basis: dict,
            source_record_key: str | None = None, source_url: str | None = None):
        norm = normalize_name(alias)
        if len(norm) < 2:
            return
        key = (entity_type.value, entity_id, norm)
        if key in aliases and aliases[key]["confidence"] >= confidence:
            return
        aliases[key] = {
            "id": stable_id("alias", ":".join(key)),
            "entity_type": entity_type.value, "entity_id": entity_id, "alias": alias, "alias_normalized": norm,
            "alias_type": alias_type.value, "source_id": source_id, "source_record_key": source_record_key,
            "source_url": source_url, "confidence": confidence,
            "evidence_status": status.value, "inference_basis": inference_basis,
        }

    company_products: dict[str, list[str]] = defaultdict(list)
    company_product_ids: dict[str, list[tuple[str, str]]] = defaultdict(list)
    company_names: dict[str, str] = {}
    for b in bundles:
        cid = b.company["id"]
        company_names[cid] = b.company["name_zh"]
        company_products[cid].append(b.product["product_name_normalized"])
        company_product_ids[cid].append((b.product["id"], b.product["product_name_normalized"]))
        short = company_short_name(b.company["name_zh"])
        if short:
            # 去除組織型態是系統推測的簡稱，不是官方登記名稱
            add(EntityType.COMPANY, cid, short, AliasType.SHORT_NAME, 0.9, EvidenceStatus.INFERRED,
                {"rule": "strip_legal_form", "official_name": b.company["name_zh"]}, b.source_record_key, b.source_url)

    # 成分 alias：只取通過 canonicalization 品質把關者（衝突／清單／具體程度不一致者不建立）
    for a in build_ingredient_canonicalization(m for b in bundles for m in b.ingredient_mentions).aliases:
        add(EntityType.INGREDIENT, a["ingredient_id"], a["alias"], AliasType(a["alias_type"]), a["confidence"],
            EvidenceStatus(a["evidence_status"]), a["inference_basis"], a["source_record_key"], a["source_url"])

    short_names = {cid: company_short_name(name) for cid, name in company_names.items()}
    reviewed = {(r["company_id"], normalize_name(r["brand"])): r for r in manual_brand_reviews}
    brands: list[dict[str, Any]] = []
    product_brands: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for cid, infos in derive_brand_candidates(company_products, short_names).items():
        for info in infos:  # 每個候選獨立通過 gate；同一公司可同時採用多個品牌
            brand_id = stable_id("brand", f"TW:{cid}:{info['prefix']}")
            # 品牌由產品名稱統計推導：屬於 inferred evidence，不是官方確認的品牌歸屬
            basis = {
                "rule": "shared_product_name_prefix",
                "prefix": info["prefix"],
                "company_name": company_names[cid],
                "official_brand_field": False,  # 官方資料沒有品牌欄位，不能標為 OFFICIAL_RECORD
                **{k: info[k] for k in ("shared", "total", "share", "records_total", "gate_reasons",
                                        "natural_boundary", "related_to_company_name", "brand_marker")},
                "gate_status": info["gate_status"].value,
            }
            review = reviewed.get((cid, info["prefix"]))
            if review:
                status, derivation = EvidenceStatus.MANUAL_REVIEWED, "MANUAL_REVIEWED"
                basis["manual_review"] = {"reviewer_note": review.get("reviewer_note", "")}
                if review.get("ownership_evidence"):  # 人工審核確認品牌存在 ≠ 確認公司擁有，需另附依據
                    basis["ownership_evidence"] = review["ownership_evidence"]
            else:
                status, derivation = EvidenceStatus.INFERRED, AliasType.DERIVED_BRAND_PREFIX.value
            relation = brand_company_relation(status, basis)
            candidates.append({
                "brand_id": brand_id, "company_id": cid, "name": info["prefix"], "confidence": info["confidence"],
                "gate_status": info["gate_status"].value, "evidence_status": status.value,
                "is_inferred": status == EvidenceStatus.INFERRED, "company_relation": relation.value,
                "source_id": source_id, "inference_basis": basis,
            })
            if info["gate_status"] != BrandGateStatus.ACCEPTED and not review:
                continue  # 不確定 / 被擋下：只保留為候選，不成為品牌、不建立產品關聯或別名
            brands.append({
                "id": brand_id, "country_code": "TW", "name": info["prefix"], "name_normalized": info["prefix"],
                "company_id": cid, "company_relation": relation.value, "source_id": source_id,
                "derivation": derivation,
                "evidence_status": status.value, "confidence": info["confidence"], "inference_basis": basis,
            })
            add(EntityType.BRAND, brand_id, info["prefix"], AliasType.DERIVED_BRAND_PREFIX, info["confidence"],
                status, basis)
            for product_id, name_norm in company_product_ids[cid]:
                if name_norm.startswith(info["prefix"]):
                    product_brands.append({
                        "product_id": product_id, "brand_id": brand_id, "source_id": source_id,
                        "derivation": derivation,
                        "evidence_status": status.value, "confidence": info["confidence"],
                        "inference_basis": {"rule": "product_name_starts_with_brand_prefix", "prefix": info["prefix"],
                                            "brand_gate_status": info["gate_status"].value},
                    })
    return AliasBuild(brands, product_brands, list(aliases.values()), candidates)
