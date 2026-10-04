"""Agent B（Evidence / Verification）的 Evidence Tools allowlist 與執行狀態。

責任邊界：
* Data Services / Data Sources（Supabase、Neo4j、pgvector、台灣官方網站）只存資料，不是 Agent。
* Evidence Tool 是 Agent B 實際呼叫的 function interface（app.tools.*），負責查詢並回傳結構化結果。
* Agent B 只能呼叫這裡註冊的 Evidence Tools；Agent A 的 Input Tools 見 app.tools.input_tools，
  兩組工具互不共用（tests/unit/test_tool_boundaries.py 檢查）。

* 只有在這裡註冊的工具能被 LLM planner 呼叫；名稱不在 allowlist 的呼叫一律拒絕。
* 每個工具有 pydantic 參數 schema（strict JSON schema 給 OpenAI function calling）。
* guard：程式端的 deterministic 檢查（例如 ID 必須來自先前的工具結果）。
* handler：真正呼叫 app.tools 中的 Tool，並把結果放入 PlannerState（證據由程式收集，不由 LLM 撰寫）。
* summarize：回傳給 LLM 的精簡結果，讓 planner 決定下一步。
"""

from dataclasses import dataclass, field
from typing import Any, Callable

from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, Field

from app.normalization import normalize_name
from app.schemas.enums import EntityType, ErrorCode
from app.schemas.evidence import (
    ApprovedClaimEvidence,
    ClaimPolicyExampleEvidence,
    ClaimPolicyRuleEvidence,
    HealthFunctionEvidence,
    EvidenceBundle,
    InferredEntityLink,
    ProductMatch,
    GraphEvidence,
    IngredientRecord,
    OfficialWebEvidence,
    ProductCandidateMatch,
    RagEvidence,
    WarningEvidence,
)
from app.schemas.user_request import NormalizedUserRequest
from app.services.errors import ServiceError
from app.tools import claim_policy_tools, graph_tools, official_web_tools, product_tools, rag_tools
from app.tools.graph_tools import GraphQueryType

MAX_LIST_ITEMS = 8
MAX_TEXT_CHARS = 300


@dataclass
class PlannerState:
    """一次查證過程中，所有工具結果的程式端暫存。"""

    request: NormalizedUserRequest
    bundle: EvidenceBundle
    repo: Any = None
    graph: Any = None
    rag: Any = None
    llm: Any = None
    claim_policy: Any = None  # ClaimPolicyRepository（TFDA 宣稱法規 corpus）
    rag_top_k: int = 5
    rag_min_similarity: float = 0.3
    known_product_ids: set[str] = field(default_factory=set)
    known_ingredient_ids: set[str] = field(default_factory=set)
    name_matches: dict[str, ProductCandidateMatch] = field(default_factory=dict)
    permit_matches: list[ProductCandidateMatch] = field(default_factory=list)
    company_names_found: list[str] = field(default_factory=list)  # 只含官方名稱直接比對到的申請商
    inferred_links: list[InferredEntityLink] = field(default_factory=list)  # 經推測別名連到的申請商
    approved_by_product: dict[str, list[ApprovedClaimEvidence]] = field(default_factory=dict)
    warnings_by_product: dict[str, list[WarningEvidence]] = field(default_factory=dict)
    ingredients_by_product: dict[str, list[IngredientRecord]] = field(default_factory=dict)
    graph_evidence: dict[str, GraphEvidence] = field(default_factory=dict)
    candidate_rag: dict[str, RagEvidence] = field(default_factory=dict)
    candidate_web: dict[str, OfficialWebEvidence] = field(default_factory=dict)
    attempted: dict[str, int] = field(default_factory=dict)  # tool name -> 次數
    failed: set[str] = field(default_factory=set)
    call_signatures: set[str] = field(default_factory=set)
    web_rejected_urls: list[str] = field(default_factory=list)
    # deterministic resolver 在 LLM planning 前已確定的結果（VERIFIED 或真正 ambiguous），planner 不可改寫
    locked_product_match: ProductMatch | None = None
    # assess_evidence 去重：已評估過的證據 id、上一次評估時的已確認參考資料與結果
    assessed_evidence_ids: set[str] = field(default_factory=set)
    last_assessment_refs: frozenset[str] | None = None
    last_assessment_links: list[Any] = field(default_factory=list)
    assessment_count: int = 0
    # 結構化 claim-policy：查到的規則 / 例示 / 保健功效（evidence_use 原樣保留）
    policy_rules: dict[str, ClaimPolicyRuleEvidence] = field(default_factory=dict)  # rule_id →
    policy_examples: dict[str, ClaimPolicyExampleEvidence] = field(default_factory=dict)  # example_id →
    health_functions: dict[str, HealthFunctionEvidence] = field(default_factory=dict)  # function_id →
    # 問句（正規化）→ 其中逐字出現的官方功效名稱（保健功效目錄）
    function_mentions: dict[str, list[str]] = field(default_factory=dict)
    # 命題（正規化文字）→ 已 deterministic 對應到的唯一官方功效名稱（同時是該產品的核准功效）
    question_functions: dict[str, str] = field(default_factory=dict)
    medical_rule_ids: set[str] = field(default_factory=set)  # 醫療效能禁止規則（statutory 來源）
    claim_policy_checked: bool = False
    policy_decided_claims: set[int] = field(default_factory=set)  # 結構化證據已足以決定的宣稱（不需 RAG / web）
    # 其中連語意比對（assess_evidence）都不需要的命題：結論完全由 deterministic 規則決定
    policy_final_claims: set[int] = field(default_factory=set)


def _require(dep: Any, source: str) -> Any:
    if dep is None:
        raise ServiceError(ErrorCode.NOT_CONFIGURED, f"{source} 未設定或暫時無法使用", source)
    return dep


def _trim_list(values: list[str]) -> list[str]:
    return [v.strip()[:MAX_TEXT_CHARS] for v in values if v and v.strip()][:MAX_LIST_ITEMS]


# ---------------------------------------------------------------------------
# 參數 schema
# ---------------------------------------------------------------------------


class LookupProductArgs(BaseModel):
    product_names: list[str] = Field(description="要查的產品名稱（使用者提供的寫法）")
    brand_names: list[str] = Field(description="相關品牌名稱，沒有就給空陣列")


class LookupPermitArgs(BaseModel):
    permit_text: str = Field(description="許可證字號或其片段")


class NamesArgs(BaseModel):
    names: list[str]


class ResolveAliasArgs(BaseModel):
    names: list[str]
    entity_types: list[EntityType] = Field(description="要解析成哪些實體類型")


class ProductIdsArgs(BaseModel):
    product_ids: list[str] = Field(description="先前工具結果中出現過的 product_id")


class IllegalAdArgs(BaseModel):
    product_names: list[str]
    company_names: list[str]


class GraphArgs(BaseModel):
    query_type: GraphQueryType
    ids: list[str] = Field(description="product_id 或 ingredient_id（須來自先前工具結果）")


class QueryArgs(BaseModel):
    query: str = Field(description="繁體中文查詢語句")


class ClaimPolicyRulesArgs(BaseModel):
    rule_ids: list[str] = Field(description="規則 ID（例如官方例示所屬的 rule_id），沒有就給空陣列")
    rule_categories: list[str] = Field(description="規則類別（例如 MEDICAL_EFFICACY），需與 policy_effects 同時提供")
    policy_effects: list[str] = Field(description="PERMITTED / RESTRICTED / PROHIBITED / REQUIRES_EVIDENCE / PRODUCT_SPECIFIC_ONLY")


class ClaimTextsArgs(BaseModel):
    claim_texts: list[str] = Field(description="要比對的宣稱原文（逐條）")


class HealthFunctionArgs(BaseModel):
    function_names: list[str] = Field(description="官方保健功效名稱，例如「調節血脂」")
    question_texts: list[str] = Field(description="要找出其中逐字提到哪些官方功效名稱的問句（已去除產品名稱），沒有就給空陣列")


# ---------------------------------------------------------------------------
# guards（回傳錯誤訊息代表拒絕執行）
# ---------------------------------------------------------------------------


def _guard_product_ids(state: PlannerState, args: ProductIdsArgs) -> str | None:
    unknown = [pid for pid in args.product_ids if pid not in state.known_product_ids]
    if unknown or not args.product_ids:
        return f"product_id 必須來自先前工具結果，未知 ID：{unknown[:3]}"
    return None


def _guard_graph(state: PlannerState, args: GraphArgs) -> str | None:
    known = (
        state.known_ingredient_ids
        if args.query_type == GraphQueryType.INGREDIENT_APPROVED_FUNCTIONS
        else state.known_product_ids
    )
    unknown = [i for i in args.ids if i not in known]
    if unknown or not args.ids:
        return f"ids 必須來自先前工具結果，未知 ID：{unknown[:3]}"
    return None


def _guard_nonempty_query(state: PlannerState, args: QueryArgs) -> str | None:
    return None if args.query.strip() else "query 不可為空"


def _guard_rule_filter(state: PlannerState, args: ClaimPolicyRulesArgs) -> str | None:
    if args.rule_ids or (args.rule_categories and args.policy_effects):
        return None
    return "需要 rule_ids，或同時提供 rule_categories 與 policy_effects"


# ---------------------------------------------------------------------------
# handlers：呼叫 Tool → 寫入 state → 回傳 (data, summary)
# ---------------------------------------------------------------------------


def _product_summary(m: ProductCandidateMatch) -> dict[str, Any]:
    p = m.product
    return {
        "product_id": p.product_id, "name": p.product_name_zh, "company": p.company_name,
        "permit_number": p.permit_number, "permit_status": p.status.value, "match_score": m.match_score,
    }


def h_lookup_product(state: PlannerState, args: LookupProductArgs):
    out = product_tools.lookup_product(_require(state.repo, "supabase"), _trim_list(args.product_names), _trim_list(args.brand_names))
    for m in out.candidates:
        state.known_product_ids.add(m.product.product_id)
        cur = state.name_matches.get(m.product.product_id)
        if cur is None or m.match_score > cur.match_score:
            state.name_matches[m.product.product_id] = m
    return out, {"candidates": [_product_summary(m) for m in out.candidates[:5]]}


def h_lookup_permit(state: PlannerState, args: LookupPermitArgs):
    out = product_tools.lookup_permit(_require(state.repo, "supabase"), args.permit_text[:MAX_TEXT_CHARS])
    for m in out.candidates:
        state.known_product_ids.add(m.product.product_id)
        if all(x.product.product_id != m.product.product_id for x in state.permit_matches):
            state.permit_matches.append(m)
    return out, {"matches": [_product_summary(m) for m in out.candidates[:5]]}


def h_lookup_company(state: PlannerState, args: NamesArgs):
    out = product_tools.lookup_company(_require(state.repo, "supabase"), _trim_list(args.names))
    summary = []
    for c in out:
        state.known_product_ids.update(c.product_ids)
        if c.is_inferred:
            if all(l.resolved_company_id != c.company_id for l in state.inferred_links):
                state.inferred_links.append(InferredEntityLink(
                    query=c.query, entity_type="BRAND" if "BRAND" in c.matched_via else "COMPANY", alias=c.alias or c.query,
                    resolved_company_id=c.company_id, resolved_company_name=c.company_name,
                    confidence=c.alias_confidence or 0.0, inference_basis=c.inference_basis,
                ))
        elif c.company_name not in state.company_names_found:
            state.company_names_found.append(c.company_name)
        summary.append({
            "company": c.company_name, "matched_via": c.matched_via, "alias_confidence": c.alias_confidence,
            "is_inferred": c.is_inferred,
            "note": "依產品名稱推測的關聯，不是官方確認" if c.is_inferred else "官方申請商名稱比對",
            "query": c.query, "product_count": len(c.product_ids), "product_ids_sample": c.product_ids[:8],
        })
    return out, {"companies": summary}


def h_resolve_alias(state: PlannerState, args: ResolveAliasArgs):
    out = product_tools.resolve_entity_alias(_require(state.repo, "supabase"), _trim_list(args.names), args.entity_types)
    for a in out:
        if a.entity_type == EntityType.INGREDIENT and not a.uncertain:
            state.known_ingredient_ids.add(a.entity_id)
    return out, {"aliases": [
        {"query": a.query, "entity_type": a.entity_type.value, "alias": a.alias, "alias_type": a.alias_type,
         "confidence": a.confidence, "uncertain": a.uncertain, "is_inferred": a.is_inferred,
         "resolved_company_id": a.resolved_company_id}
        for a in out[:8]
    ]}


def h_lookup_ingredient(state: PlannerState, args: NamesArgs):
    out = product_tools.lookup_ingredient(_require(state.repo, "supabase"), _trim_list(args.names))
    for m in out[:5]:
        state.known_ingredient_ids.add(m.ingredient.ingredient_id)
        if all(i.ingredient_id != m.ingredient.ingredient_id for i in state.bundle.ingredients):
            state.bundle.ingredients.append(m.ingredient)
    return out, {"ingredients": [
        {"ingredient_id": m.ingredient.ingredient_id, "name": m.ingredient.name_zh, "match_score": m.match_score}
        for m in out[:5]
    ]}


def h_approved_claims(state: PlannerState, args: ProductIdsArgs):
    out = product_tools.lookup_approved_claim(_require(state.repo, "supabase"), args.product_ids)
    for pid in args.product_ids:
        state.approved_by_product[pid] = [e for e in out if e.product_id == pid]
    return out, {"approved_claims": [
        {"product_id": e.product_id, "function": e.function_name, "claim_text": e.claim_text[:200]} for e in out[:10]
    ]}


def h_warnings(state: PlannerState, args: ProductIdsArgs):
    out = product_tools.lookup_product_warnings(_require(state.repo, "supabase"), args.product_ids)
    for pid in args.product_ids:
        state.warnings_by_product[pid] = [e for e in out if e.product_id == pid]
    return out, {"warning_count": len(out), "warnings": [w.text[:80] for w in out[:5]]}


def h_product_ingredients(state: PlannerState, args: ProductIdsArgs):
    repo = _require(state.repo, "supabase")
    out = []
    for pid in args.product_ids:
        items = product_tools.lookup_product_ingredients(repo, [pid])
        state.ingredients_by_product[pid] = items
        for i in items:
            state.known_ingredient_ids.add(i.ingredient_id)
        out.extend(items)
    return out, {"ingredients": [{"ingredient_id": i.ingredient_id, "name": i.name_zh} for i in out[:10]]}


def h_illegal_ads(state: PlannerState, args: IllegalAdArgs):
    out = product_tools.search_illegal_ad_cases(
        _require(state.repo, "supabase"), _trim_list(args.product_names), _trim_list(args.company_names)
    )
    known = {e.evidence_id for e in state.bundle.illegal_ad_evidence}
    state.bundle.illegal_ad_evidence.extend(e for e in out if e.evidence_id not in known)
    return out, {"records": [{"product": e.product_name, "content": e.violation_content[:100]} for e in out[:5]]}


def h_graph(state: PlannerState, args: GraphArgs):
    out = graph_tools.query_knowledge_graph(_require(state.graph, "neo4j"), args.query_type, args.ids)
    for e in out:
        state.graph_evidence[e.evidence_id] = e
    return out, {"relations": [
        {"subject": e.subject_name, "relation": e.relation, "object": e.object_name,
         "product_count": e.properties.get("product_count")}
        for e in out[:10]
    ]}


def h_rag(state: PlannerState, args: QueryArgs):
    out = rag_tools.search_rag(
        _require(state.rag, "rag"), _require(state.llm, "openai"), args.query[:MAX_TEXT_CHARS],
        state.rag_top_k, state.rag_min_similarity, state.request.country_code,
    )
    for e in out:
        state.candidate_rag.setdefault(e.evidence_id, e)
    return out, {"chunks": [
        {"title": e.title, "similarity": round(e.similarity, 3), "excerpt": e.content[:150],
         "evidence_use": e.policy_metadata.evidence_use.value if e.policy_metadata else None}
        for e in out
    ]}


def _policy_repo(state: PlannerState):
    return _require(state.claim_policy, "claim_policy")


def h_policy_rules(state: PlannerState, args: ClaimPolicyRulesArgs):
    out = claim_policy_tools.search_claim_policy_rules(
        _policy_repo(state), rule_ids=_trim_list(args.rule_ids), rule_categories=_trim_list(args.rule_categories),
        policy_effects=_trim_list(args.policy_effects),
    )
    for e in out:
        state.policy_rules[e.rule_id] = e
    return out, {"rules": [
        {"rule_id": e.rule_id, "policy_effect": e.policy_effect, "article": e.article_or_section,
         "summary": (e.rule_summary_zh or "")[:120], "evidence_use": e.evidence_use.value}
        for e in out[:MAX_LIST_ITEMS]
    ]}


def h_policy_examples(state: PlannerState, args: ClaimTextsArgs):
    out = claim_policy_tools.search_claim_policy_examples(_policy_repo(state), _trim_list(args.claim_texts))
    for e in out:
        cur = state.policy_examples.get(e.example_id)
        if cur is not None:  # 同一例示被多次查詢：合併對應到的宣稱文字
            e = e.model_copy(update={"matched_claim_texts": list(dict.fromkeys(cur.matched_claim_texts + e.matched_claim_texts))})
        state.policy_examples[e.example_id] = e
    return out, {"examples": [
        {"example_id": e.example_id, "rule_id": e.rule_id, "example_type": e.example_type,
         "text": e.example_text[:120], "evidence_use": e.evidence_use.value}
        for e in out[:MAX_LIST_ITEMS]
    ]}


def h_health_function(state: PlannerState, args: HealthFunctionArgs):
    repo = _policy_repo(state)
    out = claim_policy_tools.lookup_health_function(repo, _trim_list(args.function_names))
    for text in _trim_list(args.question_texts):
        mentioned = claim_policy_tools.health_functions_in_text(repo, text)
        state.function_mentions[normalize_name(text)] = [e.official_function_name for e in mentioned]
        out += [e for e in mentioned if all(x.function_id != e.function_id for x in out)]
    for e in out:
        state.health_functions[e.function_id] = e
    return out, {"health_functions": [
        {"function_id": e.function_id, "name": e.official_function_name,
         "definition": (e.official_definition or "")[:150], "evidence_use": e.evidence_use.value}
        for e in out
    ]}


def h_web(state: PlannerState, args: QueryArgs):
    out = official_web_tools.search_tw_official_web(_require(state.llm, "openai"), args.query[:MAX_TEXT_CHARS])
    for e in out.evidence:
        state.candidate_web.setdefault(e.evidence_id, e)
    state.web_rejected_urls.extend(out.rejected_urls)
    state.bundle.official_web_search_attempted = True
    return out, {"results": [{"title": e.title, "domain": e.domain} for e in out.evidence]}


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[PlannerState, Any], tuple[Any, dict[str, Any]]]
    guard: Callable[[PlannerState, Any], str | None] | None = None

    def openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": to_strict_json_schema(self.args_model),
            "strict": True,
        }


EVIDENCE_TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec("lookup_product", "以產品名稱（可附品牌）查台灣官方健康食品紀錄，回傳候選與比對分數。", LookupProductArgs, h_lookup_product),
        ToolSpec("lookup_permit", "以許可證字號查官方產品。", LookupPermitArgs, h_lookup_permit),
        ToolSpec("lookup_company", "以公司或品牌名稱查官方申請商及其產品（含別名解析）。", NamesArgs, h_lookup_company),
        ToolSpec("resolve_entity_alias", "把名稱解析成官方實體（品牌→申請商、公司簡稱、成分英文名）。", ResolveAliasArgs, h_resolve_alias),
        ToolSpec("lookup_ingredient", "以名稱查官方資料中的成分，回傳 ingredient_id。", NamesArgs, h_lookup_ingredient),
        ToolSpec("lookup_approved_claim", "查產品官方核准的保健功效與核准敘述。", ProductIdsArgs, h_approved_claims, _guard_product_ids),
        ToolSpec("lookup_product_warnings", "查產品官方登記的警語與注意事項。", ProductIdsArgs, h_warnings, _guard_product_ids),
        ToolSpec("lookup_product_ingredients", "查產品官方登記的保健功效相關成分。", ProductIdsArgs, h_product_ingredients, _guard_product_ids),
        ToolSpec("search_illegal_ad_cases", "查官方公布的違規廣告紀錄（依產品 / 公司名稱）。", IllegalAdArgs, h_illegal_ads),
        ToolSpec("query_knowledge_graph", "查知識圖譜：產品→成分、產品→核准功效、成分→含此成分產品的核准功效。", GraphArgs, h_graph, _guard_graph),
        ToolSpec("search_claim_policy_rules", "查 TFDA 宣稱法規的結構化規則（依 rule_id，或規則類別＋效果）。",
                 ClaimPolicyRulesArgs, h_policy_rules, _guard_rule_filter),
        ToolSpec("search_claim_policy_examples", "比對宣稱原文是否與 TFDA 官方例示詞句（可用 / 醫療效能 / 有條件）文字相同。",
                 ClaimTextsArgs, h_policy_examples),
        ToolSpec("lookup_health_function", "查健康食品保健功效項目的官方定義與評估方法。", HealthFunctionArgs, h_health_function),
        ToolSpec("search_rag", "在台灣官方法規 / 指引文件中做語意檢索。", QueryArgs, h_rag, _guard_nonempty_query),
    ]
}

OFFICIAL_WEB_TOOLS: dict[str, ToolSpec] = {
    "search_tw_official_web": ToolSpec(
        "search_tw_official_web", "搜尋台灣官方網站（fda.gov.tw、mohw.gov.tw 等）。", QueryArgs, h_web, _guard_nonempty_query
    ),
}
