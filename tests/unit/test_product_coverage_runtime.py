"""Taiwan Product Coverage runtime compatibility（健康食品＋國產維生素類錠狀膠囊＋輸入錠狀膠囊）回歸測試。

* 字號解析：完整保留官方字號（10 碼、前導零、字串），健康食品既有字號格式不退化。
* UNKNOWN（官方資料集未提供狀態）≠ EXPIRED / CANCELLED / REVOKED：不觸發「許可已非有效」；有效日期只顯示日期狀態。
* 類別感知：只有健康食品有核准保健功效；查驗登記／核備類別只說「查有官方查驗登記／核備資料」，沒有核准功效不是異常。
* Resolver：唯一字號直接確認；同名多筆 / 跨類別同名 / 同字號多筆 → 候選確認，不以品名合併。
* NOT_FOUND 說明目前涵蓋的官方類別；integrity gate 依來源 / 批次 / 類別驗證，不固定筆數。
* claim-policy corpus 路徑搬移後 importer / tests 讀得到同一份 corpus（batch_key 不變）。

全部使用 TEST_ONLY 虛構資料與 fake，不連線、不寫入任何正式資料庫。
"""

import re
from datetime import date
from pathlib import Path

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent, detail_payload, product_choices
from app.agents.verification_agent import CLARIFICATION_MODE, FAST_PATH_MODE, VerificationAgent
from app.ingestion import product_integrity as integrity
from app.ingestion import tfda_product_coverage as cov
from app.ingestion.claim_policy_corpus import load_corpus
from app.normalization import extract_permit_code, stable_id
from app.pipeline import AnalysisPipeline
from app.product_coverage import (
    CATEGORY_LABELS,
    NO_HEALTH_CLAIM_APPROVAL_TEXT,
    NOT_FOUND_COVERAGE_TEXT,
    REGISTRATION_RECORD_TEXT,
    TemporalStatus,
    derived_temporal_status,
    is_officially_inactive,
)
from app.response.line_renderer import AdaptiveLineRenderer
from app.risk.assessment_rules import resolve_product_match
from app.risk.risk_engine import DISCLAIMER_NOT_FOUND, RiskEngine
from app.schemas.enums import (
    ClaimAssessmentStatus,
    ClaimOrigin,
    ClaimSemanticCategory,
    InputType,
    Intent,
    ProductAmbiguityReason,
    ProductCategory,
    ProductMatchType,
    ProductStatus,
    ProductVerificationStatus,
    ResponseAction,
    ResponseType,
    RiskLevel,
    SemanticTone,
    SourceType,
)
from app.schemas.evidence import EvidenceBundle, ProductCandidateMatch, ProductMatch, ProductRecord, SourceRef
from app.schemas.user_request import IntakeExtraction, RawUserInput
from app.tools.product_tools import lookup_permit
from tests.builders import claim, extraction, request
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import _product

ROOT = Path(__file__).resolve().parents[2]
SRC = SourceRef(source_name="TEST_ONLY", source_type=SourceType.STRUCTURED_DB, is_official=True)
DOM, IMP, HF = (ProductCategory.DOMESTIC_VITAMIN_TABLET_CAPSULE, ProductCategory.IMPORTED_TABLET_CAPSULE,
                ProductCategory.HEALTH_FOOD)

# TEST_ONLY：虛構的查驗登記 / 核備紀錄（10 碼字號，其中一筆有前導零）
DOM_NAME = "TEST_ONLY 虛構維生素甲錠"
IMP_NAME = "TEST_ONLY 虛構魚油膠囊"
SHARED_NAME = "TEST_ONLY 虛構同名錠"
DOM_NUMBER = "9900000001"
IMP_NUMBER = "0099000002"


def _registration(pid, name, number, category, permit_type, *, expiry="2099-01-01", status="UNKNOWN"):
    row = _product(pid, name, "c-1", "虛構測試生技股份有限公司", number, status=status)
    row.update(permit_number=number, permit_code=number, permit_type=permit_type,
               product_category=category.value, expiry_date=expiry)
    return row


@pytest.fixture
def coverage_products(monkeypatch):
    rows = [
        _registration("pd-1", DOM_NAME, DOM_NUMBER, DOM, "查驗登記證"),
        _registration("pi-1", IMP_NAME, IMP_NUMBER, IMP, "核備函", expiry="2001-01-01"),
        _registration("pd-2", SHARED_NAME, "9900000003", DOM, "查驗登記證"),
        _registration("pi-2", SHARED_NAME, "9900000004", IMP, "核備函"),
    ]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + rows)
    return rows


def _pipeline(llm):
    return AnalysisPipeline(
        intake=IntakeAgent(llm),
        verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        risk_engine=RiskEngine(), reporter=ReporterAgent(llm),
    )


def run(ex: IntakeExtraction, text="TEST_ONLY 問題"):
    llm = FakeLLM(structured={IntakeExtraction: ex})
    return _pipeline(llm).run(RawUserInput(input_type=InputType.TEXT, text=text)), llm


def _record(pid, name, *, category=HF, status=ProductStatus.ACTIVE, permit=None, expiry=None, permit_type=None):
    return ProductRecord(product_id=pid, product_name_zh=name, permit_number=permit, status=status, source=SRC,
                         product_category=category, expiry_date=expiry, permit_type=permit_type)


def _m(record, score=1.0, by="product_name", query="q"):
    return ProductCandidateMatch(product=record, match_score=score, matched_by=by, query=query)


# ============================================================ 1. Registration number parsing


def test_ten_digit_registration_number_is_not_truncated():
    assert extract_permit_code("查驗登記證號 9900000001") == "9900000001"
    assert extract_permit_code("核備函字號第1234567890號") == "1234567890"


def test_leading_zero_is_preserved_as_string():
    code = extract_permit_code("0099000002")
    assert code == "0099000002" and isinstance(code, str)


def test_health_food_legacy_permit_formats_do_not_regress():
    assert extract_permit_code("衛部健食字第A00235號") == "A00235"
    assert extract_permit_code("衛部健食規字第000042號(本證失效)") == "000042"
    assert extract_permit_code("ａ00235") == "A00235"
    assert extract_permit_code("健食字第123456號，電話 0223456789") == "123456"  # 第一個字號優先
    assert extract_permit_code("沒有字號") is None
    assert extract_permit_code("12345") is None


def test_digit_runs_are_taken_whole_not_partially():
    # 6 碼健康食品格式不能從較長的數字段或英數混合編號中被切出來
    assert extract_permit_code("A002351") is None
    assert extract_permit_code("TESTX00880721095") is None


def test_lookup_permit_finds_full_number_and_not_a_six_digit_prefix(coverage_products, monkeypatch):
    six = _product("p-six", "TEST_ONLY 六碼健康食品", "c-1", "虛構測試生技股份有限公司", "990000")
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [six])
    out = lookup_permit(InMemoryOfficialRepo(), f"核備函字號 {IMP_NUMBER}")
    assert [c.product.product_id for c in out.candidates] == ["pi-1"]
    assert out.candidates[0].product.permit_number == IMP_NUMBER
    out = lookup_permit(InMemoryOfficialRepo(), DOM_NUMBER)
    assert [c.product.product_id for c in out.candidates] == ["pd-1"]  # 不會誤中 6 碼 990000


def test_product_row_maps_category_and_expiry(coverage_products):
    out = lookup_permit(InMemoryOfficialRepo(), IMP_NUMBER)
    product = out.candidates[0].product
    assert product.product_category == IMP and product.expiry_date == "2001-01-01"
    assert product.status == ProductStatus.UNKNOWN
    legacy = lookup_permit(InMemoryOfficialRepo(), "Z00001").candidates[0].product
    assert legacy.product_category == HF  # 0004 之前的 view 沒有欄位 → 既有 health_food


# ============================================================ 2. UNKNOWN regulatory status


def _risk_for(record):
    best = _m(record)
    bundle = EvidenceBundle(request_id="r", product_match=ProductMatch(
        status=ProductVerificationStatus.VERIFIED, best_match=best, candidates=[best]))
    return RiskEngine().assess(request(product_candidates=[record.product_name_zh]), bundle)


@pytest.mark.parametrize("category", [HF, DOM, IMP])
def test_unknown_status_does_not_trigger_inactive_risk(category):
    risk = _risk_for(_record("p", "TEST_ONLY", category=category, status=ProductStatus.UNKNOWN))
    assert "RISK_MED_PERMIT_INACTIVE" not in {h.rule_id for h in risk.triggered_rules}


def test_unknown_status_with_past_expiry_date_is_still_not_official_inactive():
    record = _record("p", "TEST_ONLY", category=IMP, status=ProductStatus.UNKNOWN, expiry="2001-01-01")
    risk = _risk_for(record)
    assert "RISK_MED_PERMIT_INACTIVE" not in {h.rule_id for h in risk.triggered_rules}
    assert derived_temporal_status("2001-01-01", date(2026, 10, 4)) == TemporalStatus.EXPIRED_BY_DATE
    assert derived_temporal_status("2026-10-04", date(2026, 10, 4)) == TemporalStatus.CURRENT_BY_DATE


@pytest.mark.parametrize("status", [ProductStatus.EXPIRED, ProductStatus.CANCELLED, ProductStatus.REVOKED,
                                    ProductStatus.MERGED])
@pytest.mark.parametrize("category", [HF, DOM, IMP])
def test_official_inactive_statuses_keep_original_logic(status, category):
    assert is_officially_inactive(status)
    risk = _risk_for(_record("p", "TEST_ONLY", category=category, status=status))
    assert "RISK_MED_PERMIT_INACTIVE" in {h.rule_id for h in risk.triggered_rules}
    assert risk.risk_level == RiskLevel.MEDIUM


def test_active_and_unknown_are_not_officially_inactive():
    assert not is_officially_inactive(ProductStatus.ACTIVE)
    assert not is_officially_inactive(ProductStatus.UNKNOWN)


# ============================================================ 3. Category-aware behavior


def test_registration_product_efficacy_question_is_structured_answer_without_health_claims(coverage_products):
    result, llm = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=[DOM_NAME], needs_product_lookup=True))
    bundle, payload = result.evidence, result.response
    assert bundle.product_match.status == ProductVerificationStatus.VERIFIED
    assert bundle.planner_mode == FAST_PATH_MODE  # 沒有核准功效不是缺資料：不跑 planner / RAG / web
    assert not llm.called("web_search_structured")
    assert bundle.approved_claims == [] and bundle.claim_assessment == []
    assert result.risk.risk_level == RiskLevel.LOW
    assert "RISK_MED_PERMIT_INACTIVE" not in {h.rule_id for h in result.risk.triggered_rules}
    assert payload.response_type == ResponseType.PRODUCT_INFO and payload.risk_level is None
    text = payload.plain_text()
    assert "查有官方查驗登記／核備紀錄" in text and CATEGORY_LABELS[DOM] in text
    assert "不能由這筆資料認定有核准保健功效" in text and "沒有任何功效" not in text
    assert payload.status_line == "未有健康食品核准保健功效資料" and payload.status_tone == SemanticTone.ORANGE
    assert "官方核准保健功效：" not in text
    assert "許可證已失效" not in text and "許可證狀態不明" not in text


def test_registration_official_info_has_no_approved_health_function(coverage_products):
    result, _ = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[IMP_NAME], needs_product_lookup=True))
    detail = detail_payload(ResponseAction.OFFICIAL_INFO, result.normalized_request, result.evidence, result.risk)
    body = "\n".join(detail.body)
    assert f"核備函字號：{IMP_NUMBER}" in body and f"資料類別：{CATEGORY_LABELS[IMP]}" in body
    assert "登記狀態：官方資料未提供登記狀態" in body and "非官方狀態" in body  # 有效日期只是日期比較
    assert "核准功效：" not in body and "許可狀態" not in body
    assert ResponseAction.FULL_EFFICACY not in detail.available_actions


def test_health_food_product_still_shows_approved_functions():
    result, _ = run(extraction(intent=Intent.EFFICACY_INQUIRY, product_candidates=["虛構牌晶亮測試膠囊"],
                               needs_product_lookup=True))
    text = result.response.plain_text()
    assert result.evidence.approved_claims and "的官方核准保健功效：" in text
    assert REGISTRATION_RECORD_TEXT not in text and NO_HEALTH_CLAIM_APPROVAL_TEXT not in text


def test_registration_product_medical_ad_claim_keeps_frozen_claim_policy(coverage_products):
    """類別感知不改 claim policy：已確認的查驗登記產品被廣告說成能治病，仍是醫療效能宣稱 → HIGH。"""
    ad = claim("TEST_ONLY 吃了可以治療糖尿病", category=ClaimSemanticCategory.MEDICAL_EFFICACY,
               origin=ClaimOrigin.ADVERTISEMENT_CLAIM, condition="糖尿病")
    result, _ = run(extraction(intent=Intent.CLAIM_VERIFICATION, product_candidates=[DOM_NAME], claims=[ad],
                               needs_product_lookup=True, needs_claim_check=True))
    statuses = {c.status for c in result.evidence.claim_assessment}
    assert ClaimAssessmentStatus.MEDICAL_TREATMENT_CLAIM in statuses
    assert result.risk.risk_level == RiskLevel.HIGH
    assert result.evidence.approved_claims == []


# ============================================================ 4. Resolver


def test_unique_registration_number_resolves_directly():
    rec = _record("pd", DOM_NAME, category=DOM, permit=DOM_NUMBER, status=ProductStatus.UNKNOWN)
    m = resolve_product_match(request(permit_number_candidates=[DOM_NUMBER]), [], [_m(rec, by="permit_number")],
                              [], [], True)
    assert m.status == ProductVerificationStatus.VERIFIED and m.match_type == ProductMatchType.PERMIT_NUMBER
    assert m.best_match.product.product_id == "pd"


def test_registration_number_matching_multiple_records_asks_for_choice():
    a = _record("pa", "TEST_ONLY 甲", category=DOM, permit=DOM_NUMBER)
    b = _record("pb", "TEST_ONLY 乙", category=IMP, permit=DOM_NUMBER)
    m = resolve_product_match(request(permit_number_candidates=[DOM_NUMBER]), [],
                              [_m(a, by="permit_number"), _m(b, by="permit_number")], [], [], True)
    assert m.status == ProductVerificationStatus.PARTIAL_MATCH and m.best_match is None
    assert m.ambiguity_reason == ProductAmbiguityReason.MULTIPLE_REGISTRATION_RECORDS and m.is_ambiguous
    assert {c.product.product_id for c in m.candidates} == {"pa", "pb"}


def test_same_name_multiple_registration_records_ask_for_clarification():
    recs = [_record(f"p{i}", SHARED_NAME, category=DOM, permit=f"990000001{i}") for i in range(2)]
    m = resolve_product_match(request(product_candidates=[SHARED_NAME]), [_m(r) for r in recs], [], [], [], True)
    assert m.status == ProductVerificationStatus.PARTIAL_MATCH and m.best_match is None
    assert m.ambiguity_reason == ProductAmbiguityReason.MULTIPLE_EXACT_NAMES


def test_cross_category_same_name_is_not_merged_and_asks_user(coverage_products):
    result, llm = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[SHARED_NAME],
                                 needs_product_lookup=True))
    match = result.evidence.product_match
    assert match.is_ambiguous and match.best_match is None
    assert {c.product.product_id for c in match.candidates} == {"pd-2", "pi-2"}  # 兩筆各自保留，不 SAME_AS / merge
    assert {c.product.product_category for c in match.candidates} == {DOM, IMP}
    assert result.evidence.planner_mode == CLARIFICATION_MODE and not llm.called("web_search_structured")
    payload = result.response
    assert payload.response_type == ResponseType.MISSING_INFORMATION
    choices = product_choices(result.evidence)
    assert {c.category_label for c in choices} == {CATEGORY_LABELS[DOM], CATEGORY_LABELS[IMP]}
    assert {c.number_label for c in choices} == {"查驗登記證號", "核備函字號"}
    assert all(c.is_active for c in choices)  # UNKNOWN 不標成失效
    flex = str(AdaptiveLineRenderer().render_primary(payload))
    assert CATEGORY_LABELS[DOM] in flex and "已失效" not in flex


def test_health_food_inactive_candidate_label_unchanged():
    rec = _record("p", "TEST_ONLY 甲", status=ProductStatus.EXPIRED, permit="測試健食字第Z00009號")
    other = _record("q", "TEST_ONLY 甲", permit="測試健食字第Z00008號")
    bundle = EvidenceBundle(request_id="r", product_match=ProductMatch(
        status=ProductVerificationStatus.PARTIAL_MATCH, candidates=[_m(rec), _m(other)],
        ambiguity_reason=ProductAmbiguityReason.MULTIPLE_EXACT_NAMES, candidate_count=2))
    choices = {c.product_id: c for c in product_choices(bundle)}
    assert not choices["p"].is_active and choices["p"].status_text == "許可證已失效"
    assert choices["q"].is_active and choices["q"].category_label is None and choices["q"].number_label == "許可證字號"


# ============================================================ 5. NOT_FOUND wording


def test_not_found_wording_lists_covered_official_categories():
    for label in CATEGORY_LABELS.values():
        assert label in DISCLAIMER_NOT_FOUND
    assert "查不到紀錄不代表產品是假貨或違法" in DISCLAIMER_NOT_FOUND
    assert DISCLAIMER_NOT_FOUND == NOT_FOUND_COVERAGE_TEXT
    assert "一般食品" not in NOT_FOUND_COVERAGE_TEXT and "所有" not in NOT_FOUND_COVERAGE_TEXT


def test_not_found_response_uses_coverage_wording():
    result, _ = run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=["TEST_ONLY 完全不存在的產品名"],
                               needs_product_lookup=True))
    assert result.evidence.product_match.status == ProductVerificationStatus.NOT_FOUND
    assert DISCLAIMER_NOT_FOUND in result.risk.required_disclaimers
    assert CATEGORY_LABELS[IMP] in result.response.plain_text()


# ============================================================ 6. Integrity gate


HF_SRC, DOM_SRC, IMP_SRC = "s-hf", "s-dom", "s-imp"
SOURCES = [{"id": HF_SRC, "source_key": integrity.CATEGORY_SOURCE_KEYS[HF]},
           {"id": DOM_SRC, "source_key": integrity.CATEGORY_SOURCE_KEYS[DOM]},
           {"id": IMP_SRC, "source_key": integrity.CATEGORY_SOURCE_KEYS[IMP]}]


def _rows(hf_count, dom_count):
    products, permits, raw = [], [], []
    specs = [(HF, HF_SRC, f"TEST_ONLY健食字第Z{i:05d}號") for i in range(hf_count)]
    specs += [(DOM, DOM_SRC, f"99{i:08d}") for i in range(dom_count)]
    for i, (category, sid, number) in enumerate(specs):
        pid = integrity.expected_product_id(category, number)
        products.append({"id": pid, "product_category": category.value, "permit_id": f"pm{i}", "source_id": sid})
        permits.append({"id": f"pm{i}", "permit_number": number})
        raw.append({"source_id": sid, "source_record_key": number})
    return products, permits, raw


def _failed(checks):
    return [c.label for c in checks if not c.ok]


@pytest.mark.parametrize("hf_count,dom_count", [(3, 2), (7, 0), (1, 5)])
def test_integrity_gate_has_no_fixed_product_count(hf_count, dom_count):
    products, permits, raw = _rows(hf_count, dom_count)
    assert _failed(integrity.product_count_checks(products, permits, SOURCES, raw)) == []
    expected = {HF: hf_count, DOM: dom_count}
    assert _failed(integrity.product_count_checks(products, permits, SOURCES, raw, expected=expected)) == []


def test_integrity_gate_fails_on_category_expectation_mismatch():
    products, permits, raw = _rows(3, 2)
    failed = _failed(integrity.product_count_checks(products, permits, SOURCES, raw, expected={DOM: 3}))
    assert len(failed) == 1 and "domestic_vitamin_tablet_capsule" in failed[0]


def test_integrity_gate_detects_changed_canonical_id_wrong_source_and_missing_raw():
    products, permits, raw = _rows(2, 1)
    products[0] = {**products[0], "id": stable_id("product", "TW:TEST_ONLY 其他字號")}
    products[2] = {**products[2], "source_id": HF_SRC}
    failed = " | ".join(_failed(integrity.product_count_checks(products, permits, SOURCES, raw[:-1])))
    assert "canonical ID" in failed and "來源與類別一致" in failed and "products" in failed


def test_integrity_gate_rejects_unknown_category():
    products, permits, raw = _rows(1, 0)
    products[0]["product_category"] = "TEST_ONLY_unknown"
    assert any("product_category" in f for f in _failed(integrity.product_count_checks(products, permits, SOURCES, raw)))


def test_integrity_expectations_from_cli_and_snapshot():
    assert integrity.parse_category_expectations(["health_food=12", "imported_tablet_capsule=3"]) == {HF: 12, IMP: 3}
    with pytest.raises(ValueError):
        integrity.parse_category_expectations(["not_a_category=1"])
    snapshot = ROOT / "data" / "tfda_product_coverage" / "2026-10-04"
    if (snapshot / cov.FILES["validation"]).is_file():
        exp = integrity.snapshot_expectations(snapshot)
        assert set(exp) == {DOM, IMP} and HF not in exp


def test_integrity_script_has_no_hard_coded_product_count():
    source = (ROOT / "scripts" / "check_data_integrity.py").read_text(encoding="utf-8")
    assert not re.search(r"\b565\b", source)


def test_graph_category_check_requires_category_only_after_0004():
    products = [{"id": "a", "product_category": "health_food"}, {"id": "b", "product_category": DOM.value}]
    graph = [{"id": "a", "product_category": None}, {"id": "b", "product_category": DOM.value}]
    assert integrity.graph_category_checks(products, graph, required=False)[0].ok
    assert not integrity.graph_category_checks(products, graph, required=True)[0].ok
    graph[0]["product_category"] = IMP.value
    assert not integrity.graph_category_checks(products, graph, required=False)[0].ok  # 值不同一律 blocker


# ============================================================ 7. Neo4j product_category


def test_neo4j_schema_backfills_only_missing_health_food_category():
    class FakeNeo4j:
        def __init__(self):
            self.calls = []

        def write(self, query, params):
            self.calls.append((query, params))

    neo = FakeNeo4j()
    assert cov.prepare_coverage_graph(neo, "TEST_ONLY-source") == 2
    index, backfill = (q for q, _ in neo.calls)
    assert "INDEX" in index and "product_category" in index
    assert "IS NULL" in backfill and "'health_food'" in backfill and "$health_food_source_id" in backfill
    assert all(p == {"health_food_source_id": "TEST_ONLY-source"} for _, p in neo.calls)


def test_graph_sync_writes_product_category():
    from app.ingestion import graph_sync
    import inspect

    src = inspect.getsource(graph_sync.sync_graph_from_supabase)
    assert "product_category" in src and "p.product_category = r.product_category" in src
    assert "p.product_category = r.product_category" in cov.GRAPH_QUERIES["products"]


# ============================================================ 8. 0004 migration


MIGRATION_0004 = ROOT / "migrations" / "0004_tfda_product_coverage.sql"


def test_0004_is_a_formal_additive_migration():
    assert MIGRATION_0004.is_file() and not (ROOT / "migrations" / "proposed").exists()
    sql = MIGRATION_0004.read_text(encoding="utf-8").lower()
    code = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for forbidden in ("drop table", "drop column", "delete from", "truncate", "update products", "update permits",
                      "alter column"):
        assert forbidden not in code, forbidden
    assert "migration_0003_claim_policy_corpus" in code and cov.MIGRATION_0004_MARKER in code
    assert "'health_food', 'domestic_vitamin_tablet_capsule', 'imported_tablet_capsule'" in code


def _view_columns(sql: str) -> list[str]:
    body = re.search(r"create or replace view product_details.*?select(.*?)from products p", sql, re.S | re.I).group(1)
    return [re.split(r"\s+as\s+", c.strip(), flags=re.I)[-1].split(".")[-1].strip() for c in body.split(",")]


def test_0004_view_keeps_existing_columns_and_appends_new_ones():
    old = _view_columns((ROOT / "migrations" / "0001_initial_schema.sql").read_text(encoding="utf-8"))
    new = _view_columns(MIGRATION_0004.read_text(encoding="utf-8"))
    assert new[:len(old)] == old
    assert new[len(old):] == ["product_category", "expiry_date", "signing_document_no", "permit_status_raw"]


def test_cli_apply_refuses_without_0004_marker(monkeypatch, capsys, tmp_path):
    from scripts import import_tfda_product_coverage as cli
    from tests.unit.test_tfda_product_coverage import build_snapshot, dom, imp

    class FakeSupabase:
        def __init__(self, settings):
            pass

        def ensure_schema(self):
            pass

        def table(self, name):
            return self

        def select(self, *a, **k):
            return self if a and a[0] == "value" else []

        def eq(self, *a):
            return self

        def limit(self, n):
            return self

    monkeypatch.setattr("app.config.settings.get_settings", lambda: None)
    monkeypatch.setattr("app.services.supabase_service.SupabaseService", FakeSupabase)
    monkeypatch.setattr("app.ingestion.claim_policy_corpus.SupabaseCorpusStore", lambda *a, **k: None)
    snapshot = build_snapshot(tmp_path, [dom()], [imp()])
    monkeypatch.setattr("sys.argv", ["x", "--apply", "--confirm-project", "nutri_safe_ai", "--snapshot-dir", str(snapshot)])
    assert cli.main() == 2
    assert "0004" in capsys.readouterr().out


# ============================================================ 9. claim-policy corpus path


CORPUS_ROOT = ROOT / "data" / "tfda_claim_policy"
CORPUS_DIR = CORPUS_ROOT / "2026-10-03"


@pytest.mark.skipif(not CORPUS_DIR.is_dir(), reason="corpus 不存在")
def test_claim_policy_corpus_lives_in_versioned_directory_without_duplicates():
    from app.ingestion.claim_policy_corpus import FILES
    from scripts.import_claim_policy_corpus import DEFAULT_CORPUS_DIR

    assert (ROOT / DEFAULT_CORPUS_DIR).resolve() == CORPUS_DIR.resolve()
    assert all((CORPUS_DIR / name).is_file() for name in FILES.values())
    assert [p.name for p in CORPUS_ROOT.iterdir() if p.is_file()] == []  # 根目錄不留舊檔
    corpus = load_corpus(CORPUS_DIR)
    assert corpus.corpus_version == CORPUS_DIR.name
    # batch_key 只由檔名＋內容雜湊決定：搬移目錄不會讓已匯入的 corpus 變成新批次
    assert corpus.batch_key == "79757f38507fb530bba987bb372aa79bcfa653dfdaaed98f404071acbb755bc3"
    assert (ROOT / "data" / "tfda_product_coverage" / "2026-10-04" / cov.FILES["validation"]).is_file()
