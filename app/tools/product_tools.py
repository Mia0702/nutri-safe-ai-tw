"""官方結構化資料查詢 tools（Supabase）。

Tool 只回傳結構化結果與比對分數；VERIFIED / PARTIAL_MATCH 等判定由
app.risk.assessment_rules.resolve_product_match 依固定規則決定。
"""

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from app.normalization import extract_permit_code, name_match_score, normalize_name, search_terms
from app.schemas.enums import EntityType, EvidenceScope, EvidenceStatus, ProductStatus, SourceType
from app.schemas.evidence import (
    CLAIM_KIND_UNCLASSIFIED,
    OFFICIAL_ALIAS_MATCHED_BY,
    ApprovedClaimEvidence,
    EvidenceRelevance,
    IllegalAdEvidence,
    IngredientRecord,
    ProductCandidateMatch,
    ProductRecord,
    SourceRef,
    WarningEvidence,
)

# 違規廣告紀錄要與查詢名稱高度相似才採用，避免片段命中造成誤判
ILLEGAL_AD_MIN_SCORE = 0.8
INGREDIENT_MIN_SCORE = 0.6
COMPANY_MIN_SCORE = 0.8
# 別名 confidence 達此門檻才會自動用來解析實體；低於門檻只回報為不確定候選
ALIAS_MIN_CONFIDENCE = 0.6
ALIAS_MIN_MATCH_SCORE = 0.9


class OfficialDataRepository(Protocol):
    """官方結構化資料存取介面（Supabase 實作見 app.services.supabase_service）。"""

    def search_products(self, terms: list[str], limit: int) -> list[dict[str, Any]]: ...

    def get_products_by_permit_code(self, permit_code: str) -> list[dict[str, Any]]: ...

    def search_ingredients(self, terms: list[str], limit: int) -> list[dict[str, Any]]: ...

    def get_approved_claims(self, product_ids: list[str]) -> list[dict[str, Any]]: ...

    def get_warnings(self, product_ids: list[str]) -> list[dict[str, Any]]: ...

    def get_ingredients_for_products(self, product_ids: list[str]) -> list[dict[str, Any]]: ...

    def search_illegal_ad_cases(self, terms: list[str], limit: int) -> list[dict[str, Any]]: ...

    def search_entity_aliases(self, terms: list[str], entity_types: list[str], limit: int) -> list[dict[str, Any]]: ...

    def get_products_by_company_ids(self, company_ids: list[str], limit: int) -> list[dict[str, Any]]: ...


def _parse_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _source_from_row(row: dict[str, Any], record_key_field: str = "source_record_key") -> SourceRef:
    return SourceRef(
        source_name=row.get("source_name") or "未知來源",
        source_type=SourceType.STRUCTURED_DB,
        source_url=row.get("source_url"),
        source_record_id=row.get(record_key_field),
        retrieved_at=_parse_dt(row.get("retrieved_at")),
        is_official=bool(row.get("is_official", False)),
    )


def _product_from_row(row: dict[str, Any]) -> ProductRecord:
    try:
        status = ProductStatus(row.get("status") or "UNKNOWN")
    except ValueError:
        status = ProductStatus.UNKNOWN
    return ProductRecord(
        product_id=str(row["product_id"]),
        country_code=row.get("country_code") or "TW",
        product_name_zh=row["product_name_zh"],
        product_name_en=row.get("product_name_en"),
        company_id=str(row["company_id"]) if row.get("company_id") else None,
        company_name=row.get("company_name"),
        permit_id=str(row["permit_id"]) if row.get("permit_id") else None,
        permit_number=row.get("permit_number"),
        permit_type=row.get("permit_type"),
        approval_date=str(row["approval_date"]) if row.get("approval_date") else None,
        status=status,
        source=_source_from_row(row),
    )


# ---------------------------------------------------------------------------
# lookup_product
# ---------------------------------------------------------------------------


class ProductLookupOutput(BaseModel):
    candidates: list[ProductCandidateMatch]

    @property
    def result_count(self) -> int:
        return len(self.candidates)


def lookup_product(
    repo: OfficialDataRepository,
    product_names: list[str],
    brand_names: list[str] | None = None,
    limit: int = 20,
) -> ProductLookupOutput:
    """以產品名稱（可附品牌）查官方產品，回傳依分數排序的候選。"""
    queries: list[str] = []
    for name in product_names:
        queries.append(name)
        for brand in brand_names or []:
            if normalize_name(brand) and normalize_name(brand) not in normalize_name(name):
                queries.append(f"{brand}{name}")

    best: dict[str, ProductCandidateMatch] = {}
    for query in queries:
        terms = search_terms(query)
        if not terms:
            continue
        for row in repo.search_products(terms, limit):
            product = _product_from_row(row)
            score = name_match_score(query, product.product_name_zh)
            current = best.get(product.product_id)
            if current is None or score > current.match_score:
                best[product.product_id] = ProductCandidateMatch(
                    product=product, match_score=score, matched_by="product_name", query=query
                )
    # 官方 / 人工審核的產品別名完全相同：視為與官方品名完全相同（推測別名或低信心別名不採用）
    for alias in resolve_entity_alias(repo, product_names, [EntityType.PRODUCT]):
        if alias.uncertain or alias.is_inferred or normalize_name(alias.alias) != normalize_name(alias.query):
            continue
        if alias.entity_id in best:
            best[alias.entity_id] = best[alias.entity_id].model_copy(
                update={"match_score": 1.0, "matched_by": OFFICIAL_ALIAS_MATCHED_BY, "query": alias.query}
            )
    ranked = sorted(best.values(), key=lambda m: m.match_score, reverse=True)
    return ProductLookupOutput(candidates=ranked[:limit])


# ---------------------------------------------------------------------------
# lookup_permit
# ---------------------------------------------------------------------------


def lookup_permit(repo: OfficialDataRepository, permit_text: str) -> ProductLookupOutput:
    """以許可證字號查產品；字號無法解析時回傳空結果。"""
    code = extract_permit_code(permit_text)
    if not code:
        return ProductLookupOutput(candidates=[])
    matches = [
        ProductCandidateMatch(
            product=_product_from_row(row), match_score=1.0, matched_by="permit_number", query=permit_text
        )
        for row in repo.get_products_by_permit_code(code)
    ]
    return ProductLookupOutput(candidates=matches)


# ---------------------------------------------------------------------------
# lookup_company
# ---------------------------------------------------------------------------


class CompanyMatch(BaseModel):
    company_id: str
    company_name: str
    match_score: float
    query: str
    matched_via: str = "official_name"  # official_name | alias:<ALIAS_TYPE>
    alias: str | None = None
    alias_confidence: float | None = None
    is_inferred: bool = False  # True：經由推測別名（例如推導品牌）連到此公司，不是官方確認
    inference_basis: dict[str, Any] = {}
    product_ids: list[str]


def lookup_company(repo: OfficialDataRepository, company_names: list[str], limit: int = 50) -> list[CompanyMatch]:
    """以公司 / 品牌名稱查官方申請商。

    先比對官方申請商名稱；再透過 entity_aliases（COMPANY 簡稱、BRAND→公司）解析。
    只有 confidence >= ALIAS_MIN_CONFIDENCE 的別名會被採用。
    """
    companies: dict[str, CompanyMatch] = {}
    for query in company_names:
        terms = search_terms(query)
        if not terms:
            continue
        for row in repo.search_products(terms, limit):
            if not row.get("company_id") or not row.get("company_name"):
                continue
            score = name_match_score(query, row["company_name"])
            if score < COMPANY_MIN_SCORE:
                continue
            cid = str(row["company_id"])
            match = companies.setdefault(
                cid,
                CompanyMatch(company_id=cid, company_name=row["company_name"], match_score=score, query=query, product_ids=[]),
            )
            match.match_score = max(match.match_score, score)
            if str(row["product_id"]) not in match.product_ids:
                match.product_ids.append(str(row["product_id"]))

    alias_company: dict[str, AliasMatch] = {}
    for alias in resolve_entity_alias(repo, company_names, [EntityType.COMPANY, EntityType.BRAND]):
        if alias.uncertain or not alias.resolved_company_id or alias.resolved_company_id in companies:
            continue
        current = alias_company.get(alias.resolved_company_id)
        if current is None or alias.confidence > current.confidence:
            alias_company[alias.resolved_company_id] = alias
    if alias_company:
        for row in repo.get_products_by_company_ids(list(alias_company), limit):
            cid = str(row["company_id"])
            alias = alias_company[cid]
            match = companies.setdefault(
                cid,
                CompanyMatch(
                    company_id=cid, company_name=row["company_name"], match_score=alias.match_score,
                    query=alias.query, matched_via=f"alias:{alias.alias_type}", alias=alias.alias,
                    alias_confidence=alias.confidence, is_inferred=alias.is_inferred,
                    inference_basis=alias.inference_basis, product_ids=[],
                ),
            )
            if str(row["product_id"]) not in match.product_ids:
                match.product_ids.append(str(row["product_id"]))
    return sorted(companies.values(), key=lambda m: m.match_score, reverse=True)


# ---------------------------------------------------------------------------
# resolve_entity_alias
# ---------------------------------------------------------------------------


class AliasMatch(BaseModel):
    query: str
    entity_type: EntityType
    entity_id: str
    alias: str
    alias_type: str
    confidence: float
    match_score: float
    resolved_company_id: str | None = None  # COMPANY 本身或 BRAND 所屬公司
    uncertain: bool  # confidence 低於門檻：只能當候選，不能自動解析
    evidence_status: str  # OFFICIAL_RECORD | INFERRED | MANUAL_REVIEWED
    is_inferred: bool  # 別名本身或其指向的品牌歸屬是推測
    inference_basis: dict[str, Any] = {}
    source: SourceRef


def _alias_source(row: dict[str, Any], inferred: bool) -> SourceRef:
    """推測的別名不是官方確認事實：即使推導所用的資料集是官方的，也標記為 derived_inference / 非官方。"""
    if inferred:
        return SourceRef(
            source_name=f"系統推測（依據：{row.get('source_name') or '官方資料'}）",
            source_type=SourceType.DERIVED_INFERENCE,
            source_url=row.get("source_url"),
            source_record_id=str(row.get("id")) if row.get("id") else None,
            is_official=False,
        )
    return SourceRef(
        source_name=row.get("source_name") or "未知來源",
        source_type=SourceType.STRUCTURED_DB,
        source_url=row.get("source_url"),
        source_record_id=row.get("source_record_key"),
        is_official=bool(row.get("source_is_official", row.get("is_official", False))),
    )


def resolve_entity_alias(
    repo: OfficialDataRepository, names: list[str], entity_types: list[EntityType], limit: int = 20
) -> list[AliasMatch]:
    """把使用者提到的名稱對應到 canonical entity。

    命中條件：別名與查詢幾乎相同（分數 >= ALIAS_MIN_MATCH_SCORE），
    或別名完整出現在查詢中（例如查詢含品牌字樣）。
    """
    matches: dict[tuple[str, str, str], AliasMatch] = {}
    for query in names:
        q_norm = normalize_name(query)
        terms = search_terms(query)
        if not terms:
            continue
        for row in repo.search_entity_aliases(terms, [t.value for t in entity_types], limit):
            alias_norm = row.get("alias_normalized") or normalize_name(row["alias"])
            score = name_match_score(query, row["alias"])
            if score < ALIAS_MIN_MATCH_SCORE:
                if len(alias_norm) >= 2 and alias_norm in q_norm:
                    score = round(len(alias_norm) / len(q_norm), 4)
                else:
                    continue
            entity_type = EntityType(row["entity_type"])
            if entity_type == EntityType.COMPANY:
                resolved_company = str(row["entity_id"])
            else:
                resolved_company = str(row["brand_company_id"]) if row.get("brand_company_id") else None
            confidence = float(row["confidence"])
            status = row.get("evidence_status") or EvidenceStatus.INFERRED.value
            inferred = status == EvidenceStatus.INFERRED.value or bool(row.get("brand_is_inferred"))
            basis = dict(row.get("inference_basis") or {})
            if row.get("brand_inference_basis"):
                basis.setdefault("brand", row["brand_inference_basis"])
            key = (query, row["entity_type"], str(row["entity_id"]))
            candidate = AliasMatch(
                query=query, entity_type=entity_type, entity_id=str(row["entity_id"]), alias=row["alias"],
                alias_type=row["alias_type"], confidence=confidence, match_score=score,
                resolved_company_id=resolved_company, uncertain=confidence < ALIAS_MIN_CONFIDENCE,
                evidence_status=status, is_inferred=inferred, inference_basis=basis,
                source=_alias_source(row, inferred),
            )
            if key not in matches or candidate.match_score > matches[key].match_score:
                matches[key] = candidate
    return sorted(matches.values(), key=lambda m: (m.uncertain, -m.match_score, -m.confidence))


# ---------------------------------------------------------------------------
# lookup_ingredient
# ---------------------------------------------------------------------------


class IngredientMatch(BaseModel):
    ingredient: IngredientRecord
    match_score: float
    query: str


def lookup_ingredient(repo: OfficialDataRepository, names: list[str], limit: int = 20) -> list[IngredientMatch]:
    found: dict[str, IngredientMatch] = {}
    for query in names:
        terms = search_terms(query)
        if not terms:
            continue
        for row in repo.search_ingredients(terms, limit):
            score = max(
                name_match_score(query, row.get("name_zh") or ""),
                name_match_score(query, row.get("name_en") or ""),
            )
            if score < INGREDIENT_MIN_SCORE:
                continue
            iid = str(row["id"])
            if iid not in found or score > found[iid].match_score:
                found[iid] = IngredientMatch(
                    ingredient=IngredientRecord(
                        ingredient_id=iid,
                        name_zh=row["name_zh"],
                        name_en=row.get("name_en"),
                        source=_source_from_row(row),
                    ),
                    match_score=score,
                    query=query,
                )
    for alias in resolve_entity_alias(repo, names, [EntityType.INGREDIENT]):
        if alias.uncertain or alias.entity_id in found or alias.match_score < ALIAS_MIN_MATCH_SCORE:
            continue
        for row in repo.search_ingredients(search_terms(alias.alias), limit):
            if str(row["id"]) == alias.entity_id:
                found[alias.entity_id] = IngredientMatch(
                    ingredient=IngredientRecord(
                        ingredient_id=alias.entity_id, name_zh=row["name_zh"], name_en=row.get("name_en"),
                        source=_source_from_row(row),
                    ),
                    match_score=alias.match_score,
                    query=alias.query,
                )
    return sorted(found.values(), key=lambda m: m.match_score, reverse=True)


# ---------------------------------------------------------------------------
# lookup_approved_claim / warnings / ingredients of product
# ---------------------------------------------------------------------------


def lookup_approved_claim(repo: OfficialDataRepository, product_ids: list[str]) -> list[ApprovedClaimEvidence]:
    if not product_ids:
        return []
    return [
        ApprovedClaimEvidence(
            evidence_id=f"approved_claim:{row['product_claim_id']}",
            product_id=str(row["product_id"]),
            approved_claim_id=str(row["approved_claim_id"]),
            function_name=row["function_name"],
            claim_text=row["claim_text"],
            function_classified=row.get("claim_kind") != CLAIM_KIND_UNCLASSIFIED,
            source=_source_from_row(row),
        )
        for row in repo.get_approved_claims(product_ids)
    ]


def lookup_product_warnings(repo: OfficialDataRepository, product_ids: list[str]) -> list[WarningEvidence]:
    if not product_ids:
        return []
    return [
        WarningEvidence(
            evidence_id=f"warning:{row['product_id']}:{row['warning_id']}",
            product_id=str(row["product_id"]),
            warning_id=str(row["warning_id"]),
            warning_type=row["warning_type"],
            text=row["text"],
            source=_source_from_row(row),
        )
        for row in repo.get_warnings(product_ids)
    ]


def lookup_product_ingredients(repo: OfficialDataRepository, product_ids: list[str]) -> list[IngredientRecord]:
    if not product_ids:
        return []
    return [
        IngredientRecord(
            ingredient_id=str(row["ingredient_id"]),
            name_zh=row["name_zh"],
            name_en=row.get("name_en"),
            source=_source_from_row(row),
        )
        for row in repo.get_ingredients_for_products(product_ids)
    ]


# ---------------------------------------------------------------------------
# search_illegal_ad_cases
# ---------------------------------------------------------------------------


def search_illegal_ad_cases(
    repo: OfficialDataRepository, product_names: list[str], company_names: list[str], limit: int = 20
) -> list[IllegalAdEvidence]:
    """查官方公布的違規廣告紀錄，只保留與查詢名稱高度相似者。"""
    results: dict[str, IllegalAdEvidence] = {}
    queries = [(n, "product_name") for n in product_names] + [(n, "company_name") for n in company_names]
    for query, field in queries:
        terms = search_terms(query)
        if not terms:
            continue
        for row in repo.search_illegal_ad_cases(terms, limit):
            target = row.get(field) or ""
            if name_match_score(query, target) < ILLEGAL_AD_MIN_SCORE:
                continue
            case_id = str(row["id"])
            results.setdefault(
                case_id,
                IllegalAdEvidence(
                    evidence_id=f"illegal_ad:{case_id}",
                    case_id=case_id,
                    product_name=row.get("product_name"),
                    company_name=row.get("company_name"),
                    violation_content=row["violation_content"],
                    violation_type=row.get("violation_type"),
                    penalty_date=str(row["penalty_date"]) if row.get("penalty_date") else None,
                    source=_source_from_row(row),
                    relevance=EvidenceRelevance(
                        relevance_score=name_match_score(query, target),
                        evidence_scope=EvidenceScope.PRODUCT_SPECIFIC,
                        rationale="違規紀錄的產品或公司名稱與查詢高度相符",
                    ),
                ),
            )
    return list(results.values())
