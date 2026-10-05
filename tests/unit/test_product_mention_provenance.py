"""產品名稱 provenance：使用者原文 vs Agent A 改字（只用 synthetic 名稱）。

規則：改字後的名稱不得冒充使用者輸入而走 CANONICAL_EXACT；exact / 基本品名 / 字號 / 別名行為不變。
"""

from app.agents.intake_agent import product_mentions
from app.normalization import locate_in_text
from app.schemas.enums import ProductMatchType, ProductMentionProvenance, ProductVerificationStatus
from tests.builders import extraction
from app.schemas.enums import InputType, Intent
from app.schemas.user_request import IntakeExtraction, RawUserInput
from tests.fakes import FakeLLM
from tests.unit.test_p1_fastpath_dedup import CLARIFICATION_MODE, _add_products, _pipeline


def run(ex, text):
    llm = FakeLLM(structured={IntakeExtraction: ex})
    return _pipeline(llm).run(RawUserInput(input_type=InputType.TEXT, text=text)), llm


def test_format_only_difference_is_user_text():
    products, corrected, mentions = product_mentions("請問 虛構ＡＢ 測試錠 有效嗎", ["虛構AB測試錠"])
    assert mentions[0].provenance == ProductMentionProvenance.USER_TEXT and not corrected
    assert products == ["虛構AB測試錠"]


def test_changed_characters_keep_raw_text_and_move_correction_aside():
    products, corrected, mentions = product_mentions("請問虛構精采測試錠有效嗎", ["虛構晶彩測試錠"])
    m = mentions[0]
    assert m.provenance == ProductMentionProvenance.AGENT_CORRECTED and m.correction_applied and m.edit_count >= 1
    assert products == ["虛構精采測試錠"] and corrected == ["虛構晶彩測試錠"]
    assert m.raw_text == "虛構精采測試錠" and m.corrected_name == "虛構晶彩測試錠"


def test_name_absent_from_input_is_not_user_wording():
    products, corrected, mentions = product_mentions("這個好嗎", ["完全無關的名稱測試"])
    assert mentions[0].provenance == ProductMentionProvenance.NOT_IN_USER_TEXT and not corrected


def test_locate_in_text_rejects_unrelated():
    assert locate_in_text("今天天氣很好", "虛構晶彩測試錠")[0] is None


def test_corrected_spelling_never_becomes_canonical_exact(monkeypatch):
    """使用者寫錯字、Agent A 改成官方品名：不可 CANONICAL_EXACT / VERIFIED，必須走確認路徑。"""
    _add_products(monkeypatch, ["虛構晶彩測試錠"])
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=["虛構晶彩測試錠"])
    result, _ = run(ex, text="請問虛構精采測試錠有效嗎")
    m = result.evidence.product_match
    assert m.match_type != ProductMatchType.CANONICAL_EXACT
    assert m.status != ProductVerificationStatus.VERIFIED
    assert result.evidence.planner_mode == CLARIFICATION_MODE


def test_exact_user_wording_still_canonical_exact(monkeypatch):
    _add_products(monkeypatch, ["虛構晶彩測試錠"])
    ex = extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=["虛構晶彩測試錠"])
    result, _ = run(ex, text="請問虛構晶彩測試錠有效嗎")
    m = result.evidence.product_match
    assert m.status == ProductVerificationStatus.VERIFIED and m.match_type == ProductMatchType.CANONICAL_EXACT

