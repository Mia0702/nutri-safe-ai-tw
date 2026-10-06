"""語音輸入的 Interaction 領域詞彙：ASR vocabulary hint ＋ 同音錯字的候選建議。

* 詞彙提示動態由「已審核的 Interaction Corpus」建立（不是寫死的字詞清單，也不含產品名稱），有字數上限。
* ASR 仍聽錯 → 以讀音找語料 identity 當「候選建議」：唯一候選也要使用者確認，多個候選要使用者選，不會默默採用；
  只對語音輸入啟用；文字輸入與一般語音行為不變。
全部使用 TEST_ONLY 虛構資料；測試用的字詞只是 fixture，不在 production code 中。
"""

import json
import logging
from pathlib import Path

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import InputType, Intent, ResponseType
from app.schemas.interaction import (
    InteractionMode,
    InteractionResearchStatus,
    InteractionSubjectExtraction,
    InteractionSubjectKind,
    SubjectResolutionStatus,
)
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from app.services.asr_vocabulary import (
    MAX_VOCABULARY_CHARS,
    DomainTerm,
    InteractionVocabularyProvider,
    PhoneticCandidate,
    build_vocabulary_prompt,
    interaction_terms,
    phonetic_candidates,
)
from app.services.interaction_service import InteractionService
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryInteractionRepo, InMemoryOfficialRepo, InMemoryRagStore
from tests.unit.test_interaction_runtime import ingredient_data  # noqa: F401（autouse fixture：虛構產品 / Bridge mapping）

ING = InteractionSubjectKind.INGREDIENT


class PromptRecordingLLM(FakeLLM):
    def __init__(self, transcript, **kw):
        super().__init__(**kw)
        self.transcript = transcript

    def transcribe_audio(self, *, audio, filename, prompt=None):
        self.calls.append(("transcribe_audio", filename))
        self.prompts = getattr(self, "prompts", []) + [prompt]
        return self.transcript


def references():
    return InMemoryOfficialRepo().get_ingredient_bridge_reference(), InMemoryInteractionRepo().get_drug_reference()


# ------------------------------------------------------------ 詞彙：動態、受限、只有已審核的 corpus 名稱


def test_vocabulary_is_built_dynamically_from_the_reviewed_corpus_not_hard_coded():
    bridge, drugs = references()
    base = build_vocabulary_prompt(interaction_terms(bridge, drugs))
    assert base and "鋅" in base and "銅" in base and "Testdrug-A" in base  # fixture corpus 的名稱
    extended = {**bridge, "entities": bridge["entities"] + [{
        "entity_id": "TN_NEW", "graph_label": "NutrientGroup", "canonical_name_en": "Test New Nutrient", "display_name_zh": "測試新營養素",
        "identity_level": "NUTRIENT_GROUP"}]}
    assert "測試新營養素" in build_vocabulary_prompt(interaction_terms(extended, drugs))  # corpus 多一個，詞彙就多一個
    assert "測試新營養素" not in base
    source = (Path(__file__).resolve().parents[2] / "app/services/asr_vocabulary.py").read_text(encoding="utf-8")
    assert "鋅" not in source and "星" not in source  # production code 沒有任何個別字詞


def test_vocabulary_is_bounded_excludes_unreviewed_aliases_and_has_no_product_names():
    bridge, drugs = references()
    many = [{"entity_id": f"TN_{i}", "graph_label": "NutrientGroup", "canonical_name_en": f"Nutrient {i}",
             "display_name_zh": f"測試營養素{i:03d}", "identity_level": "NUTRIENT_GROUP"} for i in range(500)]
    prompt = build_vocabulary_prompt(interaction_terms({**bridge, "entities": bridge["entities"] + many}, drugs))
    assert len(prompt) <= MAX_VOCABULARY_CHARS + 1 and prompt.startswith("可能出現的成分與藥品名稱：")
    candidate_only = {**bridge, "aliases": bridge.get("aliases", []) + [{
        "alias_id": "A", "entity_id": bridge["entities"][0]["entity_id"], "alias_text": "TEST_ONLY 未審核別名",
        "alias_origin": "REVIEWED_OVERLAY", "alias_status": "NEEDS_REVIEW", "match_policy": "CANDIDATE_ONLY_NOT_AUTOMERGE"}]}
    assert "未審核別名" not in (build_vocabulary_prompt(interaction_terms(candidate_only, drugs)) or "")
    for product in ("虛構牌晶亮測試膠囊", "虛構牌順暢測試優酪乳"):  # 產品名稱不在詞彙中
        assert product not in prompt


def test_provider_caches_and_never_breaks_transcription_when_corpus_is_unavailable():
    calls = []
    provider = InteractionVocabularyProvider(lambda: calls.append(1) or references()[0], lambda: references()[1], ttl_seconds=100, clock=lambda: 0.0)
    assert provider() == provider() and len(calls) == 1
    broken = InteractionVocabularyProvider(lambda: (_ for _ in ()).throw(RuntimeError("down")), lambda: {})
    assert broken() is None


def audio_raw():
    return RawUserInput(input_type=InputType.AUDIO, media=[MediaInput(kind="audio", content=b"aud", mime_type="audio/m4a")])


def test_transcription_receives_the_vocabulary_prompt_only_when_a_provider_exists():
    llm = PromptRecordingLLM("鋅和銅可以一起吃嗎", structured={IntakeExtraction: extraction(intent=Intent.GENERAL_QUESTION)})
    IntakeAgent(llm, vocabulary_provider=lambda: "可能出現的成分與藥品名稱：A、B").understand("r1", audio_raw())
    assert llm.prompts == ["可能出現的成分與藥品名稱：A、B"]
    plain = PromptRecordingLLM("你好", structured={IntakeExtraction: extraction(intent=Intent.GENERAL_QUESTION)})
    IntakeAgent(plain).understand("r2", audio_raw())
    assert plain.prompts == [None]
    failing = PromptRecordingLLM("你好", structured={IntakeExtraction: extraction(intent=Intent.GENERAL_QUESTION)})
    IntakeAgent(failing, vocabulary_provider=lambda: (_ for _ in ()).throw(RuntimeError("x"))).understand("r3", audio_raw())
    assert failing.prompts == [None]  # 詞彙提示失敗不影響轉錄


# ------------------------------------------------------------ 拼音候選（純函式）


TERMS = [DomainTerm("鋅", "E_ZN", "ingredient"), DomainTerm("銅", "E_CU", "ingredient"), DomainTerm("Zinc", "E_ZN", "ingredient")]


def test_phonetic_candidates_by_pinyin_with_confidence_levels():
    same = phonetic_candidates("新", TERMS)  # toneless pinyin 相同
    assert [(c.entity_id, c.confidence) for c in same] == [("E_ZN", 0.9)]
    fuzzy = phonetic_candidates("星", TERMS)  # 只差 -ing / -in 口音混淆
    assert [(c.entity_id, c.confidence) for c in fuzzy] == [("E_ZN", 0.75)]
    assert phonetic_candidates("貓", TERMS) == [] and phonetic_candidates("Zinc", TERMS) == []  # 非讀音相近 / 非中文 → 沒有候選
    assert phonetic_candidates("鋅", TERMS) == []  # 本身就是語料名稱：不是錯字


def test_multiple_homophone_identities_are_all_returned_so_the_user_must_choose():
    terms = TERMS + [DomainTerm("心", "E_X", "ingredient")]
    assert {c.entity_id for c in phonetic_candidates("新", terms)} == {"E_ZN", "E_X"}


# ------------------------------------------------------------ pipeline：只對語音、只是候選建議


def run(subjects, *, audio=True, transcript=None):
    ex = extraction(intent=Intent.INTERACTION_INQUIRY, interaction_mode=InteractionMode.PAIRWISE, interaction_subjects=[
        InteractionSubjectExtraction(text=t, kind=ING) for t in subjects])
    spoken = transcript or ("、".join(subjects) + "可以一起吃嗎")
    llm = PromptRecordingLLM(spoken, structured={IntakeExtraction: ex}) if audio else FakeLLM(structured={IntakeExtraction: ex})
    verifier = VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm, enable_official_web=True,
                                 interaction_repo=InMemoryInteractionRepo())
    pipeline = AnalysisPipeline(intake=IntakeAgent(llm), verifier=verifier, risk_engine=RiskEngine(), reporter=ReporterAgent(llm))
    raw = audio_raw() if audio else RawUserInput(input_type=InputType.TEXT, text=spoken)
    return pipeline.run(raw), llm


def research(result):
    return result.evidence.interaction_research


def test_misheard_known_entity_is_proposed_for_confirmation_never_silently_mapped(caplog):
    with caplog.at_level(logging.INFO):
        result, llm = run(["新", "銅"])  # ASR 把「鋅」聽成同音字
    r = research(result)
    sub = next(s for s in r.subjects if s.text == "新")
    assert sub.status == SubjectResolutionStatus.ENTITY_CANDIDATE_NEEDS_CONFIRMATION and sub.recovery_confidence == 0.9
    assert [c.canonical_name_en for c in sub.candidates] and not sub.entities  # 候選不是已確認 identity
    assert r.status == InteractionResearchStatus.NEEDS_CLARIFICATION and r.findings == []  # 沒有任何 interaction 研究 / 結論
    text = result.response.plain_text()
    assert "我聽到『新』，你是要問" in text and "鋅" in text and "嗎" in text and result.response.response_type == ResponseType.MISSING_INFORMATION
    assert [c.label for c in result.response.entity_choices] == ["鋅"]  # 候選確認按鈕（由結構化候選產生）
    assert not llm.called("embed") and not llm.called("web_search_structured")  # 不查官網猜 identity
    rec = next(r for r in caplog.records if getattr(r, "asr_entity_recovery_used", False))
    assert rec.recovery_candidate_count == 1 and rec.recovery_confidence == 0.9 and rec.recovery_proposed_entity


def test_the_same_misheard_word_typed_as_text_is_unchanged():
    result, _ = run(["新", "銅"], audio=False)
    sub = next(s for s in research(result).subjects if s.text == "新")
    assert sub.status == SubjectResolutionStatus.UNRESOLVED and not sub.candidates  # 文字輸入沒有這層（行為不變）
    assert "請確認寫法" in result.response.plain_text()


def test_ambiguous_phonetic_candidates_ask_the_user_to_choose(monkeypatch):
    monkeypatch.setattr(InteractionService, "phonetic_entity_candidates", lambda self, text: [
        PhoneticCandidate(entity_id=e["entity_id"], term=e["display_name_zh"], confidence=0.75)
        for e in self._ref.entities.values() if e.get("display_name_zh") in ("鋅", "銅")])
    result, _ = run(["新", "鈣"])
    sub = next(s for s in research(result).subjects if s.text == "新")
    assert sub.status == SubjectResolutionStatus.ENTITY_CANDIDATE_NEEDS_CONFIRMATION and len(sub.candidates) == 2 and not sub.entities
    text = result.response.plain_text()
    assert "你是要問下面哪一個" in text and {c.label for c in result.response.entity_choices} == {"鋅", "銅"}
    assert research(result).findings == []


def test_correctly_recognized_audio_and_unrelated_unknown_words_are_unchanged():
    ok, _ = run(["鋅", "銅"])
    assert research(ok).corpus_sufficient and all(s.status == SubjectResolutionStatus.RESOLVED for s in research(ok).subjects)
    unknown, _ = run(["鋅", "貓咪"])  # 沒有任何讀音相近的語料名稱 → 不產生候選，維持原本的確認寫法
    sub = next(s for s in research(unknown).subjects if s.text == "貓咪")
    assert sub.status == SubjectResolutionStatus.UNRESOLVED and not sub.candidates


def test_non_interaction_audio_does_not_use_recovery_or_the_corpus_at_all():
    ex = extraction(intent=Intent.GENERAL_QUESTION)
    llm = PromptRecordingLLM("你好嗎", structured={IntakeExtraction: ex})
    result = AnalysisPipeline(intake=IntakeAgent(llm), verifier=VerificationAgent(InMemoryOfficialRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
                              risk_engine=RiskEngine(), reporter=ReporterAgent(llm)).run(audio_raw())
    assert result.evidence.interaction_research is None
