from app.schemas.enums import ErrorCode
from app.tools import graph_tools, official_web_tools, product_tools, rag_tools
from app.tools.base import ToolCallRecorder, execute_tool
from app.tools.graph_tools import GraphQueryType
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore, web_result

repo = InMemoryOfficialRepo()


def test_lookup_product_ranks_exact_name_first():
    out = product_tools.lookup_product(repo, ["虛構牌晶亮測試膠囊"])
    assert out.candidates[0].product.product_id == "p-1"
    assert out.candidates[0].match_score == 1.0
    assert out.candidates[0].product.source.is_official


def test_lookup_product_with_brand_prefix_improves_match():
    without = product_tools.lookup_product(repo, ["晶亮測試膠囊"]).candidates[0].match_score
    with_brand = product_tools.lookup_product(repo, ["晶亮測試膠囊"], ["虛構牌"]).candidates[0].match_score
    assert with_brand >= without


def test_lookup_product_unknown_name_returns_low_scores():
    out = product_tools.lookup_product(repo, ["完全查不到的名字"])
    assert all(c.match_score < 0.5 for c in out.candidates)


def test_lookup_permit_parses_code():
    out = product_tools.lookup_permit(repo, "測試健食字第Z00002號")
    assert [c.product.product_id for c in out.candidates] == ["p-2"]
    assert product_tools.lookup_permit(repo, "沒有字號").candidates == []


def test_lookup_company_groups_products():
    companies = product_tools.lookup_company(repo, ["虛構測試生技股份有限公司"])
    assert companies[0].company_id == "c-1"
    assert set(companies[0].product_ids) == {"p-1", "p-2"}


def test_lookup_ingredient_and_approved_claims_and_warnings():
    ing = product_tools.lookup_ingredient(repo, ["虛構素甲"])
    assert ing[0].ingredient.ingredient_id == "i-1"
    claims = product_tools.lookup_approved_claim(repo, ["p-1"])
    assert claims[0].function_name == "測試功效甲" and claims[0].evidence_id.startswith("approved_claim:")
    assert product_tools.lookup_product_warnings(repo, ["p-1"])[0].warning_type == "PRECAUTION"
    assert product_tools.lookup_approved_claim(repo, []) == []


def test_illegal_ad_requires_high_similarity():
    assert product_tools.search_illegal_ad_cases(repo, ["另一虛構保健測試錠"], [])
    # 只有片段相同的名稱不能算命中
    assert product_tools.search_illegal_ad_cases(repo, ["虛構保健"], []) == []


def test_graph_queries_product_ingredient_and_approved_claim():
    g = InMemoryGraphRepo()
    ing = graph_tools.query_knowledge_graph(g, GraphQueryType.PRODUCT_INGREDIENTS, ["p-1"])
    assert ing[0].relation == "CONTAINS" and ing[0].object_name == "虛構素甲"
    claims = graph_tools.query_knowledge_graph(g, GraphQueryType.PRODUCT_APPROVED_CLAIMS, ["p-1"])
    assert claims[0].relation == "HAS_APPROVED_CLAIM" and claims[0].object_name == "測試功效甲"
    funcs = graph_tools.query_knowledge_graph(g, GraphQueryType.INGREDIENT_APPROVED_FUNCTIONS, ["i-1"])
    assert funcs[0].object_name == "測試功效甲"
    assert graph_tools.query_knowledge_graph(g, GraphQueryType.PRODUCT_INGREDIENTS, []) == []


def test_search_rag_embeds_query_and_returns_sources():
    llm = FakeLLM()
    out = rag_tools.search_rag(InMemoryRagStore(), llm, "TEST_ONLY 問題")
    assert llm.called("embed")
    assert out[0].source.source_url and out[0].source.is_official
    assert rag_tools.search_rag(InMemoryRagStore(), llm, "  ") == []


def test_official_web_only_keeps_allowed_domains_and_visited_pages():
    llm = FakeLLM(web=web_result(
        findings=[
            {"title": "官方", "url": "https://consumer.fda.gov.tw/page", "snippet": "s"},
            {"title": "部落格", "url": "https://blog.example.com/post", "snippet": "s"},
            {"title": "假冒網域", "url": "https://fda.gov.tw.evil.com/x", "snippet": "s"},
            {"title": "未實際造訪", "url": "https://www.mohw.gov.tw/never", "snippet": "s"},
        ],
        visited=["https://consumer.fda.gov.tw/page", "https://blog.example.com/post"],
    ))
    out = official_web_tools.search_tw_official_web(llm, "TEST_ONLY")
    assert [e.domain for e in out.evidence] == ["consumer.fda.gov.tw"]
    assert len(out.rejected_urls) == 3
    allowed = llm.calls[0][1]
    assert set(allowed) == set(official_web_tools.TW_OFFICIAL_DOMAINS)


def test_is_allowed_official_url():
    ok = official_web_tools.is_allowed_official_url
    assert ok("https://www.fda.gov.tw/x") and ok("https://data.gov.tw/dataset/1")
    assert not ok("https://notfda.gov.tw/x") and not ok("ftp://fda.gov.tw/x") and not ok("https://fda.gov.tw.evil.com")


def test_execute_tool_converts_errors_to_structured():
    failing = InMemoryOfficialRepo(fail_with=ErrorCode.UNAVAILABLE)
    result = execute_tool("lookup_product", lambda: product_tools.lookup_product(failing, ["x"]))
    assert not result.ok and result.error.code == ErrorCode.UNAVAILABLE

    def boom():
        raise RuntimeError("secret-should-not-leak")

    result = execute_tool("x", boom)
    assert result.error.code == ErrorCode.INTERNAL_ERROR
    assert "secret" not in result.error.message


def test_recorder_tracks_calls():
    rec = ToolCallRecorder("r")
    rec.call("lookup_product", {"product_names": ["x"]}, lambda: product_tools.lookup_product(repo, ["虛構牌晶亮測試膠囊"]))
    assert rec.records[0].ok and rec.records[0].result_count >= 1 and rec.errors == []


# ---------------- entity alias ----------------

from app.schemas.enums import EntityType  # noqa: E402


def test_alias_resolves_brand_to_owning_company():
    matches = product_tools.resolve_entity_alias(repo, ["虛構牌"], [EntityType.BRAND])
    assert matches[0].entity_type == EntityType.BRAND and matches[0].resolved_company_id == "c-1"
    assert matches[0].source.source_name and not matches[0].uncertain


def test_alias_contained_in_longer_query_is_matched():
    matches = product_tools.resolve_entity_alias(repo, ["虛構牌某某新產品"], [EntityType.BRAND])
    assert matches and matches[0].alias == "虛構牌" and matches[0].match_score < 1.0


def test_low_confidence_alias_is_reported_but_not_used_for_company_lookup():
    matches = product_tools.resolve_entity_alias(repo, ["低信心"], [EntityType.BRAND])
    assert matches and matches[0].uncertain
    assert product_tools.lookup_company(repo, ["低信心"]) == []


def test_lookup_company_via_brand_alias_lists_products():
    companies = product_tools.lookup_company(repo, ["虛構牌"])
    assert companies[0].company_id == "c-1" and companies[0].matched_via == "alias:DERIVED_BRAND_PREFIX"
    assert set(companies[0].product_ids) == {"p-1", "p-2"}


def test_ingredient_found_by_english_alias():
    found = product_tools.lookup_ingredient(repo, ["fictionin-a"])
    assert found[0].ingredient.ingredient_id == "i-1"
