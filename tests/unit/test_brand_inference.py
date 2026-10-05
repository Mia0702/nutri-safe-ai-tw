"""Brand 關係方向一致性，以及「推測品牌 / 別名不是官方確認」的保證。"""

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.agents.reporter_agent import INFERRED_LINK_NOTE, OFFICIAL_CONFIRMATION_RE, OWNERSHIP_CLAIM_RE, ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.ingestion.alias_builder import build_aliases
from app.ingestion.tfda_health_food import normalize_record
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import EntityType, EvidenceStatus, ProductVerificationStatus, SourceType
from app.tools import product_tools
from tests.builders import request
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore, plan

CYPHER = Path("migrations/neo4j/0001_constraints.cypher").read_text(encoding="utf-8")
GRAPH_SYNC = Path("app/ingestion/graph_sync.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")


# ======================= 1. Neo4j 關係方向 =======================

OWNS = r"\(Company\)-\[:OWNS_BRAND[^\]]*\]->\(Brand\)"
ASSOC = r"\(Company\)-\[:ASSOCIATED_WITH_BRAND[^\]]*\]->\(Brand\)"
HAS = r"\(Product\)-\[:HAS_BRAND[^\]]*\]->\(Brand\)"


@pytest.mark.parametrize("name,text", [("cypher", CYPHER), ("readme", README)], ids=["cypher", "readme"])
def test_documented_brand_relation_direction(name, text):
    assert re.search(OWNS, text), f"{name} 缺少 (Company)-[:OWNS_BRAND]->(Brand)"
    assert re.search(ASSOC, text), f"{name} 缺少 (Company)-[:ASSOCIATED_WITH_BRAND]->(Brand)"
    assert re.search(HAS, text), f"{name} 缺少 (Product)-[:HAS_BRAND]->(Brand)"


@pytest.mark.parametrize("text", [CYPHER, README, GRAPH_SYNC], ids=["cypher", "readme", "graph_sync"])
def test_no_reversed_brand_relations(text):
    rels = "OWNS_BRAND|ASSOCIATED_WITH_BRAND|HAS_BRAND"
    assert not re.search(rf"<-\[[^\]]*:({rels})", text)
    assert not re.search(rf"\(Brand\)-\[[^\]]*:({rels})", text)
    assert not re.search(rf"\(b(:Brand)?\)-\[[^\]]*:({rels})", text)


def _bound_label(query: str, var: str) -> str:
    m = re.search(rf"\({var}:(\w+)", query)
    assert m, f"變數 {var} 沒有綁定 label"
    return m.group(1)


def test_graph_sync_brand_relations_bind_correct_labels():
    block = GRAPH_SYNC[GRAPH_SYNC.index('counts["Brand/HAS_BRAND/COMPANY_BRAND"]'):]
    block = block[: block.index('""", p_brands)')]
    owns = re.search(r"MERGE \((\w+)\)-\[\w*:OWNS_BRAND\]->\((\w+)\)", block)
    assoc = re.search(r"MERGE \((\w+)\)-\[\w*:ASSOCIATED_WITH_BRAND\]->\((\w+)\)", block)
    has = re.search(r"MERGE \((\w+)\)-\[\w*:HAS_BRAND\]->\((\w+)\)", block)
    assert owns and assoc and has
    for m in (owns, assoc):
        assert (_bound_label(block, m.group(1)), _bound_label(block, m.group(2))) == ("Company", "Brand")
    assert (_bound_label(block, has.group(1)), _bound_label(block, has.group(2))) == ("Product", "Brand")
    for prop in ("is_inferred", "evidence_status", "inference_basis", "confidence"):
        assert f"own.{prop}" in block and f"assoc.{prop}" in block and f"rel.{prop}" in block


def test_graph_sync_owns_brand_only_when_relation_is_owns():
    # OWNS_BRAND 只在 company_relation = OWNS_BRAND 時建立；其他（含缺值）一律 ASSOCIATED_WITH_BRAND
    assert re.search(r"CASE WHEN r\.company_relation = 'OWNS_BRAND' THEN \[1\] ELSE \[\] END \|\s*MERGE \(c\)-\[own:OWNS_BRAND\]", GRAPH_SYNC)
    assert re.search(r"CASE WHEN r\.company_relation = 'OWNS_BRAND' THEN \[\] ELSE \[1\] END \|\s*MERGE \(c\)-\[assoc:ASSOCIATED_WITH_BRAND\]", GRAPH_SYNC)
    assert '"OWNS_BRAND" if brand.get("company_relation") == "OWNS_BRAND" else "ASSOCIATED_WITH_BRAND"' in GRAPH_SYNC
    assert "type(old) <> r.company_relation DELETE old" in GRAPH_SYNC  # 不殘留舊語意的關係


# ======================= 2. 推測品牌 / 別名 =======================


def _bundles():
    """TEST_ONLY：同一虛構申請商的 3 項產品共用名稱前綴，另有官方英文成分名。"""
    records = [
        {"許可證字號": f"衛部健食字第Z0000{i}號", "類別": "衛部健食字", "中文品名": f"甲乙牌測試產品{i}號",
         "核可日期": "2020/01/01", "申請商": "TEST_ONLY 虛構生技股份有限公司", "證況": "核可",
         "保健功效相關成分": "測試成分(test ingredient)", "保健功效": "測試功效", "保健功效宣稱": "TEST_ONLY",
         "警語": None, "注意事項": None, "網址": None}
        for i in range(1, 4)
    ]
    return [normalize_record(r, datetime(2026, 1, 1, tzinfo=timezone.utc)) for r in records]


def test_alias_builder_marks_derived_brand_as_inferred_with_basis():
    built = build_aliases(_bundles(), "src")
    assert built.brands and built.product_brands
    for brand in built.brands:
        assert brand["evidence_status"] == EvidenceStatus.INFERRED
        assert brand["inference_basis"]["rule"] == "shared_product_name_prefix"
        assert brand["inference_basis"]["shared"] == 3 and 0 < brand["confidence"] < 1
    for link in built.product_brands:
        assert link["evidence_status"] == EvidenceStatus.INFERRED and link["inference_basis"]
    by_type = {a["alias_type"]: a for a in built.aliases}
    assert by_type["DERIVED_BRAND_PREFIX"]["evidence_status"] == EvidenceStatus.INFERRED
    assert by_type["SHORT_NAME"]["evidence_status"] == EvidenceStatus.INFERRED
    assert by_type["ENGLISH_NAME"]["evidence_status"] == EvidenceStatus.OFFICIAL_RECORD
    for alias in built.aliases:
        assert alias["inference_basis"], "每一筆別名都要保存依據"
        if alias["evidence_status"] == EvidenceStatus.INFERRED:
            assert alias["confidence"] < 1


def test_inferred_alias_source_is_not_official():
    repo = InMemoryOfficialRepo()
    [brand] = product_tools.resolve_entity_alias(repo, ["虛構牌"], [EntityType.BRAND])
    assert brand.is_inferred and brand.evidence_status == "INFERRED" and brand.inference_basis
    assert brand.source.is_official is False and brand.source.source_type == SourceType.DERIVED_INFERENCE
    [english] = product_tools.resolve_entity_alias(repo, ["fictionin-a"], [EntityType.INGREDIENT])
    assert not english.is_inferred and english.source.is_official is True


def test_company_reached_via_inferred_brand_is_marked():
    [company] = product_tools.lookup_company(InMemoryOfficialRepo(), ["虛構牌"])
    assert company.is_inferred and company.alias_confidence == 0.8 and company.inference_basis


def _brand_only_bundle():
    llm = FakeLLM(plans=[plan(("lookup_company", {"names": ["虛構牌"]})), plan()])
    agent = VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm)
    req = request(brand_candidates=["虛構牌"])
    return req, agent.verify(req)


def test_agent_records_inferred_link_and_never_calls_it_official():
    req, bundle = _brand_only_bundle()
    assert bundle.product_match.status == ProductVerificationStatus.PARTIAL_MATCH  # 推測不會升級為 VERIFIED
    [link] = bundle.inferred_links
    assert link.is_official_confirmation is False and link.inference_basis and link.confidence < 1
    notes = " ".join(bundle.product_match.notes)
    assert "推測" in notes and "非官方確認" in notes
    assert "官方紀錄中有申請商" not in notes
    assert all("推測" not in s.source_name for s in bundle.all_sources())  # 推測不列入官方來源


def test_reporter_adds_inferred_note_and_marks_summary():
    req, bundle = _brand_only_bundle()
    llm = FakeLLM(text="系統推測這個品牌可能與某申請商有關。")
    out = ReporterAgent(llm).report(req, bundle, RiskEngine().assess(req, bundle))
    assert INFERRED_LINK_NOTE in out.text
    sent = [c[1] for c in llm.calls if c[0] == "generate_text"][0]
    assert '"is_official_confirmation": false' in sent


@pytest.mark.parametrize("phrase", ["經官方確認", "官方證實", "官方已確認", "官方認定"])
def test_reporter_rejects_body_that_calls_inference_official(phrase):
    req, bundle = _brand_only_bundle()
    out = ReporterAgent(FakeLLM(text=f"這個品牌{phrase}屬於某公司。")).report(req, bundle, RiskEngine().assess(req, bundle))
    assert out.used_fallback_template
    assert phrase not in out.text
    assert not OFFICIAL_CONFIRMATION_RE.search(out.text)  # 最終回覆只剩否定語境
    assert INFERRED_LINK_NOTE in out.text and "推測" in out.text


@pytest.mark.parametrize("body", ["這個品牌是某公司旗下的品牌。", "某公司擁有這個品牌。", "這個品牌屬於某公司。"])
def test_reporter_rejects_body_that_describes_inference_as_ownership(body):
    req, bundle = _brand_only_bundle()
    out = ReporterAgent(FakeLLM(text=body)).report(req, bundle, RiskEngine().assess(req, bundle))
    assert out.used_fallback_template and body not in out.text
    assert not OWNERSHIP_CLAIM_RE.search(out.text)
    assert INFERRED_LINK_NOTE in out.text


@pytest.mark.parametrize("body", ["系統推測可能有關，並非官方確認。", "這不是官方確認的資訊。", "此對應未經官方確認。"])
def test_reporter_keeps_correctly_hedged_body(body):
    req, bundle = _brand_only_bundle()
    out = ReporterAgent(FakeLLM(text=body)).report(req, bundle, RiskEngine().assess(req, bundle))
    assert not out.used_fallback_template and body in out.text


def test_reporter_keeps_llm_body_when_no_inferred_links():
    req = request(product_candidates=["虛構牌晶亮測試膠囊"])
    bundle = VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), FakeLLM()).verify(req)
    assert bundle.inferred_links == []
    out = ReporterAgent(FakeLLM(text="正文")).report(req, bundle, RiskEngine().assess(req, bundle))
    assert not out.used_fallback_template and INFERRED_LINK_NOTE not in out.text
