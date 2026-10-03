import pytest

from app.risk.assessment_rules import (
    assess_claim,
    compute_evidence_strength,
    relevance_gate,
    resolve_product_match,
)
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimEvidenceRelation,
    ClaimOrigin,
    ClaimSemanticCategory,
    EvidenceScope,
    EvidenceStrength,
    ProductReferenceStatus,
    ProductVerificationStatus,
    SourceType,
)
from app.schemas.evidence import (
    ApprovedClaimEvidence,
    ClaimAssessment,
    ClaimEvidenceLink,
    EvidenceBundle,
    EvidenceRelevance,
    ProductCandidateMatch,
    ProductMatch,
    ProductRecord,
    SourceRef,
)
from tests.builders import claim, request

SRC = SourceRef(source_name="TEST_ONLY", source_type=SourceType.STRUCTURED_DB, is_official=True)
MED = ClaimSemanticCategory.MEDICAL_EFFICACY
ASSERTING_ORIGINS = [o for o in ClaimOrigin if o != ClaimOrigin.USER_QUESTION]
CONDITIONS = ["某慢性病", "某眼部疾病", "某消化道疾病", "某皮膚症狀"]


def _match(name="TEST_ONLY 產品", score=1.0, by="product_name", permit=None, pid="p"):
    return ProductCandidateMatch(
        product=ProductRecord(product_id=pid, product_name_zh=name, permit_number=permit, source=SRC),
        match_score=score, matched_by=by, query=name,
    )


def _link(eid, relation):
    return ClaimEvidenceLink(evidence_id=eid, relation=relation, rationale="TEST_ONLY")


# ---------------- statement role ----------------


@pytest.mark.parametrize("condition", CONDITIONS)
def test_user_question_about_medical_effect_is_not_a_medical_treatment_claim(condition):
    c = claim(f"這個產品可以治療{condition}", MED, ClaimOrigin.USER_QUESTION, condition=condition)
    result = assess_claim(0, c, [], set(), set(), True)
    assert result.status != ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert result.status == ClaimAssessmentStatus.UNSUPPORTED
    assert result.involves_medical_efficacy and not result.is_product_side_assertion


@pytest.mark.parametrize("origin", ASSERTING_ORIGINS)
@pytest.mark.parametrize("condition", CONDITIONS[:2])
def test_asserted_medical_statement_is_medical_treatment_claim(origin, condition):
    c = claim(f"吃了可以治好{condition}", MED, origin, condition=condition)
    result = assess_claim(0, c, [], set(), set(), True)
    assert result.status == ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM
    assert result.origin == origin
    assert result.is_product_side_assertion == (
        origin in (ClaimOrigin.PRODUCT_LABEL_CLAIM, ClaimOrigin.ADVERTISEMENT_CLAIM)
    )


def test_question_contradicted_by_official_is_conflict():
    result = assess_claim(0, claim(category=MED), [_link("r", ClaimEvidenceRelation.CONTRADICTS)], set(), {"r"}, True)
    assert result.status == ClaimAssessmentStatus.CONFLICT_WITH_OFFICIAL
    assert result.rule_ids == ["QUESTION_CONTRADICTED_BY_OFFICIAL"]


def test_question_with_absolute_wording_is_not_flagged_as_exaggerated_claim():
    """使用者問「能不能根治」不代表有人宣稱根治。"""
    result = assess_claim(0, claim(absolute=True, origin=ClaimOrigin.USER_QUESTION), [], set(), set(), True)
    assert result.status == ClaimAssessmentStatus.UNSUPPORTED


@pytest.mark.parametrize("origin", [ClaimOrigin.ADVERTISEMENT_CLAIM, ClaimOrigin.THIRD_PARTY_CLAIM])
def test_asserted_absolute_wording_is_exaggerated(origin):
    assert assess_claim(0, claim(absolute=True, origin=origin), [], set(), set(), True).status == (
        ClaimAssessmentStatus.POTENTIALLY_EXAGGERATED
    )


@pytest.mark.parametrize("origin", list(ClaimOrigin))
def test_supported_by_approved_claim_is_consistent_for_any_origin(origin):
    result = assess_claim(0, claim(origin=origin), [_link("a", ClaimEvidenceRelation.SUPPORTED)], {"a"}, {"a"}, True)
    assert result.status == ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL


def test_supported_only_by_general_document_is_not_consistent():
    result = assess_claim(0, claim(), [_link("r", ClaimEvidenceRelation.SUPPORTED)], {"a"}, {"a", "r"}, True)
    assert result.status == ClaimAssessmentStatus.UNSUPPORTED


def test_links_to_non_accepted_evidence_are_ignored():
    result = assess_claim(0, claim(), [_link("x", ClaimEvidenceRelation.SUPPORTED)], {"x"}, set(), True)
    assert result.status == ClaimAssessmentStatus.UNSUPPORTED and result.evidence_links == []


def test_no_source_available_is_unknown():
    assert assess_claim(0, claim(), [], set(), set(), False).status == ClaimAssessmentStatus.UNKNOWN


# ---------------- Evidence Relevance Gate ----------------


def _rel(score, scope):
    return EvidenceRelevance(relevance_score=score, evidence_scope=scope)


@pytest.mark.parametrize("rel,passed", [
    (None, False),
    (_rel(0.95, EvidenceScope.UNRELATED), False),
    (_rel(0.3, EvidenceScope.PRODUCT_SPECIFIC), False),
    (_rel(0.7, EvidenceScope.GENERAL_BACKGROUND), False),
    (_rel(0.85, EvidenceScope.GENERAL_BACKGROUND), True),
    (_rel(0.6, EvidenceScope.CLAIM_REGULATION), True),
    (_rel(0.9, EvidenceScope.PRODUCT_SPECIFIC), True),
])
def test_relevance_gate_thresholds(rel, passed):
    ok, reason = relevance_gate(rel)
    assert ok is passed and reason


# ---------------- resolve_product_match ----------------


def test_missing_product_context_is_not_applicable_with_note():
    req = request(product_reference_status=ProductReferenceStatus.MISSING_CONTEXT)
    m = resolve_product_match(req, [], [], [], [], True)
    assert m.status == ProductVerificationStatus.NOT_APPLICABLE and m.notes


def test_high_score_verified_mid_score_partial_low_not_found():
    # 名稱相似度層級：候選品名與查詢不同（品名完全相同屬於 exact 層級，另有測試）
    # 分數一律以使用者原始寫法重新計算（fixture 的 score 只是工具回傳值）
    req = request(product_candidates=["虛構甲乙測試膠囊"])
    assert resolve_product_match(req, [_match("虛構甲乙測試膠囊錠", score=0.96)], [], [], [], True).status == ProductVerificationStatus.VERIFIED
    assert resolve_product_match(req, [_match("虛構甲乙測試軟膠囊組合包", score=0.87)], [], [], [], True).status == ProductVerificationStatus.PARTIAL_MATCH
    nf = resolve_product_match(req, [_match("TEST_ONLY 無關", score=0.2)], [], [], [], True)
    assert nf.status == ProductVerificationStatus.NOT_FOUND and nf.best_match is None


def test_permit_match_with_different_name_is_conflict():
    req = request(product_candidates=["完全不同的名字"], permit_number_candidates=["Z00001"])
    m = resolve_product_match(req, [], [_match(name="虛構牌晶亮測試膠囊", by="permit_number", permit="測試健食字第Z00001號")], [], [], True)
    assert m.status == ProductVerificationStatus.CONFLICT


def test_company_only_is_partial_and_failed_lookup_incomplete():
    req = request(company_candidates=["虛構測試生技"])
    assert resolve_product_match(req, [], [], ["虛構測試生技股份有限公司"], [], True).status == ProductVerificationStatus.PARTIAL_MATCH
    m = resolve_product_match(request(product_candidates=["x"]), [], [], [], [], lookup_completed=False)
    assert m.status == ProductVerificationStatus.NOT_FOUND and m.lookup_completed is False


# ---------------- evidence strength ----------------


def _bundle(status, claims=(), approved=()):
    return EvidenceBundle(
        request_id="r",
        product_match=ProductMatch(status=status, best_match=_match() if status == ProductVerificationStatus.VERIFIED else None),
        claim_assessment=list(claims),
        approved_claims=list(approved),
    )


def _ca(linked: bool):
    return ClaimAssessment(
        claim_index=0, claim_text="t", status=ClaimAssessmentStatus.UNSUPPORTED,
        evidence_links=[_link("a", ClaimEvidenceRelation.SUPPORTED)] if linked else [],
    )


def test_evidence_strength_levels():
    assert compute_evidence_strength(_bundle(ProductVerificationStatus.VERIFIED)) == EvidenceStrength.STRONG
    assert compute_evidence_strength(_bundle(ProductVerificationStatus.VERIFIED, [_ca(False)])) == EvidenceStrength.MEDIUM
    assert compute_evidence_strength(_bundle(ProductVerificationStatus.PARTIAL_MATCH)) == EvidenceStrength.WEAK
    assert compute_evidence_strength(_bundle(ProductVerificationStatus.NOT_FOUND)) == EvidenceStrength.NONE
    approved = ApprovedClaimEvidence(evidence_id="a", product_id="p", approved_claim_id="c", function_name="f", claim_text="t", source=SRC)
    assert compute_evidence_strength(
        _bundle(ProductVerificationStatus.NOT_FOUND, [_ca(True)], [approved])
    ) == EvidenceStrength.MEDIUM
