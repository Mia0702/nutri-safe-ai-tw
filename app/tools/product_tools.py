"""官方結構化資料查詢 tools（Supabase）。

Tool 只回傳結構化結果與比對分數；VERIFIED / PARTIAL_MATCH 等判定由
app.risk.assessment_rules.resolve_product_match 依固定規則決定。
"""

import time
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from app.brand_assist import (
    MAX_APPLICANT_ROWS,
    MAX_BRAND_CANDIDATES,
    MIN_REMAINDER_LEN,
    BrandAssistReport,
    ReviewedBrandOverlay,
    configured_overlay,
    is_brand_member,
    supports_remainder,
)
from app.english_name import is_single_token, is_usable_english_name, match_english_name
from app.normalization import extract_permit_code, name_match_score, normalize_name, search_terms
from app.product_coverage import category_of
from app.services.errors import ServiceError
from app.schemas.enums import EntityType, EvidenceScope, EvidenceStatus, ProductStatus, SourceType
from app.schemas.evidence import (
    CLAIM_KIND_UNCLASSIFIED,
    OFFICIAL_ALIAS_MATCHED_BY,
    OFFICIAL_ENGLISH_SINGLE_TOKEN_MATCHED_BY,
    REVIEWED_BRAND_CANDIDATE_MATCHED_BY,
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
        display_url=row.get("display_url"),
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
        product_category=category_of(row.get("product_category")),
        expiry_date=str(row["expiry_date"])[:10] if row.get("expiry_date") else None,
    )


# ---------------------------------------------------------------------------
# lookup_product
# ---------------------------------------------------------------------------


class ProductLookupOutput(BaseModel):
    candidates: list[ProductCandidateMatch]

    @property
    def result_count(self) -> int:
        return len(self.candidates)


_OVERLAY_FROM_SETTINGS: Any = object()  # sentinel：依設定（BRAND_ASSIST_OVERLAY_DIR）取得 overlay；測試 / eval 可明確傳入


def lookup_product(
    repo: OfficialDataRepository,
    product_names: list[str],
    brand_names: list[str] | None = None,
    limit: int = 20,
    brand_overlay: ReviewedBrandOverlay | None = _OVERLAY_FROM_SETTINGS,
    brand_report: BrandAssistReport | None = None,
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
    # 官方英文品名（product_name_en）完全相同：同一筆官方紀錄的品名欄位，不是別名；只做 exact（見 lookup_official_english_name）
    for name in product_names:
        for match in lookup_official_english_name(repo, name):
            current = best.get(match.product.product_id)
            if current is None or current.match_score < match.match_score:
                best[match.product.product_id] = match
    # D5：已審核品牌只補「候選」標記：既有路徑已找到的產品保留原本的 matched_by / 分數（只多一個 reviewed_brand_candidate 標記，
    # resolver 既有步驟不看它）；其餘產品以 matched_by = REVIEWED_BRAND_CANDIDATE_MATCHED_BY、分數 < 0.5 加入
    for match in lookup_reviewed_brand_candidates(repo, product_names, brand_names or [], brand_overlay, brand_report):
        current = best.get(match.product.product_id)
        best[match.product.product_id] = (current.model_copy(update={"reviewed_brand_candidate": True})
                                          if current is not None else match)
    ranked = sorted(best.values(), key=lambda m: m.match_score, reverse=True)
    return ProductLookupOutput(candidates=_truncate_keeping_brand_candidates(ranked, limit))


def _truncate_keeping_brand_candidates(ranked: list[ProductCandidateMatch], limit: int) -> list[ProductCandidateMatch]:
    """最後截斷：一般結果與 D5 之前完全相同（只看非「僅品牌」的列，取前 limit 筆）；
    被截掉的品牌輔助候選（最多 MAX_BRAND_CANDIDATES 筆）改標成「僅品牌候選」補在後面。

    * 一般結果的集合、順序、分數完全不變 → 既有 resolver 步驟（含名稱相似）的輸入與 D5 之前相同。
    * 補上的品牌候選：matched_by = REVIEWED_BRAND_CANDIDATE_MATCHED_BY、分數上限 0.49，resolver 的一般步驟不會看到它們，
      只有在既有結果沒有產品身份時才會用來列候選（永遠需使用者確認）；不提高身份信心。
    * 沒有品牌候選時結果與 ranked[:limit] 完全相同；總數 <= limit + MAX_BRAND_CANDIDATES，不重複。
    """
    regular = [m for m in ranked if m.matched_by != REVIEWED_BRAND_CANDIDATE_MATCHED_BY][:limit]
    kept = {m.product.product_id for m in regular}
    extras = [m.model_copy(update={"matched_by": REVIEWED_BRAND_CANDIDATE_MATCHED_BY,
                                   "match_score": min(m.match_score, 0.49)})
              for m in ranked if m.reviewed_brand_candidate and m.product.product_id not in kept]
    # 一般列在 limit 內已保留的品牌候選不重複計入；其餘（含原本就是「僅品牌」的列）最多補 MAX_BRAND_CANDIDATES 筆
    in_regular = sum(1 for m in regular if m.reviewed_brand_candidate)
    extras = extras[:max(0, MAX_BRAND_CANDIDATES - in_regular)]
    return sorted(regular + extras, key=lambda m: m.match_score, reverse=True) if extras else regular


def lookup_reviewed_brand_candidates(
    repo: Any, product_names: list[str], brand_names: list[str],
    overlay: ReviewedBrandOverlay | None = _OVERLAY_FROM_SETTINGS, report: BrandAssistReport | None = None,
) -> list[ProductCandidateMatch]:
    """查詢文字中的「已審核品牌（人工 ACCEPT）」→ 同一申請商、品名屬於該品牌證據的官方產品候選。

    * 品牌只縮小候選範圍：回傳的每一筆都標記 reviewed_brand_candidate、分數 < PARTIAL_MIN（0.5），
      resolver 只會把它們列為候選請使用者確認，不會自動確認（沒有品牌所有權、沒有 OWNS_BRAND）。
    * 品牌 + 產品文字：只留下品名（中文或英文）包含全部剩餘文字的成員；沒有任何成員支持 → 不回傳（維持既有行為）。
    * overlay 停用 / repo 不支援 / 候選集合看不完 / 發生例外 → 空（fail closed，既有產品查證不受影響）。
    """
    started = time.perf_counter()
    try:
        active = configured_overlay() if overlay is _OVERLAY_FROM_SETTINGS else overlay
        search = getattr(repo, "search_products_by_applicant", None)
        if active is None or not active.active or search is None:
            return []
        texts = list(dict.fromkeys(
            [n for n in product_names if n]
            + [f"{b} {n}" for b in brand_names if b for n in product_names if n]
            + ([b for b in brand_names if b] if not product_names else [])))
        out: dict[str, ProductCandidateMatch] = {}
        for text in texts:
            mention = active.find_mention(text)
            if mention is None or (not mention.brand_only and len(mention.remainder.replace(" ", "")) < MIN_REMAINDER_LEN):
                continue
            rows, complete = search(mention.brand.applicant, MAX_APPLICANT_ROWS)
            if not complete:
                continue
            members = [r for r in rows if is_brand_member(mention.brand, r)]
            if report is not None:
                report.mentioned, report.brand_only, report.member_count = True, mention.brand_only, len(members)
            for row in members:
                if not mention.brand_only and not supports_remainder(mention.remainder, row):
                    continue
                product = _product_from_row(row)
                rank = 0.0 if mention.brand_only else name_match_score(mention.remainder, product.product_name_zh)
                out.setdefault(product.product_id, ProductCandidateMatch(
                    product=product, match_score=round(0.1 + 0.3 * rank, 4),
                    matched_by=REVIEWED_BRAND_CANDIDATE_MATCHED_BY, query=text, reviewed_brand_candidate=True))
        ranked = sorted(out.values(), key=lambda m: (-m.match_score, m.product.product_name_zh, m.product.product_id))
        if report is not None:
            report.candidate_count = min(len(ranked), MAX_BRAND_CANDIDATES)
        return ranked[:MAX_BRAND_CANDIDATES]
    except Exception:  # noqa: BLE001 - 品牌輔助失敗不得影響既有查證
        return []
    finally:
        if report is not None:
            report.lookup_ms = round((time.perf_counter() - started) * 1000, 3)


def lookup_official_english_name(repo: Any, name: str) -> list[ProductCandidateMatch]:
    """以官方英文品名精確取回產品；回傳的每一筆都是 exact（score 1.0）。

    * 只比對 conservative key（NFKC / casefold / 空白折疊）；沒有相似度、子字串、標點移除、翻譯。
    * 碰撞：conservative key 或既有 runtime key（normalize_name，會移除標點 / 空白）對到 2 個以上產品 → 全部回傳，
      resolver 會把它們視為同名多筆（MULTIPLE_EXACT_NAMES）請使用者選，不挑其中一個。
    * 只有唯一、官方來源的列會是單一結果；resolver 對它的處理與官方品名 exact 相同。
    * 看不完所有可能的碰撞對象（資料庫縮小範圍過大）、任一命中列不是官方來源、repo 不支援英文取回 → 不回傳（行為同沒有此路徑）。
    """
    search = getattr(repo, "search_products_by_english_name", None)
    if search is None or not is_usable_english_name(name):
        return []
    rows, complete = search(name)
    if not complete or not rows:
        return []
    matches = match_english_name(name, rows)
    ids = set(matches.collision_ids)
    hit_rows = [r for r in rows if str(r.get("product_id")) in ids]
    if not matches.exact_ids or not hit_rows or any(not r.get("is_official") for r in hit_rows):
        return []
    # 唯一但單一詞的官方英文品名：標成「需確認」，resolver 只會給單一候選，不自動確認
    matched_by = (OFFICIAL_ENGLISH_SINGLE_TOKEN_MATCHED_BY if len(hit_rows) == 1 and is_single_token(name)
                  else OFFICIAL_ALIAS_MATCHED_BY)
    return [ProductCandidateMatch(product=_product_from_row(r), match_score=1.0,
                                  matched_by=matched_by, query=name) for r in hit_rows]


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


def identity_generic_terms(repo: Any) -> list[str]:
    """Candidate Relevance Gate 用的一般成分用語（官方成分 / ingredient entity 名稱）。
    取不到時回空清單：gate 仍有固定的一般用語規則，查詢不因此失敗。"""
    getter = getattr(repo, "get_identity_generic_terms", None)
    if getter is None:
        return []
    try:
        return list(getter())
    except ServiceError:
        return []


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
