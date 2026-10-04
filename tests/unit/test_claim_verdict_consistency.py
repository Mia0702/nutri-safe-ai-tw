"""Claim verdict 一致性 / USER_QUESTION 命題風險 / Flex 版面 回歸測試。

* Primary card、「為什麼？」、完整分析都只讀同一份 ClaimAssessment（claim_risk + policy），不各自推論。
* 產品特定提問：ApprovedClaim → 官方功效名稱 → 命題；部分重疊 → INSUFFICIENT_EVIDENCE。
* USER_QUESTION 保持提問角色；疾病治療命題 → claim_risk HIGH（不是使用者違法、不是產品危險）。
* Flex：區塊 / 重點是獨立 component；primary 最多 3 點；字體 scaling 契約不退化。

全部使用虛構產品（synthetic_data p-1：虛構牌晶亮測試膠囊，核准功效「測試功效甲」）與虛構宣稱。
"""

import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent, claim_verdict_line
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.response.line_renderer import BUTTON_HEIGHT, MAX_KEY_POINTS, TEXT_SIZES, AdaptiveLineRenderer
from app.risk.claim_policy import match_approved_function
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimOrigin,
    ClaimPolicyResult,
    ClaimSemanticCategory,
    InputType,
    Intent,
    MedicalAssertionType,
    ResponseAction,
    ResponseType,
    RiskLevel,
    SourceType,
)
from app.schemas.evidence import ApprovedClaimEvidence, SourceRef
from app.schemas.response import CLAIM_RISK_LABEL, DetailPayload, ResponsePayload
from app.schemas.user_request import IntakeExtraction, RawUserInput
from tests.builders import claim, extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore

PRODUCT = "虛構牌晶亮測試膠囊"
FUNCTION = "測試功效甲"
HF = ClaimSemanticCategory.HEALTH_FUNCTION
MED = ClaimSemanticCategory.MEDICAL_EFFICACY
M = MedicalAssertionType
Q = ClaimOrigin.USER_QUESTION
AD = ClaimOrigin.ADVERTISEMENT_CLAIM
R = AdaptiveLineRenderer()
RID = "00000000-0000-4000-8000-0000000000aa"


def c(text, origin=Q, category=HF, medical=M.NONE, function=None, condition=None):
    out = claim(text, category, origin, function=function, condition=condition)
    out.medical_assertion = medical
    return out


def run(*claims, llm_text="TEST_ONLY 白話回覆正文。", intent=Intent.CLAIM_VERIFICATION):
    ex = extraction(intent=intent, product_candidates=[PRODUCT], claims=list(claims))
    llm = FakeLLM(structured={IntakeExtraction: lambda _: ex.model_copy(deep=True)}, text=llm_text)
    pipe = AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )
    return pipe.run(RawUserInput(input_type=InputType.TEXT, text="TEST_ONLY")), llm


def details(result, llm, *actions):
    reporter = ReporterAgent(llm)
    return {a: reporter.detail(a, result.normalized_request, result.evidence, result.risk) for a in actions}


def flex_texts(message):
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(message)
    return out


# ================================================================ health-function matching


def test_approved_health_function_question_is_supported_and_green_everywhere():
    result, llm = run(c(f"{PRODUCT}可以{FUNCTION}嗎", function=FUNCTION))
    ca = result.evidence.claim_assessment[0]
    assert ca.origin == Q and ca.status == ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL
    assert ca.policy.policy_result == ClaimPolicyResult.APPROVED_SCOPE_CONSISTENT
    assert ca.policy.match_method == "health_function_name" and ca.policy.matched_function_name == FUNCTION
    assert ca.claim_risk == RiskLevel.LOW
    p = result.response
    assert p.response_type == ResponseType.CLAIM_RESULT and p.claim_risk_level == RiskLevel.LOW
    assert "官方核准的保健功效範圍內" in p.summary
    card = json.dumps(R.render_primary(p), ensure_ascii=False)
    assert CLAIM_RISK_LABEL[RiskLevel.LOW] in card
    assert "找不到官方資料支持" not in card and "找不到官方資料支持" not in p.plain_text()


def test_function_named_in_text_without_target_needs_the_catalog():
    """只看產品核准功效無法排除問句還提到其他功效：沒有保健功效目錄時不以文字包含直接判定
    （有目錄時見 test_health_function_fast_path.py）。"""
    result, _ = run(c(f"{PRODUCT}有{FUNCTION}的效果嗎"))
    policy = result.evidence.claim_assessment[0].policy
    assert policy is None or policy.match_method not in ("health_function_name", "health_function_catalog")


@pytest.mark.parametrize("target", ["測試功效", "測試功效甲與測試功效乙"])
def test_partially_overlapping_health_function_is_insufficient_not_supported(target):
    result, _ = run(c(f"{PRODUCT}可以{target}嗎", function=target))
    ca = result.evidence.claim_assessment[0]
    assert ca.status == ClaimAssessmentStatus.UNKNOWN
    assert ca.policy.policy_result == ClaimPolicyResult.INSUFFICIENT_EVIDENCE
    assert ca.claim_risk == RiskLevel.MEDIUM and result.response.claim_risk_level == RiskLevel.MEDIUM


def test_two_different_approved_functions_in_one_question_is_ambiguous():
    src = SourceRef(source_name="TEST_ONLY", source_type=SourceType.STRUCTURED_DB, is_official=True)
    approved = [ApprovedClaimEvidence(evidence_id=f"ac:{i}", product_id="p", approved_claim_id=f"a{i}",
                                      function_name=name, claim_text=f"TEST_ONLY {name}", source=src)
                for i, name in enumerate(["測試功效甲", "測試功效乙"])]
    assert match_approved_function(c("可以測試功效甲和測試功效乙嗎"), approved) == (None, True)
    unclassified = [a.model_copy(update={"function_classified": False}) for a in approved]
    assert match_approved_function(c("可以測試功效甲嗎", function="測試功效甲"), unclassified) == (None, False)


def test_absolute_wording_question_is_not_confirmed_by_function_name():
    q = claim(f"{PRODUCT}保證{FUNCTION}嗎", HF, Q, absolute=True, function=FUNCTION)
    result, _ = run(q)
    assert result.evidence.claim_assessment[0].status != ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL


# ================================================================ verdict consistency


def test_primary_why_and_full_analysis_use_the_same_verdict():
    result, llm = run(c(f"{PRODUCT}可以{FUNCTION}嗎", function=FUNCTION),
                      c("TEST_ONLY 可以治療虛構病嗎", category=MED, medical=M.TREATS_OR_CURES_DISEASE, condition="虛構病"))
    lines = [claim_verdict_line(ca) for ca in result.evidence.claim_assessment]
    d = details(result, llm, ResponseAction.WHY, ResponseAction.DETAIL)
    full = "\n".join(d[ResponseAction.DETAIL].body)
    for line in lines:
        assert line in result.response.key_points
        assert line in d[ResponseAction.WHY].body
        assert line in full
    # 完整分析標題與 primary card 相同的宣稱風險（取最高）
    assert d[ResponseAction.DETAIL].body[0] == f"說法查證結果｜{CLAIM_RISK_LABEL[RiskLevel.HIGH]}"
    assert result.response.claim_risk_level == RiskLevel.HIGH


def test_full_analysis_llm_text_contradicting_verdict_falls_back_to_template():
    result, llm = run(c(f"{PRODUCT}可以{FUNCTION}嗎", function=FUNCTION),
                      llm_text="TEST_ONLY 目前找不到官方資料支持這個說法。")
    detail = details(result, llm, ResponseAction.DETAIL)[ResponseAction.DETAIL]
    assert detail.used_fallback_template
    assert "找不到官方資料支持" not in "\n".join(detail.body)


# ================================================================ USER_QUESTION vs claim risk


def test_medical_treatment_question_keeps_role_and_shows_high_claim_risk_without_blame():
    result, llm = run(c("TEST_ONLY 可以治療虛構病嗎", category=MED, medical=M.TREATS_OR_CURES_DISEASE, condition="虛構病"))
    ca = result.evidence.claim_assessment[0]
    assert ca.origin == Q and not ca.is_product_side_assertion
    assert ca.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM and ca.claim_risk == RiskLevel.HIGH
    assert result.risk.risk_level != RiskLevel.HIGH  # 產品風險不因提問升級
    p = result.response
    assert p.response_type == ResponseType.CLAIM_RESULT and p.risk_level is None
    assert p.claim_risk_level == RiskLevel.HIGH
    why = details(result, llm, ResponseAction.WHY)[ResponseAction.WHY]
    text = p.plain_text() + json.dumps(R.render_primary(p), ensure_ascii=False) + "\n".join(why.body)
    assert "若把這句話當成產品功效宣稱" in text and CLAIM_RISK_LABEL[RiskLevel.HIGH] in text
    assert "你違法" not in text.replace("不是指你違法", "") and "產品本身危險" not in text.replace("不是指產品本身危險", "")
    assert "找不到官方資料支持" not in text


def test_advertisement_medical_claim_is_high():
    result, _ = run(c("TEST_ONLY 可以治療虛構病", origin=AD, category=MED, medical=M.TREATS_OR_CURES_DISEASE,
                      condition="虛構病"))
    ca = result.evidence.claim_assessment[0]
    assert ca.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM and ca.claim_risk == RiskLevel.HIGH
    assert result.risk.risk_level == RiskLevel.HIGH and result.response.response_type == ResponseType.RISK


@pytest.mark.parametrize("origin", [Q, AD])
def test_unsupported_non_medical_claim_is_not_high(origin):
    result, _ = run(c("TEST_ONLY 可以讓虛構毛髮變色", origin=origin))
    ca = result.evidence.claim_assessment[0]
    assert ca.status in (ClaimAssessmentStatus.UNSUPPORTED, ClaimAssessmentStatus.UNKNOWN)
    assert ca.claim_risk == RiskLevel.MEDIUM and result.risk.risk_level != RiskLevel.HIGH


# ================================================================ product info / payload contract


def test_product_info_does_not_get_a_risk_light():
    result, _ = run(intent=Intent.EFFICACY_INQUIRY)
    p = result.response
    assert p.response_type == ResponseType.PRODUCT_INFO and p.risk_level is None and p.claim_risk_level is None
    card = json.dumps(R.render_primary(p), ensure_ascii=False)
    assert not any(m in card for m in ("🟢", "🟠", "🔴"))


def test_claim_risk_level_is_only_allowed_on_claim_result():
    with pytest.raises(ValueError):
        ResponsePayload(request_id=RID, response_type=ResponseType.PRODUCT_INFO, title="t", summary="s",
                        claim_risk_level=RiskLevel.LOW)


# ================================================================ Flex layout / typography


def _claim_payload(n_points=5):
    return ResponsePayload(
        request_id=RID, response_type=ResponseType.CLAIM_RESULT, claim_risk_level=RiskLevel.MEDIUM,
        title="說法查證結果", summary="TEST_ONLY 一句結論",
        key_points=[f"🟠 TEST_ONLY 重點 {i} " + "說明" * 20 for i in range(n_points)],
        warnings=["TEST_ONLY 警語"], available_actions=[ResponseAction.DETAIL, ResponseAction.SOURCES, ResponseAction.WHY],
        source_count=1)


def _bullet_boxes(message):
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "box" and node.get("layout") == "horizontal":
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(message)
    return found


def test_primary_card_shows_at_most_three_points_as_separate_bullet_boxes():
    msg = R.render_primary(_claim_payload(5))
    bullets = _bullet_boxes(msg)
    assert len(bullets) == MAX_KEY_POINTS
    for box in bullets:
        marker, content = box["contents"]
        assert marker["text"] == "🟠" and marker["flex"] == 0 and content["flex"] == 1
        assert content["text"].startswith("TEST_ONLY 重點")
    body = msg["contents"]["body"]["contents"]
    sections = [n for n in body if n.get("type") == "box" and n.get("layout") == "vertical"]
    assert len(sections) >= 3  # 標題區 / 結論 / 重點 / 附註各自是 section
    assert sum(n.get("type") == "separator" for n in body) == len(sections) - 1
    assert any(t["text"] == CLAIM_RISK_LABEL[RiskLevel.MEDIUM] for t in flex_texts(msg))
    assert not any("\n" in t["text"] for t in flex_texts(msg))


def test_detail_paragraph_lines_and_bullets_are_separate_components():
    detail = DetailPayload(request_id=RID, action=ResponseAction.WHY, title="為什麼是這個結果？",
                           body=["查證結論：\n🔴 TEST_ONLY 命題一\n🟢 TEST_ONLY 命題二", "資料來源：\n・TEST_ONLY 來源"])
    msg = R.render_detail(detail)
    texts = [t["text"] for t in flex_texts(msg)]
    assert "查證結論：" in texts and "TEST_ONLY 命題一" in texts and "TEST_ONLY 來源" in texts
    assert not any("\n" in t for t in texts)
    assert len(_bullet_boxes(msg)) == 3


@pytest.mark.parametrize("message", [
    R.render_primary(_claim_payload()),
    R.render_detail(DetailPayload(request_id=RID, action=ResponseAction.WHY, title="t", body=["a\n・b"],
                                  available_actions=[ResponseAction.POLICY_BASIS, ResponseAction.SOURCES])),
])
def test_large_font_scaling_contract_is_kept(message):
    assert TEXT_SIZES == {"section": "sm", "title": "xl", "body": "md", "note": "sm"} and BUTTON_HEIGHT == "md"
    for t in flex_texts(message):
        assert t["wrap"] is True and t["scaling"] is True and "maxLines" not in t
        assert t["size"] != "xs" and "adjustMode" not in t  # 內文不 shrink-to-fit
    for t in flex_texts(message):
        if t["size"] == TEXT_SIZES["body"]:
            assert t["lineSpacing"] == "4px"
    buttons = [n for n in json.loads(json.dumps(message))["contents"].get("footer", {}).get("contents", [])]
    assert buttons and all(b["scaling"] is True and b["adjustMode"] == "shrink-to-fit" for b in buttons)
    assert "backgroundColor" not in json.dumps(message)
