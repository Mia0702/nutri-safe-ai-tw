"""RAG 檢索 tool（OpenAI embedding + Supabase pgvector）。

RAG 只用在非結構化官方文本（法規、指引、公告）；精確欄位請用 product_tools 的 SQL 查詢。
"""

from typing import Any, Protocol

from app.schemas.enums import SourceType
from app.schemas.evidence import RagEvidence, SourceRef
from app.tools.product_tools import _parse_dt


class RagStore(Protocol):
    def match_chunks(
        self, embedding: list[float], top_k: int, min_similarity: float, country_code: str = "TW"
    ) -> list[dict[str, Any]]: ...


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def search_rag(
    store: RagStore,
    embedder: Embedder,
    query: str,
    top_k: int = 5,
    min_similarity: float = 0.3,
    country_code: str = "TW",
) -> list[RagEvidence]:
    query = (query or "").strip()
    if not query:
        return []
    [embedding] = embedder.embed([query])
    rows = store.match_chunks(embedding, top_k, min_similarity, country_code)
    return [
        RagEvidence(
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
        )
        for row in rows
    ]
