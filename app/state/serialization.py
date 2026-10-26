"""RequestRecord <-> JSON-safe dict（明確的 schema 驗證；不使用 pickle）。

* 原始圖片 / 語音 bytes 一律不持久化（MediaInput.content 本來就不輸出）；raw_input.media 只留 metadata
  （kind / mime_type / filename / source_id），讀回時 raw_input.media 為空、record.media_omitted = True。
* owner 不放在 record JSON，由 owner_key 欄位承載；讀回的 record.owner_id 即 owner_key。
* 讀到未知版本 / 缺欄位 / 型別錯誤 → StateSerializationError（fail closed），不回傳部分資料。
"""

from typing import Any

from pydantic import ValidationError

from app.response.store import RequestRecord
from app.schemas.evidence import EvidenceBundle
from app.schemas.response import ResponsePayload
from app.schemas.risk import RiskAssessment
from app.schemas.user_request import NormalizedUserRequest, RawUserInput
from app.state.errors import StateSerializationError

SCHEMA_VERSION = 1
_KEYS = {"schema_version", "raw_input", "media_omitted", "response", "normalized_request", "evidence", "risk"}


def serialize_record(record: RequestRecord) -> dict[str, Any]:
    raw = record.raw_input.model_copy(update={"media": []}).model_dump(mode="json")
    media_meta = [{"kind": m.kind, "mime_type": m.mime_type, "filename": m.filename, "source_id": m.source_id}
                  for m in record.raw_input.media]
    return {
        "schema_version": SCHEMA_VERSION,
        "raw_input": raw,
        "media_omitted": media_meta,
        "response": record.response.model_dump(mode="json"),
        "normalized_request": record.normalized_request.model_dump(mode="json") if record.normalized_request else None,
        "evidence": record.evidence.model_dump(mode="json") if record.evidence else None,
        "risk": record.risk.model_dump(mode="json") if record.risk else None,
    }


def deserialize_record(payload: Any, *, request_id: str, owner_key: str | None, query_label: str | None,
                       received_at, created_at) -> RequestRecord:
    try:
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION or set(payload) != _KEYS:
            raise ValueError("unknown schema")
        raw = RawUserInput.model_validate(payload["raw_input"])
        if raw.media:
            raise ValueError("persisted raw_input must not contain media")
        had_media = bool(payload["media_omitted"])
        def opt(model, key):
            return model.model_validate(payload[key]) if payload[key] is not None else None
        return RequestRecord(
            request_id=request_id, owner_id=owner_key or None, raw_input=raw,
            response=ResponsePayload.model_validate(payload["response"]),
            normalized_request=opt(NormalizedUserRequest, "normalized_request"),
            evidence=opt(EvidenceBundle, "evidence"), risk=opt(RiskAssessment, "risk"),
            query_label=query_label, received_at=received_at, created_at=created_at, media_omitted=had_media)
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise StateSerializationError(f"持久化的 request record 無法還原：{type(exc).__name__}") from exc
