"""兩個對話層問題：

1. 目前產品（已確認）泛指問「它跟其他產品有沒有交互作用」→ CURRENT_PRODUCT_INTERACTION_OVERVIEW：
   以 task context 中已確認的產品為唯一對象，列出所含成分在 corpus 已知的交互作用對象（最多 3 個）；不把「其他產品」當產品名稱、
   不出現無關的產品候選卡、不建立產品 ↔ 產品關係；沒有紀錄就如實說明；之後使用者指名第二個對象 → 回到既有 pairwise 流程。
2. 語音候選：唯一候選 →「確認：X」＋「不是，重新輸入」；多個 → 選擇；按鈕以原題 request 繼續（不重跑 Controller、不改資料、不建 alias）。
全部使用 TEST_ONLY 虛構資料；候選文字全部由結構化資料動態產生，沒有任何寫死的成分 / 產品名稱。
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.line.handler import LineWebhookHandler
from app.pipeline import AnalysisPipeline
from app.response.actions import build_postback_data, parse_postback_data
from app.response.service import (
    ASR_EXPIRED_TEXT,
    ASR_INVALID_TEXT,
    ASR_REJECTED_TEXT,
    EXPIRED_ACTION_TEXT,
    AdaptiveResponseService,
)
from app.response.store import InMemoryRequestStore
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import InputType, Intent, ProductConfirmationSource, ResponseAction, ResponseType
from app.schemas.interaction import (
    InteractionMode,
    InteractionResearchStatus,
    InteractionSubjectExtraction,
    InteractionSubjectKind,
    SubjectResolutionStatus,
)
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from app.services.asr_vocabulary import PhoneticCandidate
from app.services.context_store import TaskContextStore
from app.services.interaction_service import InteractionService
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryInteractionRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.fixtures import synthetic_data as data
from tests.fixtures.synthetic_data import SOURCE, _product
from tests.unit.test_ingredient_retrieval_runtime import HF_ZINC, ingredient_data  # noqa: F401（autouse fixture）
from tests.unit.test_interaction_reporter import texts_of

ING, PRODUCT = InteractionSubjectKind.INGREDIENT, InteractionSubjectKind.PRODUCT
OWNER = "U_TEST_ONLY_OV"
GENERIC = "TEST_ONLY 其他的東西"  # 泛稱：不是任何產品 / 成分名稱
FAMILY = "虛構牌概覽測試飲"


@pytest.fixture(autouse=True)
def family_data(monkeypatch):
    rows = [_product(f"ov-{i}", f"{FAMILY}({x})", "c-1", "虛構測試生技股份有限公司", f"Z0097{i}") for i, x in enumerate(("甲款", "乙款"))]
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + rows)


class Transcriber(FakeLLM):
    def __init__(self, transcript=None, **kw):
        super().__init__(**kw)
        self.transcript = transcript

    def transcribe_audio(self, *, audio, filename, prompt=None):
        self.calls.append(("transcribe_audio", filename))
        return self.transcript


class Chat:
    """AdaptiveResponseService ＋ fake：依文字內容回傳預先指定的 Controller 結果（TEST_ONLY 替身）。"""

    def __init__(self, scripted, repo=None, transcript=None):
        self.llm = Transcriber(transcript, structured={IntakeExtraction: lambda text: self._controller(text)})
        self.scripted = scripted  # 內容中的關鍵字 → IntakeExtraction
        self.repo = repo if repo is not None else InMemoryInteractionRepo()
        verifier = VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), self.llm,
                                     enable_official_web=True, interaction_repo=self.repo)
        self.pipeline = AnalysisPipeline(intake=IntakeAgent(self.llm), verifier=verifier, risk_engine=RiskEngine(),
                                         reporter=ReporterAgent(self.llm))
        self.contexts, self.store = TaskContextStore(), InMemoryRequestStore()
        self.svc = AdaptiveResponseService(self.pipeline, ReporterAgent(self.llm), self.store, timeout_seconds=10.0,
                                           context_store=self.contexts)

    def _controller(self, input_text):
        content = json.loads(input_text)["content"]
        for key, ex in self.scripted.items():
            if key in content:
                return ex.model_copy(deep=True)
        raise AssertionError(f"no scripted controller result for: {content}")

    @property
    def controller_calls(self):
        return sum(1 for c in self.llm.calls if c[0] == "parse_structured")

    def say(self, text, owner=OWNER):
        rid = str(uuid.uuid4())
        flex = self.svc.respond(RawUserInput(input_type=InputType.TEXT, text=text), owner, request_id=rid)
        return flex, self.store.get(rid)

    def speak(self, owner=OWNER):
        rid = str(uuid.uuid4())
        raw = RawUserInput(input_type=InputType.AUDIO, media=[MediaInput(kind="audio", content=b"aud", mime_type="audio/m4a")])
        flex = self.svc.respond(raw, owner, request_id=rid)
        return flex, self.store.get(rid)

    def press(self, record, action, candidate_id=None, owner=OWNER):
        data = (f"action={action.value}&request_id={record.request_id}" + (f"&candidate_id={candidate_id}" if candidate_id else ""))
        return self.svc.handle_action(data, owner)


def verify_product(chat, name=HF_ZINC):
    return chat.say(f"TEST_VERIFY {name}")


VERIFY = {"TEST_VERIFY": extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[HF_ZINC])}
OVERVIEW = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE,
                      interaction_partner_unspecified=True, refers_to_unspecified_product=True)


def overview_chat(**kw):
    chat = Chat({**VERIFY, "TEST_OVERVIEW": OVERVIEW}, **kw)
    _, rec = verify_product(chat)
    assert rec.evidence.product_match.status.value == "VERIFIED"
    return chat


def research(rec):
    return rec.evidence.interaction_research


# ============================================================ 1. 目前產品 → 泛指交互作用總覽


def test_confirmed_product_general_interaction_question_uses_current_product_overview():
    chat = overview_chat()
    flex, rec = chat.say("TEST_OVERVIEW 它跟其他的東西有沒有交互作用")
    req, r = rec.normalized_request, research(rec)
    assert req.interaction_overview and r.mode == InteractionMode.CURRENT_PRODUCT_OVERVIEW and r.status == InteractionResearchStatus.COMPLETE
    [subject] = req.interaction_subjects  # 唯一的對象 = 已確認的目前產品（由 task context 綁定）
    assert subject.kind == PRODUCT and subject.pinned_product_name == HF_ZINC and subject.pinned_permit_number
    assert r.subjects[0].status == SubjectResolutionStatus.RESOLVED and r.subjects[0].product_confirmed
    assert rec.response.response_type == ResponseType.INTERACTION_RESULT


def test_generic_words_are_not_resolved_as_a_product_name_and_no_unrelated_candidate_card_appears():
    chat = overview_chat()
    # Controller 把代名詞唯一列成 product 對象（並標示指稱未指明）：仍是總覽，不查任何產品名稱
    lone = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE, interaction_partner_unspecified=True,
                      refers_to_unspecified_product=True,
                      interaction_subjects=[InteractionSubjectExtraction(text="TEST_ONLY 這個", kind=PRODUCT)])
    chat.scripted["TEST_LONE"] = lone
    for text in ("TEST_OVERVIEW 它跟其他的東西有沒有交互作用", "TEST_LONE TEST_ONLY 這個 跟別的有沒有交互作用"):
        flex, rec = chat.say(text)
        assert rec.response.product_choices == [] and rec.response.response_type == ResponseType.INTERACTION_RESULT
        queried = " ".join(rec.evidence.product_match.queried_terms)
        assert GENERIC not in queried and "這個" not in queried and "其他" not in queried
        assert [s.text for s in rec.normalized_request.interaction_subjects] == [HF_ZINC]
        assert "選擇：" not in texts_of(flex)


def test_known_partners_are_listed_with_evidence_status_and_no_definite_conclusion():
    chat = overview_chat()
    flex, rec = chat.say("TEST_OVERVIEW 它跟其他的東西有沒有交互作用")
    p = rec.response
    assert p.title.startswith("「") and "所含成分有這些已知的交互作用對象" in p.title
    assert 1 <= len(p.key_points) <= 3 and all("「" in k and "：" in k for k in p.key_points)
    text = "\n".join(p.key_points)
    assert "「鋅」與「銅」" in text and "「鋅」與「Testdrug-A」" in text  # 產品所含成分（經 Bridge 對應）→ corpus 已知的對象
    assert any(w in text for w in ("有可用的證據", "證據不一致", "資料庫尚待審核", "只有背景", "不能直接套用"))
    assert "不代表其他組合沒有交互作用" in p.summary and "可以直接輸入名稱" in p.summary
    for banned in ("可以一起吃", "不能一起吃", "沒有交互作用。", "過量", "有毒"):
        assert banned not in p.title + text
    assert all(f.applicability.evaluated is False for f in research(rec).findings)  # 適用條件一律尚未評估


def test_no_structured_record_gives_the_safe_coverage_wording():
    chat = overview_chat(repo=InMemoryInteractionRepo(interactions=[]))
    flex, rec = chat.say("TEST_OVERVIEW 它跟其他的東西有沒有交互作用")
    p = rec.response
    assert p.title == "目前結構化交互作用資料中沒有找到可直接列出的紀錄"
    assert "這不代表一定沒有交互作用" in p.summary and "若想比較特定產品或藥物，可以直接輸入名稱" in p.summary
    assert rec.response.product_choices == [] and research(rec).findings == []
    assert "search_rag" not in [t.tool_name for t in rec.evidence.tools_called] and not chat.llm.called("web_search_structured")


def test_later_explicit_second_target_returns_to_pairwise_mode():
    chat = overview_chat()
    chat.say("TEST_OVERVIEW 它跟其他的東西有沒有交互作用")
    chat.scripted["TEST_PAIR"] = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE, interaction_subjects=[
        InteractionSubjectExtraction(text="鋅", kind=ING), InteractionSubjectExtraction(text="銅", kind=ING)])
    _, rec = chat.say("TEST_PAIR 鋅 跟 銅 可以一起吃嗎")
    r = research(rec)
    assert r.mode == InteractionMode.PAIRWISE and not rec.normalized_request.interaction_overview
    assert any(f.interaction_id == "TI_ZN_CU" for f in r.findings) and r.pairs


def test_product_to_product_edges_are_never_created_in_overview():
    chat = overview_chat()
    _, rec = chat.say("TEST_OVERVIEW 它跟其他的東西有沒有交互作用")
    r = research(rec)
    tools = [t.tool_name for t in rec.evidence.tools_called]
    assert "lookup_duplicate_ingredients" not in tools and r.duplicates == [] and r.pairs == []
    for f in r.findings:  # 參與者只有成分 / 營養素 / 藥品 entity
        assert {p.entity.kind for p in f.participants} <= {"NutrientGroup", "IngredientForm", "DrugSubstance"}
        assert len(f.subject_matches) == 1  # 只有「目前產品」這一邊；沒有第二個產品的 side


def test_overview_without_a_confirmed_product_asks_which_product_instead_of_guessing():
    chat = Chat({"TEST_OVERVIEW": OVERVIEW})
    flex, rec = chat.say("TEST_OVERVIEW 它跟其他的東西有沒有交互作用")
    assert research(rec).clarification_reason == "OVERVIEW_NEEDS_CURRENT_PRODUCT" and rec.response.product_choices == []
    assert "請先告訴我是哪一個產品" in rec.response.plain_text()


def test_current_product_survives_the_button_to_free_text_transition():
    chat = Chat({"TEST_PICK": extraction(intent=Intent.INGREDIENT_INQUIRY, product_candidates=[FAMILY]), "TEST_OVERVIEW": OVERVIEW})
    flex, rec = chat.say("TEST_PICK " + FAMILY)
    pick = next(c for c in rec.response.product_choices)
    chat.svc.handle_action(f"action=select_product&request_id={rec.request_id}&candidate_id={pick.product_id}", OWNER)
    ctx = chat.contexts.snapshot(OWNER)
    assert ctx.confirmed_product and ctx.confirmed_product.confirmation_source == ProductConfirmationSource.USER_SELECTED
    # 之後的一般文字繼續使用 current_product（按鈕流程不是死路）
    _, rec2 = chat.say("TEST_OVERVIEW 它跟其他的東西有沒有交互作用")
    [subject] = rec2.normalized_request.interaction_subjects
    assert subject.pinned_product_name == ctx.confirmed_product.official_product_name
    assert rec2.response.product_choices == []  # 沒有無關的候選卡
    chat.svc.handle_action(build_postback_data(ResponseAction.SOURCES, rec.request_id), OWNER)  # 點 detail 按鈕也不影響
    assert chat.contexts.snapshot(OWNER).confirmed_product.product_id == ctx.confirmed_product.product_id


# ============================================================ 2. 語音候選按鈕

HEARD = "新"  # ASR 文字（fixture）；候選由結構化資料產生
SPOKEN_Q = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE, interaction_subjects=[
    InteractionSubjectExtraction(text=HEARD, kind=ING), InteractionSubjectExtraction(text="銅", kind=ING)])


def asr_chat(**kw):
    return Chat({HEARD: SPOKEN_Q}, transcript=f"{HEARD}和銅可以一起吃嗎", **kw)


def buttons(flex):
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "button":
                out.append(n["action"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(flex)
    return out


def test_single_phonetic_candidate_renders_confirm_and_retry_buttons_from_structured_data():
    chat = asr_chat()
    flex, rec = chat.speak()
    sub = next(s for s in research(rec).subjects if s.text == HEARD)
    label = sub.candidates[0].display_name_zh or sub.candidates[0].canonical_name_en
    assert [c.label for c in rec.response.entity_choices] == [label]
    text = texts_of(flex)
    assert f"我聽到『{HEARD}』，你是要問『{label}』嗎？" in text
    labels = [b["label"] for b in buttons(flex["contents"])]
    assert labels == [f"確認：{label}", "不是，重新輸入"]
    confirm = parse_postback_data(next(b["data"] for b in buttons(flex["contents"]) if b["label"].startswith("確認")))
    assert confirm.action == ResponseAction.SELECT_ASR_ENTITY and confirm.request_id == rec.request_id
    assert confirm.candidate_id == sub.candidates[0].entity_id  # postback 帶 canonical entity ID，不是按鈕文字


def test_candidate_label_is_generated_dynamically_not_hard_coded(monkeypatch):
    other = next(e for e in InteractionService(InMemoryInteractionRepo(), InMemoryOfficialRepo().get_ingredient_bridge_reference())._ref.entities.values()
                 if e.get("display_name_zh") == "銅")
    monkeypatch.setattr(InteractionService, "phonetic_entity_candidates", lambda self, text: [
        PhoneticCandidate(entity_id=other["entity_id"], term="銅", confidence=0.75)])
    chat = asr_chat()
    flex, rec = chat.speak()
    assert [b["label"] for b in buttons(flex["contents"])][0] == "確認：銅"  # 由結構化候選決定
    assert "你是要問『銅』嗎" in texts_of(flex)


def test_confirming_resumes_the_original_interaction_without_rerunning_the_controller():
    chat = asr_chat()
    flex, rec = chat.speak()
    controller_before = chat.controller_calls
    sub = next(s for s in research(rec).subjects if s.text == HEARD)
    out = chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, sub.candidates[0].entity_id)
    after = list(chat.store._data.values())[-1]
    assert chat.controller_calls == controller_before  # 沒有重跑 Controller
    assert after.request_id != rec.request_id and after.normalized_request.intent == Intent.INTERACTION_INQUIRY
    pinned = next(s for s in after.normalized_request.interaction_subjects if s.text == HEARD)
    assert pinned.pinned_entity_id == sub.candidates[0].entity_id  # 固定在「那一個」無法辨識的對象上
    r = research(after)
    assert r.status == InteractionResearchStatus.COMPLETE and any(f.interaction_id == "TI_ZN_CU" for f in r.findings)
    assert next(s for s in r.subjects if s.text == HEARD).status == SubjectResolutionStatus.RESOLVED
    assert after.response.response_type == ResponseType.INTERACTION_RESULT and out["type"] == "flex"
    assert "確認：" not in texts_of(out)  # 結果卡，不是又一張確認卡


def test_multiple_candidates_render_selection_buttons_plus_retry(monkeypatch):
    ents = {e["display_name_zh"]: e["entity_id"] for e in InteractionService(
        InMemoryInteractionRepo(), InMemoryOfficialRepo().get_ingredient_bridge_reference())._ref.entities.values() if e.get("display_name_zh")}
    monkeypatch.setattr(InteractionService, "phonetic_entity_candidates", lambda self, text: [
        PhoneticCandidate(entity_id=ents["鋅"], term="鋅", confidence=0.75), PhoneticCandidate(entity_id=ents["銅"], term="銅", confidence=0.75)])
    chat = asr_chat()
    flex, rec = chat.speak()
    assert [b["label"] for b in buttons(flex["contents"])] == ["選擇：鋅", "選擇：銅", "不是，重新輸入"]
    assert "你是要問下面哪一個" in texts_of(flex)
    out = chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, ents["銅"])
    pinned = next(s for s in list(chat.store._data.values())[-1].normalized_request.interaction_subjects if s.text == HEARD)
    assert pinned.pinned_entity_id == ents["銅"]


def test_retry_cancels_the_pending_recovery_and_the_card_cannot_confirm_afterwards():
    chat = asr_chat()
    flex, rec = chat.speak()
    sub = next(s for s in research(rec).subjects if s.text == HEARD)
    out = chat.press(rec, ResponseAction.REJECT_ASR_ENTITY)
    assert ASR_REJECTED_TEXT in texts_of(out)
    n = len(chat.store._data)
    late = chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, sub.candidates[0].entity_id)
    assert ASR_EXPIRED_TEXT in texts_of(late) and len(chat.store._data) == n  # 沒有新的查詢


def test_other_users_expired_cards_wrong_candidates_and_repeats_cannot_confirm():
    chat = asr_chat()
    flex, rec = chat.speak()
    sub = next(s for s in research(rec).subjects if s.text == HEARD)
    cid = sub.candidates[0].entity_id
    n = len(chat.store._data)
    assert EXPIRED_ACTION_TEXT in texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid, owner="U_OTHER"))
    assert ASR_INVALID_TEXT in texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, "NOT_OFFERED_ID"))  # 不是那一題提供的候選
    assert len(chat.store._data) == n
    rec.created_at = datetime.now(timezone.utc) - timedelta(minutes=11)  # 過期
    assert ASR_EXPIRED_TEXT in texts_of(chat.press(rec, ResponseAction.SELECT_ASR_ENTITY, cid))
    fresh = asr_chat()
    _, rec2 = fresh.speak()
    cid2 = next(s for s in research(rec2).subjects if s.text == HEARD).candidates[0].entity_id
    fresh.press(rec2, ResponseAction.SELECT_ASR_ENTITY, cid2)
    m = len(fresh.store._data)
    assert ASR_EXPIRED_TEXT in texts_of(fresh.press(rec2, ResponseAction.SELECT_ASR_ENTITY, cid2))  # 同一張卡只能處理一次
    assert len(fresh.store._data) == m


def test_text_input_never_gets_asr_buttons():
    chat = Chat({HEARD: SPOKEN_Q})
    flex, rec = chat.say(f"{HEARD}和銅可以一起吃嗎")
    assert rec.response.entity_choices == [] and "請確認寫法" in rec.response.plain_text()


def test_no_nutrient_or_product_names_are_hard_coded_in_the_new_logic():
    root = Path(__file__).resolve().parents[2] / "app"
    for rel in ("services/asr_vocabulary.py", "agents/interaction_research.py", "response/service.py", "response/line_renderer.py",
                "services/context_binding.py"):
        source = (root / rel).read_text(encoding="utf-8")
        for name in ("鋅", "銅", "紅麴", "星", "鈣", "鎂", "娘家"):
            assert name not in source, (rel, name)
