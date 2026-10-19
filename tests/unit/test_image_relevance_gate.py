"""圖片語意路由回歸：明確無關圖片在查詢前停止；廣告截圖保留 ADVERTISEMENT_CLAIM 並與產品身份分開。

全部使用虛構的 Vision 描述與 TEST_ONLY 產品；Controller 的 image_relevance 以 fake 結構化輸出提供（語意判斷由模型負責，
程式只做 deterministic 守門）。
"""

import json
import pathlib

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import (AD_IDENTITY_UNVERIFIED_NOTE, IMAGE_UNCERTAIN_SUMMARY, IMAGE_UNRELATED_SUMMARY,
                                       ReporterAgent)
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (ClaimContentSource, ClaimOrigin, ClaimSemanticCategory, ImageRelevance, InputType,
                               Intent, ProductVerificationStatus, ResponseType, RiskLevel)
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from tests.builders import claim, extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore

PRODUCT = "虛構牌晶亮測試膠囊"  # synthetic：唯一、有核准功效
UNKNOWN = "完全不存在的虛構廣告品"
IMG = ClaimContentSource.IMAGE_CONTENT
AD = ImageRelevance.HEALTH_SUPPLEMENT_ADVERTISEMENT


def _claim(text, category=ClaimSemanticCategory.HEALTH_FUNCTION, condition=None):
    c = claim(text, category, ClaimOrigin.PRODUCT_LABEL_CLAIM, condition=condition)
    c.content_source = IMG
    return c


SCENARIOS = {
    # 最壞情況：Controller 仍把圖片判成 product_verification 並抽到圖片上的可讀文字，但 image_relevance = UNRELATED
    b"unrelated": extraction(intent=Intent.PRODUCT_VERIFICATION, image_relevance=ImageRelevance.UNRELATED,
                             product_candidates=[PRODUCT], needs_product_lookup=True),
    b"unrelated_text": extraction(intent=Intent.PRODUCT_VERIFICATION, image_relevance=ImageRelevance.UNRELATED,
                                  product_candidates=[UNKNOWN], brand_candidates=["TEST_ONLY 路牌"],
                                  needs_product_lookup=True),
    b"uncertain": extraction(intent=Intent.UNCLEAR, image_relevance=ImageRelevance.UNCERTAIN),
    b"package": extraction(intent=Intent.PRODUCT_VERIFICATION, image_relevance=ImageRelevance.HEALTH_SUPPLEMENT_PRODUCT,
                           product_candidates=[PRODUCT], claims=[_claim("TEST_ONLY 有助於維持測試機能甲")]),
    b"ad_verified": extraction(intent=Intent.PRODUCT_VERIFICATION, image_relevance=AD, product_candidates=[PRODUCT],
                               claims=[_claim("TEST_ONLY 有助於維持測試機能甲")]),
    b"ad_unknown": extraction(intent=Intent.PRODUCT_VERIFICATION, image_relevance=AD, product_candidates=[UNKNOWN],
                              claims=[_claim("TEST_ONLY 補充每日所需營養", ClaimSemanticCategory.NUTRIENT_SUPPLEMENT)]),
    b"ad_medical": extraction(intent=Intent.PRODUCT_VERIFICATION, image_relevance=AD, product_candidates=[UNKNOWN],
                              claims=[_claim("TEST_ONLY 可以治療虛構病", ClaimSemanticCategory.MEDICAL_EFFICACY,
                                             condition="虛構病")]),
}


def run(key: bytes):
    llm = FakeLLM()
    llm.describe_image = lambda *, instructions, image, mime_type: (
        f"TEST_ONLY 圖片：{image.decode()} {' '.join(SCENARIOS[image].product_candidates)}")
    llm.structured[IntakeExtraction] = lambda input_text: SCENARIOS[
        json.loads(input_text)["content"].split("TEST_ONLY 圖片：", 1)[1].split()[0].encode()].model_copy(deep=True)
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    raw = RawUserInput(input_type=InputType.IMAGE, media=[MediaInput(kind="image", content=key, mime_type="image/jpeg")])
    return pipeline.run(raw), llm


def test_clearly_unrelated_image_stops_before_any_lookup():
    result, llm = run(b"unrelated")
    assert result.evidence.tools_called == [] and result.evidence.fast_path_reason == "image_unrelated"
    assert not (llm.called("plan_step") or llm.called("embed") or llm.called("web_search_structured"))
    payload = result.response
    assert payload.response_type == ResponseType.OUT_OF_SCOPE and payload.risk_level is None
    text = payload.plain_text()
    assert IMAGE_UNRELATED_SUMMARY in text and "廣告宣稱" in text and "傳產品包裝照片" in text
    assert "找不到" not in text and "未查到" not in text  # 沒有查產品，所以不能說「查無官方紀錄」


def test_readable_text_on_unrelated_image_does_not_imply_product_verification():
    result, _ = run(b"unrelated_text")
    req = result.normalized_request
    assert req.image_unrelated and req.intent == Intent.OUT_OF_SCOPE and not req.has_product_identifiers
    assert not req.image_identification and not req.needs_product_lookup
    assert result.evidence.tools_called == []


def test_uncertain_image_is_not_guessed_unrelated_and_runs_no_broad_search():
    result, _ = run(b"uncertain")
    assert not result.normalized_request.image_unrelated
    assert IMAGE_UNCERTAIN_SUMMARY in result.response.plain_text()
    assert result.response.response_type == ResponseType.MISSING_INFORMATION


def test_supplement_package_image_keeps_existing_product_flow():
    result, _ = run(b"package")
    req = result.normalized_request
    assert req.image_identification and req.intent == Intent.PRODUCT_VERIFICATION
    assert req.claims == [] and len(req.observed_package_claims) == 1  # 一般包裝宣稱仍只是觀察內容
    assert result.response.response_type == ResponseType.PRODUCT_INFO


def test_package_observation_is_not_labelled_advertisement():
    req = run(b"package")[0].normalized_request
    assert all(c.origin != ClaimOrigin.ADVERTISEMENT_CLAIM for c in req.observed_package_claims + req.claims)


def test_advertisement_with_verified_product_has_identity_and_claim_analysis():
    result, _ = run(b"ad_verified")
    req = result.normalized_request
    assert [c.origin for c in req.claims] == [ClaimOrigin.ADVERTISEMENT_CLAIM] and not req.observed_package_claims
    assert not req.image_identification and req.intent == Intent.CLAIM_VERIFICATION
    assert result.evidence.product_match.status == ProductVerificationStatus.VERIFIED
    assert result.claim_assessment  # 宣稱有被評估
    assert AD_IDENTITY_UNVERIFIED_NOTE not in result.response.plain_text()


def test_advertisement_with_unverified_product_still_analyses_claim():
    result, _ = run(b"ad_unknown")
    assert result.evidence.product_match.status != ProductVerificationStatus.VERIFIED
    assert result.claim_assessment and result.claim_assessment[0].origin == ClaimOrigin.ADVERTISEMENT_CLAIM
    assert AD_IDENTITY_UNVERIFIED_NOTE in result.response.plain_text()  # 身份未確認與宣稱分開說明


def test_medical_ad_claim_is_claim_level_high_and_identity_stays_unverified():
    result, _ = run(b"ad_medical")
    assert result.claim_assessment[0].claim_risk == RiskLevel.HIGH
    assert result.evidence.product_match.status != ProductVerificationStatus.VERIFIED  # 沒有因宣稱而變成「產品高風險」的身份結論
    assert AD_IDENTITY_UNVERIFIED_NOTE in result.response.plain_text()


def test_no_brand_or_object_specific_routing_rules_in_production_code():
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    source = "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*.py"))
    for needle in ("QUAKER", "Quaker", "桂格", "Lamborghini", "藍寶堅尼", "汽車"):
        assert needle not in source
