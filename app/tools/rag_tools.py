"""RAG 檢索 tool（OpenAI embedding + Supabase pgvector）。

RAG 只用在非結構化官方文本（法規、指引、公告）；精確欄位請用 product_tools 的 SQL 查詢。

claim-policy corpus 的 chunk（metadata 有 evidence_use）會帶出 RagPolicyMetadata：
source_id / status / review_status / evidence_use / article_or_section / effective_date / source_url。
EXCLUDED_SUPERSEDED 的 chunk 預設不回傳（current query 不使用）；CONTEXT_ONLY / CORROBORATION_REQUIRED 照常回傳，
但 RagEvidence.decision_eligible 為 False，Agent B 不得把它當成 final legal evidence。
"""

from typing import Any, Protocol

from pydantic import ValidationError

from app.schemas.enums import PolicyEvidenceUse, SourceType
from app.schemas.evidence import RagEvidence, RagPolicyMetadata, SourceRef
from app.tools.product_tools import _parse_dt


class RagStore(Protocol):
    def match_chunks(
        self, embedding: list[float], top_k: int, min_similarity: float, country_code: str = "TW"
    ) -> list[dict[str, Any]]: ...


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def policy_metadata(metadata: dict[str, Any] | None) -> RagPolicyMetadata | None:
    """chunk metadata → RagPolicyMetadata；不是 corpus chunk 回傳 None。

    metadata 標示了 evidence_use 卻無法解析（欄位缺漏 / 值不合法）時一律視為 CONTEXT_ONLY，不讓它成為決策證據。
    """
    if not isinstance(metadata, dict) or "evidence_use" not in metadata:
        return None
    fields = {
        "source_id": metadata.get("source_id") or "", "status": metadata.get("status") or "NEEDS_REVIEW",
        "review_status": metadata.get("review_status"), "article_or_section": metadata.get("article_or_section"),
        "effective_date": metadata.get("effective_date"), "source_url": metadata.get("source_url"),
        "extraction_method": metadata.get("extraction_method"), "corpus_version": metadata.get("corpus_version"),
    }
    try:
        return RagPolicyMetadata(evidence_use=metadata["evidence_use"], **fields)
    except ValidationError:
        return RagPolicyMetadata(evidence_use=PolicyEvidenceUse.CONTEXT_ONLY, **fields)


def search_rag(
    store: RagStore,
    embedder: Embedder,
    query: str,
    top_k: int = 5,
    min_similarity: float = 0.3,
    country_code: str = "TW",
    include_superseded: bool = False,
) -> list[RagEvidence]:
    query = (query or "").strip()
    if not query:
        return []
    [embedding] = embedder.embed([query])
    rows = store.match_chunks(embedding, top_k, min_similarity, country_code)
    out = []
    for row in rows:
        meta = policy_metadata(row.get("chunk_metadata"))
        if meta is not None and meta.evidence_use == PolicyEvidenceUse.EXCLUDED_SUPERSEDED and not include_superseded:
            continue
        out.append(_rag_evidence(row, meta))
    return out


def _rag_evidence(row: dict[str, Any], meta: RagPolicyMetadata | None) -> RagEvidence:
    return RagEvidence(
        evidence_id=f"rag:{row['chunk_id']}",
        document_id=str(row["document_id"]),
        chunk_id=str(row["chunk_id"]),
        title=row["title"],
        content=row["content"],
        similarity=float(row["similarity"]),
        document_type=row.get("document_type"),
        source=SourceRef(
            source_name=row.get("source_name") or row["title"],
            source_type=SourceType.RAG_DOCUMENT,
            source_url=row.get("source_url"),
            source_record_id=str(row["chunk_id"]),
            retrieved_at=_parse_dt(row.get("retrieved_at")),
            is_official=bool(row.get("is_official")),
        ),
        policy_metadata=meta,
    )
