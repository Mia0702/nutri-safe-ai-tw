"""TEST_ONLY — 測試用 fake 實作（repository / graph / rag / LLM）。

Fake repository 依照 Supabase RPC 的相同契約（以詞片段命中數篩選候選），
讓 tools 的比對與判定邏輯可以離線測試。
"""

import json
from typing import Any, Callable

from pydantic import BaseModel

from app.schemas.user_request import IntakeExtraction
from app.schemas.enums import ErrorCode
from app.services.errors import ServiceError
from app.services.openai_service import PlannedCall, PlanStepResult, WebSearchResult, WebSearchSource
from tests.fixtures import synthetic_data as data


def _hits(terms: list[str], *fields: str) -> int:
    return sum(1 for t in terms if t and any(t in (f or "") for f in fields))


class InMemoryOfficialRepo:
    def __init__(self, fail_with: ErrorCode | None = None):
        self.fail_with = fail_with
        self.calls: list[str] = []

    def _check(self, name: str):
        self.calls.append(name)
        if self.fail_with:
            raise ServiceError(self.fail_with, "TEST_ONLY simulated failure", "supabase")

    def search_products(self, terms, limit):
        self._check("search_products")
        rows = [(p, _hits(terms, p["product_name_normalized"], p["company_name_normalized"])) for p in data.PRODUCTS]
        rows = [r for r in rows if r[1] > 0]
        rows.sort(key=lambda r: (-r[1], len(r[0]["product_name_normalized"])))
        return [dict(r[0]) for r in rows[:limit]]

    def get_products_by_permit_code(self, permit_code):
        self._check("get_products_by_permit_code")
        return [dict(p) for p in data.PRODUCTS if p["permit_code"] == permit_code]

    def search_ingredients(self, terms, limit):
        self._check("search_ingredients")
        rows = [i for i in data.INGREDIENTS if _hits(terms, i["name_normalized"], (i["name_en"] or "").lower())]
        return [dict(i) for i in rows[:limit]]

    def get_approved_claims(self, product_ids):
        self._check("get_approved_claims")
        return [dict(c) for c in data.APPROVED_CLAIMS if c["product_id"] in product_ids]

    def get_warnings(self, product_ids):
        self._check("get_warnings")
        return [dict(w) for w in data.WARNINGS if w["product_id"] in product_ids]

    def get_ingredients_for_products(self, product_ids):
        self._check("get_ingredients_for_products")
        by_id = {i["id"]: i for i in data.INGREDIENTS}
        return [
            {**pi, "name_zh": by_id[pi["ingredient_id"]]["name_zh"], "name_en": by_id[pi["ingredient_id"]]["name_en"], **data.SOURCE}
            for pi in data.PRODUCT_INGREDIENTS
            if pi["product_id"] in product_ids
        ]

    def search_illegal_ad_cases(self, terms, limit):
        self._check("search_illegal_ad_cases")
        return [dict(a) for a in data.ILLEGAL_ADS if _hits(terms, a["product_name_normalized"], a["company_name_normalized"])]

    def search_entity_aliases(self, terms, entity_types, limit):
        self._check("search_entity_aliases")
        out = []
        for a in data.ALIASES:
            if a["entity_type"] not in entity_types:
                continue
            if any(t and (t in a["alias_normalized"] or a["alias_normalized"] in t) for t in terms):
                out.append(dict(a))
        return out[:limit]

    def get_products_by_company_ids(self, company_ids, limit):
        self._check("get_products_by_company_ids")
        return [dict(p) for p in data.PRODUCTS if p["company_id"] in company_ids][:limit]

    def get_product_ingredient_sources(self, product_ids):
        self._check("get_product_ingredient_sources")
        return [{"product_id": p["product_id"], "product_category": p.get("product_category") or "health_food",
                 "source_name": p["source_name"], "source_url": p.get("source_url"), "is_official": p["is_official"],
                 "source_record_key": p.get("source_record_key"), "retrieved_at": p.get("retrieved_at"),
                 "ingredient_staging": data.INGREDIENT_STAGING.get(p["product_id"])}
                for p in data.PRODUCTS if p["product_id"] in product_ids]

    def get_ingredient_bridge_reference(self):
        self._check("get_ingredient_bridge_reference")
        return dict(data.INGREDIENT_BRIDGE)

    def get_identity_generic_terms(self):
        self._check("get_identity_generic_terms")
        bridge = data.INGREDIENT_BRIDGE
        return ([i["name_zh"] for i in data.INGREDIENTS] + [i["name_en"] for i in data.INGREDIENTS if i.get("name_en")]
                + [e["display_name_zh"] for e in bridge.get("entities", []) if e.get("display_name_zh")]
                + [a["alias_text"] for a in bridge.get("aliases", []) if a.get("entity_id")])

    def get_ingredient_entity_mappings(self, product_ids):
        self._check("get_ingredient_entity_mappings")
        return [dict(m) for m in data.INGREDIENT_ENTITY_MAPPINGS if m["product_id"] in product_ids]


class InMemoryGraphRepo:
    def __init__(self, fail: bool = False):
        self.fail = fail

    def _check(self):
        if self.fail:
            raise ServiceError(ErrorCode.UNAVAILABLE, "TEST_ONLY graph down", "neo4j")

    def product_ingredients(self, product_ids):
        self._check()
        names = {p["product_id"]: p["product_name_zh"] for p in data.PRODUCTS}
        ing = {i["id"]: i["name_zh"] for i in data.INGREDIENTS}
        return [
            {"product_id": pi["product_id"], "product_name": names[pi["product_id"]], "ingredient_id": pi["ingredient_id"],
             "ingredient_name": ing[pi["ingredient_id"]], "raw_text": pi["raw_text"],
             "source_name": data.SOURCE["source_name"], "is_official": True, "source_url": data.SOURCE["source_url"]}
            for pi in data.PRODUCT_INGREDIENTS if pi["product_id"] in product_ids
        ]

    def product_approved_claims(self, product_ids):
        self._check()
        names = {p["product_id"]: p["product_name_zh"] for p in data.PRODUCTS}
        return [
            {"product_id": c["product_id"], "product_name": names[c["product_id"]], "claim_id": c["approved_claim_id"],
             "function_name": c["function_name"], "claim_text": c["claim_text"],
             "source_name": data.SOURCE["source_name"], "is_official": True, "source_url": data.SOURCE["source_url"]}
            for c in data.APPROVED_CLAIMS if c["product_id"] in product_ids
        ]

    def ingredient_approved_functions(self, ingredient_ids, limit):
        self._check()
        out = []
        for pi in data.PRODUCT_INGREDIENTS:
            if pi["ingredient_id"] not in ingredient_ids:
                continue
            for c in data.APPROVED_CLAIMS:
                if c["product_id"] == pi["product_id"]:
                    ing = next(i for i in data.INGREDIENTS if i["id"] == pi["ingredient_id"])
                    out.append({"ingredient_id": ing["id"], "ingredient_name": ing["name_zh"],
                                "claim_id": c["approved_claim_id"], "function_name": c["function_name"],
                                "product_count": 1, "sample_products": [], "source_name": data.SOURCE["source_name"],
                                "is_official": True})
        return out[:limit]


class InMemoryRagStore:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = data.RAG_CHUNKS if rows is None else rows

    def match_chunks(self, embedding, top_k, min_similarity, country_code="TW"):
        return [r for r in self.rows if r["similarity"] >= min_similarity][:top_k]


def default_assessment(input_text: str):
    """未指定時：所有候選證據視為高度相關（CLAIM_REGULATION），不產生命題關係。"""
    from app.agents.verification_agent import _EvidenceAssessment

    payload = json.loads(input_text)
    return _EvidenceAssessment.model_validate({
        "relevance": [
            {"evidence_id": c["evidence_id"], "relevance_score": 0.9, "evidence_scope": "CLAIM_REGULATION",
             "supports_claim": None, "rationale": "TEST_ONLY"}
            for c in payload["candidates_to_assess"]
        ],
        "claim_links": [],
    })


def plan(*calls: tuple[str, dict]) -> PlanStepResult:
    """建立一輪 planner 回應：plan(("lookup_product", {...}), ...)；不給參數代表 planner 結束。"""
    return PlanStepResult(
        response_id=f"resp-{len(calls)}",
        calls=[PlannedCall(call_id=f"call-{i}", name=n, arguments=json.dumps(a, ensure_ascii=False))
               for i, (n, a) in enumerate(calls)],
    )


class FakeLLM:
    """依 output_model 回傳預先設定的結果；planner 依腳本逐輪回應；可設定某些方法失敗。"""

    def __init__(
        self,
        structured: dict[type, Callable[[str], BaseModel] | BaseModel] | None = None,
        text: str | None = "TEST_ONLY 白話回覆正文。",
        web: WebSearchResult | None = None,
        fail: set[str] | None = None,
        plans: list[PlanStepResult] | None = None,
        web_plans: list[PlanStepResult] | None = None,
        transcript: str | None = None,
        image_text: str | None = None,
    ):
        self.structured = structured or {}
        self.text = text
        self.web = web
        self.fail = fail or set()
        self.plans = list(plans or [])
        self.web_plans = list(web_plans or [])
        self.calls: list[tuple[str, Any]] = []
        self.plan_inputs: list[Any] = []
        self.structured_inputs: list[tuple[str, str]] = []
        self.transcript = transcript
        self.image_text = image_text

    def _maybe_fail(self, name):
        if name in self.fail:
            raise ServiceError(ErrorCode.TIMEOUT, "TEST_ONLY simulated timeout", "openai")

    def parse_structured(self, *, instructions, input_text, output_model):
        self.calls.append(("parse_structured", output_model.__name__))
        self.structured_inputs.append((output_model.__name__, input_text))
        self._maybe_fail("parse_structured")
        value = self.structured.get(output_model)
        if value is None and output_model.__name__ == "_EvidenceAssessment":
            value = default_assessment
        return value(input_text) if callable(value) and not isinstance(value, BaseModel) else value

    def plan_step(self, *, instructions, input_items, tools, previous_response_id=None, tool_choice="auto"):
        names = [t["name"] for t in tools]
        self.calls.append(("plan_step", names))
        self.plan_inputs.append(input_items)
        self._maybe_fail("plan_step")
        queue = self.web_plans if names == ["search_tw_official_web"] else self.plans
        if queue:
            return queue.pop(0)
        if names == ["search_tw_official_web"]:
            return plan(("search_tw_official_web", {"query": "TEST_ONLY 官方查詢"}))
        return plan()

    def generate_text(self, *, instructions, input_text):
        self.calls.append(("generate_text", input_text))
        self._maybe_fail("generate_text")
        return self.text

    def describe_image(self, *, instructions, image, mime_type):
        self.calls.append(("describe_image", mime_type))
        self._maybe_fail("describe_image")
        if self.image_text:
            return self.image_text
        # 圖片中讀到的文字要包含 Fake Agent A 抽出的產品名稱（與真實 Agent A 相同：名稱必須在輸入文字中，provenance）
        fixed = self.structured.get(IntakeExtraction)
        names = " ".join(fixed.product_candidates) if isinstance(fixed, IntakeExtraction) else ""
        return f"TEST_ONLY 圖片文字：{names or '虛構牌晶亮測試膠囊'}"

    def transcribe_audio(self, *, audio, filename):
        self.calls.append(("transcribe_audio", filename))
        self._maybe_fail("transcribe_audio")
        return self.transcript or "TEST_ONLY 語音：這個可以降血糖嗎"

    def embed(self, texts):
        self.calls.append(("embed", texts))
        self._maybe_fail("embed")
        return [[0.0] * 1536 for _ in texts]

    def web_search_structured(self, *, instructions, input_text, output_model, allowed_domains):
        self.calls.append(("web_search_structured", allowed_domains))
        self._maybe_fail("web_search_structured")
        if self.web is None:
            return WebSearchResult(parsed=output_model(findings=[]), sources=[])
        return self.web

    def called(self, name: str) -> bool:
        return any(c[0] == name for c in self.calls)


def web_result(findings: list[dict], visited: list[str]) -> WebSearchResult:
    from app.tools.official_web_tools import _WebSearchOutput

    return WebSearchResult(
        parsed=_WebSearchOutput.model_validate({"findings": findings}),
        sources=[WebSearchSource(url=u) for u in visited],
    )
