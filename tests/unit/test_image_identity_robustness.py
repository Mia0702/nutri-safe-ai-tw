"""Image Identity Robustness 回歸測試。

* 證據優先序：許可證字號 exact > 官方品名 / 別名 exact > 品名＋公司 / 品牌 > 名稱相似 > 標章 > 標章文字。
* 字號唯一命中時，圖片品名辨識差異只標 IMAGE_TEXT_CONFLICT / OCR_UNCERTAIN，不單獨變成 CONFLICT / HIGH。
* 只有「字號指向 A、清楚完整的官方品名指向 B」才是產品身份衝突。
* 標章是視覺證據：文字讀不清楚不影響，也不能單獨確認產品。
* 局部二次 Vision 只在產品身份無法確認時做一次；字號已命中時不做。

Fake Vision / Agent A 只解析下方 TEST_ONLY 格式；全部虛構資料，不呼叫外部服務。
"""

import json
import re

import pytest

from app.agents.intake_agent import REINSPECTED_NOTE, IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    CertificationMarkType,
    InputType,
    Intent,
    ProductIdentityFlag,
    ProductMatchType,
    ProductVerificationStatus,
    RiskLevel,
)
from app.schemas.user_request import CertificationMarkObservation, IntakeExtraction, MediaInput, RawUserInput
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import _product

A_NAME, A_PERMIT = "虛構牌晶亮測試膠囊", "Z00001"  # synthetic_data p-1
B_NAME = "虛構牌順暢測試優酪乳"  # synthetic_data p-2
MARK_TEXT = "健康食品"
REREAD = "局部重讀"


class FakeVision:
    """TEST_ONLY：第一次讀圖回傳指定文字；第二次（局部重讀）回傳指定 JSON。記錄每次呼叫。"""

    def __init__(self, first: str, reread: dict | None = None):
        self.first, self.reread, self.calls = first, reread or {}, []

    def __call__(self, *, instructions, image, mime_type):
        second = REREAD in instructions
        self.calls.append("reread" if second else "first")
        return json.dumps(self.reread, ensure_ascii=False) if second else self.first


def fake_intake(marks: list[CertificationMarkObservation] | None = None):
    """TEST_ONLY Agent A：從「品名=…；字號=…；公司=…」格式抽取；LLM 誤把標章文字當品名的情況也模擬。"""
    def run(input_text: str) -> IntakeExtraction:
        content = json.loads(input_text)["content"]
        get = lambda key: [v for v in re.findall(rf"{key}=([^；\n]*)", content) if v]  # noqa: E731
        return extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=get("品名"),
                          permit_number_candidates=get("字號"), company_candidates=get("公司"),
                          certification_marks=get("標章"), certification_mark_observations=marks or [],
                          normalized_text=content)
    return run


def run_image(first: str, reread: dict | None = None, marks=None, text: str = ""):
    vision = FakeVision(first, reread)
    llm = FakeLLM(structured={IntakeExtraction: fake_intake(marks)})
    llm.describe_image = vision
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    raw = RawUserInput(input_type=InputType.MIXED if text else InputType.IMAGE, text=text,
                       media=[MediaInput(kind="image", content=b"TEST_ONLY", mime_type="image/jpeg")])
    return pipeline.run(raw, channel="test"), vision


# ============================================================ permit exact + OCR typo


@pytest.mark.parametrize("ocr_name,flags", [
    ("虛搆牌晶亮測試膠嚢", [ProductIdentityFlag.OCR_UNCERTAIN]),  # 輕微誤讀
    ("虛木冓片卑日日亭", [ProductIdentityFlag.IMAGE_TEXT_CONFLICT, ProductIdentityFlag.OCR_UNCERTAIN]),  # 嚴重誤讀
])
def test_permit_exact_with_ocr_name_typo_is_verified_with_flags(ocr_name, flags):
    result, vision = run_image(f"品名={ocr_name}；字號={A_PERMIT}")
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.VERIFIED
    assert match.match_type == ProductMatchType.PERMIT_NUMBER
    assert match.best_match.product.product_id == "p-1"
    assert match.identity_flags == flags
    assert result.risk.risk_level != RiskLevel.HIGH  # OCR 品名差異不能單獨造成 HIGH
    assert "圖片文字辨識與官方名稱有差異" in result.response.key_points[0]
    assert vision.calls == ["first"]  # 字號已命中：不做第二次 Vision


def test_permit_exact_with_partial_name_read_has_no_flag():
    result, _ = run_image(f"品名=晶亮測試膠囊；字號={A_PERMIT}")  # 只讀到品名的一部分：不是差異
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.VERIFIED and match.identity_flags == []
    assert not any("圖片文字辨識" in p for p in result.response.key_points)


def test_typed_text_permit_with_different_name_keeps_conflict():
    """使用者自己打的品名不是辨識誤差：原本的字號 / 品名不一致規則不變。"""
    llm = FakeLLM(structured={IntakeExtraction: fake_intake()})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    result = pipeline.run(RawUserInput(input_type=InputType.TEXT, text=f"品名=虛木冓片卑日日亭；字號={A_PERMIT}"))
    assert result.evidence.product_match.status == ProductVerificationStatus.CONFLICT


# ============================================================ permit exact + unreadable certification mark


def test_permit_exact_with_unreadable_certification_mark():
    marks = [CertificationMarkObservation(mark_type=CertificationMarkType.HEALTH_FOOD_MARK, visually_detected=True,
                                          confidence=1.7, text_readable=False, text="健庚食吅")]
    # LLM 誤把清楚可讀的標章文字當成品名：標章文字不是產品識別，應被移除
    result, vision = run_image(f"品名={MARK_TEXT}；品名={A_NAME}；字號={A_PERMIT}；標章={MARK_TEXT}", marks=marks)
    req = result.normalized_request
    obs = req.certification_mark_observations[0]
    assert obs.visually_detected and not obs.text_readable and obs.text == "" and obs.confidence == 1.0
    assert MARK_TEXT not in req.product_candidates and req.certification_marks == [MARK_TEXT]
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.VERIFIED and match.identity_flags == []
    assert vision.calls == ["first"]  # 不為標章文字做第二次 Vision


def test_certification_mark_alone_cannot_confirm_product():
    marks = [CertificationMarkObservation(mark_type=CertificationMarkType.HEALTH_FOOD_MARK, visually_detected=True,
                                          confidence=0.9, text_readable=True, text=MARK_TEXT)]
    result, vision = run_image(f"品名={MARK_TEXT}；標章={MARK_TEXT}", marks=marks)
    assert result.normalized_request.product_candidates == []
    assert result.evidence.product_match.status != ProductVerificationStatus.VERIFIED
    assert vision.calls == ["first", "reread"]  # 身份未確認：允許一次局部重讀（但重讀也沒有新線索）


# ============================================================ permit A + clear official name B


def test_permit_a_with_clear_official_name_b_is_identity_conflict():
    result, vision = run_image(f"品名={B_NAME}；字號={A_PERMIT}")
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.CONFLICT
    assert match.best_match.product.product_id == "p-1"
    assert B_NAME in match.notes[0]
    assert vision.calls == ["first"]  # 字號已命中：不重讀


# ============================================================ no permit + uncertain name


def test_no_permit_uncertain_name_reread_finds_permit():
    result, vision = run_image("品名=虛構牌晶亮", reread={"permit_number": f"衛部健食字第{A_PERMIT}號",
                                                     "product_name": "", "permit_readable": True,
                                                     "name_readable": False})
    assert vision.calls == ["first", "reread"]
    req = result.normalized_request
    assert REINSPECTED_NOTE in req.adapter_notes
    assert [r.tool_name for r in req.input_tools_called] == ["analyze_image", "reinspect_image_identity"]
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.VERIFIED and match.best_match.product.product_id == "p-1"


def test_no_permit_uncertain_name_reread_without_new_clue_stays_unresolved():
    result, vision = run_image("品名=虛構牌晶亮", reread={"permit_number": "", "product_name": "虛構牌晶亮",
                                                     "permit_readable": False, "name_readable": True})
    assert vision.calls == ["first", "reread"]  # 最多一次
    assert result.evidence.product_match.status != ProductVerificationStatus.VERIFIED  # 不猜


def test_reread_ignores_unreadable_fields():
    result, vision = run_image("品名=虛構牌晶亮", reread={"permit_number": "A99999", "product_name": "",
                                                     "permit_readable": False, "name_readable": False})
    assert vision.calls == ["first", "reread"]
    assert result.normalized_request.permit_number_candidates == []  # readable=false 的字號不採用


# ============================================================ second pass only when unresolved


@pytest.mark.parametrize("first", [f"字號={A_PERMIT}", f"品名={A_NAME}"])
def test_no_second_pass_when_identity_resolved(first):
    result, vision = run_image(first)
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert vision.calls == ["first"]
    assert all(r.tool_name != "reinspect_image_identity" for r in result.normalized_request.input_tools_called)


def test_no_second_pass_without_image():
    llm = FakeLLM(structured={IntakeExtraction: fake_intake()})
    calls = []
    llm.describe_image = lambda **kw: calls.append(kw) or ""
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    result = pipeline.run(RawUserInput(input_type=InputType.TEXT, text="品名=虛構牌晶亮"))
    assert result.evidence.product_match.status != ProductVerificationStatus.VERIFIED and calls == []


# ============================================================ name + company corroboration


@pytest.fixture
def two_company_variants(monkeypatch):
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [
        _product("co-1", "樣本山藥測試錠", "c-11", "甲樣本測試有限公司", "Z00951"),
        _product("co-2", "樣本山藥測試粒", "c-12", "乙樣本測試有限公司", "Z00952"),
    ])


def test_name_with_company_picks_the_corroborated_candidate(two_company_variants):
    without, _ = run_image("品名=樣本山藥測試", reread={})
    assert without.evidence.product_match.is_ambiguous  # 只有名稱：兩項分數接近，不猜
    result, _ = run_image("品名=樣本山藥測試；公司=乙樣本測試有限公司", reread={})
    match = result.evidence.product_match
    assert match.match_type == ProductMatchType.NAME_COMPANY_CORROBORATED
    assert match.best_match.product.product_id == "co-2" and match.candidate_count == 1
