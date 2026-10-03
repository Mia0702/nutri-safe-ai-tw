"""URL Verification Stabilization 回歸測試。

* URL source role：只依網域 allowlist 判斷（官方 / 非官方 / 無法判斷），頁面自稱官方不算。
* Claim role：官方監管網頁上的內容是官方來源陳述（不是待查證宣稱）；非官方網頁內容仍是外部宣稱。
* Evidence scope / provenance：官方網頁作為證據時保留網址 / 網域 / 標題 / 取得時間，範圍依能否對應到已確認產品。
* URL routing：URL evidence early-stop、official web fallback 條件、assess_evidence 去重。
  （檢查 tool-call path 與呼叫次數，不檢查秒數，避免 flaky。）

全部使用虛構網域 / 產品 / 宣稱（TEST_ONLY fake），不呼叫外部服務、不使用任何真實測試網址。
官方網址只使用 allowlist 網域底下的虛構子網域與路徑。
"""

import json

import httpx
import pytest

from app.agents.intake_agent import IntakeAgent, apply_url_source_roles
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import (
    ASSESSMENT_STEP,
    FAST_PATH_MODE,
    URL_EVIDENCE_MODE,
    VerificationAgent,
    _EvidenceAssessment,
    stage_timings_ms,
)
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimContentSource,
    ClaimOrigin,
    ClaimSemanticCategory,
    EvidenceScope,
    InputType,
    Intent,
    ResponseType,
    RiskLevel,
    UrlSourceRole,
)
from app.schemas.user_request import IntakeExtraction, RawUserInput, UrlSource
from app.tools.input_tools import NOTE_OFFICIAL_URL, NOTE_UNOFFICIAL_URL, InputToolOutput, fetch_url_content
from app.tools.url_source import classify_url_source
from tests.builders import claim, extraction, request
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore, web_result

PRODUCT = "虛構牌晶亮測試膠囊"  # TEST_ONLY（synthetic fixture，有核准功效與警語）
PERMIT = "測試健食字第Z00001號"
UNKNOWN_PRODUCT = "完全不存在的虛構測試飲"
OFFICIAL_URL = "https://test-only.fda.gov.tw/TEST_ONLY/product?id=1"
SHOP_URL = "https://shop.example-mall.invalid/item/1"
UNKNOWN_URL = "https://203.0.113.10/item/1"  # TEST-NET（文件用位址）
MED = ClaimSemanticCategory.MEDICAL_EFFICACY
URL = ClaimContentSource.URL_CONTENT
SLOW_TOOLS = {"plan_tool_calls", "search_tw_official_web"}


def _page(url, text, title="TEST_ONLY 頁面"):
    return lambda requested: InputToolOutput(tool_name="fetch_url_content", text=text, source_url=url, title=title)


def run(ex: IntakeExtraction, fetcher, text="", url=OFFICIAL_URL, llm=None):
    llm = llm or FakeLLM()
    llm.structured[IntakeExtraction] = ex
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm, url_fetcher=fetcher),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    raw = RawUserInput(input_type=InputType.URL if not text else InputType.MIXED, text=text, urls=[url])
    return pipeline.run(raw), llm


def tools(result) -> list[str]:
    return [t.tool_name for t in result.evidence.tools_called]


def official_page_text():
    return f"TEST_ONLY 健康食品資料查詢\n品名：{PRODUCT}\n許可證字號：{PERMIT}\n保健功效：有助於維持測試機能甲"


# ============================================================ 1. URL source role


@pytest.mark.parametrize("url,role", [
    ("https://test-only.fda.gov.tw/a", UrlSourceRole.OFFICIAL_REGULATORY_SOURCE),
    ("https://data.fda.gov.tw/x", UrlSourceRole.OFFICIAL_REGULATORY_SOURCE),
    ("http://www.mohw.gov.tw/", UrlSourceRole.OFFICIAL_REGULATORY_SOURCE),
    ("https://shop.example-mall.invalid/p", UrlSourceRole.NON_OFFICIAL_EXTERNAL_SOURCE),
    # 網域字面上含官方網域但不是其子網域 → 不是官方
    ("https://fda.gov.tw.example-mall.invalid/p", UrlSourceRole.NON_OFFICIAL_EXTERNAL_SOURCE),
    ("https://example-mall.invalid/fda.gov.tw/p", UrlSourceRole.NON_OFFICIAL_EXTERNAL_SOURCE),
    # 無法可靠判斷：IP、allowlist 以外的政府網域、非 http(s)、無法解析
    ("https://203.0.113.10/p", UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE),
    ("https://notfda.gov.tw/p", UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE),
    ("https://other-agency.gov.tw/p", UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE),
    ("ftp://test-only.fda.gov.tw/p", UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE),
    ("not a url", UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE),
    (None, UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE),
])
def test_classify_url_source_uses_domain_allowlist_only(url, role):
    assert classify_url_source(url) == role


def _client(routes):
    def handler(req):
        return routes[str(req.url)]
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_role_uses_final_url_after_redirect_and_keeps_title():
    """官方網址重新導向到非官方頁 → 依實際讀取的頁面判斷為非官方。"""
    client = _client({
        "https://test-only.fda.gov.tw/r": httpx.Response(302, headers={"location": SHOP_URL}),
        SHOP_URL: httpx.Response(200, headers={"content-type": "text/html"},
                                 text="<title>TEST_ONLY 商品頁</title><p>官方認證 TEST_ONLY</p>"),
    })
    out = fetch_url_content("https://test-only.fda.gov.tw/r", client=client, resolve=lambda h: ["93.184.216.34"])
    assert out.source_url == SHOP_URL and out.notes == [NOTE_UNOFFICIAL_URL]
    assert out.title == "TEST_ONLY 商品頁"


def test_fetch_official_page_note():
    client = _client({OFFICIAL_URL: httpx.Response(200, headers={"content-type": "text/html"},
                                                   text="<title>TEST_ONLY 官方</title><p>內容</p>")})
    out = fetch_url_content(OFFICIAL_URL, client=client, resolve=lambda h: ["93.184.216.34"])
    assert out.notes == [NOTE_OFFICIAL_URL]


# ============================================================ 2. Claim role（Agent A）


def _src(url, role, fetched=True):
    return UrlSource(requested_url=url, source_url=url, source_domain="d", source_role=role, fetched=fetched,
                     title="TEST_ONLY", excerpt="TEST_ONLY")


def test_official_page_statements_are_not_claims_to_verify():
    req = request(claims=[
        claim("TEST_ONLY 有助於維持測試機能甲", origin=ClaimOrigin.OFFICIAL_CLAIM),
        claim("TEST_ONLY 可以幫助睡眠嗎", origin=ClaimOrigin.USER_QUESTION),
    ])
    req.claims[0].content_source = URL
    req.claims[1].content_source = ClaimContentSource.USER_TEXT
    req.needs_official_web_search = True
    apply_url_source_roles(req, [_src(OFFICIAL_URL, UrlSourceRole.OFFICIAL_REGULATORY_SOURCE)])
    assert [c.origin for c in req.claims] == [ClaimOrigin.USER_QUESTION]  # 使用者自己的問題保留
    [stmt] = req.official_source_statements
    assert stmt.origin == ClaimOrigin.OFFICIAL_CLAIM and stmt.source_url == OFFICIAL_URL
    assert stmt.source_role == UrlSourceRole.OFFICIAL_REGULATORY_SOURCE
    assert req.needs_official_web_search is False


@pytest.mark.parametrize("origin", [ClaimOrigin.ADVERTISEMENT_CLAIM, ClaimOrigin.THIRD_PARTY_CLAIM])
def test_official_page_content_never_becomes_ad_or_third_party_claim(origin):
    """即使 LLM 把官方頁的內容標成廣告 / 轉傳，也不會進 claims（不會被判成流傳說法）。"""
    req = request(claims=[claim("TEST_ONLY 頁面敘述", origin=origin)])
    req.claims[0].content_source = URL
    apply_url_source_roles(req, [_src(OFFICIAL_URL, UrlSourceRole.OFFICIAL_REGULATORY_SOURCE)])
    assert req.claims == [] and len(req.official_source_statements) == 1


def test_non_official_page_claiming_official_approval_stays_external_claim():
    req = request(claims=[
        claim("TEST_ONLY 通過官方認證可以治療疾病", MED, ClaimOrigin.OFFICIAL_CLAIM),
        claim("TEST_ONLY 可以治療疾病", MED, ClaimOrigin.ADVERTISEMENT_CLAIM),
    ])
    for c in req.claims:
        c.content_source = URL
    apply_url_source_roles(req, [_src(SHOP_URL, UrlSourceRole.NON_OFFICIAL_EXTERNAL_SOURCE)])
    assert [c.origin for c in req.claims] == [ClaimOrigin.THIRD_PARTY_CLAIM, ClaimOrigin.ADVERTISEMENT_CLAIM]
    assert all(c.source_role == UrlSourceRole.NON_OFFICIAL_EXTERNAL_SOURCE and c.source_url == SHOP_URL
               for c in req.claims)
    assert req.official_source_statements == []


def test_url_only_input_attributes_unlabelled_claims_to_page():
    req = request(claims=[claim("TEST_ONLY 頁面敘述", origin=ClaimOrigin.OFFICIAL_CLAIM)])  # content_source 缺漏
    apply_url_source_roles(req, [_src(OFFICIAL_URL, UrlSourceRole.OFFICIAL_REGULATORY_SOURCE)], only_url_content=True)
    assert req.claims == [] and len(req.official_source_statements) == 1


def test_mixed_roles_use_most_conservative_and_never_official():
    req = request(claims=[claim("TEST_ONLY 頁面敘述", origin=ClaimOrigin.OFFICIAL_CLAIM)])
    req.claims[0].content_source = URL
    apply_url_source_roles(req, [_src(OFFICIAL_URL, UrlSourceRole.OFFICIAL_REGULATORY_SOURCE),
                                 _src(UNKNOWN_URL, UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE)])
    assert req.claims[0].origin == ClaimOrigin.THIRD_PARTY_CLAIM
    assert req.claims[0].source_role == UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE
    assert req.official_source_statements == []


def test_intake_payload_labels_official_section_and_roles():
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT])
    result, llm = run(ex, _page(OFFICIAL_URL, official_page_text()))
    payload = json.loads(llm.structured_inputs[0][1])
    assert "【官方網頁內容】" in payload["content"] and "【網頁內容】" not in payload["content"]
    assert payload["url_sources"] == [{"source_url": OFFICIAL_URL,
                                       "source_role": UrlSourceRole.OFFICIAL_REGULATORY_SOURCE.value}]
    [src] = result.normalized_request.url_sources
    assert src.source_domain == "test-only.fda.gov.tw" and src.title and src.retrieved_at


def test_text_payload_has_no_url_fields():
    """文字輸入的語意 payload 不變（modality parity 不受影響）。"""
    llm = FakeLLM(structured={IntakeExtraction: extraction()})
    IntakeAgent(llm).understand("r", RawUserInput(input_type=InputType.TEXT, text="TEST_ONLY"))
    assert "url_sources" not in json.loads(llm.structured_inputs[0][1])


# ============================================================ 3. Official URL（情況 B）


def test_official_url_mapped_to_product_answers_with_official_evidence():
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT], claims=[
        claim(f"{PRODUCT}有助於維持測試機能甲", origin=ClaimOrigin.OFFICIAL_CLAIM)])
    ex.claims[0].content_source = URL
    result, llm = run(ex, _page(OFFICIAL_URL, official_page_text()))
    req, bundle = result.normalized_request, result.evidence
    assert req.claims == [] and len(req.official_source_statements) == 1
    assert bundle.planner_mode == URL_EVIDENCE_MODE
    assert not (set(tools(result)) & SLOW_TOOLS) and ASSESSMENT_STEP not in tools(result)
    assert "search_illegal_ad_cases" in tools(result)
    [page] = [e for e in bundle.official_web_evidence if e.url == OFFICIAL_URL]
    assert page.source.is_official and page.domain == "test-only.fda.gov.tw" and page.source.retrieved_at
    assert page.relevance.evidence_scope == EvidenceScope.PRODUCT_SPECIFIC
    assert OFFICIAL_URL in {s.source_url for s in bundle.all_sources()}
    # 不會被說成流傳說法、不會因官方頁本身被判誇大
    assert result.claim_assessment == []
    assert "RISK_MED_CIRCULATING_CLAIM_ISSUE" not in {h.rule_id for h in result.risk.triggered_rules}
    text = result.response.plain_text()
    assert "流傳" not in text and "誇大" not in text
    assert result.response.response_type == ResponseType.PRODUCT_INFO


def test_official_url_product_verification_uses_fast_path_and_keeps_provenance():
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT])
    result, _ = run(ex, _page(OFFICIAL_URL, official_page_text()))
    assert result.evidence.planner_mode == FAST_PATH_MODE
    assert result.risk.risk_level == RiskLevel.LOW
    assert any(e.url == OFFICIAL_URL for e in result.evidence.official_web_evidence)


def test_official_page_not_mapped_to_product_keeps_general_scope():
    """官方網域只代表來源角色：頁面對不到已確認產品時不走 early-stop，證據範圍只是一般背景。"""
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT])
    result, llm = run(ex, _page(OFFICIAL_URL, "TEST_ONLY 與任何產品無關的官方公告"))
    bundle = result.evidence
    assert bundle.planner_mode != URL_EVIDENCE_MODE
    assert bundle.fast_path_reason == "url_path_declined:official_page_not_mapped_to_product"
    [page] = [e for e in bundle.official_web_evidence if e.url == OFFICIAL_URL]
    assert page.relevance.evidence_scope == EvidenceScope.GENERAL_BACKGROUND


# ============================================================ 4. Non-official URL（情況 A）


def test_shop_page_medical_claim_single_assessment_no_web():
    ad = claim(f"{PRODUCT}可以治療糖尿病", MED, ClaimOrigin.ADVERTISEMENT_CLAIM, condition="糖尿病")
    ad.content_source = URL
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT], claims=[ad])
    result, llm = run(ex, _page(SHOP_URL, f"TEST_ONLY 商品頁 {PRODUCT} 可以治療糖尿病"), url=SHOP_URL)
    bundle = result.evidence
    assert bundle.planner_mode == URL_EVIDENCE_MODE
    assert tools(result).count(ASSESSMENT_STEP) == 1
    assert not (set(tools(result)) & SLOW_TOOLS) and not llm.called("web_search_structured")
    [ca] = result.claim_assessment
    assert ca.origin == ClaimOrigin.ADVERTISEMENT_CLAIM and ca.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert result.risk.risk_level == RiskLevel.HIGH  # 既有 Risk Engine 規則
    assert result.normalized_request.claims[0].source_role == UrlSourceRole.NON_OFFICIAL_EXTERNAL_SOURCE
    assert all(e.url != SHOP_URL for e in bundle.official_web_evidence)  # 非官方頁不會變官方證據


def _shop_health_claim_run(llm, claim_text="TEST_ONLY 可以維持測試機能甲"):
    c = claim(claim_text, origin=ClaimOrigin.ADVERTISEMENT_CLAIM, function="測試機能甲")
    c.content_source = URL
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT], claims=[c])
    return run(ex, _page(SHOP_URL, "TEST_ONLY 商品頁"), url=SHOP_URL, llm=llm)


def _links(*links):
    return lambda _input: _EvidenceAssessment.model_validate({
        "relevance": [{"evidence_id": c["evidence_id"], "relevance_score": 0.9, "evidence_scope": "CLAIM_REGULATION",
                       "supports_claim": None, "rationale": "TEST_ONLY"}
                      for c in json.loads(_input)["candidates_to_assess"]],
        "claim_links": [{"claim_index": 0, "evidence_id": e, "relation": r, "rationale": "TEST_ONLY"} for e, r in links],
    })


def test_shop_claim_covered_by_structured_evidence_skips_web():
    llm = FakeLLM(structured={_EvidenceAssessment: _links(("approved_claim:pc-1", "supported"))})
    result, _ = _shop_health_claim_run(llm)
    assert result.evidence.planner_mode == URL_EVIDENCE_MODE
    assert result.evidence.fast_path_reason == "url_claims_assessed:internal_evidence_sufficient"
    assert tools(result).count(ASSESSMENT_STEP) == 1 and not llm.called("web_search_structured")
    assert result.claim_assessment[0].status == ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL


def test_shop_claim_beyond_internal_evidence_allows_one_web_without_reassess_when_nothing_new():
    llm = FakeLLM()  # 預設評估：沒有命題關係 → 命題沒有內部證據可對照；官方網站回傳 0 筆
    result, _ = _shop_health_claim_run(llm)
    assert result.evidence.fast_path_reason == "url_claims_assessed:claim_beyond_internal_evidence"
    assert tools(result).count("search_tw_official_web") == 1
    assert tools(result).count(ASSESSMENT_STEP) == 1  # web 沒有新增證據 → 不重跑 assess
    assert not llm.called("plan_step") or all(c[1] == ["search_tw_official_web"]
                                              for c in llm.calls if c[0] == "plan_step")


def test_web_with_new_evidence_reassesses_at_most_once():
    web_url = "https://test-only.fda.gov.tw/TEST_ONLY/rule"
    llm = FakeLLM(web=web_result([{"title": "TEST_ONLY 規範", "url": web_url, "snippet": "TEST_ONLY"}], [web_url]))
    result, _ = _shop_health_claim_run(llm)
    assert tools(result).count("search_tw_official_web") == 1
    assert tools(result).count(ASSESSMENT_STEP) == 2


def test_web_returning_only_the_user_supplied_page_is_not_new_evidence():
    """使用者貼的官方頁＋自己的命題：官方網站只找回同一頁 → 不算新增證據、不重新評估。"""
    q = claim(f"{PRODUCT}可以幫助睡眠", origin=ClaimOrigin.USER_QUESTION, function="睡眠")
    q.content_source = ClaimContentSource.USER_TEXT
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT], claims=[q])
    llm = FakeLLM(web=web_result([{"title": "TEST_ONLY", "url": OFFICIAL_URL, "snippet": "TEST_ONLY"}], [OFFICIAL_URL]))
    result, _ = run(ex, _page(OFFICIAL_URL, official_page_text()), text="TEST_ONLY 可以幫助睡眠嗎", llm=llm)
    assert tools(result).count("search_tw_official_web") == 1
    assert tools(result).count(ASSESSMENT_STEP) == 1
    assert [c.origin for c in result.normalized_request.claims] == [ClaimOrigin.USER_QUESTION]
    assert len([e for e in result.evidence.official_web_evidence if e.url == OFFICIAL_URL]) == 1


def test_evidence_conflict_allows_web_fallback():
    llm = FakeLLM(structured={_EvidenceAssessment: _links(("approved_claim:pc-1", "contradicts"))})
    result, _ = _shop_health_claim_run(llm)
    assert result.evidence.fast_path_reason == "url_claims_assessed:evidence_conflict"
    assert tools(result).count("search_tw_official_web") == 1


# ============================================================ 5. 何時回到完整流程


def test_unknown_source_goes_to_full_flow():
    c = claim("TEST_ONLY 可以維持測試機能甲", origin=ClaimOrigin.ADVERTISEMENT_CLAIM)
    c.content_source = URL
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT], claims=[c])
    result, llm = run(ex, _page(UNKNOWN_URL, "TEST_ONLY"), url=UNKNOWN_URL)
    assert result.evidence.fast_path_reason == "url_path_declined:source_unknown"
    assert result.evidence.planner_mode != URL_EVIDENCE_MODE and llm.called("plan_step")
    assert result.normalized_request.claims[0].source_role == UrlSourceRole.UNKNOWN_EXTERNAL_SOURCE


def test_product_not_found_goes_to_full_flow_with_web():
    c = claim("TEST_ONLY 可以維持測試機能甲", origin=ClaimOrigin.ADVERTISEMENT_CLAIM)
    c.content_source = URL
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[UNKNOWN_PRODUCT], claims=[c])
    result, llm = run(ex, _page(SHOP_URL, "TEST_ONLY"), url=SHOP_URL)
    assert result.evidence.fast_path_reason.startswith("url_path_declined:product_")
    assert "search_tw_official_web" in tools(result)


def test_full_flow_does_not_repeat_assessment_without_new_evidence():
    """完整流程：官方網站沒有新增證據時，assess_evidence 只跑一次。"""
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[UNKNOWN_PRODUCT],
                    claims=[claim("TEST_ONLY 可以維持測試機能甲", origin=ClaimOrigin.ADVERTISEMENT_CLAIM)])
    ex.claims[0].content_source = URL
    result, _ = run(ex, _page(SHOP_URL, "TEST_ONLY"), url=SHOP_URL)
    assert tools(result).count(ASSESSMENT_STEP) <= 1


# ============================================================ 6. 延遲紀錄


def test_stage_timings_cover_each_stage():
    ex = extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT])
    result, _ = run(ex, _page(OFFICIAL_URL, official_page_text()))
    timings = stage_timings_ms(result.tools_called)
    assert {"fetch_ms", "product_resolution_ms", "db_kg_ms", "rag_ms", "planner_ms",
            "assessment_ms", "official_web_ms"} <= set(timings)


# ============================================================ 7. LINE URL smoke（fake LINE）


@pytest.mark.parametrize("url,page,expect_official", [
    (OFFICIAL_URL, official_page_text(), True),
    (SHOP_URL, f"TEST_ONLY 商品頁 {PRODUCT}", False),
])
def test_line_url_message_replies_once_with_source_role(url, page, expect_official):
    from tests.unit.test_line_webhook import FakeLine, event, send
    from app.line.handler import LineWebhookHandler
    from app.response.service import AdaptiveResponseService
    from app.response.store import InMemoryRequestStore
    from app.services.context_store import TaskContextStore

    llm = FakeLLM(structured={IntakeExtraction: extraction(intent=Intent.PRODUCT_VERIFICATION,
                                                           product_candidates=[PRODUCT])})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm, url_fetcher=_page(url, page)),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    store, contexts, line = InMemoryRequestStore(), TaskContextStore(), FakeLine()
    svc = AdaptiveResponseService(pipeline, ReporterAgent(llm), store, timeout_seconds=5.0, context_store=contexts)
    handler = LineWebhookHandler(svc, line, contexts)
    send(handler, event(text=f"TEST_ONLY {url}"))
    assert len(line.sent) == 1  # 每個 request 只送一則
    assert "流傳" not in line.dumps()
    [record] = list(store._data.values())
    [src] = record.normalized_request.url_sources
    assert src.source_url == url and src.is_official == expect_official
    page_evidence = [e for e in record.evidence.official_web_evidence if e.url == url]
    assert bool(page_evidence) == expect_official  # 只有官方頁會成為官方證據
