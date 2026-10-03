"""Supabase / Neo4j / RAG 的 live 測試（需要已執行 migration 與 seed 的 nutri_safe_ai DB）。

讀取的是真實台灣官方 seed 資料；測試只檢查「能力」與結構，不檢查特定產品答案。
"""

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import InputType, ProductVerificationStatus
from app.schemas.user_request import RawUserInput
from app.services.supabase_service import SupabaseOfficialDataRepository, SupabaseRagStore
from app.tools import graph_tools, product_tools, rag_tools
from app.tools.graph_tools import GraphQueryType, Neo4jGraphRepository

pytestmark = pytest.mark.integration


def _any_product(supabase_service) -> dict:
    rows = supabase_service.select(supabase_service.table("product_details").select("*").limit(1))
    if not rows:
        pytest.skip("SKIPPED_EXTERNAL: Supabase 尚未匯入 seed 資料（請執行 scripts.seed_tw_official_data）")
    return rows[0]


def test_supabase_product_lookup_by_name_and_permit(supabase_service):
    """Test A（資料層）：以官方品名與許可證字號查回同一筆產品。"""
    row = _any_product(supabase_service)
    repo = SupabaseOfficialDataRepository(supabase_service)
    by_name = product_tools.lookup_product(repo, [row["product_name_zh"]])
    assert by_name.candidates[0].product.product_id == row["product_id"]
    assert by_name.candidates[0].product.source.is_official
    by_permit = product_tools.lookup_permit(repo, row["permit_number"])
    assert by_permit.candidates[0].product.product_id == row["product_id"]


def test_supabase_approved_claims_have_sources(supabase_service):
    row = _any_product(supabase_service)
    claims = product_tools.lookup_approved_claim(SupabaseOfficialDataRepository(supabase_service), [row["product_id"]])
    for c in claims:
        assert c.source.source_name and c.source.source_record_id


def test_neo4j_product_ingredient_and_approved_claim(supabase_service, neo4j_service):
    """Test I：Product → Ingredient、Product → Approved Claim。"""
    rows = supabase_service.select(supabase_service.table("product_ingredients").select("product_id").limit(1))
    if not rows:
        pytest.skip("SKIPPED_EXTERNAL: 尚未匯入 seed 資料")
    pid = rows[0]["product_id"]
    repo = Neo4jGraphRepository(neo4j_service)
    ingredients = graph_tools.query_knowledge_graph(repo, GraphQueryType.PRODUCT_INGREDIENTS, [pid])
    assert ingredients and ingredients[0].relation == "CONTAINS"
    claims = graph_tools.query_knowledge_graph(repo, GraphQueryType.PRODUCT_APPROVED_CLAIMS, [pid])
    assert claims and claims[0].relation == "HAS_APPROVED_CLAIM"
    # canonical ID 與 Supabase 一致
    assert ingredients[0].subject_id == pid


def test_rag_retrieval_returns_official_chunks(supabase_service, openai_service):
    """Test J：question → embedding → similarity retrieval → evidence。"""
    out = rag_tools.search_rag(SupabaseRagStore(supabase_service), openai_service, "健康食品可以宣稱治療疾病嗎？", top_k=3)
    if not out:
        pytest.skip("SKIPPED_EXTERNAL: 尚未匯入 RAG 文件")
    assert out[0].source.is_official and out[0].source.source_url
    assert out[0].similarity >= 0.3


def test_full_stack_text_pipeline(live_settings, supabase_service, neo4j_service, openai_service):
    """真實 OpenAI + Supabase + Neo4j + RAG 的 text pipeline。"""
    row = _any_product(supabase_service)
    pipeline = AnalysisPipeline(
        intake=IntakeAgent(openai_service),
        verifier=VerificationAgent(
            SupabaseOfficialDataRepository(supabase_service), Neo4jGraphRepository(neo4j_service),
            SupabaseRagStore(supabase_service), openai_service,
        ),
        risk_engine=RiskEngine(),
        reporter=ReporterAgent(openai_service),
    )
    result = pipeline.run(RawUserInput(input_type=InputType.TEXT, text=f"「{row['product_name_zh']}」有官方資料嗎？可以補什麼？"))
    print("\n" + result.final_answer)
    assert result.product_verification.status in (ProductVerificationStatus.VERIFIED, ProductVerificationStatus.PARTIAL_MATCH)
    assert result.errors == []


def test_derived_brand_alias_resolves_to_company(supabase_service):
    """任取一個高信心推導品牌，確認可經 alias 解析到其申請商（不指定特定品牌）。"""
    rows = supabase_service.select(
        supabase_service.table("brands").select("name,company_id,confidence").gte("confidence", 0.6).limit(1)
    )
    if not rows:
        pytest.skip("SKIPPED_EXTERNAL: 尚未匯入 brands / entity_aliases（需 migration 0001 與 seed）")
    companies = product_tools.lookup_company(SupabaseOfficialDataRepository(supabase_service), [rows[0]["name"]])
    assert any(c.company_id == rows[0]["company_id"] for c in companies)
