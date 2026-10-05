"""Real Phone Demo correctness（P0-1 / P0-2 / P0-3）回歸測試。

* P0-1 Deterministic product resolver：LLM planning 前先以固定規則確定產品；VERIFIED / ambiguous 結果鎖定，
  planner 不可無證據降級或猜測；同一基本品名有多個品項時不猜，請使用者確認。
* P0-2 Modality semantic parity：語音轉錄後與直接輸入同一段文字，送進語意理解的內容完全相同。
* P0-3 Claim-role：開放式功效詢問不是命題；疾病是非題是 USER_QUESTION；廣告宣稱才是 ADVERTISEMENT_CLAIM。

全部使用 TEST_ONLY 虛構資料與 fake，不呼叫外部服務；真實手機測試中的產品名稱不會出現在這裡。
"""

import json
import re

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import CLARIFICATION_MODE, FAST_PATH_MODE, VerificationAgent
from app.normalization import base_product_name, normalize_name
from app.pipeline import AnalysisPipeline, context_entities_for_turn
from app.risk.assessment_rules import resolve_product_match
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimOrigin,
    ClaimSemanticCategory,
    EntityType,
    InputType,
    Intent,
    ProductAmbiguityReason,
    ProductMatchType,
    ProductVerificationStatus,
    ResponseType,
    RiskLevel,
    SourceType,
)
from app.schemas.evidence import ProductCandidateMatch, ProductRecord, SourceRef
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from app.tools.product_tools import lookup_product
from tests.builders import claim, extraction, request, user_text
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore, plan
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product

PRODUCT_1 = "虛構牌晶亮測試膠囊"  # TEST_ONLY：synthetic_data 中唯一、有核准功效與成分
VARIANT_BASE = "虛構牌雙款測試飲"  # TEST_ONLY：官方紀錄有兩個帶括號規格的品項
VARIANTS = [f"{VARIANT_BASE}(原味)", f"{VARIANT_BASE}(無糖)"]
SLOW_PATH_TOOLS = {"plan_tool_calls", "search_rag", "assess_evidence", "search_tw_official_web"}
MED = ClaimSemanticCategory.MEDICAL_EFFICACY
SRC = SourceRef(source_name="TEST_ONLY", source_type=SourceType.STRUCTURED_DB, is_official=True)


@pytest.fixture
def variant_products(monkeypatch):
    """TEST_ONLY：同一基本品名的兩個品項（各自有許可證、核准功效、成分）。"""
    products = [_product(f"pv-{i}", name, "c-1", "虛構測試生技股份有限公司", f"Z0090{i}") for i, name in enumerate(VARIANTS)]
    claims = [{"product_claim_id": f"pcv-{i}", "product_id": f"pv-{i}", "approved_claim_id": "ac-1",
               "function_name": "測試功效甲", "claim_text": "經測試證實：有助於維持測試機能甲。", **SOURCE}
              for i in range(len(VARIANTS))]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + products)
    monkeypatch.setattr(data, "APPROVED_CLAIMS", data.APPROVED_CLAIMS + claims)
    monkeypatch.setattr(data, "PRODUCT_INGREDIENTS", data.PRODUCT_INGREDIENTS + [
        {"product_id": f"pv-{i}", "ingredient_id": "i-1", "raw_text": "虛構素甲"} for i in range(len(VARIANTS))])
    return products


def _m(name, score, pid=None, by="product_name", permit=None, query="q"):
    return ProductCandidateMatch(
        product=ProductRecord(product_id=pid or f"id-{name}", product_name_zh=name, permit_number=permit, source=SRC),
        match_score=score, matched_by=by, query=query,
    )


def _pipeline(llm, repo=None):
    return AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(repo or InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )


def run(ex: IntakeExtraction, text=None, repo=None):
    llm = FakeLLM(structured={IntakeExtraction: ex})
    return _pipeline(llm, repo).run(RawUserInput(input_type=InputType.TEXT, text=text or user_text(ex))), llm


def tool_path(result) -> set[str]:
    return {t.tool_name for t in result.evidence.tools_called}


# ============================================================ P0-1 resolver 規則


def test_base_product_name_strips_only_trailing_qualifiers():
    assert base_product_name("某測試品(原味)") == "某測試品"
    assert base_product_name("某測試品（原味）(新版)") == "某測試品"
    assert base_product_name("某(甲)測試品") == "某(甲)測試品"  # 括號不在尾端：保留
    assert base_product_name("(原味)") == "(原味)"  # 去掉後為空：保留
    assert base_product_name("某測試品") == "某測試品"


def test_unique_canonical_exact_is_verified_even_with_many_noise_rows():
    """lookup 回傳很多列不等於很多候選：只有一筆品名完全相同、其他都是片段命中 → VERIFIED。"""
    req = request(product_candidates=["TEST_ONLY 甲品"])
    noise = [_m(f"TEST_ONLY 無關品{i}", 0.55 + i * 0.01) for i in range(19)]
    m = resolve_product_match(req, noise + [_m("TEST_ONLY 甲品", 1.0)], [], [], [], True)
    assert m.status == ProductVerificationStatus.VERIFIED
    assert m.match_type == ProductMatchType.CANONICAL_EXACT and m.candidate_count == 1
    assert m.resolver_debug()["selected_product_id"] == "id-TEST_ONLY 甲品"


def test_brand_plus_name_exact_is_verified():
    req = request(product_candidates=["甲測試品"], brand_candidates=["TEST_ONLY牌"])
    m = resolve_product_match(req, [_m("TEST_ONLY牌甲測試品", 0.8)], [], [], [], True)
    assert m.status == ProductVerificationStatus.VERIFIED and m.match_type == ProductMatchType.CANONICAL_EXACT


def test_single_variant_base_name_is_verified():
    req = request(product_candidates=["TEST_ONLY 乙品"])
    m = resolve_product_match(req, [_m("TEST_ONLY 乙品(原味)", 0.85), _m("TEST_ONLY 其他", 0.6)], [], [], [], True)
    assert m.status == ProductVerificationStatus.VERIFIED and m.match_type == ProductMatchType.BASE_NAME_EXACT


def test_multiple_variants_are_ambiguous_and_not_guessed():
    req = request(product_candidates=["TEST_ONLY 乙品"])
    m = resolve_product_match(req, [_m("TEST_ONLY 乙品(原味)", 0.85), _m("TEST_ONLY 乙品(無糖)", 0.85),
                                    _m("TEST_ONLY 乙茶", 0.6)], [], [], [], True)
    assert m.status == ProductVerificationStatus.PARTIAL_MATCH
    assert m.is_ambiguous and m.ambiguity_reason == ProductAmbiguityReason.MULTIPLE_VARIANTS
    assert m.best_match is None and m.candidate_count == 2  # 片段命中的「乙茶」不是合理候選


def test_duplicate_exact_names_are_ambiguous():
    req = request(product_candidates=["TEST_ONLY 丙品"])
    m = resolve_product_match(req, [_m("TEST_ONLY 丙品", 1.0, pid="a"), _m("TEST_ONLY 丙品", 1.0, pid="b")], [], [], [], True)
    assert m.is_ambiguous and m.ambiguity_reason == ProductAmbiguityReason.MULTIPLE_EXACT_NAMES


def test_only_user_provided_permit_numbers_verify():
    """planner 從工具結果抄出的字號不是使用者提供的識別資訊：不能用來在多個品項中挑一個。"""
    variants = [_m("TEST_ONLY 乙品(原味)", 0.85, pid="v1", permit="測試健食字第Z00901號"),
                _m("TEST_ONLY 乙品(無糖)", 0.85, pid="v2", permit="測試健食字第Z00902號")]
    planner_permit = [_m("TEST_ONLY 乙品(原味)", 1.0, pid="v1", by="permit_number", permit="測試健食字第Z00901號",
                         query="測試健食字第Z00901號")]
    no_user_permit = resolve_product_match(request(product_candidates=["TEST_ONLY 乙品"]), variants, planner_permit, [], [], True)
    assert no_user_permit.is_ambiguous
    with_user_permit = resolve_product_match(
        request(product_candidates=["TEST_ONLY 乙品"], permit_number_candidates=["Z00901"]), variants, planner_permit, [], [], True)
    assert with_user_permit.status == ProductVerificationStatus.VERIFIED
    assert with_user_permit.match_type == ProductMatchType.PERMIT_NUMBER
    assert with_user_permit.best_match.product.product_id == "v1"


@pytest.mark.parametrize("status, expected", [
    ("OFFICIAL_RECORD", ProductMatchType.OFFICIAL_ALIAS_EXACT),
    ("INFERRED", None),  # 推測別名不可直接確認產品
])
def test_official_product_alias_exact_match(monkeypatch, status, expected):
    alias = {"id": "a-p", "entity_type": "PRODUCT", "entity_id": "p-1", "alias": "晶亮測試俗稱",
             "alias_normalized": normalize_name("晶亮測試俗稱"), "alias_type": "OFFICIAL_ALT_NAME", "confidence": 1.0,
             "evidence_status": status, "inference_basis": {}, **SOURCE}
    monkeypatch.setattr(data, "ALIASES", data.ALIASES + [alias])

    class Repo(InMemoryOfficialRepo):  # 別名查詢命中後，產品查詢也找得到該產品（片段命中）
        def search_products(self, terms, limit):
            return super().search_products(terms + ["晶亮測試"], limit)

    out = lookup_product(Repo(), ["晶亮測試俗稱"])
    m = resolve_product_match(request(product_candidates=["晶亮測試俗稱"]), out.candidates, [], [], [], True)
    if expected is None:
        assert m.match_type != ProductMatchType.OFFICIAL_ALIAS_EXACT
    else:
        assert m.status == ProductVerificationStatus.VERIFIED and m.match_type == expected
        assert m.best_match.product.product_id == "p-1"


# ============================================================ P0-1 Agent B：鎖定 + fast path + clarification


@pytest.mark.parametrize("intent", [Intent.EFFICACY_INQUIRY, Intent.INGREDIENT_INQUIRY, Intent.PRODUCT_VERIFICATION])
def test_ambiguous_variants_ask_for_clarification_without_planner(variant_products, intent):
    result, llm = run(extraction(intent=intent, product_candidates=[VARIANT_BASE]))
    bundle = result.evidence
    assert bundle.planner_mode == CLARIFICATION_MODE
    assert not (tool_path(result) & SLOW_PATH_TOOLS) and not llm.called("plan_step")
    assert bundle.product_match.is_ambiguous and bundle.product_match.best_match is None
    assert bundle.fast_path_reason == "product_needs_user_choice:MULTIPLE_VARIANTS"
    payload = result.response
    assert payload.response_type == ResponseType.MISSING_INFORMATION
    assert all(any(v in p for p in payload.key_points) for v in VARIANTS)  # 列出所有官方候選，不猜
    assert bundle.approved_claims == []  # 不把任一品項的資料當成答案


def test_exact_variant_name_is_verified_and_uses_fast_path(variant_products):
    result, llm = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[VARIANTS[1]]))
    bundle = result.evidence
    assert bundle.product_match.status == ProductVerificationStatus.VERIFIED
    assert bundle.product_match.best_match.product.product_name_zh == VARIANTS[1]
    assert bundle.planner_mode == FAST_PATH_MODE and not (tool_path(result) & SLOW_PATH_TOOLS)
    assert bundle.fast_path_eligible and bundle.fast_path_reason == "verified_structured_evidence_sufficient"
    assert result.response.response_type == ResponseType.PRODUCT_INFO


def test_planner_cannot_downgrade_or_switch_deterministically_verified_product():
    """完整流程（有命題）中，planner 用其他名稱 / 抄來的字號查到更多產品，不得改寫已 VERIFIED 的結果。"""
    q = claim(f"{PRODUCT_1}可以幫助睡眠", origin=ClaimOrigin.USER_QUESTION, function="睡眠")
    llm = FakeLLM(structured={IntakeExtraction: extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_1], claims=[q])},
                  plans=[plan(("lookup_product", {"product_names": ["虛構牌"], "brand_names": []})),
                         plan(("lookup_permit", {"permit_text": "測試健食字第Z00002號"})), plan()])
    result = _pipeline(llm).run(RawUserInput(input_type=InputType.TEXT, text=f"TEST_ONLY {PRODUCT_1}"))
    m = result.evidence.product_match
    assert llm.called("plan_step")  # 有命題：仍走完整流程
    assert m.status == ProductVerificationStatus.VERIFIED and m.best_match.product.product_id == "p-1"
    assert m.match_type == ProductMatchType.CANONICAL_EXACT


def test_verified_turn_does_not_store_raw_user_name_as_separate_product():
    result, _ = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=["虛構牌 晶亮測試膠囊"]))
    products = [e for e in context_entities_for_turn(result) if e.entity_type == EntityType.PRODUCT]
    assert [(e.name, e.entity_id) for e in products] == [(PRODUCT_1, "p-1")]


# ============================================================ P0-2 modality semantic parity

# (使用者說的內容, 虛構的語意理解結果)：參數化涵蓋五種語意類型
PARITY_CASES = {
    "open_efficacy": (f"{PRODUCT_1}的核准功效是什麼？",
                      extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_1])),
    "disease_question": (f"{PRODUCT_1}可以治療糖尿病嗎？",
                         extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_1],
                                    claims=[claim(f"{PRODUCT_1}可以治療糖尿病", MED, ClaimOrigin.USER_QUESTION, condition="糖尿病")])),
    "ad_assertion": (f"廣告說{PRODUCT_1}可以治療糖尿病",
                     extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[PRODUCT_1],
                                claims=[claim(f"{PRODUCT_1}可以治療糖尿病", MED, ClaimOrigin.ADVERTISEMENT_CLAIM, condition="糖尿病")])),
    "ingredient": (f"{PRODUCT_1}有什麼成分？",
                   extraction(intent=Intent.INGREDIENT_INQUIRY, product_candidates=[PRODUCT_1])),
    "product_verification": (f"{PRODUCT_1}有沒有合法登記？",
                             extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT_1])),
}


class TranscribingLLM(FakeLLM):
    """TEST_ONLY：語音轉錄結果固定為指定文字（模擬 transcription 完成）。"""

    def __init__(self, transcript: str, **kw):
        super().__init__(**kw)
        self.transcript = transcript

    def transcribe_audio(self, *, audio, filename):
        self.calls.append(("transcribe_audio", filename))
        return self.transcript


def _modality_sensitive_analyzer(table: dict[str, IntakeExtraction]):
    """TEST_ONLY 語意理解替身：依 content 回傳結果；若輸入帶有任何 modality 訊號（input_type 不是 text、
    來源標示、轉錄附註），就把命題改判成不同 role —— 用來偵測語音是否真的走同一個語意契約。"""
    def analyze(input_text: str) -> IntakeExtraction:
        payload = json.loads(input_text)
        ex = table[re.sub(r"^【[^】]*】\n", "", payload["content"]).strip()]
        if payload["input_type"] != "text" or payload["adapter_notes"] or "【" in payload["content"]:
            ex = ex.model_copy(update={"intent": Intent.CLAIM_VERIFICATION, "claims": [
                claim("TEST_ONLY modality-specific claim", MED, ClaimOrigin.THIRD_PARTY_CLAIM)]})
        return ex
    return analyze


def _semantic_view(req):
    return {
        "intent": req.intent,
        "product_candidates": req.product_candidates,
        "claim_roles": [c.origin for c in req.claims],
        "claim_targets": [(c.target_condition, c.target_function) for c in req.claims],
        "needs_claim_check": req.needs_claim_check,
        "needs_product_lookup": req.needs_product_lookup,
        "requested": req.question,
    }


@pytest.mark.parametrize("case", list(PARITY_CASES))
def test_transcription_enters_same_semantic_contract_as_text(case):
    text, ex = PARITY_CASES[case]
    analyzer = {IntakeExtraction: _modality_sensitive_analyzer({text: ex})}

    text_llm = FakeLLM(structured=analyzer)
    text_req = IntakeAgent(text_llm).understand("r-text", RawUserInput(input_type=InputType.TEXT, text=text))

    audio_llm = TranscribingLLM(text, structured=analyzer)
    audio_raw = RawUserInput(input_type=InputType.AUDIO, media=[MediaInput(kind="audio", content=b"aud", mime_type="audio/m4a")])
    audio_req = IntakeAgent(audio_llm).understand("r-audio", audio_raw)

    # 語意理解收到的輸入完全相同 → NormalizedUserRequest 的語意欄位一致
    assert text_llm.structured_inputs[-1][1] == audio_llm.structured_inputs[-1][1]
    assert _semantic_view(audio_req) == _semantic_view(text_req)
    # audio 仍保留自己的紀錄：input_type、transcription tool call、轉錄附註
    assert audio_req.input_type == InputType.AUDIO
    assert [t.tool_name for t in audio_req.input_tools_called] == ["transcribe_audio"]
    assert any("語音" in n for n in audio_req.adapter_notes)


def test_general_audio_question_is_not_turned_into_claim_verification():
    """一般資訊詢問的語音：不因 input_type=audio 變成 claim verification，並走同樣的 fast path。"""
    text, ex = PARITY_CASES["open_efficacy"]
    llm = TranscribingLLM(text, structured={IntakeExtraction: _modality_sensitive_analyzer({text: ex})})
    raw = RawUserInput(input_type=InputType.AUDIO, media=[MediaInput(kind="audio", content=b"aud", mime_type="audio/m4a")])
    result = _pipeline(llm).run(raw)
    assert result.normalized_request.intent == Intent.EFFICACY_INQUIRY and result.normalized_request.claims == []
    assert result.evidence.planner_mode == FAST_PATH_MODE and not (tool_path(result) & SLOW_PATH_TOOLS)
    assert result.response.response_type == ResponseType.PRODUCT_INFO


def test_image_and_url_content_keep_their_source_labels():
    """parity 只適用於使用者自己說的話；圖片 / 網頁是第三方文字，仍保留來源標示供 statement role 判斷。"""
    llm = FakeLLM(structured={IntakeExtraction: extraction()})
    raw = RawUserInput(input_type=InputType.IMAGE, media=[MediaInput(kind="image", content=b"img", mime_type="image/png")])
    IntakeAgent(llm).understand("r", raw)
    payload = json.loads(llm.structured_inputs[-1][1])
    assert payload["input_type"] == "image" and "【圖片內容】" in payload["content"]


# ============================================================ P0-3 claim-role semantics


def test_open_efficacy_question_with_generic_target_word_is_not_a_claim():
    """LLM 把「功效」這種泛稱填進 target_function 時，仍是開放式詢問，不是待查證命題。"""
    q = claim(f"{PRODUCT_1}具有何種核准功效", origin=ClaimOrigin.USER_QUESTION, function="核准功效")
    result, _ = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[PRODUCT_1], claims=[q]))
    assert result.normalized_request.claims == []
    assert result.response.response_type == ResponseType.PRODUCT_INFO
    assert result.evidence.planner_mode == FAST_PATH_MODE


@pytest.mark.parametrize("input_type", [InputType.TEXT, InputType.AUDIO])
@pytest.mark.parametrize("case, expected_type, expected_status, expected_risk", [
    ("open_efficacy", ResponseType.PRODUCT_INFO, None, None),
    ("disease_question", ResponseType.CLAIM_RESULT, "question_proposition_high", None),
    ("ad_assertion", ResponseType.RISK, ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM, RiskLevel.HIGH),
])
def test_claim_role_outcomes_are_identical_for_text_and_audio(input_type, case, expected_type, expected_status, expected_risk):
    text, ex = PARITY_CASES[case]
    analyzer = {IntakeExtraction: _modality_sensitive_analyzer({text: ex})}
    if input_type == InputType.TEXT:
        llm, raw = FakeLLM(structured=analyzer), RawUserInput(input_type=InputType.TEXT, text=text)
    else:
        llm = TranscribingLLM(text, structured=analyzer)
        raw = RawUserInput(input_type=InputType.AUDIO, media=[MediaInput(kind="audio", content=b"aud", mime_type="audio/m4a")])
    result = _pipeline(llm).run(raw)
    assert result.response.response_type == expected_type
    if expected_status is None:
        assert result.claim_assessment == []
    elif expected_status == "question_proposition_high":
        # 仍是使用者提問：產品風險不升級、卡片沒有產品風險燈；命題若作為產品功效宣稱 → claim risk HIGH
        ca = result.claim_assessment[0]
        assert ca.origin == ClaimOrigin.USER_QUESTION and ca.claim_risk == RiskLevel.HIGH
        assert result.response.risk_level is None and result.risk.risk_level != RiskLevel.HIGH
        assert result.response.claim_risk_level == RiskLevel.HIGH
    else:
        assert result.claim_assessment[0].status == expected_status
    if expected_risk is not None:
        assert result.risk.risk_level == expected_risk
