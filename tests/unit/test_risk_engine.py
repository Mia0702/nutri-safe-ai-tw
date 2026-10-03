import pytest

from app.risk.risk_engine import (
    DISCLAIMER_DATA_INCOMPLETE,
    DISCLAIMER_MEDICAL,
    DISCLAIMER_NOT_FOUND,
    DISCLAIMER_PRODUCT_CONTEXT,
    DISCLAIMER_TW_SCOPE,
    RiskEngine,
)
from app.schemas.enums import (
    PRODUCT_SIDE_ORIGINS,
    ClaimAssessmentStatus,
    ClaimOrigin,
    ErrorCode,
    EvidenceStrength,
    ProductReferenceStatus,
    ProductStatus,
    ProductVerificationStatus,
    RiskLevel,
    SourceType,
)
from app.schemas.evidence import (
    ClaimAssessment,
    EvidenceBundle,
    IllegalAdEvidence,
    ProductCandidateMatch,
    ProductMatch,
    ProductRecord,
    SourceRef,
    ToolError,
)
from tests.builders import request

SRC = SourceRef(source_name="TEST_ONLY", source_type=SourceType.STRUCTURED_DB, is_official=True)
engine = RiskEngine()
S = ClaimAssessmentStatus
P = ProductVerificationStatus


def ca(status: ClaimAssessmentStatus, origin: ClaimOrigin = ClaimOrigin.USER_QUESTION, medical: bool = False):
    return ClaimAssessment(
        claim_index=0, claim_text="TEST_ONLY", status=status, origin=origin,
        is_product_side_assertion=origin in PRODUCT_SIDE_ORIGINS, involves_medical_efficacy=medical,
    )


def bundle(product=P.VERIFIED, claims=(), strength=EvidenceStrength.STRONG, product_status=ProductStatus.ACTIVE,
           lookup_completed=True, errors=(), illegal_ads=()):
    best = None
    if product in (P.VERIFIED, P.CONFLICT):
        best = ProductCandidateMatch(
            product=ProductRecord(product_id="p", product_name_zh="TEST_ONLY", status=product_status, source=SRC),
            match_score=1.0, matched_by="product_name", query="x",
        )
    return EvidenceBundle(
        request_id="r",
        product_match=ProductMatch(status=product, best_match=best, lookup_completed=lookup_completed),
        claim_assessment=list(claims), evidence_strength=strength, errors=list(errors),
        illegal_ad_evidence=list(illegal_ads),
    )


PRODUCT_REQ = request(product_candidates=["TEST_ONLY"], product_reference_status=ProductReferenceStatus.EXPLICIT)
MISSING_CTX_REQ = request(refers_to_unspecified_product=True, product_reference_status=ProductReferenceStatus.MISSING_CONTEXT)


def rules(r):
    return [h.rule_id for h in r.triggered_rules]


# ================= 使用者提問 ≠ 產品 / 廣告宣稱 =================


@pytest.mark.parametrize("status", [S.UNSUPPORTED, S.CONFLICT_WITH_OFFICIAL])
def test_question_about_disease_on_verified_product_is_not_high(status):
    r = engine.assess(PRODUCT_REQ, bundle(claims=[ca(status, ClaimOrigin.USER_QUESTION, medical=True)]))
    assert r.risk_level == RiskLevel.LOW
    assert DISCLAIMER_MEDICAL in r.required_disclaimers


@pytest.mark.parametrize("origin", sorted(PRODUCT_SIDE_ORIGINS))
def test_product_side_medical_claim_is_high_even_for_verified_product(origin):
    r = engine.assess(PRODUCT_REQ, bundle(claims=[ca(S.MEDICAL_TREATMENT_CLAIM, origin, medical=True)]))
    assert r.risk_level == RiskLevel.HIGH
    assert "RISK_HIGH_PRODUCT_SIDE_MEDICAL_CLAIM" in rules(r)


@pytest.mark.parametrize("origin", [ClaimOrigin.USER_ASSERTION, ClaimOrigin.THIRD_PARTY_CLAIM, ClaimOrigin.OFFICIAL_CLAIM])
def test_circulating_medical_claim_is_medium_not_high(origin):
    r = engine.assess(PRODUCT_REQ, bundle(claims=[ca(S.MEDICAL_TREATMENT_CLAIM, origin, medical=True)]))
    assert r.risk_level == RiskLevel.MEDIUM
    assert "RISK_MED_CIRCULATING_CLAIM_ISSUE" in rules(r)


def test_missing_product_context_question_is_medium_inconclusive_never_high():
    r = engine.assess(
        MISSING_CTX_REQ,
        bundle(product=P.NOT_APPLICABLE, strength=EvidenceStrength.NONE,
               claims=[ca(S.UNSUPPORTED, ClaimOrigin.USER_QUESTION, medical=True)]),
    )
    assert r.risk_level == RiskLevel.MEDIUM
    assert not r.is_conclusive
    assert "RISK_MED_PRODUCT_CONTEXT_MISSING" in rules(r)
    assert DISCLAIMER_PRODUCT_CONTEXT in r.required_disclaimers and DISCLAIMER_MEDICAL in r.required_disclaimers


def test_advertisement_without_product_name_can_still_be_high():
    """廣告文字本身就是產品方宣稱的證據。"""
    r = engine.assess(
        request(), bundle(product=P.NOT_APPLICABLE, strength=EvidenceStrength.NONE,
                          claims=[ca(S.MEDICAL_TREATMENT_CLAIM, ClaimOrigin.ADVERTISEMENT_CLAIM, medical=True)]),
    )
    assert r.risk_level == RiskLevel.HIGH


@pytest.mark.parametrize("status", [S.POTENTIALLY_EXAGGERATED, S.UNSUPPORTED, S.UNKNOWN])
def test_product_side_claim_issues_are_medium(status):
    r = engine.assess(PRODUCT_REQ, bundle(claims=[ca(status, ClaimOrigin.ADVERTISEMENT_CLAIM)]))
    assert r.risk_level == RiskLevel.MEDIUM and "RISK_MED_PRODUCT_SIDE_CLAIM_ISSUE" in rules(r)


def test_product_side_conflict_is_high():
    r = engine.assess(PRODUCT_REQ, bundle(claims=[ca(S.CONFLICT_WITH_OFFICIAL, ClaimOrigin.PRODUCT_LABEL_CLAIM)]))
    assert r.risk_level == RiskLevel.HIGH


# ================= 產品與證據 =================


def test_verified_without_claims_is_low():
    r = engine.assess(PRODUCT_REQ, bundle())
    assert r.risk_level == RiskLevel.LOW and rules(r) == ["RISK_LOW_VERIFIED_CONSISTENT"]


def test_not_found_is_medium_with_not_fake_disclaimer_and_never_high_alone():
    r = engine.assess(PRODUCT_REQ, bundle(product=P.NOT_FOUND, strength=EvidenceStrength.NONE))
    assert r.risk_level == RiskLevel.MEDIUM
    assert DISCLAIMER_NOT_FOUND in r.required_disclaimers


def test_failed_lookup_does_not_claim_not_found():
    r = engine.assess(
        PRODUCT_REQ,
        bundle(product=P.NOT_FOUND, strength=EvidenceStrength.NONE, lookup_completed=False,
               errors=[ToolError(code=ErrorCode.UNAVAILABLE, message="x", source="supabase")]),
    )
    assert DISCLAIMER_NOT_FOUND not in r.required_disclaimers
    assert DISCLAIMER_DATA_INCOMPLETE in r.required_disclaimers
    assert "RISK_MED_PRODUCT_LOOKUP_FAILED" in rules(r) and r.data_incomplete and not r.is_conclusive


def test_product_conflict_and_illegal_ad_record_are_high():
    assert engine.assess(PRODUCT_REQ, bundle(product=P.CONFLICT)).risk_level == RiskLevel.HIGH
    ad = IllegalAdEvidence(evidence_id="a", case_id="a", violation_content="TEST_ONLY", source=SRC)
    assert engine.assess(PRODUCT_REQ, bundle(illegal_ads=[ad])).risk_level == RiskLevel.HIGH


def test_inactive_permit_and_weak_evidence_are_medium():
    assert "RISK_MED_PERMIT_INACTIVE" in rules(engine.assess(PRODUCT_REQ, bundle(product_status=ProductStatus.EXPIRED)))
    assert engine.assess(PRODUCT_REQ, bundle(strength=EvidenceStrength.WEAK)).risk_level == RiskLevel.MEDIUM


def test_general_info_with_evidence_is_low():
    r = engine.assess(request(), bundle(product=P.NOT_APPLICABLE, strength=EvidenceStrength.MEDIUM))
    assert r.risk_level == RiskLevel.LOW


def test_foreign_country_adds_tw_scope_disclaimer():
    req = request(product_candidates=["x"], mentioned_country_codes=["JP"])
    r = engine.assess(req, bundle(product=P.NOT_FOUND, strength=EvidenceStrength.NONE))
    assert DISCLAIMER_TW_SCOPE in r.required_disclaimers
