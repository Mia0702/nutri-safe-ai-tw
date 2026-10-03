"""知識圖譜查詢 tool（Neo4j）。

只提供固定、參數化的查詢類型，不讓 LLM 自行產生 Cypher。
"""

from enum import StrEnum
from typing import Any, Protocol

from app.schemas.enums import EvidenceScope, SourceType
from app.schemas.evidence import PRODUCT_RECORD_RELEVANCE, EvidenceRelevance, GraphEvidence, SourceRef

# 成分層級的關聯（含此成分的其他產品的核准功效）只能當間接參考
INGREDIENT_LEVEL_RELEVANCE = EvidenceRelevance(
    relevance_score=0.7,
    evidence_scope=EvidenceScope.INGREDIENT_LEVEL,
    rationale="含相同成分的健康食品曾獲核准的功效，不是特定產品的核准內容",
)


class GraphQueryType(StrEnum):
    PRODUCT_INGREDIENTS = "product_ingredients"  # Product → Ingredient
    PRODUCT_APPROVED_CLAIMS = "product_approved_claims"  # Product → Approved Claim
    INGREDIENT_APPROVED_FUNCTIONS = "ingredient_approved_functions"  # Ingredient ← Product → Claim


class GraphRepository(Protocol):
    def product_ingredients(self, product_ids: list[str]) -> list[dict[str, Any]]: ...

    def product_approved_claims(self, product_ids: list[str]) -> list[dict[str, Any]]: ...

    def ingredient_approved_functions(self, ingredient_ids: list[str], limit: int) -> list[dict[str, Any]]: ...


class Neo4jGraphRepository:
    def __init__(self, neo4j_service):
        self._neo4j = neo4j_service

    def product_ingredients(self, product_ids: list[str]) -> list[dict[str, Any]]:
        return self._neo4j.read(
            """
            MATCH (p:Product)-[r:CONTAINS]->(i:Ingredient)
            WHERE p.product_id IN $ids
            OPTIONAL MATCH (p)-[s:SUPPORTED_BY]->(src:Source)
            RETURN p.product_id AS product_id, p.name_zh AS product_name,
                   i.ingredient_id AS ingredient_id, i.name_zh AS ingredient_name,
                   r.raw_text AS raw_text,
                   src.source_name AS source_name, src.is_official AS is_official, s.source_url AS source_url
            """,
            {"ids": product_ids},
        )

    def product_approved_claims(self, product_ids: list[str]) -> list[dict[str, Any]]:
        return self._neo4j.read(
            """
            MATCH (p:Product)-[r:HAS_APPROVED_CLAIM]->(c:Claim)
            WHERE p.product_id IN $ids
            OPTIONAL MATCH (p)-[s:SUPPORTED_BY]->(src:Source)
            RETURN p.product_id AS product_id, p.name_zh AS product_name,
                   c.claim_id AS claim_id, c.function_name AS function_name, r.claim_text AS claim_text,
                   src.source_name AS source_name, src.is_official AS is_official, s.source_url AS source_url
            """,
            {"ids": product_ids},
        )

    def ingredient_approved_functions(self, ingredient_ids: list[str], limit: int) -> list[dict[str, Any]]:
        return self._neo4j.read(
            """
            MATCH (i:Ingredient)<-[:CONTAINS]-(p:Product)-[:HAS_APPROVED_CLAIM]->(c:Claim)
            WHERE i.ingredient_id IN $ids
            OPTIONAL MATCH (c)-[:SUPPORTED_BY]->(src:Source)
            WITH i, c, src, count(DISTINCT p) AS product_count, collect(DISTINCT p.name_zh)[0..3] AS sample_products
            RETURN i.ingredient_id AS ingredient_id, i.name_zh AS ingredient_name,
                   c.claim_id AS claim_id, c.function_name AS function_name,
                   product_count, sample_products,
                   src.source_name AS source_name, src.is_official AS is_official
            ORDER BY product_count DESC
            LIMIT $limit
            """,
            {"ids": ingredient_ids, "limit": limit},
        )


def _graph_source(row: dict[str, Any]) -> SourceRef:
    return SourceRef(
        source_name=row.get("source_name") or "呷安心知識圖譜",
        source_type=SourceType.KNOWLEDGE_GRAPH,
        source_url=row.get("source_url"),
        is_official=bool(row.get("is_official")),
    )


def query_knowledge_graph(
    repo: GraphRepository, query_type: GraphQueryType, ids: list[str], limit: int = 20
) -> list[GraphEvidence]:
    if not ids:
        return []
    evidence: list[GraphEvidence] = []
    if query_type == GraphQueryType.PRODUCT_INGREDIENTS:
        for row in repo.product_ingredients(ids):
            evidence.append(
                GraphEvidence(
                    evidence_id=f"graph:contains:{row['product_id']}:{row['ingredient_id']}",
                    query_type=query_type,
                    subject_id=row["product_id"],
                    subject_name=row["product_name"],
                    relation="CONTAINS",
                    object_id=row["ingredient_id"],
                    object_name=row["ingredient_name"],
                    properties={"raw_text": row.get("raw_text")},
                    source=_graph_source(row),
                    relevance=PRODUCT_RECORD_RELEVANCE,
                )
            )
    elif query_type == GraphQueryType.PRODUCT_APPROVED_CLAIMS:
        for row in repo.product_approved_claims(ids):
            evidence.append(
                GraphEvidence(
                    evidence_id=f"graph:approved_claim:{row['product_id']}:{row['claim_id']}",
                    query_type=query_type,
                    subject_id=row["product_id"],
                    subject_name=row["product_name"],
                    relation="HAS_APPROVED_CLAIM",
                    object_id=row["claim_id"],
                    object_name=row["function_name"],
                    properties={"claim_text": row.get("claim_text")},
                    source=_graph_source(row),
                    relevance=PRODUCT_RECORD_RELEVANCE,
                )
            )
    elif query_type == GraphQueryType.INGREDIENT_APPROVED_FUNCTIONS:
        for row in repo.ingredient_approved_functions(ids, limit):
            evidence.append(
                GraphEvidence(
                    evidence_id=f"graph:ingredient_function:{row['ingredient_id']}:{row['claim_id']}",
                    query_type=query_type,
                    subject_id=row["ingredient_id"],
                    subject_name=row["ingredient_name"],
                    relation="PRODUCTS_WITH_INGREDIENT_HAVE_APPROVED_CLAIM",
                    object_id=row["claim_id"],
                    object_name=row["function_name"],
                    properties={
                        "product_count": row.get("product_count"),
                        "sample_products": row.get("sample_products") or [],
                    },
                    source=_graph_source(row),
                    relevance=INGREDIENT_LEVEL_RELEVANCE,
                )
            )
    return evidence
