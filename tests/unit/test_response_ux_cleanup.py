"""Adaptive Response UX cleanup 回歸測試。

* 查證對象還沒確定（多 / 低信心候選、只有相似名稱 / 公司、圖片無法確認、完全沒有產品資訊）→ MISSING_INFORMATION，
  不顯示風險燈號、不放完整分析 / 為什麼 / 資料來源；已有明確對象但證據不完整 → MEDIUM 風險卡仍可能出現。
* 圖片尚未確認產品時，簡短顯示「圖片辨識到：」；有候選時沿用候選卡。
* 官方資訊卡改為摘要式；完整功效 / 警語 / 來源由下一層 request-scoped 按鈕載入。

全部使用 TEST_ONLY 虛構產品與 fake，不呼叫外部服務。
"""

import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent, detail_payload
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.response.actions import build_postback_data, build_select_product_data
from app.response.line_renderer import AdaptiveLineRenderer
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import InputType, Intent, ResponseAction, ResponseType, RiskLevel
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product
from tests.unit.test_line_webhook import FakeLine, event, send

PRODUCT = "虛構牌晶亮測試膠囊"  # synthetic：p-1
COMPANY = "虛構測試生技股份有限公司"
EXPIRED = "虛構牌過期測試錠"  # TEST_ONLY：許可證已失效、無違規紀錄
FAMILY = "虛構牌整理測試飲"  # TEST_ONLY：兩個品項
UNKNOWN = "完全不存在的虛構測試品"
LONG_CLAIM = "TEST_ONLY 經動物實驗結果證實：有助於維持測試機能甲，並可能有助於調節虛構指標，詳細試驗設計與劑量如官方核准內容所載。"
LONG_WARNING = "TEST_ONLY 本品不適合孕婦食用。服用抗凝血藥物者請先諮詢醫師；請依建議量食用。"
NO_ANALYSIS = {ResponseAction.DETAIL, ResponseAction.WHY, ResponseAction.SOURCES}


@pytest.fixture(autouse=True)
def ux_data(monkeypatch):
    extra = [_product("exp-1", EXPIRED, "c-1", COMPANY, "Z00950", status="EXPIRED")]
    extra += [_product(f"fam-{i}", f"{FAMILY}({x})", "c-1", COMPANY, f"Z0096{i}") for i, x in enumerate(("甲款", "乙款"))]
    claims = [{"product_claim_id": "pc-long", "product_id": "p-1", "approved_claim_id": "ac-long",
               "function_name": "測試功效乙", "claim_text": LONG_CLAIM, **SOURCE}]
    claims += [{"product_claim_id": f"pc-{p['product_id']}", "product_id": p["product_id"], "approved_claim_id": "ac-f",
                "function_name": "測試功效甲", "claim_text": "TEST_ONLY 有助於維持測試機能甲。", **SOURCE} for p in extra]
    warnings = [{"product_id": "p-1", "warning_id": "w-long", "warning_type": "PRECAUTION", "text": LONG_WARNING, **SOURCE}]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + extra)
    monkeypatch.setattr(data, "APPROVED_CLAIMS", data.APPROVED_CLAIMS + claims)
    monkeypatch.setattr(data, "WARNINGS", data.WARNINGS + warnings)


def build_pipeline(llm):
    return AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )


def run_text(ex: IntakeExtraction):
    llm = FakeLLM(structured={IntakeExtraction: ex})
    return build_pipeline(llm).run(RawUserInput(input_type=InputType.TEXT, text="TEST_ONLY")), llm


def run_image(ex: IntakeExtraction):
    llm = FakeLLM(structured={IntakeExtraction: ex})
    raw = RawUserInput(input_type=InputType.IMAGE, media=[MediaInput(kind="image", content=b"x", mime_type="image/jpeg")])
    return build_pipeline(llm).run(raw), llm


def assert_missing_info(payload):
    assert payload.response_type == ResponseType.MISSING_INFORMATION
    assert payload.risk_level is None  # 沒有風險燈號
    assert not (set(payload.available_actions) & NO_ANALYSIS)
    assert len(payload.available_actions) <= 3


# ============================================================ 1–2. Missing-info semantics / actions


def test_company_only_match_is_missing_information_not_medium_risk():
    result, _ = run_text(extraction(intent=Intent.PRODUCT_VERIFICATION, company_candidates=[COMPANY]))
    assert result.evidence.product_match.status.value == "PARTIAL_MATCH"
    assert result.risk.risk_level == RiskLevel.MEDIUM  # Risk Engine 規則不變
    assert_missing_info(result.response)  # 只是回覆型態不同：還不知道是哪一項


def test_product_question_without_any_product_is_missing_information():
    result, _ = run_text(extraction(intent=Intent.PRODUCT_VERIFICATION))
    assert_missing_info(result.response)


def test_ambiguous_text_product_is_missing_information_with_choices_only():
    result, _ = run_text(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[FAMILY]))
    payload = result.response
    assert_missing_info(payload)
    assert len(payload.product_choices) == 2 and payload.available_actions == []


def test_verified_product_with_inactive_permit_still_medium_risk():
    result, _ = run_text(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[EXPIRED]))
    assert result.response.response_type == ResponseType.RISK
    assert result.response.risk_level == RiskLevel.MEDIUM


def test_named_product_not_found_keeps_conservative_risk_card():
    """有明確的查證對象（使用者指名的產品）但查無紀錄：仍是既有的保守 MEDIUM 卡（不等於假貨）。"""
    result, _ = run_text(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[UNKNOWN]))
    assert result.response.response_type == ResponseType.RISK and result.response.risk_level == RiskLevel.MEDIUM
    assert any("不代表" in w for w in result.response.warnings)


def test_missing_information_offers_retry_only_when_a_source_failed():
    from app.agents.reporter_agent import compose_payload

    result, _ = run_text(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[FAMILY]))
    assert result.response.available_actions == []  # 正常：只有候選按鈕，請使用者選
    failed = result.risk.model_copy(update={"data_incomplete": True})
    payload = compose_payload(result.normalized_request, result.evidence, failed)
    assert payload.available_actions == [ResponseAction.RETRY]  # 資料來源暫時失敗：可重新查詢


# ============================================================ 3–4. Image recognition summary / candidates


def test_image_not_found_shows_short_recognized_content():
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[UNKNOWN], brand_candidates=["虛構測試牌"],
                    permit_number_candidates=["測試健食字第Z99999號"], company_candidates=["範例虛構公司"],
                    ingredient_candidates=["虛構素甲", "虛構素乙"])
    result, _ = run_image(ex)
    payload = result.response
    assert_missing_info(payload)
    assert payload.recognized_content == [f"產品名稱：{UNKNOWN}", "品牌：虛構測試牌", "許可證字號：測試健食字第Z99999號",
                                          "公司：範例虛構公司"]  # 最多 4 項，不是整張 OCR
    text = json.dumps(AdaptiveLineRenderer().render_primary(payload), ensure_ascii=False)
    assert "圖片辨識到：" in text and "虛構素乙" not in text
    assert "許可證字號" in payload.plain_text() and "不代表是假貨" in payload.plain_text()


def test_image_without_any_identifier_asks_for_clearer_photo():
    result, _ = run_image(extraction(intent=Intent.UNCLEAR))
    payload = result.response
    assert_missing_info(payload)
    assert payload.recognized_content == [] and not payload.product_choices
    assert "沒有辨識到產品名稱" in payload.summary and any("拍清楚" in p for p in payload.key_points)


def test_verified_image_has_no_recognized_block():
    result, _ = run_image(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT]))
    assert result.response.response_type == ResponseType.PRODUCT_INFO and result.response.recognized_content == []


def build_line(ex):
    from app.line.handler import LineWebhookHandler
    from app.response.service import AdaptiveResponseService
    from app.response.store import InMemoryRequestStore
    from app.services.context_store import TaskContextStore

    llm = FakeLLM(structured={IntakeExtraction: ex})
    store = InMemoryRequestStore()
    svc = AdaptiveResponseService(build_pipeline(llm), ReporterAgent(llm), store, timeout_seconds=5.0,
                                  context_store=TaskContextStore())
    line = FakeLine()
    line.contents = {"img": (b"x", "image/jpeg")}
    return LineWebhookHandler(svc, line, TaskContextStore()), line, store


def test_image_candidates_use_choice_card_then_resume_to_product_info():
    handler, line, store = build_line(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[FAMILY]))
    [rid] = send(handler, event(kind="image", message_id="img"))
    payload = store.get(rid).response
    assert_missing_info(payload)
    assert [c.full_name for c in payload.product_choices] == [f"{FAMILY}(甲款)", f"{FAMILY}(乙款)"]
    card = json.dumps(line.sent[-1][2], ensure_ascii=False)
    assert "選擇：甲款" in card and "測試健食字第Z00960號" in card and "圖片辨識到：" in card
    send(handler, event(kind="postback", data=build_select_product_data(rid, "fam-1")))
    rec = list(store._data.values())[-1]
    assert rec.response.response_type == ResponseType.PRODUCT_INFO
    assert rec.evidence.product_match.best_match.product.product_id == "fam-1"


# ============================================================ 5–6. Compact official info / detail cards


def _verified():
    result, _ = run_text(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT]))
    return result


def test_official_info_is_compact_summary_with_follow_up_actions():
    r = _verified()
    detail = detail_payload(ResponseAction.OFFICIAL_INFO, r.normalized_request, r.evidence, r.risk)
    body = "\n".join(detail.body)
    assert detail.body[0] == f"官方品名：{PRODUCT}"
    assert "許可證字號：測試健食字第Z00001號" in body and "許可狀態：許可證有效" in body and f"申請商：{COMPANY}" in body
    assert "核准功效：測試功效甲、測試功效乙" in body
    assert len(detail.body) <= 6
    assert "核可日期" not in body and "官方警語" not in body and LONG_WARNING not in body
    assert LONG_CLAIM not in body  # 長篇原文不在第一層
    assert detail.available_actions == [ResponseAction.FULL_EFFICACY, ResponseAction.WARNINGS, ResponseAction.SOURCES]


def test_full_efficacy_detail_shows_full_deduped_wording():
    r = _verified()
    detail = detail_payload(ResponseAction.FULL_EFFICACY, r.normalized_request, r.evidence, r.risk)
    assert "【測試功效乙】\n" + LONG_CLAIM in detail.body
    assert sum(LONG_CLAIM in p for p in detail.body) == 1 and len(detail.body) == 2


def test_warning_detail_is_split_into_points():
    r = _verified()
    detail = detail_payload(ResponseAction.WARNINGS, r.normalized_request, r.evidence, r.risk)
    assert "・TEST_ONLY 本品不適合孕婦食用。" in detail.body
    assert "・服用抗凝血藥物者請先諮詢醫師；" in detail.body and "・請依建議量食用。" in detail.body
    assert all(p.startswith("・") for p in detail.body)


def test_detail_follow_up_buttons_are_request_scoped_and_reachable_only_via_official_info():
    handler, line, store = build_line(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[PRODUCT]))
    [rid] = send(handler, event(text=PRODUCT))
    assert ResponseAction.OFFICIAL_INFO in store.get(rid).response.available_actions
    send(handler, event(kind="postback", data=build_postback_data(ResponseAction.OFFICIAL_INFO, rid)))
    card = line.sent[-1][2]
    labels = [c["action"]["label"] for c in card["contents"]["footer"]["contents"]]
    assert labels == ["查看完整功效", "警語與注意事項", "查看資料來源"]
    send(handler, event(kind="postback", data=build_postback_data(ResponseAction.FULL_EFFICACY, rid)))
    assert LONG_CLAIM in json.dumps(line.sent[-1][2], ensure_ascii=False)
    send(handler, event(kind="postback", data=build_postback_data(ResponseAction.WARNINGS, rid)))
    assert "請依建議量食用" in json.dumps(line.sent[-1][2], ensure_ascii=False)


def test_follow_up_action_rejected_when_primary_card_had_no_official_info():
    handler, line, store = build_line(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[FAMILY]))
    [rid] = send(handler, event(text=FAMILY))  # 確認卡：沒有官方資訊按鈕
    send(handler, event(kind="postback", data=build_postback_data(ResponseAction.FULL_EFFICACY, rid)))
    assert "這一題沒有這項內容" in json.dumps(line.sent[-1][2], ensure_ascii=False)
