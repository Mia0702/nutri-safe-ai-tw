"""Product ingredient bridge 的 runtime structured payload（不產生使用者可見句子）。

只輸出 structured facts / canonical entities / mapping 狀態 / missing information / semantic status（enum），
由 Agent C 依使用者意圖產生自然繁體中文；renderer 只依 semantic status 決定標題、顏色、區塊與按鈕，
不得自行重新判斷 clinical meaning。

重複攝取：兩個產品對應到同一營養素群 → 只回報 `DUPLICATE_INGREDIENT_DETECTED`。
沒有每份含量、份量、每日份數、amount basis 時，`intake_amount_assessment = NOT_EVALUATED`；
本模組不判斷過量 / 超過上限 / 危險，也不建立 Product↔Product 交互作用。
"""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.ingestion.ingredient_entity_mapping import (
    CANDIDATE_STATUSES,
    AmountBasis,
    IngredientMapping,
    IngredientSource,
    InteractionCorpus,
    MappingStatus,
    MatchMethod,
    ProductIngredientSource,
    ProductMappingResult,
    ReviewStatus,
)


class BridgeStatus(StrEnum):
    """產品層 semantic status（renderer 用來決定版面，不是風險等級）。"""

    INGREDIENTS_MAPPED = "INGREDIENTS_MAPPED"                    # 至少一個正式 mapping
    CANDIDATES_ONLY = "CANDIDATES_ONLY"                          # 只有 AMBIGUOUS / NEEDS_REVIEW 候選
    NO_CORPUS_MATCH = "NO_CORPUS_MATCH"                          # 有成分資料但不在 corpus 範圍
    NO_SOURCE_INGREDIENT = "NO_SOURCE_INGREDIENT"                # 官方資料沒有成分欄位


class EvidenceStatus(StrEnum):
    CORPUS_IDENTITY_MAPPING = "CORPUS_IDENTITY_MAPPING"          # 只有成分身分對應，尚未查任何 interaction evidence
    NOT_AVAILABLE = "NOT_AVAILABLE"


class MissingInformation(StrEnum):
    AMOUNT_PER_SERVING = "AMOUNT_PER_SERVING"
    SERVING_SIZE = "SERVING_SIZE"
    DAILY_SERVINGS = "DAILY_SERVINGS"
    AMOUNT_BASIS = "AMOUNT_BASIS"                                # 元素量 / 鹽重量 / 活性量
    INGREDIENT_MAPPING_REVIEW = "INGREDIENT_MAPPING_REVIEW"      # 有候選待人工審核
    SOURCE_INGREDIENT_LIST = "SOURCE_INGREDIENT_LIST"


class DuplicateFinding(StrEnum):
    DUPLICATE_INGREDIENT_DETECTED = "DUPLICATE_INGREDIENT_DETECTED"


class IntakeAmountAssessment(StrEnum):
    NOT_EVALUATED = "NOT_EVALUATED"                              # 第一版永遠不評估過量


AMOUNT_REQUIREMENTS = (MissingInformation.AMOUNT_PER_SERVING, MissingInformation.SERVING_SIZE,
                       MissingInformation.DAILY_SERVINGS, MissingInformation.AMOUNT_BASIS)


class EntityRef(BaseModel):
    entity_id: str
    identity_level: str
    canonical_name_en: str
    display_name_zh: str | None = None
    graph_label: str
    source_id: str
    source_url: str | None = None
    origin: str                              # BASE_CORPUS / REVIEWED_OVERLAY


class MatchedIngredient(BaseModel):
    entity: EntityRef
    mapping_status: MappingStatus
    match_method: str
    confidence: float
    review_status: str
    raw_texts: list[str]
    alias_ids: list[str] = Field(default_factory=list)
    alias_evidence_status: str | None = None                        # overlay：VERIFIED_OFFICIAL / SUPPORTED_AUTHORITATIVE
    contributes_to: list[EntityRef] = Field(default_factory=list)   # 只對 form；不是 SAME_AS
    relationship_ids: list[str] = Field(default_factory=list)       # CONTRIBUTES_TO_NUTRIENT 審核紀錄
    amount_raw: str | None = None
    amount_normalized: float | None = None
    unit: str | None = None
    amount_basis: str


class CandidateIngredient(BaseModel):
    raw_text: str
    mapping_status: MappingStatus
    match_method: str
    candidate_entities: list[EntityRef]      # overlay 審核為候選但未指定 entity 時可為空
    review_status: str
    alias_ids: list[str] = Field(default_factory=list)
    alias_evidence_status: str | None = None


class ProductIngredientPayload(BaseModel):
    product_id: str
    product_category: str
    ingredient_source: str
    source_parse_status: str | None = None   # 研究 staging 解析就緒度（不是 clinical verification）
    bridge_status: BridgeStatus
    evidence_status: EvidenceStatus
    matched_ingredients: list[MatchedIngredient]
    candidate_ingredients: list[CandidateIngredient]
    unmatched_raw_count: int
    missing_information: list[MissingInformation]
    corpus_version: str
    overlay_version: str | None = None
    official_raw_ingredients: list[str] = Field(default_factory=list)   # 官方原文（原樣，順序同來源）
    unresolved_raw_texts: list[str] = Field(default_factory=list)       # 沒有正式 / 候選 mapping 的原文


class DuplicateProductRef(BaseModel):
    product_id: str
    via_entities: list[EntityRef]            # 本產品經由哪些 form / group 對應到該營養素群


class DuplicateCandidate(BaseModel):
    finding: DuplicateFinding
    nutrient: EntityRef
    products: list[DuplicateProductRef]
    same_identity_level_across_products: bool  # 例如兩邊都是 Zinc；魚油 vs EPA → False（不可比較重量）
    duplicate_template_id: str | None = None   # corpus DUP_* 模板（SELF_ACCUMULATION，不是 INTERACTS_WITH）
    intake_amount_assessment: IntakeAmountAssessment
    missing_information: list[MissingInformation]


class DuplicateIntakePayload(BaseModel):
    product_ids: list[str]
    duplicate_candidates: list[DuplicateCandidate]
    excluded_candidate_only_products: list[str]   # 只有候選 mapping 的產品不參與重複判定
    corpus_version: str


def entity_ref(corpus: InteractionCorpus, entity_id: str) -> EntityRef:
    e = corpus.entities[entity_id]
    return EntityRef(entity_id=e.entity_id, identity_level=e.identity_level, canonical_name_en=e.canonical_name_en,
                     display_name_zh=e.display_name_zh, graph_label=e.graph_label, source_id=e.source_id,
                     source_url=e.source_url, origin=e.origin)


def product_ingredient_payload(corpus: InteractionCorpus, result: ProductMappingResult) -> ProductIngredientPayload:
    src = result.source
    matched = []
    for entity_id, ms in sorted(result.contains_targets().items()):
        first = max(ms, key=lambda m: m.confidence)
        parent = corpus.parent(entity_id)
        matched.append(MatchedIngredient(
            entity=entity_ref(corpus, entity_id), mapping_status=first.mapping_status,
            match_method=first.match_method.value, confidence=first.confidence, review_status=first.review_status.value,
            raw_texts=sorted({m.raw_text for m in ms}),
            alias_ids=sorted({a for m in ms for a in m.matched_alias_ids}),
            alias_evidence_status=first.alias_evidence_status,
            contributes_to=[entity_ref(corpus, parent)] if parent else [],
            relationship_ids=[corpus.relationship_id(entity_id)] if parent else [],
            amount_raw=first.amount_raw, amount_normalized=None, unit=first.unit, amount_basis=first.amount_basis.value,
        ))
    candidates = [
        CandidateIngredient(raw_text=m.raw_text, mapping_status=m.mapping_status, match_method=m.match_method.value,
                            candidate_entities=[entity_ref(corpus, e) for e in m.candidate_entity_ids],
                            review_status=m.review_status.value, alias_ids=list(m.matched_alias_ids),
                            alias_evidence_status=m.alias_evidence_status)
        for m in result.mappings if m.mapping_status in CANDIDATE_STATUSES
    ]
    unmatched = sum(1 for m in result.mappings if m.mapping_status == MappingStatus.NO_MATCH)
    if not src.ingredients:
        status = BridgeStatus.NO_SOURCE_INGREDIENT
    elif matched:
        status = BridgeStatus.INGREDIENTS_MAPPED
    elif candidates:
        status = BridgeStatus.CANDIDATES_ONLY
    else:
        status = BridgeStatus.NO_CORPUS_MATCH
    missing: list[MissingInformation] = []
    if not src.ingredients:
        missing.append(MissingInformation.SOURCE_INGREDIENT_LIST)
    if candidates:
        missing.append(MissingInformation.INGREDIENT_MAPPING_REVIEW)
    if matched:
        missing.extend(AMOUNT_REQUIREMENTS)
    return ProductIngredientPayload(
        product_id=src.product_id, product_category=src.product_category, ingredient_source=src.ingredient_source.value,
        source_parse_status=src.source_parse_status, bridge_status=status,
        evidence_status=EvidenceStatus.CORPUS_IDENTITY_MAPPING if matched else EvidenceStatus.NOT_AVAILABLE,
        matched_ingredients=matched, candidate_ingredients=candidates, unmatched_raw_count=unmatched,
        missing_information=missing, corpus_version=corpus.version, overlay_version=corpus.overlay_version,
        official_raw_ingredients=[i.raw_text for i in src.ingredients],
        unresolved_raw_texts=[m.raw_text for m in result.mappings if m.mapping_status == MappingStatus.NO_MATCH],
    )


def detect_duplicate_ingredients(corpus: InteractionCorpus, results: list[ProductMappingResult]) -> DuplicateIntakePayload:
    """不同產品對應到同一營養素群（只用正式 mapping）→ DUPLICATE_INGREDIENT_DETECTED；不評估劑量。"""
    by_group: dict[str, dict[str, set[str]]] = defaultdict(dict)
    excluded = []
    for result in results:
        groups = result.nutrient_groups()
        if not groups and any(m.mapping_status in CANDIDATE_STATUSES for m in result.mappings):
            excluded.append(result.product_id)
        for group_id, via in groups.items():
            by_group[group_id][result.product_id] = via
    duplicates = []
    for group_id in sorted(by_group):
        products = by_group[group_id]
        if len(products) < 2:
            continue
        via_sets = [frozenset(v) for v in products.values()]
        duplicates.append(DuplicateCandidate(
            finding=DuplicateFinding.DUPLICATE_INGREDIENT_DETECTED, nutrient=entity_ref(corpus, group_id),
            products=[DuplicateProductRef(product_id=pid, via_entities=[entity_ref(corpus, e) for e in sorted(via)])
                      for pid, via in sorted(products.items())],
            same_identity_level_across_products=len(set(via_sets)) == 1,
            duplicate_template_id=corpus.duplicate_templates.get(group_id),
            intake_amount_assessment=IntakeAmountAssessment.NOT_EVALUATED,
            missing_information=list(AMOUNT_REQUIREMENTS),
        ))
    return DuplicateIntakePayload(
        product_ids=[r.product_id for r in results], duplicate_candidates=duplicates,
        excluded_candidate_only_products=sorted(excluded), corpus_version=corpus.version,
    )


class StoredBridgeVersionMismatch(ValueError):
    """已存的 bridge 列與目前載入的 corpus / overlay 版本不同（不混用版本）。"""


def result_from_stored_rows(corpus: InteractionCorpus, source: ProductIngredientSource,
                            rows: list[dict[str, Any]]) -> ProductMappingResult:
    """product_ingredient_entity_mappings 的已存列 → ProductMappingResult（不重新比對）。

    沒有任何已存列的官方原文（importer 不存 NO_MATCH）→ NO_MATCH；未解析成分不影響其他正式成分。
    """
    mappings: list[IngredientMapping] = []
    for r in rows:
        if r["corpus_version"] != corpus.version or r.get("overlay_version") != corpus.overlay_version:
            raise StoredBridgeVersionMismatch(f"{r['corpus_version']}/{r.get('overlay_version')}")
        mappings.append(IngredientMapping(
            product_id=str(r["product_id"]), ingredient_source=IngredientSource(r["ingredient_source"]),
            raw_text=r["raw_text"], mapping_status=MappingStatus(r["mapping_status"]),
            match_method=MatchMethod(r["match_method"]), confidence=float(r["confidence"]),
            review_status=ReviewStatus(r["review_status"]), corpus_version=r["corpus_version"],
            entity_id=r.get("entity_id"), entity_type=r.get("entity_type"),
            identity_level=corpus.entities[r["entity_id"]].identity_level if r.get("entity_id") else None,
            candidate_entity_ids=tuple(r.get("candidate_entity_ids") or ()),
            matched_alias_ids=tuple(r.get("matched_alias_ids") or ()), via_entity_id=r.get("via_entity_id"),
            source_ingredient_id=str(r["source_ingredient_id"]) if r.get("source_ingredient_id") else None,
            token_index=r.get("token_index"), amount_raw=r.get("amount_raw"), unit=r.get("unit"),
            amount_basis=AmountBasis(r.get("amount_basis") or "UNKNOWN"), source_record_key=r.get("source_record_key"),
            source_url=r.get("source_url"), overlay_version=r.get("overlay_version"),
            alias_evidence_status=r.get("alias_evidence_status"), overlay_precedence=r.get("overlay_precedence"),
            relationship_id=r.get("relationship_id"),
        ))
    seen = {(m.source_ingredient_id, m.token_index) for m in mappings}
    for ing in source.ingredients:
        if (ing.source_ingredient_id, ing.token_index) not in seen:
            mappings.append(IngredientMapping(
                product_id=source.product_id, ingredient_source=source.ingredient_source, raw_text=ing.raw_text,
                mapping_status=MappingStatus.NO_MATCH, match_method=MatchMethod.NONE, confidence=0.0,
                review_status=ReviewStatus.NOT_APPLICABLE, corpus_version=corpus.version,
                source_ingredient_id=ing.source_ingredient_id, token_index=ing.token_index, amount_raw=ing.amount_raw,
                overlay_version=corpus.overlay_version,
            ))
    return ProductMappingResult(source=source, mappings=mappings)


def payload_dict(payload: BaseModel) -> dict[str, Any]:
    return payload.model_dump(mode="json")
