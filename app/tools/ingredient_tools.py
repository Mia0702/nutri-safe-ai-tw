"""lookup_product_ingredients 的成分結構化證據（唯讀；Supabase 為 canonical source）。

輸出三個分開的層級（見 app.schemas.evidence.ProductIngredientEvidence）：
A. 官方原始成分：health_food = 既有 ProductIngredient；國產 / 輸入 = 官方成分欄位的 token 原文。
   即使沒有任何 canonical mapping 也照樣回傳（Ingredient Bridge 不是完整成分清單的替代品）。
B. 已確認 canonical ingredient：只有 VERIFIED_EXACT / SUPPORTED_ALIAS / FORM_TO_GROUP。
C. 未確認：AMBIGUOUS / NEEDS_REVIEW 候選或完全沒有 mapping —— 只能標「尚未確認」。

成分是非題（「有鋅嗎」）：使用者寫法只以 canonical entity 的正式名稱 / 正式 alias（找不到時才用顯示譯名）
「完全相同」解析成一個 entity，再用 B 判斷；form → nutrient 只經 CONTRIBUTES_TO_NUTRIENT（FORM_TO_GROUP 列），
不是 SAME_AS。候選（例如賦形劑鹽類）永遠不會變成「有」。不比對 Ingredient Bridge 以外的知識、不產生句子。
"""

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Protocol

from app.normalization import normalize_name
from app.product_coverage import category_of, has_health_claim_approval
from app.schemas.enums import IngredientPresence, IngredientQueryResolution, SourceType
from app.schemas.evidence import (
    ConfirmedIngredientEntry,
    IngredientCoverage,
    IngredientEntityRef,
    IngredientQueryAnswer,
    OfficialIngredientEntry,
    ProductIngredientEvidence,
    SourceRef,
    UnresolvedIngredientEntry,
)

HEALTH_FOOD_SOURCE = "HEALTH_FOOD_CANONICAL_INGREDIENT"
REGISTRATION_SOURCE = "TFDA_REGISTRATION_INGREDIENTS_RAW"
FORMAL_STATUSES = ("VERIFIED_EXACT", "SUPPORTED_ALIAS", "FORM_TO_GROUP")
CANDIDATE_STATUSES = ("AMBIGUOUS", "NEEDS_REVIEW")
NO_MATCH = "NO_MATCH"
BRIDGE_UNAVAILABLE = "BRIDGE_UNAVAILABLE"
NUTRIENT_GROUP_LABEL = "NutrientGroup"
DISPLAY_ONLY_POLICY = "DISPLAY_ONLY_NOT_AUTOMERGE"
OVERLAY_FORMAL_ALIAS_STATUSES = ("VERIFIED_OFFICIAL", "SUPPORTED_AUTHORITATIVE")
# 台灣口語「維他命」與官方用語「維生素」是同一個詞（一般性寫法差異，不是 identity 推論）
_QUERY_SPELLING = (("維他命", "維生素"),)


class IngredientEvidenceRepository(Protocol):
    """成分證據需要的唯讀資料存取（Supabase 實作見 app.services.supabase_service）。"""

    def get_ingredients_for_products(self, product_ids: list[str]) -> list[dict[str, Any]]: ...

    def get_product_ingredient_sources(self, product_ids: list[str]) -> list[dict[str, Any]]: ...

    def get_ingredient_bridge_reference(self) -> dict[str, Any]: ...

    def get_ingredient_entity_mappings(self, product_ids: list[str]) -> list[dict[str, Any]]: ...


def _parse_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _source(row: dict[str, Any]) -> SourceRef:
    return SourceRef(
        source_name=row.get("source_name") or "未知來源", source_type=SourceType.STRUCTURED_DB,
        source_url=row.get("source_url"), source_record_id=row.get("source_record_key"),
        retrieved_at=_parse_dt(row.get("retrieved_at")), is_official=bool(row.get("is_official", False)),
    )


def query_key(text: str | None) -> str:
    """使用者成分寫法的比對鍵：只消除格式差異（NFKC、大小寫、空白標點）與「維他命 / 維生素」寫法。"""
    text = text or ""
    for old, new in _QUERY_SPELLING:
        text = text.replace(old, new)
    return normalize_name(text)


# ----------------------------------------------------------------------------- A. 官方原始成分


def _unit_key(row: dict[str, Any]) -> tuple:
    if row.get("source_ingredient_id"):
        return ("ingredient", str(row["source_ingredient_id"]))
    if row.get("token_index") is not None:
        return ("token", int(row["token_index"]))
    return ("raw", row.get("raw_text") or "")


def _health_food_official(rows: list[dict[str, Any]]) -> list[tuple[tuple, OfficialIngredientEntry]]:
    out, seen = [], set()
    for r in rows:
        iid = str(r["ingredient_id"])
        if iid in seen:
            continue
        seen.add(iid)
        name_zh, name_en = r.get("name_zh") or "", r.get("name_en")
        out.append((("ingredient", iid), OfficialIngredientEntry(
            raw_text=r.get("raw_text") or name_zh, display_text=f"{name_zh}（{name_en}）" if name_en else name_zh,
            ingredient_source=HEALTH_FOOD_SOURCE, ingredient_id=iid, name_zh=name_zh or None, name_en=name_en,
            source=_source(r))))
    return out


def _registration_official(staging: dict[str, Any] | None, source: SourceRef) -> list[tuple[tuple, OfficialIngredientEntry]]:
    staging = staging or {}
    tokens = (staging.get("parser_draft") or {}).get("tokens") or []
    out = [(("token", i), OfficialIngredientEntry(raw_text=t, display_text=t.strip(), ingredient_source=REGISTRATION_SOURCE,
                                                  token_index=i, source=source))
           for i, t in enumerate(tokens) if isinstance(t, str) and t.strip()]
    raw = (staging.get("ingredients_raw") or "").strip()
    if not out and raw:  # 沒有 token 時保留整段官方原文（不自行拆解）
        out = [(("raw", raw), OfficialIngredientEntry(raw_text=raw, display_text=raw, ingredient_source=REGISTRATION_SOURCE,
                                                       source=source))]
    return out


# ----------------------------------------------------------------------------- B / C. bridge mapping


class _Reference:
    """Ingredient Bridge 參考資料（entity / alias）與目前使用的版本。"""

    def __init__(self, raw: dict[str, Any] | None):
        raw = raw or {}
        self.corpus_version: str | None = raw.get("corpus_version")
        self.overlay_version: str | None = raw.get("overlay_version")
        self.entities: dict[str, dict[str, Any]] = {e["entity_id"]: e for e in raw.get("entities") or []}
        self.aliases: list[dict[str, Any]] = list(raw.get("aliases") or [])

    @property
    def available(self) -> bool:
        return bool(self.corpus_version) and bool(self.entities)

    def ref(self, entity_id: str | None) -> IngredientEntityRef | None:
        e = self.entities.get(entity_id or "")
        if e is None:
            return None
        return IngredientEntityRef(entity_id=e["entity_id"], entity_type=e["graph_label"], identity_level=e["identity_level"],
                                   canonical_name_en=e["canonical_name_en"], display_name_zh=e.get("display_name_zh"))

    def parent_id(self, entity_id: str | None) -> str | None:
        return (self.entities.get(entity_id or "") or {}).get("parent_nutrient_entity_id")

    def is_group(self, entity_id: str) -> bool:
        return (self.entities.get(entity_id) or {}).get("graph_label") == NUTRIENT_GROUP_LABEL

    def same_version(self, row: dict[str, Any]) -> bool:
        return row.get("corpus_version") == self.corpus_version and row.get("overlay_version") == self.overlay_version

    def resolve_query(self, text: str) -> tuple[IngredientQueryResolution, str | None]:
        """使用者寫法 → 唯一 entity。正式名稱 / 正式 alias 優先；沒有時才看顯示譯名。多個 entity → 不猜。"""
        key = query_key(text)
        if not key:
            return IngredientQueryResolution.NOT_IN_CORPUS, None
        formal: set[str] = {eid for eid, e in self.entities.items() if query_key(e.get("canonical_name_en")) == key}
        display: set[str] = {eid for eid, e in self.entities.items() if query_key(e.get("display_name_zh")) == key}
        for a in self.aliases:
            eid = a.get("entity_id")
            if not eid or eid not in self.entities or query_key(a.get("alias_text")) != key:
                continue
            if a.get("match_policy") == DISPLAY_ONLY_POLICY:
                display.add(eid)
            elif a.get("alias_origin") != "REVIEWED_OVERLAY" or a.get("alias_status") in OVERLAY_FORMAL_ALIAS_STATUSES:
                formal.add(eid)
        ids = formal or display
        if len(ids) == 1:
            return IngredientQueryResolution.RESOLVED, next(iter(ids))
        if ids:
            return IngredientQueryResolution.AMBIGUOUS, None
        return IngredientQueryResolution.NOT_IN_CORPUS, None


IngredientReference = _Reference  # 供 routing 使用的公開名稱


def _confirmed(ref: _Reference, formal_rows: list[dict[str, Any]]) -> list[ConfirmedIngredientEntry]:
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in formal_rows:
        if ref.ref(r.get("entity_id")) is None:
            continue  # 未知 entity：資料完整性問題，不當成已確認
        groups[(r["entity_id"], r["mapping_status"], r.get("via_entity_id"))].append(r)
    out = []
    for (entity_id, status, via), rows in groups.items():
        best = max(rows, key=lambda r: float(r.get("confidence") or 0))
        out.append(ConfirmedIngredientEntry(
            entity=ref.ref(entity_id), mapping_status=status, match_method=best["match_method"],
            review_status=best["review_status"], confidence=float(best.get("confidence") or 0),
            raw_texts=list(dict.fromkeys(r["raw_text"] for r in rows)),
            parent_nutrient=None if via else ref.ref(ref.parent_id(entity_id)),
            via_entity=ref.ref(via) if via else None, relationship_id=best.get("relationship_id"),
            alias_ids=sorted({a for r in rows for a in (r.get("matched_alias_ids") or [])}),
            alias_evidence_status=best.get("alias_evidence_status"),
        ))
    return out


# ----------------------------------------------------------------------------- 成分是非題


def _listed(official: list[OfficialIngredientEntry], key: str) -> list[str]:
    return [o.display_text for o in official
            if key and key in {query_key(o.raw_text), query_key(o.display_text), query_key(o.name_zh), query_key(o.name_en)}]


def answer_ingredient_query(text: str, evidence: ProductIngredientEvidence, ref: _Reference) -> IngredientQueryAnswer:
    """一個成分寫法 → presence。只有正式 mapping 才是 CONFIRMED；候選只會是 CANDIDATE_ONLY。"""
    key = query_key(text)
    listed = _listed(evidence.official_ingredients, key)
    resolution, entity_id = ref.resolve_query(text) if ref.available else (IngredientQueryResolution.NOT_IN_CORPUS, None)
    remaining = evidence.coverage.unresolved_count
    if resolution != IngredientQueryResolution.RESOLVED:
        presence = IngredientPresence.LISTED_IN_OFFICIAL_TEXT if listed else IngredientPresence.NOT_CONFIRMED
        return IngredientQueryAnswer(query_text=text, resolution=resolution, presence=presence, listed_raw_texts=listed,
                                     unresolved_remaining=remaining)
    matched = [c for c in evidence.confirmed_ingredients if c.entity.entity_id == entity_id]
    if matched:
        return IngredientQueryAnswer(query_text=text, resolution=resolution, query_entity=ref.ref(entity_id),
                                     presence=IngredientPresence.CONFIRMED, matched=matched, listed_raw_texts=listed)
    group = entity_id if ref.is_group(entity_id) else ref.parent_id(entity_id)
    related = [c for c in evidence.confirmed_ingredients if c.is_direct and group and (
        c.entity.entity_id == group or (c.parent_nutrient is not None and c.parent_nutrient.entity_id == group))]
    targets = {entity_id}
    candidates = [u.display_text for u in evidence.unresolved_ingredients
                  if any(cid in targets or (ref.is_group(entity_id) and ref.parent_id(cid) == entity_id)
                         for cid in u.candidate_entity_ids)]
    presence = (IngredientPresence.LISTED_IN_OFFICIAL_TEXT if listed
                else IngredientPresence.CANDIDATE_ONLY if candidates else IngredientPresence.NOT_CONFIRMED)
    return IngredientQueryAnswer(query_text=text, resolution=resolution, query_entity=ref.ref(entity_id), presence=presence,
                                 related_confirmed=related, candidate_raw_texts=candidates, listed_raw_texts=listed,
                                 unresolved_remaining=remaining)


def ingredient_query_terms(terms: Iterable[str], product_names: Iterable[str] = (), user_text: str = "") -> list[str]:
    """要回答的成分寫法（去重）。只出現在產品名稱裡的詞（例如「某某維他命C有哪些成分」中的品名字樣）不當成是非題；
    使用者在品名以外又問到它（「某某鋅膠囊有鋅嗎」）就照樣回答。"""
    names = sorted({query_key(n) for n in product_names if query_key(n)}, key=len, reverse=True)
    rest = query_key(user_text)
    for name in names:
        rest = rest.replace(name, " ")
    out, seen = [], set()
    for t in terms:
        key = query_key(t)
        if not key or key in seen:
            continue
        if any(key in name for name in names) and key not in rest:
            continue
        seen.add(key)
        out.append(t.strip())
    return out


# ----------------------------------------------------------------------------- tool


def lookup_product_ingredient_evidence(
    repo: IngredientEvidenceRepository, product_id: str, ingredient_queries: Iterable[str] = (),
) -> ProductIngredientEvidence | None:
    """一個已確認產品的成分證據。查詢失敗一律拋出 ServiceError（不把失敗當成「沒有成分」）。"""
    rows = repo.get_product_ingredient_sources([product_id])
    if not rows:
        return None
    product = rows[0]
    category = category_of(product.get("product_category"))
    product_source = _source(product)
    if has_health_claim_approval(category):
        official = _health_food_official(repo.get_ingredients_for_products([product_id]))
        ingredient_source = HEALTH_FOOD_SOURCE
        raw_text = None
    else:
        staging = product.get("ingredient_staging") or {}
        official = _registration_official(staging, product_source)
        ingredient_source = REGISTRATION_SOURCE
        raw_text = (staging.get("ingredients_raw") or "").strip() or None

    ref = _Reference(repo.get_ingredient_bridge_reference())
    mapping_rows = ([r for r in repo.get_ingredient_entity_mappings([product_id]) if ref.same_version(r)]
                    if ref.available else [])
    by_unit: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in mapping_rows:
        by_unit[_unit_key(r)].append(r)
    formal_rows = [r for r in mapping_rows if r["mapping_status"] in FORMAL_STATUSES]
    confirmed = _confirmed(ref, formal_rows)

    unresolved, confirmed_units = [], 0
    for key, entry in official:
        unit_rows = by_unit.get(key, [])
        if any(r["mapping_status"] in FORMAL_STATUSES and ref.ref(r.get("entity_id")) for r in unit_rows):
            confirmed_units += 1
            continue
        candidates = [r for r in unit_rows if r["mapping_status"] in CANDIDATE_STATUSES]
        if not ref.available:
            status, review = BRIDGE_UNAVAILABLE, "NOT_APPLICABLE"
        elif candidates:
            status = "AMBIGUOUS" if any(r["mapping_status"] == "AMBIGUOUS" for r in candidates) else "NEEDS_REVIEW"
            review = candidates[0]["review_status"]
        else:
            status, review = NO_MATCH, "NOT_APPLICABLE"
        unresolved.append(UnresolvedIngredientEntry(
            raw_text=entry.raw_text, display_text=entry.display_text, mapping_status=status, review_status=review,
            candidate_entity_ids=sorted({c for r in candidates for c in (r.get("candidate_entity_ids") or [])})))

    evidence = ProductIngredientEvidence(
        evidence_id=f"product_ingredients:{product_id}", product_id=product_id, product_category=category,
        ingredient_source=ingredient_source, official_ingredients=[e for _, e in official],
        official_ingredients_raw=raw_text, confirmed_ingredients=confirmed, unresolved_ingredients=unresolved,
        coverage=IngredientCoverage(official_count=len(official), confirmed_count=confirmed_units,
                                    unresolved_count=len(official) - confirmed_units),
        bridge_available=ref.available, corpus_version=ref.corpus_version, overlay_version=ref.overlay_version,
        source=product_source,
    )
    evidence.query_answers = [answer_ingredient_query(t, evidence, ref) for t in ingredient_queries]
    return evidence
