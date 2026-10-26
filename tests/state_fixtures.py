"""持久化 store 測試用的 synthetic RequestRecord（TEST_ONLY 資料）。"""

import uuid
from datetime import datetime, timezone

from app.agents.reporter_agent import compose_payload
from app.agents.verification_agent import VerificationAgent
from app.response.store import RequestRecord
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import ClaimOrigin, ClaimSemanticCategory, InputType
from app.schemas.user_request import MediaInput, RawUserInput
from tests.builders import claim, request
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore

PRODUCT = "虛構牌晶亮測試膠囊"


def rich_record(request_id: str | None = None, owner_id: str | None = "U_TEST_ONLY", summary: str | None = None,
                with_media: bytes | None = None, created_at: datetime | None = None) -> RequestRecord:
    rid = request_id or str(uuid.uuid4())
    req = request(product_candidates=[PRODUCT], request_id=rid, claims=[
        claim("可以治療某疾病", ClaimSemanticCategory.MEDICAL_EFFICACY, ClaimOrigin.ADVERTISEMENT_CLAIM)])
    bundle = VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), FakeLLM()).verify(req)
    risk = RiskEngine().assess(req, bundle)
    payload = compose_payload(req, bundle, risk)
    if summary is not None:
        payload = payload.model_copy(update={"summary": summary})
    media = [MediaInput(kind="image", content=with_media, mime_type="image/jpeg", source_id="TEST_ONLY_MSG")] if with_media else []
    raw = RawUserInput(input_type=InputType.IMAGE if media else InputType.TEXT, text=f"TEST_ONLY {PRODUCT} 有效嗎", media=media)
    kw = {"created_at": created_at} if created_at else {}
    return RequestRecord(request_id=rid, owner_id=owner_id, raw_input=raw, response=payload, normalized_request=req,
                         evidence=bundle, risk=risk, query_label="TEST_ONLY 標籤",
                         received_at=datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc), **kw)


def build_service(extractions, store, pipeline=None, timeout: float = 5.0):
    """以指定 RequestStore 建立 AdaptiveResponseService（TEST_ONLY 假 LLM / 假資料來源）。"""
    from app.agents.intake_agent import IntakeAgent
    from app.agents.reporter_agent import ReporterAgent
    from app.pipeline import AnalysisPipeline
    from app.response.service import AdaptiveResponseService
    from app.schemas.user_request import IntakeExtraction

    llm = FakeLLM(structured={IntakeExtraction: lambda _: extractions.pop(0)})
    pipeline = pipeline or AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    return AdaptiveResponseService(pipeline, ReporterAgent(llm), store, timeout_seconds=timeout)


class DictStateBackend:
    """測試用：以 dict 實作 StateBackend（與 SupabaseStateBackend 同一個介面）；broken=True 模擬後端故障。"""

    def __init__(self):
        self.tables: dict[str, dict[tuple, dict]] = {}
        self.broken = False

    def _check(self):
        from app.state.errors import StateUnavailableError
        if self.broken:
            raise StateUnavailableError("simulated backend outage")

    def insert_if_absent(self, table, row, conflict_columns):
        self._check()
        key = tuple(row[c] for c in conflict_columns)
        rows = self.tables.setdefault(table, {})
        if key in rows:
            return False
        rows[key] = dict(row)
        return True

    def select_one(self, table, match):
        self._check()
        for row in self.tables.get(table, {}).values():
            if all(row.get(k) == v for k, v in match.items()):
                return dict(row)
        return None

    def delete_expired(self, table, match, now):
        self._check()
        rows = self.tables.get(table, {})
        for key, row in list(rows.items()):
            if all(row.get(k) == v for k, v in match.items()) and datetime.fromisoformat(row["expires_at"]) <= now:
                del rows[key]

    def update_if(self, table, match, values):
        self._check()
        for row in self.tables.get(table, {}).values():
            if all(row.get(k) == v for k, v in match.items()):
                row.update(values)
                return True
        return False

    def delete(self, table, match):
        self._check()
        rows = self.tables.get(table, {})
        for key, row in list(rows.items()):
            if all(row.get(k) == v for k, v in match.items()):
                del rows[key]
