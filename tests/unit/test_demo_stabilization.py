"""Real Phone Demo Stabilization（P0）回歸測試。

* 核准功效詢問：問題本身不是待查證的宣稱；直接以官方核准功效回答。
* Structured evidence fast path：簡單產品 / 成分 / 核准功效問題不呼叫 planner / RAG / 證據評估 / 官方網站。
  （檢查 tool-call path，不檢查秒數，避免 flaky。）
* LINE audio / URL / queue：任何失敗都只回一張 ERROR card、不會永久 PROCESSING、不會晚到補發。

全部使用 TEST_ONLY fake，不呼叫外部服務。
"""

import threading
import time

import httpx
import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import FAST_PATH_MODE, VerificationAgent
from app.line.client import LineApiError, LineMessagingClient
from app.line.request_queue import PerUserRequestQueue, RequestState
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import InputType, ClaimAssessmentStatus, ClaimOrigin, ClaimSemanticCategory, ErrorCode, Intent, ResponseType, RiskLevel
from app.schemas.user_request import IntakeExtraction, RawUserInput
from app.services.errors import ServiceError
from app.tools import input_tools
from app.tools.input_tools import fetch_url_content
from tests.builders import claim, extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.unit.test_line_webhook import PRODUCT_1, USER, FakeLine, build, event, send

PRODUCT_NO_CLAIMS = "另一虛構保健測試錠"  # TEST_ONLY：沒有核准功效 / 成分的產品
SLOW_PATH_TOOLS = {"plan_tool_calls", "search_rag", "assess_evidence", "search_tw_official_web"}
MED = ClaimSemanticCategory.MEDICAL_EFFICACY


def run(ex: IntakeExtraction, text="TEST_ONLY 問題"):
    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    return pipeline.run(RawUserInput(input_type=InputType.TEXT, text=text)), llm


def tool_path(result) -> set[str]:
    return {t.tool_name for t in result.evidence.tools_called}


# ============================================================ P0-1 efficacy inquiry 語意


def test_open_efficacy_question_answers_with_approved_claims_not_unsupported():
    """「<產品>的核准功效是什麼？」：即使 LLM 把問題抽成 USER_QUESTION 命題，也不得回「官方不支持這個說法」。"""
    open_question = claim(f"{PRODUCT_1}的核准功效是什麼", origin=ClaimOrigin.USER_QUESTION)
    result, _ = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_1], claims=[open_question]))
    assert result.normalized_request.claims == []
    assert result.claim_assessment == []
    payload = result.response
    assert payload.response_type == ResponseType.PRODUCT_INFO
    text = payload.plain_text()
    assert "找不到官方資料支持" not in text and "不支持" not in text
    assert "官方核准的保健功效" in payload.summary
    assert any("測試功效甲" in p and "有助於維持測試機能甲" in p for p in payload.key_points)
    assert "search_tw_official_web" not in tool_path(result)


def test_user_question_about_disease_stays_claim_result():
    q = claim(f"{PRODUCT_1}可以治療糖尿病", MED, ClaimOrigin.USER_QUESTION, condition="糖尿病")
    result, _ = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_1], claims=[q]))
    assert [c.origin for c in result.normalized_request.claims] == [ClaimOrigin.USER_QUESTION]
    assert result.response.response_type == ResponseType.CLAIM_RESULT
    # 命題（若作為產品功效宣稱）是醫療效能 → claim risk HIGH；但仍是提問，不是產品風險燈
    assert result.claim_assessment[0].status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert result.claim_assessment[0].origin == ClaimOrigin.USER_QUESTION
    assert result.response.risk_level is None and result.risk.risk_level != RiskLevel.HIGH
    assert result.response.claim_risk_level == RiskLevel.HIGH
    # 已確認產品：一併列出官方核准的功效，讓使用者知道官方核准的是什麼
    assert any("測試功效甲" in p for p in result.response.key_points)


def test_advertisement_disease_claim_stays_high_risk_even_in_efficacy_intent():
    ad = claim(f"{PRODUCT_1}可以治療糖尿病", MED, ClaimOrigin.ADVERTISEMENT_CLAIM, condition="糖尿病")
    result, _ = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_1], claims=[ad]))
    assert result.claim_assessment[0].status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert result.response.response_type == ResponseType.RISK
    assert result.risk.risk_level == RiskLevel.HIGH
    assert result.evidence.planner_mode != FAST_PATH_MODE  # 廣告宣稱一定走完整查證


def test_specific_function_question_is_kept_as_claim():
    q = claim(f"{PRODUCT_1}可以幫助睡眠", origin=ClaimOrigin.USER_QUESTION, function="睡眠")
    result, _ = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_1], claims=[q]))
    assert len(result.normalized_request.claims) == 1
    assert result.response.response_type == ResponseType.CLAIM_RESULT


# ============================================================ P0-2 structured evidence fast path


@pytest.mark.parametrize("intent", [Intent.PRODUCT_VERIFICATION, Intent.INGREDIENT_INQUIRY, Intent.EFFICACY_INQUIRY])
def test_simple_verified_product_questions_use_fast_path(intent):
    result, llm = run(extraction(intent=intent, product_candidates=[PRODUCT_1]))
    bundle = result.evidence
    assert bundle.planner_mode == FAST_PATH_MODE
    assert not (tool_path(result) & SLOW_PATH_TOOLS)
    assert not llm.called("plan_step") and not llm.called("web_search_structured") and not llm.called("embed")
    assert "search_illegal_ad_cases" in tool_path(result)  # 安全檢查不因 fast path 省略
    assert bundle.product_match.status.value == "VERIFIED"
    assert bundle.approved_claims and bundle.ingredients
    assert result.response.response_type != ResponseType.ERROR


def test_fast_path_falls_back_when_requested_detail_is_missing():
    """核准功效問題但結構化資料中沒有核准功效：證據不足，回到完整查證。"""
    result, llm = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_NO_CLAIMS]))
    assert result.evidence.planner_mode != FAST_PATH_MODE
    assert llm.called("plan_step")


def test_product_not_found_uses_full_path_with_official_web():
    result, llm = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=["完全不存在的TEST_ONLY產品"]))
    assert result.evidence.planner_mode != FAST_PATH_MODE
    assert "search_tw_official_web" in tool_path(result)


@pytest.mark.parametrize("intent", [Intent.SAFETY_INQUIRY, Intent.CLAIM_VERIFICATION])
def test_safety_and_claim_intents_never_use_fast_path(intent):
    result, llm = run(extraction(intent=intent, product_candidates=[PRODUCT_1]))
    assert result.evidence.planner_mode != FAST_PATH_MODE
    assert llm.called("plan_step")


def test_fast_path_does_not_hide_source_failures():
    class FailingAds(InMemoryOfficialRepo):
        def search_illegal_ad_cases(self, terms, limit):
            raise ServiceError(ErrorCode.UNAVAILABLE, "TEST_ONLY down", "supabase")

    llm = FakeLLM(structured={IntakeExtraction: extraction(intent=Intent.PRODUCT_VERIFICATION,
                                                           product_candidates=[PRODUCT_1])})
    verifier = VerificationAgent(FailingAds(), InMemoryGraphRepo(), InMemoryRagStore(), llm)
    req = IntakeAgent(llm).understand("req-x", RawUserInput(input_type=InputType.TEXT, text="TEST_ONLY"))
    bundle = verifier.verify(req)
    assert any(t.tool_name == "search_illegal_ad_cases" and not t.ok for t in bundle.tools_called)
    assert bundle.planner_mode != FAST_PATH_MODE  # 來源失敗＝證據不完整，回到完整流程
    assert any(e.source == "supabase" for e in bundle.errors)  # 失敗如實記錄


# ============================================================ P0-3 / P0-4 LINE audio / URL / queue


class RaisingContentLine(FakeLine):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def get_content(self, message_id):
        raise self.exc


@pytest.mark.parametrize("exc", [
    LineApiError(408, "content"),
    httpx.ReadTimeout("TEST_ONLY"),  # 舊版只接 LineApiError / OSError：httpx 逾時會讓使用者完全沒回覆
    RuntimeError("TEST_ONLY unexpected"),
])
def test_audio_download_failure_sends_one_error_card_and_releases_queue(exc):
    handler, line, *_ = build(line=RaisingContentLine(exc))
    a, b = send(handler, event("audio", message_id="aud-1"), event(text=f"{PRODUCT_1} 有登記嗎"))
    assert len(line.sent) == 2
    assert "沒有下載成功" in line.dumps(0) and PRODUCT_1 in line.dumps(1)
    assert handler.queue.state(a) == RequestState.DONE and handler.queue.state(b) == RequestState.DONE


def test_transcription_timeout_sends_one_error_card_and_next_request_runs():
    handler, line, *_ = build()
    llm = handler._service._pipeline._intake._media

    def timeout(**kw):
        raise ServiceError(ErrorCode.TIMEOUT, "TEST_ONLY transcription timeout", "openai")

    llm.transcribe_audio = timeout
    send(handler, event("audio", message_id="aud-1"), event(text=f"{PRODUCT_1} 有登記嗎"))
    assert len(line.sent) == 2
    first = line.sent[0][2]
    assert first["type"] == "flex" and "沒有完成查詢" in line.dumps(0)
    assert PRODUCT_1 in line.dumps(1)


def test_unexpected_service_exception_still_replies_once():
    handler, line, *_ = build()

    def boom(*a, **kw):
        raise RuntimeError("TEST_ONLY")

    handler._service.respond = boom
    rid = send(handler, event(text=PRODUCT_1))[0]
    assert len(line.sent) == 1 and "沒有完成查詢" in line.dumps()
    assert handler.queue.state(rid) == RequestState.DONE


def test_url_fetch_error_still_replies_once():
    handler, line, *_ = build()

    def failing(url):
        raise ServiceError(ErrorCode.TIMEOUT, "讀取網頁逾時", "fetch_url_content")

    handler._service._pipeline._intake._fetch_url = failing
    send(handler, event(text="https://example.com/ad"))
    assert len(line.sent) == 1 and line.sent[0][2]["type"] == "flex"


def test_queue_watchdog_releases_hung_job_and_discards_late_result():
    """job 卡住超過 watchdog：送一張逾時卡、釋放 queue；之後卡住的 job 才完成也不會再送第二則。"""
    release = threading.Event()
    handler, line, *_ = build()
    handler._queue = PerUserRequestQueue(job_timeout_seconds=0.3)
    real_respond = handler._service.respond

    def hang(raw, **kw):
        if "HANG" in raw.text:
            release.wait(5)
        return real_respond(raw, **kw)

    handler._service.respond = hang
    a, b = send(handler, event(text=f"HANG {PRODUCT_1}"), event(text=f"{PRODUCT_1} 有登記嗎"))
    assert handler.queue.state(a) == RequestState.TIMEOUT and handler.queue.state(b) == RequestState.DONE
    assert len(line.sent) == 2 and "花太久時間" in line.dumps(0)
    release.set()
    time.sleep(0.5)
    assert len(line.sent) == 2  # 晚到結果不補發


def test_queue_releases_even_on_base_exception():
    q = PerUserRequestQueue(max_workers=2)
    done = []

    def bad():
        raise KeyboardInterrupt

    q.submit(USER, "r1", bad)
    q.submit(USER, "r2", lambda: done.append("r2"))
    assert q.wait_idle(5)
    assert done == ["r2"] and q.state("r1") != RequestState.PROCESSING


# ---------------------------------------------------------------- LINE content download（MockTransport）


def _client(handler_fn) -> LineMessagingClient:
    from pydantic import SecretStr

    return LineMessagingClient(SecretStr("TEST_ONLY_token"), http=httpx.Client(transport=httpx.MockTransport(handler_fn)))


def test_content_202_waits_for_transcoding_then_downloads(monkeypatch):
    monkeypatch.setattr("app.line.client.TRANSCODING_POLL_SECONDS", 0.01)
    state = {"ready": False}

    def fn(req: httpx.Request):
        if req.url.path.endswith("/transcoding"):
            state["ready"] = True
            return httpx.Response(200, json={"status": "succeeded"})
        if not state["ready"]:
            return httpx.Response(202)
        return httpx.Response(200, content=b"TEST_ONLY audio", headers={"content-type": "audio/x-m4a"})

    assert _client(fn).get_content("m1") == (b"TEST_ONLY audio", "audio/x-m4a")


def test_content_still_transcoding_gives_up_within_deadline(monkeypatch):
    monkeypatch.setattr("app.line.client.TRANSCODING_POLL_SECONDS", 0.01)

    def fn(req: httpx.Request):
        if req.url.path.endswith("/transcoding"):
            return httpx.Response(200, json={"status": "processing"})
        return httpx.Response(202)

    started = time.monotonic()
    with pytest.raises(LineApiError):
        _client(fn).get_content("m1", deadline_seconds=0.2)
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("response, exc", [
    (lambda req: httpx.Response(200, content=b""), None),  # 空內容不當成功
    (None, httpx.ConnectTimeout("TEST_ONLY")),
])
def test_content_errors_become_line_api_error(response, exc):
    def fn(req):
        if exc:
            raise exc
        return response(req)

    with pytest.raises(LineApiError):
        _client(fn).get_content("m1")


# ---------------------------------------------------------------- URL fetch 有上限


def _fetch(fn, url="https://example.com/a"):
    client = httpx.Client(transport=httpx.MockTransport(fn))
    return fetch_url_content(url, client=client, resolve=lambda host: ["93.184.216.34"])


def test_url_unsupported_content_type_fails_fast():
    with pytest.raises(ServiceError) as err:
        _fetch(lambda req: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))
    assert err.value.code == ErrorCode.INVALID_INPUT


def test_url_redirect_loop_is_bounded():
    with pytest.raises(ServiceError):
        _fetch(lambda req: httpx.Response(302, headers={"location": "https://example.com/loop"}))


def test_url_slow_body_hits_total_deadline(monkeypatch):
    monkeypatch.setattr(input_tools, "URL_TOTAL_DEADLINE_SECONDS", 0.2)

    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(50):
                time.sleep(0.05)
                yield b"<p>TEST_ONLY</p>"

    with pytest.raises(ServiceError) as err:
        _fetch(lambda req: httpx.Response(200, stream=Slow(), headers={"content-type": "text/html"}))
    assert err.value.code == ErrorCode.TIMEOUT


def test_url_body_size_is_capped():
    big = b"<p>" + b"a" * (input_tools.MAX_URL_BYTES * 2) + b"</p>"
    out = _fetch(lambda req: httpx.Response(200, content=big, headers={"content-type": "text/html"}))
    assert len(out.text) <= input_tools.MAX_URL_TEXT_CHARS
