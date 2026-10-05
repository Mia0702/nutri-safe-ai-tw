"""Interaction 題的 task-context 安全：兩個以上產品的比較不可把任一產品寫成「它」。

* 兩個以上產品對象 → 不建立 / 取代 confirmed_product，也不留任何產品實體（含更早的產品指稱）。
* 比較之後的單數代名詞（「它有哪些成分？」）不會悄悄選其中一個 → 要求補充（MISSING_INFORMATION）。
* 下一題明確寫產品名稱 → 照常處理。
* 只有一個產品 ＋ 藥品 → 沿用既有單一產品 context 契約。
全部 TEST_ONLY 虛構資料。
"""

import json

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.response.service import AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import EntityType, InputType, Intent, ProductReferenceStatus, ResponseType
from app.schemas.interaction import InteractionMode, InteractionSubjectExtraction, InteractionSubjectKind
from app.schemas.user_request import IntakeExtraction, RawUserInput
from app.services.context_store import TaskContextStore
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryInteractionRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.unit.test_ingredient_retrieval_runtime import DOM_NAME, HF_ZINC, IMP_NAME, ingredient_data  # noqa: F401

PRODUCT, DRUG = InteractionSubjectKind.PRODUCT, InteractionSubjectKind.DRUG
USER = "U_TEST_ONLY_CTX"


def _intake(input_text: str) -> IntakeExtraction:
    content = json.loads(input_text)["content"]
    if "可以一起吃" in content:
        subjects = [(n, PRODUCT) for n in (HF_ZINC, DOM_NAME, IMP_NAME) if n in content]
        if "Testdrug-B" in content:
            subjects.append(("Testdrug-B", DRUG))
        return extraction(intent=Intent.INTERACTION_INQUIRY, normalized_text=content, interaction_mode=InteractionMode.PAIRWISE,
                          interaction_subjects=[InteractionSubjectExtraction(text=t, kind=k) for t, k in subjects])
    names = [n for n in (HF_ZINC, DOM_NAME, IMP_NAME) if n in content]
    if names:
        return extraction(intent=Intent.INGREDIENT_INQUIRY, product_candidates=names[:1], normalized_text=content)
    return extraction(intent=Intent.INGREDIENT_INQUIRY, refers_to_unspecified_product=True, normalized_text=content)


@pytest.fixture
def env():
    llm = FakeLLM(structured={IntakeExtraction: _intake})
    verifier = VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm,
                                 interaction_repo=InMemoryInteractionRepo())
    pipeline = AnalysisPipeline(intake=IntakeAgent(llm), verifier=verifier, risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    contexts, store = TaskContextStore(), InMemoryRequestStore()
    svc = AdaptiveResponseService(pipeline, ReporterAgent(llm), store, timeout_seconds=10.0, context_store=contexts)

    def send(text):
        import uuid

        rid = str(uuid.uuid4())
        svc.respond(RawUserInput(input_type=InputType.TEXT, text=text), USER, request_id=rid)
        return store.get(rid)

    return send, contexts


def product_entities(contexts):
    ctx = contexts.snapshot(USER)
    return [e for e in ctx.recent_confirmed_entities + [x for t in ctx.turns for x in t.entities]
            if e.entity_type == EntityType.PRODUCT]


def test_two_product_comparison_writes_no_product_context(env):
    send, contexts = env
    rec = send(f"{HF_ZINC} 跟 {DOM_NAME} 可以一起吃嗎")
    assert rec.response.response_type == ResponseType.INTERACTION_RESULT
    ctx = contexts.snapshot(USER)
    assert ctx.confirmed_product is None and ctx.current_product_id is None and ctx.current_product_name is None
    assert product_entities(contexts) == []


def test_comparison_clears_earlier_single_product_context(env):
    send, contexts = env
    send(f"{DOM_NAME} 有哪些成分")
    assert contexts.snapshot(USER).confirmed_product.official_product_name == DOM_NAME      # 前提：先有單一產品 context
    send(f"{HF_ZINC} 跟 {IMP_NAME} 可以一起吃嗎")
    ctx = contexts.snapshot(USER)
    assert ctx.confirmed_product is None and ctx.current_product_name is None and product_entities(contexts) == []


def test_ambiguous_pronoun_after_comparison_is_not_resolved_to_either_product(env):
    send, contexts = env
    send(f"{HF_ZINC} 跟 {DOM_NAME} 可以一起吃嗎")
    rec = send("它有哪些成分？")
    assert rec.normalized_request.product_reference_status != ProductReferenceStatus.RESOLVED_FROM_CONTEXT
    assert rec.response.response_type == ResponseType.MISSING_INFORMATION
    assert rec.evidence.product_match.best_match is None
    assert contexts.snapshot(USER).confirmed_product is None


def test_pronoun_after_comparison_does_not_fall_back_to_an_older_product(env):
    send, _ = env
    send(f"{DOM_NAME} 有哪些成分")
    send(f"{HF_ZINC} 跟 {IMP_NAME} 可以一起吃嗎")
    rec = send("它有哪些成分？")
    assert rec.response.response_type == ResponseType.MISSING_INFORMATION and rec.evidence.product_match.best_match is None


def test_explicit_product_after_comparison_behaves_normally(env):
    send, contexts = env
    send(f"{HF_ZINC} 跟 {DOM_NAME} 可以一起吃嗎")
    rec = send(f"{IMP_NAME} 有哪些成分")
    assert rec.response.response_type == ResponseType.INGREDIENT_LIST
    assert contexts.snapshot(USER).confirmed_product.official_product_name == IMP_NAME


def test_single_product_with_drug_keeps_existing_single_product_context(env):
    send, contexts = env
    rec = send(f"{IMP_NAME} 跟 Testdrug-B 可以一起吃嗎")
    assert rec.response.response_type == ResponseType.INTERACTION_RESULT
    assert contexts.snapshot(USER).confirmed_product.official_product_name == IMP_NAME
    follow = send("它有哪些成分？")
    assert follow.response.response_type == ResponseType.INGREDIENT_LIST
    assert follow.evidence.product_match.best_match.product.product_name_zh == IMP_NAME
