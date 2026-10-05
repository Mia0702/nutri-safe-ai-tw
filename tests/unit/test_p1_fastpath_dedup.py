"""Real Phone Demo P1 回歸測試。

1. Unique high-confidence resolver → structured fast path：
   只有「唯一合理候選 + 達安全門檻 + margin 內無競爭者」或 exact（字號 / 官方品名）才在 deterministic 階段確認並走 fast path；
   Agent A 的錯字 / 同音修正寫法必須由使用者原始寫法佐證才可確認產品；多候選仍 clarification。
2. Response-level approved claim 去重：同一份官方敘述只顯示一次，不同敘述不合併，證據 / 來源保留。

全部使用 TEST_ONLY 虛構資料與 fake，不呼叫外部服務。
"""

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent, dedupe_approved_claims, detail_payload
from app.agents.verification_agent import CLARIFICATION_MODE, FAST_PATH_MODE, VerificationAgent
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimOrigin,
    ClaimSemanticCategory,
    InputType,
    Intent,
    ProductMatchType,
    ProductVerificationStatus,
    ResponseAction,
    ResponseType,
    RiskLevel,
    SourceType,
)
from app.schemas.evidence import ApprovedClaimEvidence, SourceRef
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from tests.builders import claim, extraction, user_text
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore, plan
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product

SLOW_PATH_TOOLS = {"plan_tool_calls", "search_rag", "assess_evidence", "search_tw_official_web"}
QUERY = "虛構甲乙測試膠囊"  # TEST_ONLY：使用者寫法


def _add_products(monkeypatch, names: list[str]) -> list[dict]:
    """TEST_ONLY：加入虛構產品，各自有許可證、核准功效、成分。"""
    products = [_product(f"px-{i}", n, "c-1", "虛構測試生技股份有限公司", f"Z0080{i}") for i, n in enumerate(names)]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + products)
    monkeypatch.setattr(data, "APPROVED_CLAIMS", data.APPROVED_CLAIMS + [
        {"product_claim_id": f"pcx-{i}", "product_id": f"px-{i}", "approved_claim_id": "ac-1",
         "function_name": "測試功效甲", "claim_text": "經測試證實：有助於維持測試機能甲。", **SOURCE} for i in range(len(names))])
    monkeypatch.setattr(data, "PRODUCT_INGREDIENTS", data.PRODUCT_INGREDIENTS + [
        {"product_id": f"px-{i}", "ingredient_id": "i-1", "raw_text": "虛構素甲"} for i in range(len(names))])
    return products


def _pipeline(llm):
    return AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )


def run(ex: IntakeExtraction, audio: bool = False):
    llm = FakeLLM(structured={IntakeExtraction: ex}, transcript=user_text(ex, "TEST_ONLY 語音"))
    raw = (RawUserInput(input_type=InputType.AUDIO, media=[MediaInput(kind="audio", content=b"a", mime_type="audio/m4a")])
           if audio else RawUserInput(input_type=InputType.TEXT, text=user_text(ex)))
    return _pipeline(llm).run(raw), llm


def slow_tools(result) -> set[str]:
    return {t.tool_name for t in result.evidence.tools_called} & SLOW_PATH_TOOLS


# ============================================================ 1. resolver → fast path


# (官方紀錄中的虛構產品, 使用者提供的品名, 字號, 預期狀態, 預期 match_type, 是否 fast path, 是否 clarification)
RESOLVER_CASES = {
    "unique_high_confidence_similarity": (
        ["虛構甲乙測試膠囊錠", "虛構丙丁測試粉"], [QUERY], [],
        ProductVerificationStatus.VERIFIED, ProductMatchType.NAME_SIMILARITY, True, False),
    "two_close_candidates": (
        ["虛構甲乙測試膠囊錠", "虛構甲乙測試膠囊粉"], [QUERY], [],
        ProductVerificationStatus.PARTIAL_MATCH, ProductMatchType.NAME_SIMILARITY, False, True),
    "low_confidence_candidate": (
        ["虛構甲乙測試軟膠囊組合包"], [QUERY], [],
        ProductVerificationStatus.PARTIAL_MATCH, ProductMatchType.NAME_SIMILARITY, False, True),
    "exact_permit": (
        ["虛構甲乙測試膠囊錠", "虛構甲乙測試膠囊粉"], [], ["Z00801"],
        ProductVerificationStatus.VERIFIED, ProductMatchType.PERMIT_NUMBER, True, False),
    "exact_official_name": (
        ["虛構甲乙測試膠囊", "虛構甲乙測試膠囊錠"], [QUERY], [],
        ProductVerificationStatus.VERIFIED, ProductMatchType.CANONICAL_EXACT, True, False),
}


@pytest.mark.parametrize("intent", [Intent.EFFICACY_INQUIRY, Intent.INGREDIENT_INQUIRY, Intent.PRODUCT_VERIFICATION])
@pytest.mark.parametrize("case", list(RESOLVER_CASES))
def test_only_unique_or_exact_matches_take_fast_path(monkeypatch, case, intent):
    names, products, permits, status, match_type, fast, clarify = RESOLVER_CASES[case]
    _add_products(monkeypatch, names)
    result, llm = run(extraction(intent=intent, product_candidates=products, permit_number_candidates=permits))
    m, bundle = result.evidence.product_match, result.evidence
    assert m.status == status and m.match_type == match_type
    if fast:
        assert bundle.planner_mode == FAST_PATH_MODE and not slow_tools(result) and not llm.called("plan_step")
        assert m.candidate_count == 1 and m.best_match is not None
    else:  # 多候選 / 低信心：不猜、不 fast path、不跑 planner，請使用者確認
        assert clarify
        assert bundle.planner_mode == CLARIFICATION_MODE and not slow_tools(result) and not llm.called("plan_step")
        assert m.status != ProductVerificationStatus.VERIFIED
        assert result.response.response_type == ResponseType.MISSING_INFORMATION
        assert any("虛構甲乙測試" in p for p in result.response.key_points)  # 列出最接近的官方候選


@pytest.mark.parametrize("audio", [False, True])
def test_corrected_name_corroborated_by_user_wording_needs_user_confirmation(monkeypatch, audio):
    """語音同音錯字：修正寫法只是 Agent A 的推測，與使用者寫法一致也只列為單一候選請使用者確認（不自動 VERIFIED）。"""
    _add_products(monkeypatch, ["虛構晶彩測試錠"])
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=["虛構精采測試錠"],
                    corrected_product_candidates=["虛構晶彩測試錠"])
    result, llm = run(ex, audio=audio)
    m = result.evidence.product_match
    assert m.status == ProductVerificationStatus.PARTIAL_MATCH
    assert m.match_type == ProductMatchType.CORRECTED_NAME_CORROBORATED and m.needs_confirmation
    assert m.candidate_count == 1 and not llm.called("plan_step")
    assert result.evidence.planner_mode == CLARIFICATION_MODE
    assert result.response.response_type == ResponseType.MISSING_INFORMATION


def test_correction_that_disagrees_with_user_wording_is_not_verified(monkeypatch):
    """修正寫法指向的產品，不是使用者原始寫法最接近的那一個 → 不確認（不讓 LLM 猜產品）。"""
    _add_products(monkeypatch, ["虛構晶彩測試錠", "虛構精彩測試錠"])
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=["虛構精采測試錠"],
                    corrected_product_candidates=["虛構晶彩測試錠"])
    result, _ = run(ex)
    m = result.evidence.product_match
    assert m.status != ProductVerificationStatus.VERIFIED
    assert result.evidence.planner_mode != FAST_PATH_MODE


def test_correction_without_any_support_from_user_wording_is_not_verified(monkeypatch):
    _add_products(monkeypatch, ["虛構晶彩測試錠"])
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=["完全不同的說法"],
                    corrected_product_candidates=["虛構晶彩測試錠"])
    result, _ = run(ex)
    assert result.evidence.product_match.status != ProductVerificationStatus.VERIFIED


def test_unconfirmed_candidate_stops_before_planner_so_no_requery_can_verify(monkeypatch):
    """低信心候選（身份未確認）：不進 planner / web / assess（Clarification before research），也就不可能被 planner 重查而升為 VERIFIED。"""
    _add_products(monkeypatch, ["虛構甲乙測試軟膠囊組合包"])
    # safety_inquiry 不走 fast path，過去一定經過 planner；現在身份未確認就先請使用者確認
    llm = FakeLLM(structured={IntakeExtraction: extraction(intent=Intent.SAFETY_INQUIRY, product_candidates=[QUERY])},
                  plans=[plan(("lookup_product", {"product_names": ["虛構甲乙測試軟膠囊組合包"], "brand_names": []})), plan()])
    result = _pipeline(llm).run(RawUserInput(input_type=InputType.TEXT, text=f"TEST_ONLY {QUERY}"))
    assert not llm.called("plan_step") and not slow_tools(result)
    assert result.evidence.planner_mode == CLARIFICATION_MODE
    assert result.evidence.product_match.status == ProductVerificationStatus.PARTIAL_MATCH


def test_correction_cannot_pick_one_variant_out_of_several(monkeypatch):
    """多品項仍 clarification：修正寫法不可替使用者選擇其中一個規格。"""
    _add_products(monkeypatch, ["虛構甲乙測試飲(原味)", "虛構甲乙測試飲(無糖)"])
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=["虛構甲乙測試飲"],
                    corrected_product_candidates=["虛構甲乙測試飲(原味)"])
    result, _ = run(ex)
    assert result.evidence.planner_mode == CLARIFICATION_MODE
    assert result.evidence.product_match.is_ambiguous


def test_claims_and_safety_never_take_fast_path_even_when_verified(monkeypatch):
    _add_products(monkeypatch, ["虛構甲乙測試膠囊"])
    ad = claim(f"{QUERY}可以治療糖尿病", ClaimSemanticCategory.MEDICAL_EFFICACY, ClaimOrigin.ADVERTISEMENT_CLAIM, condition="糖尿病")
    result, _ = run(extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[QUERY], claims=[ad]))
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert result.evidence.planner_mode != FAST_PATH_MODE and result.risk.risk_level == RiskLevel.HIGH
    result, _ = run(extraction(intent=Intent.SAFETY_INQUIRY, product_candidates=[QUERY]))
    assert result.evidence.planner_mode != FAST_PATH_MODE


# ============================================================ 2. approved claim 去重

SRC_A = SourceRef(source_name="TEST_ONLY 官方來源甲", source_type=SourceType.STRUCTURED_DB, source_url="https://example.invalid/a", is_official=True)
SRC_B = SourceRef(source_name="TEST_ONLY 官方來源乙", source_type=SourceType.STRUCTURED_DB, source_url="https://example.invalid/b", is_official=True)
SHARED_TEXT = "(一)測試功效甲\n經測試證實，有助於維持測試機能甲。(二)測試功效乙\n有助於維持測試機能乙。"


def _ac(eid, cid, function, text, source=SRC_A, pid="p-1"):
    return ApprovedClaimEvidence(evidence_id=eid, product_id=pid, approved_claim_id=cid,
                                 function_name=function, claim_text=text, source=source)


def test_same_official_text_under_different_claim_ids_is_shown_once_with_all_functions():
    claims = [
        _ac("e1", "ac-x", "測試功效甲", SHARED_TEXT),
        _ac("e2", "ac-y", "測試功效乙", SHARED_TEXT.replace("\n", " ") + " ", source=SRC_B),  # 只差空白 / 換行
        _ac("e3", "ac-z", "測試功效丙", SHARED_TEXT),
    ]
    lines = dedupe_approved_claims(claims)
    assert len(lines) == 1
    assert lines[0].function_names == ["測試功效甲", "測試功效乙", "測試功效丙"]  # 不丟任何功效類別
    assert lines[0].evidence_ids == ["e1", "e2", "e3"]  # traceability 保留
    assert {s.source_name for s in lines[0].sources} == {SRC_A.source_name, SRC_B.source_name}


def test_same_canonical_claim_id_from_multiple_records_is_one_line():
    lines = dedupe_approved_claims([_ac("e1", "ac-x", "測試功效甲", "有助於維持測試機能甲。"),
                                    _ac("e2", "ac-x", "測試功效甲", "有助於維持測試機能甲。", source=SRC_B)])
    assert len(lines) == 1 and lines[0].function_names == ["測試功效甲"] and len(lines[0].sources) == 2


def test_different_wording_is_never_merged():
    lines = dedupe_approved_claims([_ac("e1", "ac-x", "測試功效甲", "有助於維持測試機能甲。"),
                                    _ac("e2", "ac-y", "測試功效甲", "有助於維持測試機能甲與乙。"),
                                    _ac("e3", "ac-x", "測試功效甲", "有助於調整測試機能甲。")])
    assert len(lines) == 3


def test_product_info_card_and_official_info_show_shared_claim_text_once(monkeypatch):
    monkeypatch.setattr(data, "APPROVED_CLAIMS", [c for c in data.APPROVED_CLAIMS if c["product_id"] != "p-1"] + [
        {"product_claim_id": f"pc-dup-{i}", "product_id": "p-1", "approved_claim_id": f"ac-dup-{i}",
         "function_name": fn, "claim_text": SHARED_TEXT, **SOURCE}
        for i, fn in enumerate(["測試功效甲", "測試功效乙", "測試功效丙"])])
    result, _ = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=["虛構牌晶亮測試膠囊"]))
    payload = result.response
    assert payload.response_type == ResponseType.PRODUCT_INFO
    assert payload.key_points == ["測試功效甲", "測試功效乙", "測試功效丙"]  # primary 只列功效名稱，各一次
    full = detail_payload(ResponseAction.FULL_EFFICACY, result.normalized_request, result.evidence, result.risk)
    assert sum(SHARED_TEXT in line for line in full.body) == 1  # 相同核准原文在完整功效只出現一次
    assert len(result.evidence.approved_claims) == 3  # EvidenceBundle 未被改動：每筆官方紀錄仍可追溯
    detail = detail_payload(ResponseAction.OFFICIAL_INFO, result.normalized_request, result.evidence, result.risk)
    assert sum(SHARED_TEXT in line for line in detail.body) <= 1  # 官方資訊卡為摘要式
    full = detail_payload(ResponseAction.FULL_EFFICACY, result.normalized_request, result.evidence, result.risk)
    assert sum(SHARED_TEXT in line for line in full.body) == 1
