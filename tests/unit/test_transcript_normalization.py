"""語音逐字稿字形正規化（簡體 → 繁體；最小範圍，只處理字形）。

* 簡體逐字稿在送進 Controller 前轉成繁體字形；已是繁體 → no-op。
* 英文 / 數字 / 許可證字號 / 標點不變；不做詞意改寫、不猜產品、不影響 resolver 的 identity 判斷。
* raw_transcript 與 normalized_transcript 都保存在 request（log / debug）；Controller 與後續使用 normalized。
全部使用 TEST_ONLY 虛構資料。
"""

import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import InputType, Intent, ProductVerificationStatus
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from app.services.text_normalization import normalize_transcript
from app.tools.input_tools import transcribe_audio
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.unit.test_resolver_parity import TranscribingLLM

PRODUCT = "虛構牌晶亮測試膠囊"  # synthetic p-1
SIMPLIFIED_PRODUCT = "虚构牌晶亮测试胶囊"


# ------------------------------------------------------------ 純函式


def test_simplified_transcript_becomes_traditional_with_the_same_meaning():
    result = normalize_transcript(f"请问{SIMPLIFIED_PRODUCT}有哪些成分？")
    assert result.normalized == f"請問{PRODUCT}有哪些成分？" and result.changed
    assert result.raw == f"请问{SIMPLIFIED_PRODUCT}有哪些成分？"


def test_traditional_input_is_a_no_op_including_valid_traditional_variants():
    for text in (f"請問{PRODUCT}有哪些成分？", "台灣 臺灣 裏面 裡面 乾貝 麵 髮 著名 皇后", "衛部健食字第A00235號", ""):
        result = normalize_transcript(text)
        assert result.normalized == text and not result.changed


def test_english_numbers_and_permit_numbers_are_untouched():
    text = "请问 Omega-3 Vitamin B12 500 mg 衛部健食字第A00235號 ABC-123 有哪些成分"
    out = normalize_transcript(text).normalized
    for kept in ("Omega-3", "Vitamin B12", "500 mg", "衛部健食字第A00235號", "ABC-123"):
        assert kept in out
    assert out.startswith("請問") and out.endswith("有哪些成分")


def test_only_script_is_converted_no_synonym_or_phrase_rewriting():
    out = normalize_transcript("这个软件的维生素").normalized
    assert out == "這個軟件的維生素"  # 不改寫成「軟體」等台灣慣用詞
    assert normalize_transcript("台湾").normalized == "台灣"  # 「台」保留


# ------------------------------------------------------------ tool / intake


def test_transcribe_tool_returns_normalized_text_and_keeps_raw():
    out = transcribe_audio(TranscribingLLM(f"请问{SIMPLIFIED_PRODUCT}有哪些成分"), b"aud")
    assert out.text == f"請問{PRODUCT}有哪些成分" and out.raw_text == f"请问{SIMPLIFIED_PRODUCT}有哪些成分"
    assert any("簡體" in n for n in out.notes)
    plain = transcribe_audio(TranscribingLLM(f"請問{PRODUCT}有哪些成分"), b"aud")
    assert plain.text == plain.raw_text and not any("簡體" in n for n in plain.notes)


def audio_request(llm):
    raw = RawUserInput(input_type=InputType.AUDIO, media=[MediaInput(kind="audio", content=b"aud", mime_type="audio/m4a")])
    return IntakeAgent(llm).understand("r-audio", raw)


def test_controller_and_request_use_normalized_transcript_while_raw_is_kept():
    simplified = f"请问{SIMPLIFIED_PRODUCT}有哪些成分？"
    llm = TranscribingLLM(simplified, structured={IntakeExtraction: extraction(
        intent=Intent.INGREDIENT_INQUIRY, product_candidates=[PRODUCT])})
    req = audio_request(llm)
    sent = json.loads(llm.structured_inputs[-1][1])["content"]
    assert sent == f"請問{PRODUCT}有哪些成分？" and "请" not in sent  # Controller 只看到繁體逐字稿
    assert req.raw_transcript == simplified and req.normalized_transcript == f"請問{PRODUCT}有哪些成分？"
    assert req.original_text == req.normalized_transcript  # 使用者介面 / 後續處理使用 normalized
    assert [t.tool_name for t in req.input_tools_called] == ["transcribe_audio"]


def test_traditional_transcript_is_unchanged_end_to_end():
    text = f"請問{PRODUCT}有哪些成分？"
    llm = TranscribingLLM(text, structured={IntakeExtraction: extraction(intent=Intent.INGREDIENT_INQUIRY,
                                                                          product_candidates=[PRODUCT])})
    req = audio_request(llm)
    assert req.raw_transcript == req.normalized_transcript == text
    assert json.loads(llm.structured_inputs[-1][1])["content"] == text


def test_text_input_has_no_transcripts():
    llm = FakeLLM(structured={IntakeExtraction: extraction(intent=Intent.INGREDIENT_INQUIRY, product_candidates=[PRODUCT])})
    req = IntakeAgent(llm).understand("r-text", RawUserInput(input_type=InputType.TEXT, text=f"请问{SIMPLIFIED_PRODUCT}"))
    assert req.raw_transcript is None and req.normalized_transcript is None
    assert "请" in json.loads(llm.structured_inputs[-1][1])["content"]  # 文字輸入不受影響（只處理語音逐字稿）


# ------------------------------------------------------------ 不影響 identity 判斷


def run_audio(transcript, candidate):
    llm = TranscribingLLM(transcript, structured={IntakeExtraction: extraction(
        intent=Intent.INGREDIENT_INQUIRY, product_candidates=[candidate])})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm), verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    raw = RawUserInput(input_type=InputType.AUDIO, media=[MediaInput(kind="audio", content=b"aud", mime_type="audio/m4a")])
    return pipeline.run(raw)


def test_normalization_does_not_auto_confirm_a_misheard_name():
    """簡體逐字稿裡聽錯一個字（替換字）：轉成繁體後仍然只是「使用者說的話」→ 走既有候選確認，不會變成 VERIFIED。"""
    misheard_traditional = PRODUCT.replace("測", "側")  # 同音替換字
    misheard_simplified = "请问" + SIMPLIFIED_PRODUCT.replace("测", "侧") + "有哪些成分"
    result = run_audio(misheard_simplified, misheard_traditional)
    assert result.normalized_request.normalized_transcript == f"請問{misheard_traditional}有哪些成分"
    match = result.evidence.product_match
    assert match.status == ProductVerificationStatus.PARTIAL_MATCH and match.needs_confirmation  # 單一候選，等使用者確認
    assert match.status != ProductVerificationStatus.VERIFIED and result.response.response_type.value == "MISSING_INFORMATION"


def test_exact_spoken_name_resolves_the_same_as_typed_text():
    """說得完全正確（簡體逐字稿）→ 與直接打繁體字完全相同的 exact 規則與結果。"""
    audio = run_audio(f"请问{SIMPLIFIED_PRODUCT}有哪些成分", PRODUCT)
    typed_llm = FakeLLM(structured={IntakeExtraction: extraction(intent=Intent.INGREDIENT_INQUIRY, product_candidates=[PRODUCT])})
    typed = AnalysisPipeline(
        intake=IntakeAgent(typed_llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), typed_llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(typed_llm)).run(
        RawUserInput(input_type=InputType.TEXT, text=f"請問{PRODUCT}有哪些成分"))
    for result in (audio, typed):
        assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert audio.evidence.product_match.match_type == typed.evidence.product_match.match_type


@pytest.mark.parametrize("module", ["app/normalization.py", "app/risk/assessment_rules.py"])
def test_resolver_modules_do_not_depend_on_script_conversion(module):
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / module).read_text(encoding="utf-8")
    assert "text_normalization" not in source and "opencc" not in source.lower()
