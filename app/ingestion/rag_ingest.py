"""把台灣官方文件（只允許官方網域）切塊、embedding 後寫入 rag_documents / rag_chunks。"""

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel

from app.config.project import USER_AGENT
from app.tools.html_text import html_to_text
from app.tools.official_web_tools import TW_OFFICIAL_DOMAINS, is_allowed_official_url

MAX_CHUNK_CHARS = 700
OVERLAP_CHARS = 100


class FetchedDocument(BaseModel):
    url: str
    title: str
    text: str
    retrieved_at: datetime


def fetch_official_document(url: str, timeout: float = 30.0) -> FetchedDocument:
    if not is_allowed_official_url(url, TW_OFFICIAL_DOMAINS):
        raise ValueError(f"非允許的台灣官方網域：{url}")
    resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    if not is_allowed_official_url(str(resp.url), TW_OFFICIAL_DOMAINS):
        raise ValueError(f"重新導向到非官方網域：{resp.url}")
    title, text = html_to_text(resp.text)
    return FetchedDocument(url=str(resp.url), title=title or url, text=text, retrieved_at=datetime.now(timezone.utc))


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """依段落累積到 max_chars；單一過長段落以固定長度切開並保留 overlap。"""
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        while len(para) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:max_chars])
            para = para[max_chars - overlap :]
        if len(buf) + len(para) + 1 > max_chars and buf:
            chunks.append(buf)
            buf = buf[-overlap:] + "\n" + para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def normalize_url(url: str) -> str:
    """網址正規化：scheme / host 小寫、去 fragment、去結尾斜線、query 參數排序。"""
    parts = urlsplit(url.strip())
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def content_fingerprint(text: str) -> str:
    """正規化全文（NFKC、合併空白）的 sha256；排版差異不會被當成不同文件。"""
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def ingest_document(
    service, embedder, doc: FetchedDocument, *, source_name: str, document_type: str,
    embedding_model: str, published_at: str | None = None, source_id: str | None = None,
) -> dict[str, Any]:
    """Idempotent 匯入官方文件。

    * 相同內容（content_hash）已存在：不重新匯入、不重新 embedding（除非先前沒有 chunk）。
    * 同一網址的新版本：舊版 is_current 改為 false，再寫入新版本。
    """
    content_hash = content_fingerprint(doc.text)
    url_norm = normalize_url(doc.url) if doc.url else None
    existing = service.select(
        service.table("rag_documents").select("id").eq("content_hash", content_hash).limit(1)
    )
    if existing:
        document_id = existing[0]["id"]
        has_chunks = service.select(
            service.table("rag_chunks").select("id").eq("document_id", document_id).limit(1)
        )
        if has_chunks:
            return {"document_id": document_id, "chunks": 0, "skipped": "相同內容已匯入"}
    else:
        if url_norm:
            service.update("rag_documents", {"is_current": False},
                           {"source_url_normalized": url_norm, "is_current": True})
        [saved] = service.insert(
            "rag_documents",
            [{
                "title": doc.title, "source_url": doc.url, "source_url_normalized": url_norm,
                "source_name": source_name, "source_id": source_id,
                "document_type": document_type, "country_code": "TW", "is_official": True, "is_current": True,
                "published_at": published_at, "retrieved_at": doc.retrieved_at.isoformat(),
                "content_hash": content_hash, "metadata": {"char_count": len(doc.text)},
            }],
        )
        document_id = saved["id"]
    chunks = chunk_text(doc.text)
    vectors = embedder.embed(chunks)
    rows = [
        {"document_id": document_id, "chunk_index": i, "content": c, "embedding": v,
         "embedding_model": embedding_model, "metadata": {"source_url": doc.url}}
        for i, (c, v) in enumerate(zip(chunks, vectors))
    ]
    service.upsert("rag_chunks", rows, on_conflict="document_id,chunk_index")
    return {"document_id": document_id, "chunks": len(rows)}
