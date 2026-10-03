"""TFDA Claim Policy Corpus data layer：載入 → 驗證 → quarantine → 匯入計畫 → 寫入 Supabase / RAG / Neo4j。

* 只處理已驗證的結構化 corpus 檔案（sources / rules / examples / health function catalog / documents.jsonl /
  raw manifest / open-data inventory）。不下載違規廣告案例；open-data inventory 只保存來源 metadata。
* 不解讀條文內容、不寫死任何法規句子：quarantine 只看 corpus 的 status / review_status / policy_effect /
  extraction_method 欄位。
* Idempotent / version-aware：每筆以 corpus 原始 ID 為自然鍵、uuid5 為 canonical id、content_hash 判斷是否變更；
  相同 corpus 重跑全部 unchanged；比資料庫已匯入版本舊的 corpus 預設拒絕匯入。
* RAG 沿用既有 rag_documents / rag_chunks：一個（來源, 網址）= 一個 rag_document，每個 corpus 區段 = 1..N 個 chunk，
  區段 metadata 保存在 chunk metadata。SUPERSEDED → is_current = false（既有 match_rag_chunks 只檢索 current）。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit

from app.ingestion.rag_ingest import MAX_CHUNK_CHARS, chunk_text, content_fingerprint, normalize_url
from app.normalization import normalize_name, stable_id
from app.schemas.evidence import CLAIM_KIND_UNCLASSIFIED

CORPUS_NAME = "tfda_claim_policy"
SOURCE_KEY_PREFIX = f"{CORPUS_NAME}:"
EMBED_BATCH = 100
WRITE_BATCH = 200
NEO4J_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "migrations" / "neo4j" / "0002_claim_policy_constraints.cypher"

FILES = {
    "sources": "tfda_claim_policy_sources.csv",
    "rules": "tfda_claim_policy_rules.csv",
    "examples": "tfda_claim_policy_examples.csv",
    "functions": "tfda_health_function_catalog.csv",
    "documents": "tfda_claim_policy_documents.jsonl",
    "manifest": "tfda_claim_policy_raw_manifest.csv",
    "inventory": "tfda_claim_open_data_inventory.csv",
    "validation": "tfda_claim_policy_validation.json",
}
# validation.json counts 與實際檔案列數的對應
EXPECTED_COUNT_KEYS = {"sources": "sources", "rules": "rules", "examples": "examples",
                       "functions": "health_functions", "documents": "documents"}


# ---------------------------------------------------------------------------- enums（與 migration 0003 CHECK 一致）


class EvidenceUse(StrEnum):
    DECISION_ELIGIBLE = "DECISION_ELIGIBLE"
    CORROBORATION_REQUIRED = "CORROBORATION_REQUIRED"  # 可檢索，不得作唯一決策證據
    CONTEXT_ONLY = "CONTEXT_ONLY"  # 可保存與檢索，不得作 deterministic final legal conclusion
    EXCLUDED_SUPERSEDED = "EXCLUDED_SUPERSEDED"  # 保存，但預設不參與 current decision / retrieval


class RecordStatus(StrEnum):
    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class BatchStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReviewStatus(StrEnum):
    SOURCE_TEXT_VERIFIED = "SOURCE_TEXT_VERIFIED"
    SOURCE_VERIFIED_CONTEXT_REQUIRED = "SOURCE_VERIFIED_CONTEXT_REQUIRED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class PolicyEffect(StrEnum):
    PERMITTED = "PERMITTED"
    RESTRICTED = "RESTRICTED"
    PROHIBITED = "PROHIBITED"
    REQUIRES_EVIDENCE = "REQUIRES_EVIDENCE"
    PRODUCT_SPECIFIC_ONLY = "PRODUCT_SPECIFIC_ONLY"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ExampleType(StrEnum):
    PERMITTED_EXAMPLE = "PERMITTED_EXAMPLE"
    GENERAL_FUNCTION_EXAMPLE = "GENERAL_FUNCTION_EXAMPLE"
    CONDITIONAL_EXAMPLE = "CONDITIONAL_EXAMPLE"
    MEDICAL_EFFICACY_EXAMPLE = "MEDICAL_EFFICACY_EXAMPLE"
    PROHIBITED_EXAMPLE = "PROHIBITED_EXAMPLE"


class DateReviewStatus(StrEnum):
    VERIFIED = "VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NOT_APPLICABLE_DATASET = "NOT_APPLICABLE_DATASET"


class ExtractionMethod(StrEnum):
    HTML_LAW_ARTICLE = "HTML_LAW_ARTICLE"
    HTML_TEXT = "HTML_TEXT"
    PDF_TEXT = "PDF_TEXT"
    MANUAL_VERIFIED_PDF_PAGE = "MANUAL_VERIFIED_PDF_PAGE"
    OFFICIAL_WEB_METADATA = "OFFICIAL_WEB_METADATA"
    OCR_UNVERIFIED = "OCR_UNVERIFIED"
    PDF_TEXT_LEGACY_UNCONFIRMED = "PDF_TEXT_LEGACY_UNCONFIRMED"


# 未逐字校對的擷取方式：不得作唯一決策證據
UNVERIFIED_EXTRACTION = {ExtractionMethod.OCR_UNVERIFIED, ExtractionMethod.PDF_TEXT_LEGACY_UNCONFIRMED}


class QuarantineReason(StrEnum):
    SUPERSEDED = "SUPERSEDED"
    SOURCE_SUPERSEDED = "SOURCE_SUPERSEDED"
    RULE_SUPERSEDED = "RULE_SUPERSEDED"
    STATUS_NEEDS_REVIEW = "STATUS_NEEDS_REVIEW"
    SOURCE_NEEDS_REVIEW = "SOURCE_NEEDS_REVIEW"
    REVIEW_STATUS_NEEDS_REVIEW = "REVIEW_STATUS_NEEDS_REVIEW"
    POLICY_EFFECT_NEEDS_REVIEW = "POLICY_EFFECT_NEEDS_REVIEW"
    RULE_NOT_DECISION_ELIGIBLE = "RULE_NOT_DECISION_ELIGIBLE"
    UNVERIFIED_EXTRACTION = "UNVERIFIED_EXTRACTION"


_REASON_LEVEL = {
    QuarantineReason.SUPERSEDED: EvidenceUse.EXCLUDED_SUPERSEDED,
    QuarantineReason.SOURCE_SUPERSEDED: EvidenceUse.EXCLUDED_SUPERSEDED,
    QuarantineReason.RULE_SUPERSEDED: EvidenceUse.EXCLUDED_SUPERSEDED,
    QuarantineReason.STATUS_NEEDS_REVIEW: EvidenceUse.CONTEXT_ONLY,
    QuarantineReason.SOURCE_NEEDS_REVIEW: EvidenceUse.CONTEXT_ONLY,
    QuarantineReason.REVIEW_STATUS_NEEDS_REVIEW: EvidenceUse.CONTEXT_ONLY,
    QuarantineReason.POLICY_EFFECT_NEEDS_REVIEW: EvidenceUse.CONTEXT_ONLY,
    QuarantineReason.RULE_NOT_DECISION_ELIGIBLE: EvidenceUse.CONTEXT_ONLY,
    QuarantineReason.UNVERIFIED_EXTRACTION: EvidenceUse.CORROBORATION_REQUIRED,
}
_USE_ORDER = [EvidenceUse.DECISION_ELIGIBLE, EvidenceUse.CORROBORATION_REQUIRED,
              EvidenceUse.CONTEXT_ONLY, EvidenceUse.EXCLUDED_SUPERSEDED]

# corpus 原始文件分類 → rag_documents.document_type（0001 CHECK）。未知分類一律驗證失敗，不猜。
RAG_DOCUMENT_TYPE = {
    "法律": "regulation", "法規命令": "regulation", "行政規則": "regulation", "法規附件": "regulation",
    "歷史法規": "regulation",
    "保健功效評估方法": "guideline", "歷史評估方法": "guideline", "行政指導": "guideline",
    "使用規範": "guideline", "申請程序": "guideline",
    "公告": "announcement", "歷史公告": "announcement", "廢止公告": "announcement", "勘誤": "announcement",
    "官方QA": "faq",
    "官方資料集metadata": "other", "官方案例新聞": "other", "官方說明新聞": "other", "官方歷史彙編": "other",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------- quarantine（純函式）


def status_reasons(status: str, *, of_source: bool = False) -> list[QuarantineReason]:
    if status == RecordStatus.SUPERSEDED:
        return [QuarantineReason.SOURCE_SUPERSEDED if of_source else QuarantineReason.SUPERSEDED]
    if status == RecordStatus.NEEDS_REVIEW:
        return [QuarantineReason.SOURCE_NEEDS_REVIEW if of_source else QuarantineReason.STATUS_NEEDS_REVIEW]
    return []


def decide_evidence_use(reasons: Iterable[str]) -> EvidenceUse:
    """最嚴格的原因決定 evidence_use；沒有原因才是 DECISION_ELIGIBLE。"""
    levels = [_REASON_LEVEL[QuarantineReason(r)] for r in reasons]
    return max(levels, key=_USE_ORDER.index, default=EvidenceUse.DECISION_ELIGIBLE)


def _uniq(reasons: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(r) for r in reasons))


def source_quarantine(source: dict[str, Any]) -> list[str]:
    return _uniq(status_reasons(source["status"]))


def rule_quarantine(rule: dict[str, Any], source: dict[str, Any]) -> list[str]:
    reasons: list[QuarantineReason] = [*status_reasons(rule["status"]), *status_reasons(source["status"], of_source=True)]
    if rule["review_status"] == ReviewStatus.NEEDS_REVIEW:
        reasons.append(QuarantineReason.REVIEW_STATUS_NEEDS_REVIEW)
    if rule["policy_effect"] == PolicyEffect.NEEDS_REVIEW:
        reasons.append(QuarantineReason.POLICY_EFFECT_NEEDS_REVIEW)
    return _uniq(reasons)


def example_quarantine(rule_reasons: list[str], example_source: dict[str, Any]) -> list[str]:
    """例示不能比所屬規則更可信：規則被取代 → 例示排除；規則不可作決策 → 例示只能當脈絡。"""
    rule_use = decide_evidence_use(rule_reasons)
    reasons: list[QuarantineReason] = status_reasons(example_source["status"], of_source=True)
    if rule_use == EvidenceUse.EXCLUDED_SUPERSEDED:
        reasons.append(QuarantineReason.RULE_SUPERSEDED)
    elif rule_use != EvidenceUse.DECISION_ELIGIBLE:
        reasons.append(QuarantineReason.RULE_NOT_DECISION_ELIGIBLE)
    return _uniq(reasons)


def function_quarantine(func: dict[str, Any], catalog_source: dict[str, Any], evaluation_source: dict[str, Any]) -> list[str]:
    reasons: list[QuarantineReason] = [
        *status_reasons(func["status"]),
        *status_reasons(catalog_source["status"], of_source=True),
        *status_reasons(evaluation_source["status"], of_source=True),
    ]
    if func["review_status"] == ReviewStatus.NEEDS_REVIEW:
        reasons.append(QuarantineReason.REVIEW_STATUS_NEEDS_REVIEW)
    return _uniq(reasons)


def section_quarantine(section: dict[str, Any]) -> list[str]:
    reasons: list[QuarantineReason] = list(status_reasons(section["status"]))
    if section["review_status"] == ReviewStatus.NEEDS_REVIEW:
        reasons.append(QuarantineReason.REVIEW_STATUS_NEEDS_REVIEW)
    if section["extraction_method"] in UNVERIFIED_EXTRACTION:
        reasons.append(QuarantineReason.UNVERIFIED_EXTRACTION)
    return _uniq(reasons)


# ---------------------------------------------------------------------------- 載入


@dataclass
class Corpus:
    directory: Path
    sources: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    examples: list[dict[str, Any]]
    functions: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    manifest: list[dict[str, Any]]
    inventory: list[dict[str, Any]]
    validation: dict[str, Any]
    file_hashes: dict[str, str]

    @property
    def corpus_version(self) -> str:
        return str(self.validation.get("research_reference_date") or "")

    @property
    def batch_key(self) -> str:
        joined = "\n".join(f"{name}:{digest}" for name, digest in sorted(self.file_hashes.items()))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def load_corpus(directory: str | Path) -> Corpus:
    d = Path(directory)
    missing = [name for name in FILES.values() if not (d / name).is_file()]
    if missing:
        raise FileNotFoundError(f"corpus 目錄 {d} 缺少檔案：{', '.join(missing)}")
    file_hashes = {name: hashlib.sha256((d / name).read_bytes()).hexdigest() for name in FILES.values()}
    documents = [json.loads(line) for line in (d / FILES["documents"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    return Corpus(
        directory=d,
        sources=_read_csv(d / FILES["sources"]),
        rules=_read_csv(d / FILES["rules"]),
        examples=_read_csv(d / FILES["examples"]),
        functions=_read_csv(d / FILES["functions"]),
        documents=documents,
        manifest=_read_csv(d / FILES["manifest"]),
        inventory=_read_csv(d / FILES["inventory"]),
        validation=json.loads((d / FILES["validation"]).read_text(encoding="utf-8")),
        file_hashes=file_hashes,
    )


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError(f"預期 JSON array：{value!r}")
    return parsed


def _date_or_none(value: Any) -> str | None:
    value = (value or "").strip()
    return value or None


def _text_or_none(value: Any) -> str | None:
    value = (value or "").strip() if isinstance(value, str) else value
    return value or None


# ---------------------------------------------------------------------------- 驗證（fail closed）


def _check_unique(rows: list[dict], key: str, label: str, errors: list[str]) -> None:
    dup = [k for k, n in Counter(r.get(key) for r in rows).items() if n > 1]
    if dup:
        errors.append(f"{label}.{key} 重複：{dup[:5]}")
    if any(not r.get(key) for r in rows):
        errors.append(f"{label}.{key} 有空值")


def _check_enum(rows: list[dict], key: str, enum: type[StrEnum], label: str, errors: list[str]) -> None:
    allowed = {e.value for e in enum}
    bad = Counter(r.get(key) for r in rows if r.get(key) not in allowed)
    if bad:
        errors.append(f"{label}.{key} 不在 enum {enum.__name__}：{dict(bad)}")


def _check_dates(rows: list[dict], keys: Iterable[str], label: str, errors: list[str]) -> None:
    for key in keys:
        bad = [r.get(key) for r in rows if (r.get(key) or "") and not _valid_date(r[key])]
        if bad:
            errors.append(f"{label}.{key} 日期格式錯誤：{bad[:5]}")


def _valid_date(value: str) -> bool:
    if not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _check_refs(values: Iterable[tuple[str, str]], valid: set[str], label: str, errors: list[str]) -> None:
    bad = sorted({f"{owner}→{ref}" for owner, ref in values if ref not in valid})
    if bad:
        errors.append(f"{label} 參照不存在：{bad[:5]}（共 {len(bad)}）")


def _is_official_tw_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host.endswith(".gov.tw")


def validate_corpus(corpus: Corpus) -> list[str]:
    """所有 FK / ID / enum / 日期 / 官方網域 / 筆數檢查；回傳錯誤清單（空 = 通過）。"""
    errors: list[str] = []
    if not corpus.validation.get("passed"):
        errors.append("validation.json passed != true")
    if not corpus.corpus_version or not _valid_date(corpus.corpus_version):
        errors.append(f"validation.json research_reference_date 無效：{corpus.corpus_version!r}")
    for attr, key in EXPECTED_COUNT_KEYS.items():
        expected = (corpus.validation.get("counts") or {}).get(key)
        actual = len(getattr(corpus, attr))
        if expected != actual:
            errors.append(f"{attr} 筆數 {actual} 與 validation.json counts.{key}={expected} 不符")

    _check_unique(corpus.sources, "source_id", "sources", errors)
    _check_unique(corpus.rules, "rule_id", "rules", errors)
    _check_unique(corpus.examples, "example_id", "examples", errors)
    _check_unique(corpus.functions, "function_id", "functions", errors)
    _check_unique(corpus.functions, "official_function_name", "functions", errors)
    _check_unique(corpus.documents, "document_id", "documents", errors)

    _check_enum(corpus.sources, "status", RecordStatus, "sources", errors)
    _check_enum(corpus.sources, "date_review_status", DateReviewStatus, "sources", errors)
    _check_enum(corpus.rules, "status", RecordStatus, "rules", errors)
    _check_enum(corpus.rules, "review_status", ReviewStatus, "rules", errors)
    _check_enum(corpus.rules, "policy_effect", PolicyEffect, "rules", errors)
    _check_enum(corpus.examples, "example_type", ExampleType, "examples", errors)
    _check_enum(corpus.functions, "status", RecordStatus, "functions", errors)
    _check_enum(corpus.functions, "review_status", ReviewStatus, "functions", errors)
    _check_enum(corpus.documents, "status", RecordStatus, "documents", errors)
    _check_enum(corpus.documents, "review_status", ReviewStatus, "documents", errors)
    _check_enum(corpus.documents, "extraction_method", ExtractionMethod, "documents", errors)
    unknown_types = Counter(r["document_type"] for r in corpus.documents if r.get("document_type") not in RAG_DOCUMENT_TYPE)
    if unknown_types:
        errors.append(f"documents.document_type 無 RAG 分類對應：{dict(unknown_types)}")

    _check_dates(corpus.sources, ("published_date", "effective_date", "last_amended_date"), "sources", errors)
    _check_dates(corpus.rules, ("effective_date",), "rules", errors)
    _check_dates(corpus.functions, ("effective_date", "evaluation_effective_date", "catalog_effective_date"), "functions", errors)
    _check_dates(corpus.documents, ("published_date", "effective_date"), "documents", errors)
    for label, rows in (("sources", corpus.sources), ("documents", corpus.documents), ("manifest", corpus.manifest)):
        bad = [r.get("retrieved_at") for r in rows if not _valid_datetime(r.get("retrieved_at"))]
        if bad:
            errors.append(f"{label}.retrieved_at 格式錯誤：{bad[:3]}")

    source_ids = {s["source_id"] for s in corpus.sources}
    rule_ids = {r["rule_id"] for r in corpus.rules}
    _check_refs(((r["rule_id"], r["source_id"]) for r in corpus.rules), source_ids, "rules.source_id", errors)
    _check_refs(((e["example_id"], e["rule_id"]) for e in corpus.examples), rule_ids, "examples.rule_id", errors)
    _check_refs(((e["example_id"], e["source_id"]) for e in corpus.examples), source_ids, "examples.source_id", errors)
    _check_refs(((f["function_id"], f["source_id"]) for f in corpus.functions), source_ids, "functions.source_id", errors)
    _check_refs(((f["function_id"], f["evaluation_source_id"]) for f in corpus.functions), source_ids,
                "functions.evaluation_source_id", errors)
    try:
        hist = [(f["function_id"], s) for f in corpus.functions for s in _json_list(f.get("historical_evaluation_source_ids"))]
        _check_refs(hist, source_ids, "functions.historical_evaluation_source_ids", errors)
    except (ValueError, json.JSONDecodeError) as exc:
        errors.append(f"functions.historical_evaluation_source_ids 不是 JSON array：{exc}")
    _check_refs(((d["document_id"], d["source_id"]) for d in corpus.documents), source_ids, "documents.source_id", errors)
    _check_refs(((m["archive_path"], m["source_id"]) for m in corpus.manifest), source_ids, "manifest.source_id", errors)
    _check_refs(((i["dataset_id"], i["source_id"]) for i in corpus.inventory), source_ids, "inventory.source_id", errors)
    for s in corpus.sources:
        for key in ("original_download_urls", "raw_files"):
            try:
                _json_list(s.get(key))
            except (ValueError, json.JSONDecodeError):
                errors.append(f"sources.{key} 不是 JSON array：{s['source_id']}")

    for s in corpus.sources:
        host = (urlsplit(s["source_url"]).hostname or "").lower()
        if not _is_official_tw_host(s["source_url"]) or host != s.get("official_domain", "").lower():
            errors.append(f"sources 非台灣官方網域或與 official_domain 不符：{s['source_id']}")
    # 區段網址：台灣官方網域；或是同一來源 raw manifest 中、官方父頁在台灣官方網域的附件下載網址（官方檔案儲存）
    attested = {(m["source_id"], m["download_url"]) for m in corpus.manifest
                if _is_official_tw_host(m.get("official_parent_url") or "")}
    bad_doc_urls = [d["document_id"] for d in corpus.documents
                    if not _is_official_tw_host(d["source_url"]) and (d["source_id"], d["source_url"]) not in attested]
    if bad_doc_urls:
        errors.append(f"documents 非台灣官方網域且無官方父頁附件紀錄：{bad_doc_urls[:5]}")

    for label, rows, key in (("rules", corpus.rules, "official_text"), ("examples", corpus.examples, "example_text"),
                             ("documents", corpus.documents, "content")):
        empty = [r for r in rows if not (r.get(key) or "").strip()]
        if empty:
            errors.append(f"{label}.{key} 有 {len(empty)} 筆空白")

    # 同一 rag_document（來源 + 網址）內的區段狀態必須一致，is_current 才有單一意義
    group_status: dict[str, set[str]] = defaultdict(set)
    for doc in corpus.documents:
        group_status[rag_group_key(doc)].add(doc["status"])
    mixed = [k for k, v in group_status.items() if len(v) > 1]
    if mixed:
        errors.append(f"同一來源網址的區段狀態不一致：{mixed[:3]}")
    return errors


def _valid_datetime(value: Any) -> bool:
    try:
        datetime.fromisoformat(str(value))
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------- 列建構（純函式）


def canonical_id(kind: str, corpus_id: str) -> str:
    return stable_id(f"{CORPUS_NAME}_{kind}", corpus_id)


def source_registry_key(source_id: str) -> str:
    return f"{SOURCE_KEY_PREFIX}{source_id}"


def source_registry_id(source_id: str) -> str:
    return stable_id("source_registry", source_registry_key(source_id))


def content_hash(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rag_group_key(doc: dict[str, Any]) -> str:
    return f"{doc['source_id']}|{normalize_url(doc['source_url'])}"


@dataclass
class TableRows:
    """每張表的資料列（不含 batch / version bookkeeping；那些在 diff 時才加）。"""

    source_registry: list[dict[str, Any]]
    claim_policy_sources: list[dict[str, Any]]
    claim_policy_rules: list[dict[str, Any]]
    claim_policy_examples: list[dict[str, Any]]
    health_function_catalog: list[dict[str, Any]]
    health_function_historical_sources: list[dict[str, Any]]


# 有 bookkeeping 欄位（content_hash / record_version / batch）的表與其自然鍵
VERSIONED_TABLES = {
    "claim_policy_sources": "source_id",
    "claim_policy_rules": "rule_id",
    "claim_policy_examples": "example_id",
    "health_function_catalog": "function_id",
}


def build_table_rows(corpus: Corpus) -> TableRows:
    sources = {s["source_id"]: s for s in corpus.sources}
    manifest = defaultdict(list)
    for m in corpus.manifest:
        manifest[m["source_id"]].append({k: v for k, v in m.items() if k != "source_id"})
    inventory = defaultdict(list)
    for i in corpus.inventory:
        inventory[i["source_id"]].append(i)

    registry, source_rows = [], []
    for s in corpus.sources:
        reasons = source_quarantine(s)
        registry.append({
            "id": source_registry_id(s["source_id"]),
            "source_key": source_registry_key(s["source_id"]),
            "source_name": s["document_title"],
            "source_type": "open_data" if s["source_id"] in inventory else "official_document",
            "publisher": s["authority"],
            "base_url": s["source_url"],
            "country_code": "TW",
            "is_official": True,
            "description": f"{CORPUS_NAME} {s['document_type']}",
        })
        source_rows.append({
            "id": canonical_id("source", s["source_id"]),
            "source_id": s["source_id"],
            "source_registry_id": source_registry_id(s["source_id"]),
            "country_code": "TW",
            "authority": s["authority"],
            "document_title": s["document_title"],
            "document_type": s["document_type"],
            "document_number": _text_or_none(s.get("document_number")),
            "source_url": s["source_url"],
            "official_domain": s["official_domain"],
            "published_date": _date_or_none(s.get("published_date")),
            "effective_date": _date_or_none(s.get("effective_date")),
            "last_amended_date": _date_or_none(s.get("last_amended_date")),
            "retrieved_at": s["retrieved_at"],
            "metadata_updated_at": _text_or_none(s.get("metadata_updated_at")),
            "status": s["status"],
            "version_note": _text_or_none(s.get("version_note")),
            "date_review_status": s["date_review_status"],
            "original_download_urls": _json_list(s.get("original_download_urls")),
            "raw_files": _json_list(s.get("raw_files")),
            "raw_manifest": manifest.get(s["source_id"], []),
            "open_data_inventory": [_inventory_metadata(i) for i in inventory.get(s["source_id"], [])],
            "is_official": True,
            "evidence_use": decide_evidence_use(reasons).value,
            "quarantine_reasons": reasons,
            "raw_data": s,
        })

    rule_rows, rule_reasons = [], {}
    for r in corpus.rules:
        reasons = rule_quarantine(r, sources[r["source_id"]])
        rule_reasons[r["rule_id"]] = reasons
        rule_rows.append({
            "id": canonical_id("rule", r["rule_id"]),
            "rule_id": r["rule_id"],
            "claim_policy_source_id": canonical_id("source", r["source_id"]),
            "rule_category": r["rule_category"],
            "policy_effect": r["policy_effect"],
            "claim_category": r["claim_category"],
            "scope": _text_or_none(r.get("scope")),
            "rule_summary_zh": _text_or_none(r.get("rule_summary_zh")),
            "official_text": r["official_text"],
            "conditions": _text_or_none(r.get("conditions")),
            "exceptions": _text_or_none(r.get("exceptions")),
            "legal_basis": _text_or_none(r.get("legal_basis")),
            "article_or_attachment": r["article_or_attachment"],
            "effective_date": _date_or_none(r.get("effective_date")),
            "status": r["status"],
            "review_status": r["review_status"],
            "evidence_use": decide_evidence_use(reasons).value,
            "quarantine_reasons": reasons,
            "raw_data": r,
        })

    example_rows = []
    for e in corpus.examples:
        reasons = example_quarantine(rule_reasons[e["rule_id"]], sources[e["source_id"]])
        example_rows.append({
            "id": canonical_id("example", e["example_id"]),
            "example_id": e["example_id"],
            "claim_policy_rule_id": canonical_id("rule", e["rule_id"]),
            "claim_policy_source_id": canonical_id("source", e["source_id"]),
            "example_text": e["example_text"],
            "example_type": e["example_type"],
            "claim_category": e["claim_category"],
            "conditions": _text_or_none(e.get("conditions")),
            "notes": _text_or_none(e.get("notes")),
            "article_or_attachment": _text_or_none(e.get("article_or_attachment")),
            "evidence_use": decide_evidence_use(reasons).value,
            "quarantine_reasons": reasons,
            "raw_data": e,
        })

    function_rows, historical = [], []
    for f in corpus.functions:
        reasons = function_quarantine(f, sources[f["source_id"]], sources[f["evaluation_source_id"]])
        fid = canonical_id("health_function", f["function_id"])
        function_rows.append({
            "id": fid,
            "function_id": f["function_id"],
            "official_function_name": f["official_function_name"],
            "official_definition": _text_or_none(f.get("official_definition")),  # 官方未提供時留空，不補寫
            "evaluation_method_title": _text_or_none(f.get("evaluation_method_title")),
            "legal_basis": _text_or_none(f.get("legal_basis")),
            "catalog_source_id": canonical_id("source", f["source_id"]),
            "evaluation_source_id": canonical_id("source", f["evaluation_source_id"]),
            "effective_date": _date_or_none(f.get("effective_date")),
            "evaluation_effective_date": _date_or_none(f.get("evaluation_effective_date")),
            "catalog_effective_date": _date_or_none(f.get("catalog_effective_date")),
            "status": f["status"],
            "review_status": f["review_status"],
            "notes": _text_or_none(f.get("notes")),
            "evidence_use": decide_evidence_use(reasons).value,
            "quarantine_reasons": reasons,
            "raw_data": f,
        })
        for hist_source in _json_list(f.get("historical_evaluation_source_ids")):
            historical.append({"health_function_id": fid, "claim_policy_source_id": canonical_id("source", hist_source)})

    return TableRows(registry, source_rows, rule_rows, example_rows, function_rows, historical)


def _inventory_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """資料集 metadata（欄位、下載方式、授權）；不含任何案例內容。"""
    out = dict(row)
    try:
        out["fields"] = _json_list(row.get("fields"))
    except (ValueError, json.JSONDecodeError):
        pass
    return out


# ---------------------------------------------------------------------------- RAG（沿用 rag_documents / rag_chunks）


@dataclass
class RagGroup:
    group_key: str
    document: dict[str, Any]
    chunks: list[dict[str, Any]]  # 每列含 embedding_input（寫入前移除）
    section_ids: list[str]


def build_rag_groups(corpus: Corpus) -> list[RagGroup]:
    sources = {s["source_id"]: s for s in corpus.sources}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in sorted(corpus.documents, key=lambda d: d["document_id"]):
        grouped[rag_group_key(doc)].append(doc)

    groups = []
    for key, sections in grouped.items():
        first = sections[0]
        src = sources[first["source_id"]]
        status = first["status"]
        url = first["source_url"]
        full_text = "\n\n".join(s["content"] for s in sections)
        doc_hash = content_fingerprint(f"{key}\n{full_text}")
        doc_id = stable_id(f"{CORPUS_NAME}_rag_document", f"{key}|{doc_hash}")
        chunks: list[dict[str, Any]] = []
        use_counts: Counter = Counter()
        for section in sections:
            reasons = section_quarantine(section)
            use = decide_evidence_use(reasons)
            use_counts[use.value] += 1
            parts = [section["content"]] if len(section["content"]) <= MAX_CHUNK_CHARS else chunk_text(section["content"])
            for part_index, part in enumerate(parts):
                index = len(chunks)
                chunks.append({
                    "id": stable_id(f"{CORPUS_NAME}_rag_chunk", f"{doc_id}|{index}"),
                    "document_id": doc_id,
                    "chunk_index": index,
                    "content": part,
                    "embedding_input": f"{section['title']}｜{section['article_or_section']}\n{part}",
                    "metadata": {
                        "corpus": CORPUS_NAME,
                        "corpus_version": corpus.corpus_version,
                        "corpus_document_id": section["document_id"],
                        "source_id": section["source_id"],
                        "title": section["title"],
                        "authority": section["authority"],
                        "document_type_original": section["document_type"],
                        "article_or_section": section["article_or_section"],
                        "status": section["status"],
                        "review_status": section["review_status"],
                        "extraction_method": section["extraction_method"],
                        "published_date": _date_or_none(section.get("published_date")),
                        "effective_date": _date_or_none(section.get("effective_date")),
                        "retrieved_at": section["retrieved_at"],
                        "source_url": section["source_url"],
                        "raw_key": section.get("raw_key"),
                        "evidence_use": use.value,
                        "quarantine_reasons": reasons,
                        "section_part": part_index,
                        "section_parts": len(parts),
                    },
                })
        document = {
            "id": doc_id,
            "title": first["title"],
            "source_url": url,
            "source_url_normalized": normalize_url(url),
            "source_name": f"{src['authority']} {src['document_title']}",
            "source_id": source_registry_id(first["source_id"]),
            "document_type": RAG_DOCUMENT_TYPE[first["document_type"]],
            "country_code": "TW",
            "is_official": True,
            "is_current": status != RecordStatus.SUPERSEDED,
            "published_at": _date_or_none(first.get("published_date")) or _date_or_none(src.get("published_date")),
            "retrieved_at": max(s["retrieved_at"] for s in sections),
            "content_hash": doc_hash,
            "metadata": {
                "corpus": CORPUS_NAME,
                "corpus_version": corpus.corpus_version,
                "corpus_group_key": key,
                "corpus_source_id": first["source_id"],
                "status": status,
                "authority": src["authority"],
                "document_type_original": first["document_type"],
                "effective_date": _date_or_none(src.get("effective_date")),
                "source_record_ids": [s["document_id"] for s in sections],
                "section_count": len(sections),
                "chunk_count": len(chunks),
                "evidence_use_counts": dict(sorted(use_counts.items())),
                "char_count": len(full_text),
            },
        }
        groups.append(RagGroup(key, document, chunks, [s["document_id"] for s in sections]))
    return groups


# ---------------------------------------------------------------------------- Neo4j（只有有關係價值的節點）


def map_approved_claims_to_functions(
    approved_claims: list[dict[str, Any]], functions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deterministic mapping：approved_claims.function_name 與官方功效名稱正規化後完全相同才建立。

    未分類官方宣稱、名稱不同（包含 / 近似 / 同義）、或正規化後對到多個功效項目 → 不建立。
    """
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in functions:
        by_name[normalize_name(f["official_function_name"])].append(f)
    out = []
    for claim in approved_claims:
        if claim.get("claim_kind") == CLAIM_KIND_UNCLASSIFIED:
            continue
        matches = by_name.get(normalize_name(claim.get("function_name") or ""), [])
        if len(matches) == 1 and normalize_name(claim.get("function_name") or ""):
            out.append({"claim_id": claim["id"], "health_function_id": matches[0]["id"],
                        "mapping_method": "EXACT_OFFICIAL_FUNCTION_NAME"})
    return out


@dataclass
class GraphRows:
    sources: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    functions: list[dict[str, Any]]
    defined_by: list[dict[str, Any]]
    historical: list[dict[str, Any]]
    claim_mappings: list[dict[str, Any]]

    def counts(self) -> dict[str, int]:
        return {
            "Source": len(self.sources), "ClaimPolicyRule": len(self.rules), "HealthFunction": len(self.functions),
            "SUPPORTED_BY(ClaimPolicyRule→Source)": len(self.rules),
            "DEFINED_BY(HealthFunction→Source)": len(self.defined_by),
            "HAS_HISTORICAL_METHOD": len(self.historical),
            "MAPS_TO_HEALTH_FUNCTION(Claim→HealthFunction)": len(self.claim_mappings),
        }


def build_graph_rows(
    sources: list[dict[str, Any]], rules: list[dict[str, Any]], functions: list[dict[str, Any]],
    historical_links: list[dict[str, Any]], approved_claims: list[dict[str, Any]] | None = None,
) -> GraphRows:
    """由 claim_policy_* 表的資料列（Supabase 讀回或本次計算結果）建立 graph 寫入列。"""
    registry_of = {s["id"]: s["source_registry_id"] for s in sources}
    source_nodes = [{
        "id": s["source_registry_id"], "source_name": s["document_title"], "is_official": s["is_official"],
        "base_url": s["source_url"], "corpus_source_id": s["source_id"], "status": s["status"],
        "evidence_use": s["evidence_use"],
    } for s in sources]
    rule_nodes = [{
        "id": r["id"], "rule_id": r["rule_id"], "policy_effect": r["policy_effect"],
        "rule_category": r["rule_category"], "claim_category": r["claim_category"], "status": r["status"],
        "review_status": r["review_status"], "evidence_use": r["evidence_use"],
        "article_or_attachment": r["article_or_attachment"], "source_id": registry_of[r["claim_policy_source_id"]],
    } for r in rules]
    function_nodes = [{
        "id": f["id"], "function_id": f["function_id"], "official_function_name": f["official_function_name"],
        "status": f["status"], "review_status": f["review_status"], "evidence_use": f["evidence_use"],
    } for f in functions]
    defined_by = []
    for f in functions:
        defined_by.append({"function_id": f["id"], "source_id": registry_of[f["catalog_source_id"]], "role": "CATALOG"})
        defined_by.append({"function_id": f["id"], "source_id": registry_of[f["evaluation_source_id"]],
                           "role": "EVALUATION_METHOD"})
    historical = [{"function_id": h["health_function_id"], "source_id": registry_of[h["claim_policy_source_id"]]}
                  for h in historical_links]
    mappings = map_approved_claims_to_functions(approved_claims or [], functions)
    return GraphRows(source_nodes, rule_nodes, function_nodes, defined_by, historical, mappings)


GRAPH_QUERIES = {
    "Source": """
        UNWIND $rows AS r MERGE (s:Source {source_id: r.id})
        SET s.source_name = r.source_name, s.is_official = r.is_official, s.base_url = r.base_url,
            s.corpus_source_id = r.corpus_source_id, s.status = r.status, s.evidence_use = r.evidence_use""",
    "ClaimPolicyRule": """
        UNWIND $rows AS r MERGE (n:ClaimPolicyRule {claim_policy_rule_id: r.id})
        SET n.rule_id = r.rule_id, n.policy_effect = r.policy_effect, n.rule_category = r.rule_category,
            n.claim_category = r.claim_category, n.status = r.status, n.review_status = r.review_status,
            n.evidence_use = r.evidence_use, n.article_or_attachment = r.article_or_attachment
        WITH n, r MATCH (s:Source {source_id: r.source_id})
        MERGE (n)-[rel:SUPPORTED_BY]->(s) SET rel.article_or_attachment = r.article_or_attachment""",
    "HealthFunction": """
        UNWIND $rows AS r MERGE (n:HealthFunction {health_function_id: r.id})
        SET n.function_id = r.function_id, n.official_function_name = r.official_function_name,
            n.status = r.status, n.review_status = r.review_status, n.evidence_use = r.evidence_use""",
    "DEFINED_BY": """
        UNWIND $rows AS r MATCH (n:HealthFunction {health_function_id: r.function_id})
        MATCH (s:Source {source_id: r.source_id})
        MERGE (n)-[rel:DEFINED_BY {role: r.role}]->(s)""",
    "HAS_HISTORICAL_METHOD": """
        UNWIND $rows AS r MATCH (n:HealthFunction {health_function_id: r.function_id})
        MATCH (s:Source {source_id: r.source_id})
        MERGE (n)-[:HAS_HISTORICAL_METHOD]->(s)""",
    # 只連既有 Claim（approved_claims）節點，不在這裡建立 Claim
    "MAPS_TO_HEALTH_FUNCTION": """
        UNWIND $rows AS r MATCH (c:Claim {claim_id: r.claim_id})
        MATCH (n:HealthFunction {health_function_id: r.health_function_id})
        MERGE (c)-[rel:MAPS_TO_HEALTH_FUNCTION]->(n) SET rel.mapping_method = r.mapping_method""",
}


def write_graph(neo4j, rows: GraphRows, batch_size: int = 500) -> dict[str, int]:
    from app.services.neo4j_service import load_schema_statements

    for stmt in load_schema_statements(NEO4J_SCHEMA_FILE):
        neo4j.write(stmt)
    payloads = {
        "Source": rows.sources, "ClaimPolicyRule": rows.rules, "HealthFunction": rows.functions,
        "DEFINED_BY": rows.defined_by, "HAS_HISTORICAL_METHOD": rows.historical,
        "MAPS_TO_HEALTH_FUNCTION": rows.claim_mappings,
    }
    for stage, data in payloads.items():
        for i in range(0, len(data), batch_size):
            neo4j.write(GRAPH_QUERIES[stage], {"rows": data[i : i + batch_size]})
    return rows.counts()


# ---------------------------------------------------------------------------- 匯入計畫（diff）


class CorpusStore(Protocol):
    """importer 需要的最小資料存取介面（Supabase / 本機 PostgreSQL / 空白 dry-run 皆可實作）。"""

    def fetch(self, table: str, columns: list[str], eq: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None: ...

    def update(self, table: str, values: dict[str, Any], match: dict[str, Any]) -> None: ...


class EmptyStore:
    """dry-run 預設：視為尚未匯入任何資料；任何寫入都拒絕。"""

    def fetch(self, table, columns, eq=None):
        return []

    def upsert(self, table, rows, on_conflict):
        raise RuntimeError("dry-run store 不允許寫入")

    def update(self, table, values, match):
        raise RuntimeError("dry-run store 不允許寫入")


@dataclass
class TableDiff:
    insert: list[dict[str, Any]] = field(default_factory=list)
    update: list[dict[str, Any]] = field(default_factory=list)
    unchanged: int = 0
    not_in_corpus: int = 0

    def counts(self) -> dict[str, int]:
        return {"insert": len(self.insert), "update": len(self.update), "unchanged": self.unchanged,
                "not_in_corpus": self.not_in_corpus}


@dataclass
class RagDiff:
    insert: list[RagGroup] = field(default_factory=list)
    new_version: list[tuple[RagGroup, list[str]]] = field(default_factory=list)  # (group, 舊版 doc ids)
    metadata_update: list[RagGroup] = field(default_factory=list)
    repair_chunks: list[RagGroup] = field(default_factory=list)
    unchanged: int = 0
    not_in_corpus: int = 0
    external_url_conflicts: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "insert": len(self.insert), "update": len(self.new_version) + len(self.metadata_update),
            "new_version": len(self.new_version), "metadata_update": len(self.metadata_update),
            "repair_chunks": len(self.repair_chunks), "unchanged": self.unchanged,
            "not_in_corpus": self.not_in_corpus, "external_url_conflicts": len(self.external_url_conflicts),
        }

    def groups_to_embed(self) -> list[RagGroup]:
        return [*self.insert, *(g for g, _ in self.new_version), *self.repair_chunks]


@dataclass
class ImportPlan:
    corpus: Corpus
    batch: dict[str, Any]
    rows: TableRows
    rag_groups: list[RagGroup]
    diffs: dict[str, TableDiff]
    rag: RagDiff
    graph: GraphRows
    errors: list[str]
    notes: list[str]

    @property
    def batch_id(self) -> str:
        return self.batch["id"]

    def report(self) -> dict[str, Any]:
        rows = self.rows
        quarantine = {
            table: dict(Counter(r["evidence_use"] for r in getattr(rows, table)))
            for table in VERSIONED_TABLES
        }
        sections = [c["metadata"] for g in self.rag_groups for c in g.chunks if c["metadata"]["section_part"] == 0]
        quarantine["rag_sections"] = dict(Counter(m["evidence_use"] for m in sections))
        review = {
            "sources_status_needs_review": sum(r["status"] == RecordStatus.NEEDS_REVIEW for r in rows.claim_policy_sources),
            "rules_review_status_needs_review": sum(r["review_status"] == ReviewStatus.NEEDS_REVIEW for r in rows.claim_policy_rules),
            "rules_policy_effect_needs_review": sum(r["policy_effect"] == PolicyEffect.NEEDS_REVIEW for r in rows.claim_policy_rules),
            "functions_review_status_needs_review": sum(r["review_status"] == ReviewStatus.NEEDS_REVIEW for r in rows.health_function_catalog),
            "rag_sections_needs_review": sum(m["review_status"] == ReviewStatus.NEEDS_REVIEW for m in sections),
            "rag_sections_unverified_extraction": sum(m["extraction_method"] in UNVERIFIED_EXTRACTION for m in sections),
        }
        docs = [g.document for g in self.rag_groups]
        current_sections = [m for m in sections if m["status"] != RecordStatus.SUPERSEDED]
        return {
            "corpus_version": self.corpus.corpus_version,
            "batch_key": self.batch["batch_key"],
            "tables": {t: d.counts() for t, d in self.diffs.items()},
            "quarantine": quarantine,
            "quarantined_records": {t: sum(v for k, v in c.items() if k != EvidenceUse.DECISION_ELIGIBLE)
                                    for t, c in quarantine.items()},
            "review": review,
            "rag": {
                "source_sections": len(sections),
                "documents": len(docs),
                "current_documents": sum(d["is_current"] for d in docs),
                "superseded_documents": sum(not d["is_current"] for d in docs),
                "current_retrieval_sections": len(current_sections),
                "excluded_superseded_sections": len(sections) - len(current_sections),
                "chunks": sum(len(g.chunks) for g in self.rag_groups),
                "chunks_to_embed": sum(len(g.chunks) for g in self.rag.groups_to_embed()),
                "document_types": dict(Counter(d["document_type"] for d in docs)),
                **self.rag.counts(),
            },
            "graph": self.graph.counts(),
            "open_data_inventory_sources": sum(bool(r["open_data_inventory"]) for r in rows.claim_policy_sources),
            "illegal_ad_cases_written": 0,
            "errors": self.errors,
            "notes": self.notes,
        }


def _bookkeep(row: dict[str, Any], *, corpus_version: str, batch_id: str, existing: dict[str, Any] | None,
              digest: str) -> dict[str, Any]:
    out = dict(row)
    out.update({
        "corpus_name": CORPUS_NAME,
        "corpus_version": corpus_version,
        "content_hash": digest,
        "record_version": (int(existing["record_version"]) + 1) if existing else 1,
        "first_import_batch_id": existing["first_import_batch_id"] if existing else batch_id,
        "last_import_batch_id": batch_id,
    })
    return out


def _diff_versioned(table: str, key: str, rows: list[dict[str, Any]], store: CorpusStore,
                    corpus_version: str, batch_id: str, errors: list[str]) -> TableDiff:
    existing = {r[key]: r for r in store.fetch(
        table, ["id", key, "content_hash", "record_version", "first_import_batch_id"])}
    diff = TableDiff()
    for row in rows:
        digest = content_hash(row)
        old = existing.get(row[key])
        if old and str(old["id"]) != row["id"]:
            errors.append(f"{table}.{key}={row[key]} 既有 id 與 canonical id 不同（{old['id']} ≠ {row['id']}）")
            continue
        if old and old["content_hash"] == digest:
            diff.unchanged += 1
        elif old:
            diff.update.append(_bookkeep(row, corpus_version=corpus_version, batch_id=batch_id, existing=old, digest=digest))
        else:
            diff.insert.append(_bookkeep(row, corpus_version=corpus_version, batch_id=batch_id, existing=None, digest=digest))
    diff.not_in_corpus = len(set(existing) - {r[key] for r in rows})
    return diff


_REGISTRY_FIELDS = ("source_name", "source_type", "publisher", "base_url", "country_code", "is_official", "description")


def _diff_registry(rows: list[dict[str, Any]], store: CorpusStore, errors: list[str]) -> TableDiff:
    existing = {r["source_key"]: r for r in store.fetch("source_registry", ["id", "source_key", *_REGISTRY_FIELDS])
                if str(r["source_key"]).startswith(SOURCE_KEY_PREFIX)}
    diff = TableDiff()
    for row in rows:
        old = existing.get(row["source_key"])
        if old and str(old["id"]) != row["id"]:
            errors.append(f"source_registry.source_key={row['source_key']} 既有 id 與 canonical id 不同")
        elif old and all(old.get(f) == row[f] for f in _REGISTRY_FIELDS):
            diff.unchanged += 1
        else:
            (diff.update if old else diff.insert).append(row)
    diff.not_in_corpus = len(set(existing) - {r["source_key"] for r in rows})
    return diff


def _diff_links(rows: list[dict[str, Any]], store: CorpusStore, batch_id: str) -> TableDiff:
    existing = {(str(r["health_function_id"]), str(r["claim_policy_source_id"]))
                for r in store.fetch("health_function_historical_sources", ["health_function_id", "claim_policy_source_id"])}
    wanted = {(r["health_function_id"], r["claim_policy_source_id"]) for r in rows}
    diff = TableDiff()
    for row in rows:
        if (row["health_function_id"], row["claim_policy_source_id"]) in existing:
            diff.unchanged += 1
        else:
            diff.insert.append({**row, "last_import_batch_id": batch_id})
    diff.not_in_corpus = len(existing - wanted)
    return diff


def _diff_rag(groups: list[RagGroup], store: CorpusStore) -> RagDiff:
    existing_docs = store.fetch("rag_documents", ["id", "content_hash", "is_current", "metadata", "source_url_normalized"],
                                eq={"metadata->>corpus": CORPUS_NAME})
    chunk_counts = Counter(str(c["document_id"]) for c in store.fetch(
        "rag_chunks", ["id", "document_id"], eq={"metadata->>corpus": CORPUS_NAME}))
    by_hash = {d["content_hash"]: d for d in existing_docs}
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in existing_docs:
        by_group[(d.get("metadata") or {}).get("corpus_group_key")].append(d)

    diff = RagDiff()
    for g in groups:
        old = by_hash.get(g.document["content_hash"])
        if old:
            if str(old["id"]) != g.document["id"]:
                diff.external_url_conflicts.append(f"content_hash 相同但 id 不同：{g.group_key}")
                continue
            if chunk_counts.get(str(old["id"]), 0) != len(g.chunks):
                diff.repair_chunks.append(g)
            elif bool(old["is_current"]) != g.document["is_current"] or (old.get("metadata") or {}) != g.document["metadata"]:
                diff.metadata_update.append(g)
            else:
                diff.unchanged += 1
            continue
        older = [str(d["id"]) for d in by_group.get(g.group_key, []) if d["is_current"]]
        (diff.new_version.append((g, older)) if by_group.get(g.group_key) else diff.insert.append(g))
    diff.not_in_corpus = len(set(by_group) - {g.group_key for g in groups} - {None})

    # 既有非 corpus 的 current 文件若與本次 current 文件同網址，會撞 rag_documents_current_url_unique：
    # 是否取代既有文件屬於人工決定，一律列為 blocker，不自動改動既有 RAG
    current_urls = {g.document["source_url_normalized"] for g in groups if g.document["is_current"]}
    if current_urls:
        for d in store.fetch("rag_documents", ["id", "source_url_normalized", "metadata"], eq={"is_current": True}):
            if (d.get("metadata") or {}).get("corpus") != CORPUS_NAME and d.get("source_url_normalized") in current_urls:
                diff.external_url_conflicts.append(f"既有非 corpus RAG 文件與 corpus 同網址：{d['source_url_normalized']}")
    return diff


def plan_import(
    corpus: Corpus, store: CorpusStore | None = None, *, approved_claims: list[dict[str, Any]] | None = None,
    allow_older_corpus: bool = False,
) -> ImportPlan:
    store = store or EmptyStore()
    errors = validate_corpus(corpus)
    notes: list[str] = []
    version = corpus.corpus_version
    batch_id = stable_id(f"{CORPUS_NAME}_import_batch", corpus.batch_key)

    previous = store.fetch("claim_policy_import_batches", ["id", "batch_key", "corpus_version", "status", "run_count"])
    newer = [b for b in previous if b["status"] == BatchStatus.COMPLETED and str(b["corpus_version"]) > version]
    if newer and not allow_older_corpus:
        errors.append(f"資料庫已有較新的 corpus 版本 {max(str(b['corpus_version']) for b in newer)} > {version}（version-aware：拒絕降版）")
    same = next((b for b in previous if b["batch_key"] == corpus.batch_key), None)
    batch = {
        "id": batch_id, "batch_key": corpus.batch_key, "corpus_name": CORPUS_NAME, "corpus_version": version,
        "validation_checked_at": corpus.validation.get("checked_at"),
        "validation_passed": bool(corpus.validation.get("passed")),
        "file_hashes": corpus.file_hashes,
        "expected_counts": {k: (corpus.validation.get("counts") or {}).get(v) for k, v in EXPECTED_COUNT_KEYS.items()},
        "run_count": (int(same["run_count"]) + 1) if same else 1,
    }
    if same:
        notes.append(f"相同 corpus（batch_key 相同）已匯入過 {same['run_count']} 次")

    if errors:  # 驗證失敗就不計算列，避免參照錯誤
        empty = TableRows([], [], [], [], [], [])
        return ImportPlan(corpus, batch, empty, [], {}, RagDiff(), GraphRows([], [], [], [], [], []), errors, notes)

    rows = build_table_rows(corpus)
    diffs = {"source_registry": _diff_registry(rows.source_registry, store, errors)}
    for table, key in VERSIONED_TABLES.items():
        diffs[table] = _diff_versioned(table, key, getattr(rows, table), store, version, batch_id, errors)
    diffs["health_function_historical_sources"] = _diff_links(rows.health_function_historical_sources, store, batch_id)

    groups = build_rag_groups(corpus)
    rag = _diff_rag(groups, store)
    errors.extend(rag.external_url_conflicts)

    graph = build_graph_rows(rows.claim_policy_sources, rows.claim_policy_rules, rows.health_function_catalog,
                             rows.health_function_historical_sources, approved_claims)
    if approved_claims is None:
        notes.append("未提供 approved_claims：MAPS_TO_HEALTH_FUNCTION 只在 apply 時由 Supabase 讀取後計算")
    return ImportPlan(corpus, batch, rows, groups, diffs, rag, graph, errors, notes)


# ---------------------------------------------------------------------------- 寫入（只有 apply 模式）


def _batched_upsert(store: CorpusStore, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
    for i in range(0, len(rows), WRITE_BATCH):
        store.upsert(table, rows[i : i + WRITE_BATCH], on_conflict)


def _embed(embedder, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        vectors.extend(embedder.embed(texts[i : i + EMBED_BATCH]))
    if len(vectors) != len(texts):
        raise RuntimeError(f"embedding 數量 {len(vectors)} 與 chunk 數 {len(texts)} 不符")
    return vectors


def _chunk_rows(group: RagGroup, vectors: list[list[float]], embedding_model: str) -> list[dict[str, Any]]:
    return [{
        "id": c["id"], "document_id": c["document_id"], "chunk_index": c["chunk_index"], "content": c["content"],
        "embedding": v, "embedding_model": embedding_model, "metadata": c["metadata"],
    } for c, v in zip(group.chunks, vectors)]


def apply_import(
    plan: ImportPlan, store: CorpusStore, *, embedder=None, embedding_model: str | None = None,
    skip_rag: bool = False, neo4j=None, read_approved_claims=None,
) -> dict[str, Any]:
    """依計畫寫入。驗證錯誤時拒絕；任何例外都把批次標為 FAILED 後重新拋出（重跑即可補齊，全部 upsert）。"""
    if plan.errors:
        raise RuntimeError(f"匯入計畫有 {len(plan.errors)} 個錯誤，拒絕寫入：{plan.errors[:3]}")
    if not skip_rag and (embedder is None or not embedding_model) and plan.rag.groups_to_embed():
        raise RuntimeError("RAG 需要 embedder 與 embedding_model（或使用 skip_rag）")

    now = datetime.now(timezone.utc).isoformat()
    store.upsert("claim_policy_import_batches",
                 [{**plan.batch, "status": BatchStatus.RUNNING.value, "started_at": now, "finished_at": None, "error_message": None}],
                 "batch_key")
    try:
        d = plan.diffs
        _batched_upsert(store, "source_registry", d["source_registry"].insert + d["source_registry"].update, "source_key")
        for table, key in VERSIONED_TABLES.items():  # FK 順序：sources → rules → examples → functions
            _batched_upsert(store, table, d[table].insert + d[table].update, key)
        _batched_upsert(store, "health_function_historical_sources", d["health_function_historical_sources"].insert,
                        "health_function_id,claim_policy_source_id")

        if not skip_rag:
            for group, older in plan.rag.new_version:
                for doc_id in older:
                    store.update("rag_documents", {"is_current": False}, {"id": doc_id})
            for group in [*plan.rag.insert, *(g for g, _ in plan.rag.new_version), *plan.rag.repair_chunks]:
                store.upsert("rag_documents", [group.document], "id")
                vectors = _embed(embedder, [c["embedding_input"] for c in group.chunks])
                _batched_upsert(store, "rag_chunks", _chunk_rows(group, vectors, embedding_model), "document_id,chunk_index")
            for group in plan.rag.metadata_update:
                store.update("rag_documents", {"is_current": group.document["is_current"],
                                               "metadata": group.document["metadata"]}, {"id": group.document["id"]})
                for c in group.chunks:
                    store.update("rag_chunks", {"metadata": c["metadata"]}, {"id": c["id"]})

        report = plan.report()
        if skip_rag:
            report["rag"]["skipped"] = True
        if neo4j is not None:
            report["graph"] = sync_claim_policy_graph(store, neo4j, read_approved_claims)
        store.upsert("claim_policy_import_batches", [{
            **plan.batch, "status": BatchStatus.COMPLETED.value, "result_counts": {"tables": report["tables"], "rag": report["rag"]},
            "finished_at": datetime.now(timezone.utc).isoformat(), "error_message": None,
        }], "batch_key")
        return report
    except Exception as exc:
        store.upsert("claim_policy_import_batches", [{
            **plan.batch, "status": BatchStatus.FAILED.value, "finished_at": datetime.now(timezone.utc).isoformat(),
            "error_message": f"{type(exc).__name__}: {str(exc)[:500]}",
        }], "batch_key")
        raise


def sync_claim_policy_graph(store: CorpusStore, neo4j, read_approved_claims=None) -> dict[str, int]:
    """以 Supabase（canonical）讀回的 claim_policy_* 資料 MERGE 到 Neo4j；可重複執行。"""
    sources = store.fetch("claim_policy_sources", ["id", "source_id", "source_registry_id", "document_title",
                                                   "is_official", "source_url", "status", "evidence_use"])
    rules = store.fetch("claim_policy_rules", ["id", "rule_id", "claim_policy_source_id", "policy_effect", "rule_category",
                                               "claim_category", "status", "review_status", "evidence_use",
                                               "article_or_attachment"])
    functions = store.fetch("health_function_catalog", ["id", "function_id", "official_function_name", "catalog_source_id",
                                                        "evaluation_source_id", "status", "review_status", "evidence_use"])
    links = store.fetch("health_function_historical_sources", ["health_function_id", "claim_policy_source_id"])
    for rows in (sources, rules, functions, links):
        for r in rows:
            for k, v in r.items():
                if k.endswith("id") and v is not None:
                    r[k] = str(v)
    approved = read_approved_claims() if read_approved_claims else []
    return write_graph(neo4j, build_graph_rows(sources, rules, functions, links, approved))


# ---------------------------------------------------------------------------- Supabase adapter


class SupabaseCorpusStore:
    """CorpusStore 的 Supabase 實作。read_only=True 時拒絕寫入，缺表（0003 未套用）視為空表並記錄。"""

    PAGE = 1000

    def __init__(self, service, *, read_only: bool = True):
        self._db = service
        self.read_only = read_only
        self.missing_tables: set[str] = set()

    def fetch(self, table, columns, eq=None):
        from app.schemas.enums import ErrorCode
        from app.services.errors import ServiceError

        rows: list[dict[str, Any]] = []
        start = 0
        try:
            while True:
                query = self._db.table(table).select(",".join(columns))
                for column, value in (eq or {}).items():
                    query = query.eq(column, value)
                page = self._db.select(query.range(start, start + self.PAGE - 1))
                rows.extend(page)
                if len(page) < self.PAGE:
                    return rows
                start += self.PAGE
        except ServiceError as exc:
            if self.read_only and exc.code == ErrorCode.SCHEMA_NOT_INITIALIZED:
                self.missing_tables.add(table)
                return []
            raise

    def upsert(self, table, rows, on_conflict):
        if self.read_only:
            raise RuntimeError("read-only store 不允許寫入")
        self._db.upsert(table, rows, on_conflict=on_conflict)

    def update(self, table, values, match):
        if self.read_only:
            raise RuntimeError("read-only store 不允許寫入")
        self._db.update(table, values, match)
