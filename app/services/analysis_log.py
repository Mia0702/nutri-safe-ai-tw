"""把每次分析的 request / evidence 紀錄寫入 Supabase（analysis_requests / evidence_logs / observed_claims）。"""

from app.schemas.analysis import AnalysisResult
from app.services.supabase_service import SupabaseService


class SupabaseAnalysisLogWriter:
    def __init__(self, service: SupabaseService):
        self._db = service

    def write(self, result: AnalysisResult, channel: str, line_user_record_id: str | None = None) -> None:
        """line_user_record_id 是 line_users.id（內部 UUID），不是 LINE 平台的 userId。"""
        req = result.normalized_request
        bundle = result.evidence
        risk = result.risk
        self._db.insert(
            "analysis_requests",
            [{
                "id": result.request_id,
                "line_user_record_id": line_user_record_id,
                "channel": channel,
                "input_type": result.input_type or (req.input_type.value if req else "text"),
                "intent": req.intent.value if req else None,
                "normalized_request": req.model_dump(mode="json") if req else None,
                "product_verification": bundle.product_match.status.value if bundle else None,
                "evidence_strength": bundle.evidence_strength.value if bundle else None,
                "risk_level": risk.risk_level.value if risk else None,
                "risk": risk.model_dump(mode="json") if risk else None,
                "final_answer": result.final_answer,
                "error_codes": sorted({e.code.value for e in result.errors}),
                "duration_ms": result.duration_ms,
            }],
        )
        if bundle is None:
            return
        rows = [{
            "request_id": result.request_id,
            "evidence_kind": "product_match",
            "evidence": bundle.product_match.model_dump(mode="json"),
            "source_name": bundle.product_match.best_match.product.source.source_name if bundle.product_match.best_match else None,
            "source_url": bundle.product_match.best_match.product.source.source_url if bundle.product_match.best_match else None,
            "is_official": bundle.product_match.best_match.product.source.is_official if bundle.product_match.best_match else None,
        }]
        groups = (
            ("approved_claim", bundle.approved_claims),
            ("warning", bundle.warnings),
            ("illegal_ad", bundle.illegal_ad_evidence),
            ("graph", bundle.graph_evidence),
            ("rag", bundle.rag_evidence),
            ("official_web", bundle.official_web_evidence),
        )
        for kind, items in groups:
            for item in items:
                rows.append({
                    "request_id": result.request_id,
                    "evidence_kind": kind,
                    "evidence": item.model_dump(mode="json"),
                    "source_name": item.source.source_name,
                    "source_url": item.source.source_url,
                    "is_official": item.source.is_official,
                })
        for item in bundle.rejected_evidence:
            rows.append({
                "request_id": result.request_id,
                "evidence_kind": "rejected_evidence",
                "evidence": item.model_dump(mode="json"),
                "source_name": item.source.source_name,
                "source_url": item.source.source_url,
                "is_official": item.source.is_official,
            })
        for call in bundle.tools_called:
            rows.append({"request_id": result.request_id, "evidence_kind": "tool_call", "evidence": call.model_dump(mode="json")})
        self._db.insert("evidence_logs", rows)

        if req and req.claims:
            statuses = {c.claim_index: c.status.value for c in bundle.claim_assessment}
            self._db.insert(
                "observed_claims",
                [{
                    "request_id": result.request_id,
                    "claim_index": i,
                    "claim_text": c.claim_text,
                    "origin": c.origin.value,
                    "semantic_category": c.semantic_category.value,
                    "assessment_status": statuses.get(i),
                } for i, c in enumerate(req.claims)],
            )
