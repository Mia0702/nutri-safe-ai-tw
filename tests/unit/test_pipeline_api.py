import json
import logging

from fastapi.testclient import TestClient

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.config.settings import Settings, get_settings
from app.dependencies import get_pipeline
from app.main import app
from app.observability import JsonFormatter
from app.pipeline import UNDERSTANDING_FAILED_TEXT, AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import ClaimOrigin, ClaimSemanticCategory, InputType, Intent
from app.schemas.user_request import IntakeExtraction, RawUserInput
from tests.builders import claim, extraction
from app.services.context_store import InMemoryConversationStore
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore


def build(ex, fail: set[str] | None = None, store=None) -> AnalysisPipeline:
    llm = FakeLLM(structured={IntakeExtraction: ex}, fail=fail)
    return AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(),
        reporter=ReporterAgent(llm),
        conversation_store=store,
    )


def client_with(pipeline: AnalysisPipeline, env: str = "development") -> TestClient:
    app.dependency_overrides[get_pipeline] = lambda: pipeline
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, app_env=env)
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_debug_analyze_returns_full_debug_structure():
    ex = extraction(
        intent=Intent.CLAIM_VERIFICATION, product_candidates=["虛構牌晶亮測試膠囊"],
        claims=[claim("可以治療某疾病", ClaimSemanticCategory.MEDICAL_EFFICACY, ClaimOrigin.ADVERTISEMENT_CLAIM)],
    )
    resp = client_with(build(ex)).post("/api/debug/analyze", json={"type": "text", "content": "TEST_ONLY 問題"})
    assert resp.status_code == 200
    body = resp.json()
    for key in ("request_id", "normalized_request", "tools_called", "product_verification",
                "evidence", "claim_assessment", "risk", "response", "final_answer", "errors"):
        assert key in body
    assert body["risk"]["risk_level"] == "HIGH"
    assert body["product_verification"]["status"] == "VERIFIED"
    assert body["final_answer"].startswith("🔴")
    assert body["response"]["response_type"] == "RISK" and body["response"]["risk_level"] == "HIGH"


def test_debug_analyze_rejects_unimplemented_input_types():
    resp = client_with(build(extraction())).post("/api/debug/analyze", json={"type": "image", "content": "x"})
    assert resp.status_code == 501
    assert resp.json()["errors"][0]["code"] == "UNSUPPORTED_INPUT_TYPE"


def test_debug_endpoint_hidden_in_production():
    resp = client_with(build(extraction()), env="production").post("/api/debug/analyze", json={"type": "text", "content": "x"})
    assert resp.status_code == 404


def test_pipeline_intake_failure_returns_structured_error():
    result = build(extraction(), fail={"parse_structured"}).run(RawUserInput(input_type=InputType.TEXT, text="x"))
    assert result.final_answer == UNDERSTANDING_FAILED_TEXT
    assert result.response.response_type == "ERROR" and result.response.available_actions == ["retry"]
    assert result.errors[0].code == "TIMEOUT"
    assert result.risk is None


def test_json_log_formatter_redacts_secrets_and_drops_unknown_fields():
    secret = "sk-TEST_ONLY-1234567890"
    fmt = JsonFormatter([secret])
    record = logging.LogRecord("nutri_safe_ai", logging.INFO, __file__, 1, f"token={secret}", None, None)
    record.request_id = "r1"
    record.authorization = secret  # 非白名單欄位
    out = json.loads(fmt.format(record))
    assert secret not in json.dumps(out)
    assert out["request_id"] == "r1" and "authorization" not in out


def test_conversation_context_resolves_reference_in_next_turn():
    """第一輪提到產品，第二輪用「它」→ 由前文解析，不是 missing context。"""
    store = InMemoryConversationStore()
    first = extraction(product_candidates=["虛構牌晶亮測試膠囊"])

    def second(input_text):
        payload = json.loads(input_text)
        names = [e["name"] for e in payload["known_context_entities"]]
        return extraction(refers_to_unspecified_product=True, context_resolved_names=names[:1],
                          claims=[claim("它可以治療某疾病", ClaimSemanticCategory.MEDICAL_EFFICACY, ClaimOrigin.USER_QUESTION)])

    build(first, store=store).run(RawUserInput(input_type=InputType.TEXT, text="第一句"), conversation_id="conv-1")
    result = build(second, store=store).run(RawUserInput(input_type=InputType.TEXT, text="那它呢"), conversation_id="conv-1")
    req = result.normalized_request
    assert req.product_reference_status == "RESOLVED_FROM_CONTEXT"
    assert req.context_entities_used[0].entity_id == "p-1"
    assert result.product_verification.status == "VERIFIED"
    assert result.risk.risk_level != "HIGH"


def test_without_conversation_id_reference_is_missing_context():
    ex = extraction(refers_to_unspecified_product=True,
                    claims=[claim("這個可以治療某疾病", ClaimSemanticCategory.MEDICAL_EFFICACY, ClaimOrigin.USER_QUESTION)])
    result = build(ex, store=InMemoryConversationStore()).run(RawUserInput(input_type=InputType.TEXT, text="這個呢"))
    assert result.normalized_request.product_reference_status == "MISSING_CONTEXT"
    assert result.risk.risk_level == "MEDIUM"
