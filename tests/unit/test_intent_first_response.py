"""Intent-first primary response＋semantic color＋NOT_FOUND semantics 回歸測試（全部 TEST_ONLY 虛構資料）。

* 第一層先回答使用者問的事：功效 → 功效；登記 → 登記狀態；說法 → 說法風險；成分 → 成分。
* 語意顏色：GREEN 官方確認 / 核准；ORANGE 資料不足、查無涵蓋紀錄、登記≠健康食品核准；RED 高風險宣稱。只替關鍵短語上色。
* 查無產品 = MISSING_INFORMATION：無風險燈號、不說中風險、列出涵蓋類別、不說假貨或違法。
* FC-E8：primary 最多 3 個重點。
"""

import json

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent, detail_payload
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.product_coverage import CATEGORY_LABELS
from app.response.actions import ACTION_LABELS, reachable_actions
from app.response.line_renderer import COLOR_BODY, MAX_KEY_POINTS, TONE_COLORS, AdaptiveLineRenderer
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimOrigin,
    ClaimSemanticCategory,
    ErrorCode,
    InputType,
    Intent,
    ProductCategory,
    ResponseAction,
    ResponseType,
    RiskLevel,
    SemanticTone,
)
from app.schemas.user_request import IntakeExtraction, RawUserInput
from tests.builders import claim, extraction, user_text
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE
from tests.unit.test_product_coverage_runtime import DOM_NAME, IMP_NAME, coverage_products  # noqa: F401（fixture）

R = AdaptiveLineRenderer()
HF_NAME = "虛構牌晶亮測試膠囊"  # TEST_ONLY：synthetic_data 中有核准功效的健康食品
DOM, IMP = ProductCategory.DOMESTIC_VITAMIN_TABLET_CAPSULE, ProductCategory.IMPORTED_TABLET_CAPSULE


def run(ex: IntakeExtraction, repo=None):
    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(repo or InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    return pipeline.run(RawUserInput(input_type=InputType.TEXT, text=user_text(ex, "TEST_ONLY 問題")))


def card_json(payload) -> str:
    return json.dumps(R.render_primary(payload), ensure_ascii=False)


def texts(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("type") == "text":
            out.append(node)
        for v in node.values():
            texts(v, out)
    elif isinstance(node, list):
        for v in node:
            texts(v, out)
    return out


def bullet_rows(message) -> int:
    """primary 第一層重點數（horizontal box＝一個重點）。"""
    count = 0

    def walk(node):
        nonlocal count
        if isinstance(node, dict):
            if node.get("type") == "box" and node.get("layout") == "horizontal":
                count += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(message)
    return count


# ============================================================ 功效詢問


def test_health_food_efficacy_inquiry_answers_functions_first():
    result = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[HF_NAME], needs_product_lookup=True))
    p = result.response
    assert p.response_type == ResponseType.PRODUCT_INFO and p.risk_level is None
    assert p.title == "核准功效查詢"
    assert (p.status_line, p.status_tone) == ("官方核准功效", SemanticTone.GREEN)
    assert p.key_points == ["測試功效甲"]  # 只有功效名稱
    assert p.available_actions == [ResponseAction.FULL_EFFICACY, ResponseAction.OFFICIAL_INFO, ResponseAction.SOURCES]
    text = p.plain_text()
    assert "申請商" not in text and "許可證字號" not in text  # 登記細節在第二層
    assert ACTION_LABELS[ResponseAction.OFFICIAL_INFO] == "查看產品資訊"
    assert ACTION_LABELS[ResponseAction.FULL_EFFICACY] == "查看完整功效"


def _assert_registration_efficacy(result, category):
    p = result.response
    assert p.response_type == ResponseType.PRODUCT_INFO and p.risk_level is None and p.title == "核准功效查詢"
    assert (p.status_line, p.status_tone) == ("未有健康食品核准保健功效資料", SemanticTone.ORANGE)
    assert "查有官方查驗登記／核備紀錄，但這類登記不等於健康食品許可" in p.summary
    assert "不能由這筆資料認定有核准保健功效" in p.summary
    assert "沒有任何功效" not in p.plain_text() and "沒有功效" not in p.plain_text()
    assert "申請商" not in p.plain_text() and "有效日期" not in p.plain_text()
    assert p.key_points == [f"資料類別：{CATEGORY_LABELS[category]}"]
    assert p.available_actions == [ResponseAction.REGISTRATION_INFO, ResponseAction.SOURCES]
    assert ResponseAction.FULL_EFFICACY not in p.available_actions and result.evidence.approved_claims == []
    detail = detail_payload(ResponseAction.REGISTRATION_INFO, result.normalized_request, result.evidence, result.risk)
    body = "\n".join(detail.body)
    assert detail.title == "產品登記資料" and "申請商" in body and "有效日期" in body  # 登記細節在第二層
    assert ResponseAction.REGISTRATION_INFO in reachable_actions(p.available_actions)
    assert ACTION_LABELS[ResponseAction.REGISTRATION_INFO] == "查看產品登記資料"


def test_domestic_efficacy_inquiry_says_no_health_food_approval(coverage_products):
    result = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[DOM_NAME], needs_product_lookup=True))
    _assert_registration_efficacy(result, DOM)


def test_imported_efficacy_inquiry_says_no_health_food_approval(coverage_products):
    result = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[IMP_NAME], needs_product_lookup=True))
    _assert_registration_efficacy(result, IMP)


# ============================================================ 登記 / 說法 / 成分


def test_product_verification_answers_registration_first(coverage_products):
    hf = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[HF_NAME], needs_product_lookup=True)).response
    assert hf.response_type == ResponseType.PRODUCT_INFO and hf.risk_level is None
    assert (hf.status_line, hf.status_tone) == ("查有健康食品許可（許可證有效）", SemanticTone.GREEN)
    assert hf.summary.startswith("官方紀錄中有") and ResponseAction.FULL_EFFICACY not in hf.available_actions
    reg = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[DOM_NAME], needs_product_lookup=True)).response
    assert (reg.status_line, reg.status_tone) == ("查有官方查驗登記／核備紀錄", SemanticTone.GREEN)
    assert ResponseAction.REGISTRATION_INFO in reg.available_actions and ResponseAction.OFFICIAL_INFO not in reg.available_actions


def test_claim_verification_answers_claim_risk_first():
    q = claim("TEST_ONLY 可以維持測試機能甲嗎", function="測試功效甲")
    p = run(extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[HF_NAME], claims=[q],
                       needs_product_lookup=True, needs_claim_check=True)).response
    assert p.response_type == ResponseType.CLAIM_RESULT and p.claim_risk_level is not None
    assert p.status_line is None  # 說法查證用既有的宣稱風險標示
    assert R.claim_badge(p) is not None


def test_high_risk_medical_claim_is_still_red():
    ad = claim("TEST_ONLY 可以治療某疾病", category=ClaimSemanticCategory.MEDICAL_EFFICACY,
               origin=ClaimOrigin.ADVERTISEMENT_CLAIM, condition="某疾病")
    p = run(extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[HF_NAME], claims=[ad],
                       needs_product_lookup=True, needs_claim_check=True)).response
    assert p.response_type == ResponseType.RISK and p.risk_level == RiskLevel.HIGH and p.tone == SemanticTone.RED
    heading = next(t for t in texts(R.render_primary(p)) if t["text"].startswith("🔴"))
    assert heading["color"] == TONE_COLORS[SemanticTone.RED]


def test_ingredient_inquiry_on_registration_product_answers_ingredients_first(coverage_products):
    p = run(extraction(intent=Intent.INGREDIENT_INQUIRY, product_candidates=[DOM_NAME], needs_product_lookup=True)).response
    assert p.response_type == ResponseType.INGREDIENT_LIST
    assert (p.status_line, p.status_tone) == ("未有已整理的成分資料", SemanticTone.ORANGE)
    assert "成分" in p.summary


# ============================================================ NOT_FOUND


def _not_found():
    return run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=["TEST_ONLY 完全不存在的產品"],
                          needs_product_lookup=True))


def test_not_found_is_missing_information_without_risk_light():
    result = _not_found()
    p = result.response
    assert result.risk.risk_level == RiskLevel.MEDIUM  # Risk Engine 不變
    assert p.response_type == ResponseType.MISSING_INFORMATION and p.risk_level is None and p.claim_risk_level is None
    assert p.title == "需要更多資訊"
    card = card_json(p)
    for banned in ("中風險", "MEDIUM", "低風險", "高風險", "先以", "🔴"):
        assert banned not in card and banned not in p.plain_text()


def test_not_found_lists_coverage_and_does_not_imply_fake():
    p = _not_found().response
    assert (p.status_line, p.status_tone) == ("未查到對應官方紀錄", SemanticTone.ORANGE)
    text = p.plain_text()
    for label in CATEGORY_LABELS.values():
        assert label in text
    assert "查不到紀錄不代表產品是假貨或違法" in text and "一般食品" not in text
    assert "完整品名" in text and "包裝照片" in text and "證號" in text
    assert text.count("查不到紀錄不代表產品是假貨或違法") == 1  # 附註不重複


def test_missing_official_record_uses_orange_not_red():
    p = _not_found().response
    nodes = texts(R.render_primary(p))
    status = next(t for t in nodes if t["text"].startswith("🟠"))
    assert status["text"] == "🟠 未查到對應官方紀錄" and status["color"] == TONE_COLORS[SemanticTone.ORANGE]
    assert all(t["color"] != TONE_COLORS[SemanticTone.RED] for t in nodes)
    summary = next(t for t in nodes if t["text"] == p.summary)
    assert summary["color"] == COLOR_BODY  # 只替關鍵短語上色，內文維持黑字


def test_lookup_failure_is_missing_information_with_retry():
    result = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=["TEST_ONLY 某產品"],
                            needs_product_lookup=True), repo=InMemoryOfficialRepo(fail_with=ErrorCode.UNAVAILABLE))
    p = result.response
    assert p.response_type == ResponseType.MISSING_INFORMATION and p.risk_level is None
    assert (p.status_line, p.status_tone) == ("暫時無法完成產品查詢", SemanticTone.ORANGE)
    assert ResponseAction.RETRY in p.available_actions and "查無" not in p.summary


# ============================================================ FC-E8


def test_primary_shows_at_most_three_points(monkeypatch):
    monkeypatch.setattr(data, "APPROVED_CLAIMS", data.APPROVED_CLAIMS + [
        {"product_claim_id": f"pc-many-{i}", "product_id": "p-1", "approved_claim_id": f"ac-many-{i}",
         "function_name": f"測試功效多{i}", "claim_text": f"TEST_ONLY 敘述{i}", **SOURCE} for i in range(4)])
    p = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[HF_NAME], needs_product_lookup=True)).response
    assert len(p.key_points) == 5
    msg = R.render_primary(p)
    assert bullet_rows(msg) == MAX_KEY_POINTS == 3
    assert "還有 2 項" in json.dumps(msg, ensure_ascii=False)
