"""Interaction Runtime Phase 3：Reporter Agent ＋ LINE UI。

全部 TEST_ONLY 虛構產品 / entity（TN_ / TF_ / TD_ / TI_），不呼叫外部服務、不寫任何 DB。
* primary card deterministic、最多 3 個重點、每個重點 ≤ 80 字（Renderer 不截斷關鍵限制）。
* 只有 CONCLUSION_ELIGIBLE 可說「有可用的交互作用證據」，且一律說明適用條件尚未評估。
* NO_CORPUS_RECORD ≠ 沒有交互作用；重複成分 ≠ 過量；證據方向不誤導；內部 ID / enum 不外洩。
* 完整分析只用 interaction_research 的 structured facts，不重跑 Controller / 不重新查資料。
"""

import json
import re
import uuid

import pytest

from app.agents import interaction_reporter as ir
from app.agents.reporter_agent import ReporterAgent
from app.response.actions import build_postback_data, parse_postback_data
from app.response.line_renderer import MAX_BUTTONS, MAX_KEY_POINTS, MAX_POINT_CHARS, AdaptiveLineRenderer
from app.response.service import EXPIRED_ACTION_TEXT, UNAVAILABLE_ACTION_TEXT, AdaptiveResponseService
from app.response.store import InMemoryRequestStore
from app.schemas.enums import InputType, Intent, ProductCategory, ResponseAction, ResponseType, SemanticTone
from app.schemas.interaction import (
    InteractionConclusionStatus,
    InteractionMode,
    InteractionQuestion,
    InteractionSubjectExtraction,
    InteractionSubjectKind,
)
from app.schemas.user_request import IntakeExtraction, RawUserInput
from app.services.context_store import TaskContextStore
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryInteractionRepo
from tests.fixtures import interaction_data as idata
from tests.fixtures import synthetic_data as data
from tests.unit.test_interaction_runtime import (  # noqa: F401（autouse fixture：虛構產品 / Bridge mapping）
    TYPO_ZINC,
    build,
    research_of,
)
from tests.unit.test_ingredient_retrieval_runtime import (  # noqa: F401
    DOM_NAME,
    EPA_NAME,
    FOLATE_NAME,
    HF_ZINC,
    IMP_NAME,
    _form_mapping,
    _registration,
    ingredient_data,
)

ING, DRUG, PRODUCT = InteractionSubjectKind.INGREDIENT, InteractionSubjectKind.DRUG, InteractionSubjectKind.PRODUCT
R = AdaptiveLineRenderer()
OWNER, OTHER_OWNER = "U_TEST_ONLY_A", "U_TEST_ONLY_B"
MIX_NAME = "虛構牌雙元素測試錠"   # TEST_ONLY：同時含葡萄糖酸鋅與葡萄糖酸銅（用來做 pairwise ＋ 重複成分同時存在）

FORBIDDEN_PRIMARY = ("不能一起吃", "不可以一起吃", "過量", "超過上限", "有毒", "一定有風險", "沒有交互作用", "可以一起吃")
INTERNAL_RE = re.compile(r"\b(?:T?[NFDI])_[A-Z0-9_]+\b|TDUP_|TREL|CONCLUSION_ELIGIBLE|EVIDENCE_CONFLICT|NO_CORPUS_RECORD"
                         r"|LINEAGE_NOT_INHERITED|NOT_EVALUATED|NO_SIGNIFICANT_EFFECT|CONTEXT_NOT_PAIR_SPECIFIC|[A-Z]+_[A-Z_]+")


def has_internal(text: str) -> bool:
    """內部 ID / enum 代碼（TEST_ONLY 是 fixture 內容標記，不算）。"""
    return bool(INTERNAL_RE.search(text.replace("TEST_ONLY", "")))


# ------------------------------------------------------------------ helpers


@pytest.fixture
def mix_product(monkeypatch):
    monkeypatch.setattr(data, "PRODUCTS", data.PRODUCTS + [_registration("ing-mix", MIX_NAME, "9990000005",
                                                                          ProductCategory.IMPORTED_TABLET_CAPSULE)])
    data.INGREDIENT_STAGING["ing-mix"] = {"ingredients_raw": "葡萄糖酸鋅,葡萄糖酸銅",
                                          "parser_draft": {"tokens": ["葡萄糖酸鋅", "葡萄糖酸銅"]}}
    monkeypatch.setattr(data, "INGREDIENT_ENTITY_MAPPINGS", data.INGREDIENT_ENTITY_MAPPINGS
                        + _form_mapping("ing-mix", "葡萄糖酸鋅", "TF_ZN_GLUCONATE", "TN_ZN", token=0)
                        + _form_mapping("ing-mix", "葡萄糖酸銅", "TF_CU_GLUCONATE", "TN_CU", token=1))


def inheriting_repo() -> InMemoryInteractionRepo:
    """資料驅動：把 form → group link 的 automatic_interaction_inheritance 設為 true（不是程式 hard-code）。"""
    links = [{**l, "automatic_interaction_inheritance": True} for l in idata.FORM_LINKS]
    links.append({"form_entity_id": "TF_CU_GLUCONATE", "nutrient_entity_id": "TN_CU", "relationship_id": "TREL:TF_CU_GLUCONATE",
                  "automatic_interaction_inheritance": True, "amount_conversion_allowed": False})
    return InMemoryInteractionRepo(links=links)


class Service:
    """AdaptiveResponseService ＋ fake（虛構資料）：send() 走完整 respond()，回傳 (flex, record)。"""

    def __init__(self, subjects, mode=InteractionMode.PAIRWISE, repo=None, llm_text=None, ingredients=(), question=None):
        ex = extraction(intent=Intent.INTERACTION_INQUIRY, ingredient_candidates=list(ingredients), interaction_mode=mode,
                        interaction_question=question,
                        interaction_subjects=[InteractionSubjectExtraction(text=t, kind=k) for t, k in subjects])
        self.llm = FakeLLM(structured={IntakeExtraction: ex}, **({"text": llm_text} if llm_text is not None else {}))
        self.repo = repo if repo is not None else InMemoryInteractionRepo()
        self.pipeline, _ = build(llm=self.llm, repo=self.repo)
        self.store, self.contexts = InMemoryRequestStore(), TaskContextStore()
        self.svc = AdaptiveResponseService(self.pipeline, ReporterAgent(self.llm), self.store, timeout_seconds=10.0,
                                           context_store=self.contexts)
        self.text = " 跟 ".join(t for t, _ in subjects) + " 可以一起吃嗎"

    def send(self, text=None, owner=OWNER):
        rid = str(uuid.uuid4())
        flex = self.svc.respond(RawUserInput(input_type=InputType.TEXT, text=text or self.text), owner, request_id=rid)
        return flex, self.store.get(rid)

    def action(self, record, action, owner=OWNER):
        return self.svc.handle_action(build_postback_data(action, record.request_id), owner)


def pairwise(subjects, **kw):
    return Service(subjects, **kw).send()[1]


def duplicate(subjects, **kw):
    return pairwise(subjects, mode=InteractionMode.DUPLICATE_COMPARISON, **kw)


def texts_of(flex) -> str:
    return json.dumps(flex, ensure_ascii=False)


def primary_text(payload) -> str:
    return "\n".join([payload.title, payload.status_line or "", payload.summary, *payload.key_points, *payload.warnings])


def postbacks(flex):
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "button" and n["action"].get("type") == "postback":
                out.append(n["action"]["data"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(flex)
    return out


def assert_primary_is_safe(payload):
    text = primary_text(payload)
    assert not has_internal(text), text
    assert not any(w in text for w in FORBIDDEN_PRIMARY), text
    assert len(payload.key_points) <= MAX_KEY_POINTS
    assert all(len(p) <= MAX_POINT_CHARS for p in payload.key_points), payload.key_points   # 不會被 Renderer 截斷
    assert payload.risk_level is None and payload.claim_risk_level is None                 # 沒有風險燈號
    assert payload.status_tone in (None, SemanticTone.ORANGE)


TOGETHER, EXISTENCE, EXPLANATION = (InteractionQuestion.TOGETHER_USE, InteractionQuestion.INTERACTION_EXISTENCE,
                                    InteractionQuestion.EXPLANATION)
EVIDENCE_LEAKS = ("TEST_ONLY 來源", "證據方向", "GRADE", "筆支持", "證據：", "有可用的交互作用證據", "資料來源", "證據筆數")
ELIGIBLE_Q = [("鋅", ING), ("銅", ING)]
CONFLICT_Q = [("魚油", ING), ("Testdrug-Anticoag", DRUG)]
NO_RECORD_Q = [("鎂", ING), ("銅", ING)]


def assert_answer_first(p):
    """標題就是直接回答；primary 不含證據筆數 / 來源 / 證據方向 / 技術說明（在按鈕裡）；重點 ≤ 3。"""
    text = primary_text(p)
    assert not any(leak in text for leak in EVIDENCE_LEAKS), text
    assert p.title and p.title.endswith(("。", "如下。")) and len(p.key_points) <= 2
    assert p.available_actions[0] == ResponseAction.WHY and len(p.available_actions) <= MAX_BUTTONS


# ============================================================ 1. eligible + together-use


def test_eligible_together_use_answers_first_without_saying_yes_or_no():
    p = pairwise(ELIGIBLE_Q, question=TOGETHER).response
    assert p.response_type == ResponseType.INTERACTION_RESULT
    assert p.title == "目前不能直接判定是否適合一起使用。"                          # 直接回答（不說可以 / 不可以）
    assert p.summary == "資料顯示：TEST_ONLY 效果"                                 # 一句原因（clinical_effect）
    assert p.key_points == ["是否適用於你，目前無法自動判斷；需要確認實際劑量、用法與使用情況。"]   # applicability 尚未評估
    assert p.status_line == "需要確認實際用量與使用方式" and p.status_tone == SemanticTone.ORANGE
    assert p.is_conclusive is True
    assert_answer_first(p)
    assert_primary_is_safe(p)


def test_eligible_drug_pair_uses_canonical_drug_name_in_detail():
    s = Service([("鋅", ING), ("Testdrug-A", DRUG)], question=EXISTENCE)
    _, rec = s.send()
    assert rec.response.title == "有，資料庫收錄了「鋅」與「Testdrug-A」的交互作用。"


def test_question_default_when_controller_gives_none_is_together_use():
    p = pairwise(ELIGIBLE_Q).response
    assert p.title == "目前不能直接判定是否適合一起使用。"


# ============================================================ 2. conflict


def test_conflict_together_use_makes_no_conclusion():
    p = pairwise(CONFLICT_Q, question=TOGETHER).response
    assert p.title == "目前無法確定是否適合一起使用，因為現有證據不一致。" and p.status_line == "現有證據不一致"
    assert p.is_conclusive is False and "不下肯定結論" in p.summary
    assert "存在已收錄的交互作用" not in primary_text(p)
    assert_answer_first(p)
    assert_primary_is_safe(p)


# ============================================================ 3. lineage not inherited


def test_lineage_not_inherited_explains_but_does_not_conclude():
    p = pairwise([(IMP_NAME, PRODUCT), ("Testdrug-B", DRUG)], question=TOGETHER).response
    assert p.title == "目前不能直接判定是否適合一起使用。"
    assert "含有「魚油」，可追到「Omega-3脂肪酸群」類成分" in p.summary and "不允許自動繼承" in p.summary
    assert p.is_conclusive is False and ir.NOT_PRODUCT_RISK in p.warnings    # 產品風險與 interaction finding 分開
    assert_answer_first(p)
    assert_primary_is_safe(p)


def test_lineage_inheritance_is_data_driven_in_reporter():
    p = pairwise([(IMP_NAME, PRODUCT), ("Testdrug-B", DRUG)], repo=inheriting_repo(), question=EXISTENCE).response
    assert p.title == "有，資料庫收錄了「虛構牌海洋測試軟膠囊」所含的「Omega-3脂肪酸群」與「Testdrug-B」的交互作用。"


# ============================================================ 4. needs review / insufficient


@pytest.mark.parametrize("subjects", [[("鎂", ING), ("Testdrug-A", DRUG)], [("葉酸營養素群", ING), ("銅", ING)]])
def test_needs_review_and_context_only_say_data_insufficient(subjects):
    p = pairwise(subjects, question=TOGETHER).response
    assert p.title == "目前資料不足，無法確定是否適合一起使用。" and p.status_line == "資料不足" and p.is_conclusive is False
    assert_answer_first(p)
    assert_primary_is_safe(p)


# ============================================================ 5. NO_CORPUS_RECORD + together-use


def test_no_record_together_use_is_insufficient_not_no_interaction():
    p = pairwise(NO_RECORD_Q, question=TOGETHER).response
    assert p.title == "目前資料不足，不能判定兩者是否可以一起使用。" and p.status_line == "資料庫未收錄這組關係"
    assert "資料庫沒有收錄這組的紀錄" in p.summary and "不能當作可以放心一起使用的依據" in p.summary
    text = primary_text(p)
    assert "沒有交互作用" not in text and "無交互作用" not in text and "不會有交互作用" not in text
    assert p.available_actions == [ResponseAction.WHY]
    assert_primary_is_safe(p)


# ============================================================ 6. interaction-existence / explanation 問法


def test_interaction_existence_question_answers_existence():
    eligible = pairwise(ELIGIBLE_Q, question=EXISTENCE).response
    assert eligible.title == "有，資料庫收錄了「鋅」與「銅」的交互作用。"
    assert eligible.key_points == ["是否適用於你，目前無法自動判斷；需要確認實際劑量、用法與使用情況。"]
    assert pairwise(CONFLICT_Q, question=EXISTENCE).response.title == "目前無法確定是否有交互作用，因為現有證據不一致。"
    assert pairwise(NO_RECORD_Q, question=EXISTENCE).response.title == "目前資料庫沒有收錄這組關係，無法判定是否有交互作用。"
    assert pairwise([("鎂", ING), ("Testdrug-A", DRUG)], question=EXISTENCE).response.title == "目前資料不足，無法確定是否有交互作用。"


def test_explanation_question_answers_with_the_recorded_explanation():
    p = pairwise(ELIGIBLE_Q, question=EXPLANATION).response
    assert p.title == "資料庫對「鋅」與「銅」的說明如下。" and p.summary == "資料顯示：TEST_ONLY 效果"
    assert pairwise(CONFLICT_Q, question=EXPLANATION).response.title == "目前無法確定是否有交互作用，因為現有證據不一致。"


@pytest.mark.parametrize("question", [TOGETHER, EXISTENCE, EXPLANATION, None])
@pytest.mark.parametrize("subjects", [ELIGIBLE_Q, CONFLICT_Q, NO_RECORD_Q, [("鎂", ING), ("Testdrug-A", DRUG)],
                                      [(IMP_NAME, PRODUCT), ("Testdrug-B", DRUG)]])
def test_applicability_unknown_never_becomes_yes_or_no(subjects, question):
    p = pairwise(subjects, question=question).response
    text = primary_text(p)
    for banned in ("可以一起吃", "不可以一起吃", "不能一起吃", "適合一起吃", "請勿一起", "避免一起", "安全"):
        assert banned not in text, (banned, text)
    # 「適合 / 可以一起使用」只會出現在否定 / 不確定的句子裡
    assert not re.search(r"^(?:是的?，)?(?:可以|適合)", p.title) and "不適合" not in text
    assert_primary_is_safe(p)


# ============================================================ 7. duplicate existence


def test_duplicate_question_answers_yes_with_dose_not_evaluated():
    rec = duplicate([(HF_ZINC, PRODUCT), (DOM_NAME, PRODUCT)])
    p = rec.response
    assert p.title == "有，兩項產品都含有「鋅」。" and p.summary == "目前尚未評估兩項產品的實際攝取量。"
    assert p.status_line == "尚未評估實際攝取量"
    assert any("仍有" in k and "項成分尚未完成辨識" in k for k in p.key_points)      # unresolved ingredients 簡短說明
    d = rec.evidence.interaction_research.duplicates[0]
    assert d.dose_evaluation.value == "NOT_EVALUATED" and d.severity == "DOSE_DEPENDENT_NOT_ASSIGNED"
    assert "交互作用" not in primary_text(p).replace(ir.NOT_PRODUCT_RISK, "")      # 不是交互作用句型
    assert_primary_is_safe(p)


def test_duplicate_question_without_confirmed_duplicate_answers_from_coverage():
    p = duplicate([(IMP_NAME, PRODUCT), (FOLATE_NAME, PRODUCT)]).response
    assert p.title == "以已確認的成分比對，沒有找到重複的成分。"
    assert "尚未完成辨識" in p.summary and "不能確定沒有重複" in p.summary
    assert_primary_is_safe(p)


def test_duplicate_is_not_overdose_in_any_surface():
    s = Service([(IMP_NAME, PRODUCT), (EPA_NAME, PRODUCT)], mode=InteractionMode.DUPLICATE_COMPARISON)
    flex, rec = s.send()
    surfaces = [texts_of(flex)]
    for action in (ResponseAction.WHY, ResponseAction.INTERACTION_EVIDENCE, ResponseAction.INTERACTION_MAPPING):
        assert action in rec.response.available_actions
        surfaces.append(texts_of(s.action(rec, action)))
    for text in surfaces:
        for word in ("過量", "超過上限", "有毒", "不能一起吃", "一定有風險", "沒有交互作用"):
            assert word not in text, (word, text[:200])
    assert any("成分形式不同" in k for k in rec.response.key_points)


# ============================================================ 8. pairwise + duplicate 同一張 primary / primary 結構


def test_pairwise_and_duplicate_are_one_primary_message(mix_product):
    s = Service([(HF_ZINC, PRODUCT), (MIX_NAME, PRODUCT)], repo=inheriting_repo(), question=TOGETHER)
    flex, rec = s.send()
    r = rec.evidence.interaction_research
    assert r.findings and r.duplicates                       # 兩種結果同時存在
    p = rec.response
    assert p.title == "目前不能直接判定是否適合一起使用。"     # 先回答使用者的問題
    assert p.summary.startswith("資料顯示：")                 # 一句原因
    assert len(p.key_points) == 2 and p.key_points[0].startswith("兩項產品都含有「鋅」") and "尚未完成劑量評估" in p.key_points[0]
    assert "需要確認實際劑量、用法與使用情況" in p.key_points[1]   # 限制
    assert flex["type"] == "flex" and flex["contents"]["type"] == "bubble"   # 一則 Flex，不拆成多則
    assert_primary_is_safe(p)


def test_primary_card_is_answer_first_and_evidence_is_behind_actions():
    s = Service(ELIGIBLE_Q, question=TOGETHER)
    flex, rec = s.send()
    text = texts_of(flex)
    for leak in EVIDENCE_LEAKS + ("NIH", "ods.od.nih.gov"):
        assert leak not in text, leak
    # 渲染順序：標題（直接回答）在原因與限制之前
    assert text.index("目前不能直接判定是否適合一起使用") < text.index("資料顯示：") < text.index("需要確認實際劑量")
    evidence = texts_of(s.action(rec, ResponseAction.INTERACTION_EVIDENCE))  # 預設：簡短摘要
    assert "TEST_ONLY 來源 TE_1" in evidence and "證據方向：支持此交互作用紀錄" in evidence
    full = texts_of(s.action(rec, ResponseAction.INTERACTION_EVIDENCE_ALL))  # 完整明細（含限制）
    assert "TEST_ONLY 來源 TE_1" in full and "限制：" in full
    why = texts_of(s.action(rec, ResponseAction.WHY))
    assert "可能影響" in why and "TEST_ONLY 效果" in why and "目前尚未評估，需另外確認適用條件" in why
    assert "證據方向" not in why


def test_why_detail_is_deterministic_and_uses_only_stored_facts(monkeypatch):
    s = Service(CONFLICT_Q, question=TOGETHER)
    _, rec = s.send()
    llm_calls, repo_calls = list(s.llm.calls), list(s.repo.calls)
    monkeypatch.setattr(s.pipeline, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不得重跑")))
    why = texts_of(s.action(rec, ResponseAction.WHY))
    assert s.llm.calls == llm_calls and s.repo.calls == repo_calls            # 沒有 LLM、沒有重新查詢
    assert "證據不一致，不下肯定結論" in why and not has_internal(why)


# ============================================================ 9. evidence direction


def test_evidence_direction_semantics_are_preserved():
    s = Service([("魚油", ING), ("Testdrug-Anticoag", DRUG)])
    _, rec = s.send()
    body = ir.evidence_detail(rec.normalized_request, rec.evidence)
    by_source = {}
    for para in body:
        for ln in para.split("\n"):
            if ln.startswith("・"):
                key = ln[1:]
        direction = next((ln for ln in para.split("\n") if ln.startswith("證據方向：")), None)
        if direction:
            by_source[key] = direction
    assert by_source["TEST_ONLY 來源 TE_4"] == "證據方向：支持此交互作用紀錄"
    assert by_source["TEST_ONLY 來源 TE_5"] == "證據方向：未發現明顯影響（不支持此交互作用紀錄）"    # NO_SIGNIFICANT_EFFECT
    assert by_source["TEST_ONLY 來源 TE_3"] == "證據方向：結果不一致（不能視為支持）"          # MIXED
    assert by_source["TEST_ONLY 來源 TE_6"] == "證據方向：背景資料，不是這一組的直接證據"        # CONTEXT_NOT_PAIR_SPECIFIC
    non_official = next(p for p in body if "TE_5" in p)
    assert "非官方資料" in non_official
    assert "證據 4 筆（支持 1 筆" in body[0]


def test_unknown_direction_value_is_not_presented_as_support():
    from app.schemas.interaction import EvidenceDirectionRole, InteractionEvidenceItem

    item = InteractionEvidenceItem(
        evidence_id="x", source_id="s", source_type="t", is_official=True, supports_direction="BRAND_NEW_VALUE",
        direction_role=EvidenceDirectionRole.UNCLASSIFIED, counts_as_support=False, evidence_use="SUPPORTING",
        evidence_level="l", certainty="c", source_retrieval_status="ok")
    assert ir.direction_label(item) == "方向尚未分類，不當作支持"


def test_duplicate_rule_evidence_is_not_called_harmful_interaction_support():
    s = Service([(HF_ZINC, PRODUCT), (DOM_NAME, PRODUCT)], mode=InteractionMode.DUPLICATE_COMPARISON)
    _, rec = s.send()
    body = "\n".join(ir.evidence_detail(rec.normalized_request, rec.evidence))
    assert "重複成分說明：「鋅」" in body and "支持這項重複成分的說明" in body and "支持此交互作用紀錄" not in body


# ============================================================ 10. full analysis 不重新搜尋


def test_full_analysis_uses_only_stored_facts_and_never_reruns_pipeline(monkeypatch):
    s = Service([("鋅", ING), ("Testdrug-A", DRUG)], llm_text="資料庫收錄了鋅與這個藥品的交互作用，請另外確認適用條件。")
    flex, rec = s.send()
    repo_calls, llm_calls = list(s.repo.calls), list(s.llm.calls)

    def forbidden(*a, **k):
        raise AssertionError("完整分析不得重跑 pipeline / Controller / 查詢")

    monkeypatch.setattr(s.pipeline, "run", forbidden)
    monkeypatch.setattr(s.pipeline, "resume_with_request", forbidden)
    detail = ReporterAgent(s.llm).detail(ResponseAction.DETAIL, rec.normalized_request, rec.evidence, rec.risk)
    assert s.repo.calls == repo_calls                                         # 沒有再查 corpus
    new_calls = s.llm.calls[len(llm_calls):]
    assert [c[0] for c in new_calls] == ["generate_text"]                      # 只有 Reporter LLM（沒有 Controller / planner / web / embed）
    facts = json.loads(new_calls[0][1])
    assert facts["交互作用紀錄"][0]["適用條件是否已對照使用者的劑量、用法、族群"] == "尚未評估"
    assert not has_internal(json.dumps(facts, ensure_ascii=False))       # facts 也不含內部 ID / enum
    text = "\n".join(detail.body)
    assert "資料庫收錄了鋅與這個藥品的交互作用" in text and "目前尚未評估，需另外確認適用條件" in text   # 限制說明由程式附上
    assert "限制與提醒" in text


def test_full_analysis_rejects_unsafe_llm_body_and_falls_back_to_template():
    for unsafe in ("N_CA 與 Testdrug-A 不能一起吃", "這兩者一定會有交互作用，超過上限會中毒", "兩者沒有交互作用，可以放心"):
        s = Service([("魚油", ING), ("Testdrug-Anticoag", DRUG)], llm_text=unsafe)
        _, rec = s.send()
        out = ReporterAgent(s.llm).report(rec.normalized_request, rec.evidence, rec.risk)
        assert out.used_fallback_template and unsafe not in out.text
        assert "證據不一致" in out.text and "限制與提醒" in out.text


def test_full_analysis_conflict_body_cannot_assert_an_interaction():
    s = Service([("魚油", ING), ("Testdrug-Anticoag", DRUG)], llm_text="魚油與這個藥品會產生交互作用。")
    _, rec = s.send()
    assert ReporterAgent(s.llm).report(rec.normalized_request, rec.evidence, rec.risk).used_fallback_template


def test_full_analysis_llm_failure_uses_template():
    s = Service([("鋅", ING), ("Testdrug-A", DRUG)])
    _, rec = s.send()
    s.llm.fail.add("generate_text")
    out = ReporterAgent(s.llm).report(rec.normalized_request, rec.evidence, rec.risk)
    assert out.used_fallback_template and "資料庫記載的影響：TEST_ONLY 效果" in out.text
    assert "資料庫記載的建議：TEST_ONLY 建議" in out.text


# ============================================================ 11. postback request-scoped


def test_interaction_buttons_are_request_scoped_and_labelled():
    s = Service([(HF_ZINC, PRODUCT), ("Testdrug-A", DRUG)], repo=inheriting_repo())
    flex, rec = s.send()
    data_list = postbacks(flex)
    assert 1 <= len(data_list) <= MAX_BUTTONS
    parsed = [parse_postback_data(d) for d in data_list]
    assert all(a is not None and a.request_id == rec.request_id and a.candidate_id is None for a in parsed)
    assert [a.action for a in parsed] == [ResponseAction.WHY, ResponseAction.INTERACTION_EVIDENCE,
                                          ResponseAction.INTERACTION_MAPPING]
    labels = re.findall(r'"label": "([^"]+)"', texts_of(flex))
    assert labels == ["為什麼？", "查看證據", "查看成分對應"]


def test_source_and_mapping_postbacks_are_owner_and_request_scoped():
    s = Service([(HF_ZINC, PRODUCT), ("Testdrug-A", DRUG)], repo=inheriting_repo())
    _, rec = s.send()
    own = s.action(rec, ResponseAction.INTERACTION_EVIDENCE)
    assert "證據摘要" in texts_of(own)
    assert EXPIRED_ACTION_TEXT in texts_of(s.action(rec, ResponseAction.INTERACTION_EVIDENCE, owner=OTHER_OWNER))   # 他人不能取
    unknown = build_postback_data(ResponseAction.INTERACTION_EVIDENCE, str(uuid.uuid4()))
    assert EXPIRED_ACTION_TEXT in texts_of(s.svc.handle_action(unknown, OWNER))
    # 新一題之後，舊卡仍回答舊題
    s.send(text="鋅 跟 銅 可以一起吃嗎")
    again = texts_of(s.action(rec, ResponseAction.INTERACTION_MAPPING))
    assert "虛構牌補給測試錠" in again


def test_evidence_detail_offers_clickable_sources_via_existing_sources_action():
    s = Service([("鋅", ING), ("銅", ING)])
    _, rec = s.send()
    detail = s.action(rec, ResponseAction.INTERACTION_EVIDENCE)
    assert [parse_postback_data(d).action for d in postbacks(detail)] == [ResponseAction.SOURCES]
    sources = s.action(rec, ResponseAction.SOURCES)
    assert "TEST_ONLY 來源 TE_1" in texts_of(sources) and "開啟官方來源" in texts_of(sources)


def test_unoffered_actions_are_rejected():
    s = Service([("鋅", ING), ("銅", ING)])     # 成分 ↔ 成分：沒有產品 → 不提供「查看成分對應」
    _, rec = s.send()
    assert ResponseAction.INTERACTION_MAPPING not in rec.response.available_actions
    assert UNAVAILABLE_ACTION_TEXT in texts_of(s.action(rec, ResponseAction.INTERACTION_MAPPING))
    assert ResponseAction.DETAIL not in rec.response.available_actions          # 技術性完整分析不在 primary
    assert UNAVAILABLE_ACTION_TEXT in texts_of(s.action(rec, ResponseAction.DETAIL))


def test_mapping_detail_shows_confirmed_forms_and_groups_without_ids():
    s = Service([(IMP_NAME, PRODUCT), ("Testdrug-B", DRUG)])
    _, rec = s.send()
    body = "\n".join(ir.mapping_detail(rec.normalized_request, rec.evidence))
    assert "「虛構牌海洋測試軟膠囊」的成分對應" in body
    assert "官方成分「FISH OIL」 → 「魚油」（成分形式），屬於「Omega-3脂肪酸群」類" in body
    assert "官方成分「葡萄糖酸銅」 → 「葡萄糖酸銅」（成分形式），屬於「銅」類" in body
    assert "另有 1 項官方成分尚未完成辨識，未納入比對。" in body
    assert not has_internal(body) and "Testdrug-B" in body


# ============================================================ 12. candidate 未確認 → 不出 interaction card


def test_unconfirmed_candidate_shows_candidate_card_not_interaction_conclusion():
    s = Service([(TYPO_ZINC, PRODUCT), ("Testdrug-A", DRUG)])
    flex, rec = s.send(text=f"{TYPO_ZINC} 跟 Testdrug-A 可以一起吃嗎")
    p = rec.response
    assert p.response_type == ResponseType.MISSING_INFORMATION and p.product_choices
    assert p.title == "需要更多資訊" and p.available_actions == [] and not p.status_line
    text = texts_of(flex)
    for banned in ("目前不能直接判定", "現有證據不一致", "存在已收錄的交互作用", "資料庫沒有收錄這組關係", "有，資料庫收錄"):
        assert banned not in text
    actions = [parse_postback_data(d) for d in postbacks(flex)]
    assert actions and all(a.action == ResponseAction.SELECT_PRODUCT and a.request_id == rec.request_id for a in actions)


def test_selecting_candidate_then_shows_interaction_card_without_controller_rerun():
    s = Service([(TYPO_ZINC, PRODUCT), ("Testdrug-A", DRUG)])
    _, rec = s.send(text=f"{TYPO_ZINC} 跟 Testdrug-A 可以一起吃嗎")
    controller = lambda: [c for c in s.llm.calls if c == ("parse_structured", "IntakeExtraction")]  # noqa: E731
    assert len(controller()) == 1
    choice = rec.response.product_choices[0]
    flex = s.svc.handle_action(f"action=select_product&request_id={rec.request_id}&candidate_id={choice.product_id}", OWNER)
    assert "目前不能直接判定是否適合一起使用" in texts_of(flex) and "不允許自動繼承" in texts_of(flex) and len(controller()) == 1


def test_replying_yes_to_single_candidate_continues_the_interaction_question():
    s = Service([(TYPO_ZINC, PRODUCT), ("Testdrug-A", DRUG)])
    _, rec = s.send(text=f"{TYPO_ZINC} 跟 Testdrug-A 可以一起吃嗎")
    assert s.contexts.snapshot(OWNER).pending_clarification is not None
    flex, rec2 = s.send(text="是")
    assert rec2.response.response_type == ResponseType.INTERACTION_RESULT
    assert rec2.normalized_request.interaction_subjects[0].pinned_permit_number.endswith("Z00900號")
    assert "目前不能直接判定是否適合一起使用" in texts_of(flex)


# ============================================================ 13. drug unmapped → clarification


def test_drug_without_active_substance_mapping_asks_for_active_ingredient():
    p = pairwise([("鋅", ING), ("某某牌藥品商品名", DRUG)]).response
    assert p.response_type == ResponseType.MISSING_INFORMATION and p.title == "需要更多資訊"
    assert "目前無法對應到藥品的有效成分" in p.summary
    text = primary_text(p) + "".join(p.key_points)
    assert "請補充藥品的有效成分" in text and "還不能從商品名判斷" in text
    assert "存在已收錄的交互作用" not in text and "資料庫沒有收錄這組關係" not in text
    assert p.available_actions == [] and not p.status_line


@pytest.mark.parametrize("subjects,expected", [
    ([("鋅", ING)], "至少需要兩個對象"),
    ([("完全不存在的虛構測試錠", PRODUCT), ("鋅", ING)], "目前找不到「完全不存在的虛構測試錠」對應的官方產品紀錄"),
    ([("鋅", ING), ("完全不存在的虛構成分", ING)], "找不到「完全不存在的虛構成分」對應的成分或藥品名稱"),
])
def test_other_clarifications_never_conclude(subjects, expected):
    p = pairwise(subjects).response
    assert p.response_type == ResponseType.MISSING_INFORMATION and expected in p.summary
    assert "資料庫沒有收錄這組關係" not in primary_text(p)


def test_corpus_unavailable_is_reported_as_unavailable_not_as_no_interaction():
    flex, rec = Service([("鋅", ING), ("銅", ING)], repo=InMemoryInteractionRepo(version=None)).send()
    p = rec.response
    assert p.response_type == ResponseType.MISSING_INFORMATION and "暫時查詢不到" in p.summary
    assert p.available_actions == [ResponseAction.RETRY]
    assert "沒有交互作用" not in primary_text(p) and "沒有收錄這組關係" not in primary_text(p)


# ============================================================ 14. 既有卡片不變


def test_non_interaction_requests_are_unaffected():
    from tests.unit.test_ingredient_retrieval_runtime import run

    r = run(DOM_NAME, ["鋅"])
    assert r.evidence.interaction_research is None
    assert r.response.response_type == ResponseType.INGREDIENT_LIST
    assert ResponseType.INTERACTION_RESULT not in {r.response.response_type}
    assert ResponseAction.INGREDIENTS in r.response.available_actions
    assert ResponseAction.INTERACTION_EVIDENCE not in r.response.available_actions


def test_interaction_intent_without_research_falls_back_to_generic_response():
    from app.schemas.evidence import EvidenceBundle, ProductMatch
    from app.schemas.enums import ProductVerificationStatus
    from app.agents.reporter_agent import select_response_type
    from app.risk.risk_engine import RiskEngine
    from tests.builders import request as make_request

    req = make_request(intent=Intent.INTERACTION_INQUIRY)
    bundle = EvidenceBundle(request_id=req.request_id, product_match=ProductMatch(status=ProductVerificationStatus.NOT_APPLICABLE))
    assert select_response_type(req, bundle, RiskEngine().assess(req, bundle)) != ResponseType.INTERACTION_RESULT


def test_candidate_card_for_interaction_uses_existing_candidate_layout():
    s = Service([(TYPO_ZINC, PRODUCT), ("Testdrug-A", DRUG)])
    flex, rec = s.send(text=f"{TYPO_ZINC} 跟 Testdrug-A 可以一起吃嗎")
    assert "選擇：" in texts_of(flex) and flex["contents"]["type"] == "bubble"


# ============================================================ 15. Flex 結構（validate API 見 integration）


def all_interaction_records(mix_product):
    cases = [
        Service([("鋅", ING), ("銅", ING)]), Service([("魚油", ING), ("Testdrug-Anticoag", DRUG)]),
        Service([(IMP_NAME, PRODUCT), ("Testdrug-B", DRUG)]), Service([("鎂", ING), ("Testdrug-A", DRUG)]),
        Service([("鎂", ING), ("銅", ING)]), Service([(HF_ZINC, PRODUCT), (DOM_NAME, PRODUCT)], mode=InteractionMode.DUPLICATE_COMPARISON),
        Service([(HF_ZINC, PRODUCT), (MIX_NAME, PRODUCT)], repo=inheriting_repo()),
        Service([("鋅", ING), ("某某牌藥品商品名", DRUG)]),
    ]
    return [(c, *c.send()) for c in cases]


def test_every_interaction_state_renders_one_valid_flex(mix_product):
    for s, flex, rec in all_interaction_records(mix_product):
        assert flex["type"] == "flex" and flex["contents"]["type"] == "bubble" and len(flex["altText"]) <= 400
        assert len(postbacks(flex)) <= MAX_BUTTONS
        assert rec.response.response_type in (ResponseType.INTERACTION_RESULT, ResponseType.MISSING_INFORMATION)
        assert_primary_is_safe(rec.response)
        text = texts_of(flex)
        assert not re.search(r"\b(?:T[NFDI])_[A-Z0-9_]+", text)
        for action in rec.response.available_actions:
            if action in (ResponseAction.WHY, ResponseAction.INTERACTION_EVIDENCE, ResponseAction.INTERACTION_MAPPING):
                detail = s.action(rec, action)
                assert detail["type"] == "flex" and not re.search(r"\b(?:T[NFDI])_[A-Z0-9_]+", texts_of(detail))


# ============================================================ phone smoke 後：措辭 / 來源卡 / 劑量主語


def test_source_card_explains_grade_in_plain_words_but_structured_evidence_keeps_it():
    s = Service([("鋅", ING), ("銅", ING)])
    _, rec = s.send()
    item = rec.evidence.interaction_research.findings[0].evidence[0]
    item.limitations_zh = "官方摘要不是單一臨床試驗；未正式GRADE評分，須核對產品製劑與病人情境。"
    item.evidence_level = "OFFICIAL_SYNTHESIS"
    card = "\n".join(ir.evidence_detail(rec.normalized_request, rec.evidence))
    assert "GRADE" not in card and "官方摘要不是單一臨床試驗；" in card
    assert "這是官方資料摘要，是否適用仍需看實際劑量與使用情境。" in card
    assert "GRADE" in item.limitations_zh and "GRADE" in json.dumps(ir.interaction_facts(rec.normalized_request, rec.evidence), ensure_ascii=False)


def test_full_analysis_rejects_llm_dose_restatement_and_appends_verbatim_evidence_digest():
    s = Service([("鋅", ING), ("銅", ING)], llm_text="銅高達約60mg/day最多10週可降低銅狀態指標。")
    _, rec = s.send()
    out = ReporterAgent(s.llm).report(rec.normalized_request, rec.evidence, rec.risk)
    assert out.used_fallback_template and "60mg" not in out.text.replace("TEST_ONLY", "")
    assert "證據摘要（資料庫原文）：" in out.text and "TEST_ONLY 摘要" in out.text and "支持此交互作用紀錄" in out.text


def test_support_label_is_not_generic_universal_wording():
    assert ir.SUPPORTS_LABEL == "支持此交互作用紀錄" and "支持有交互作用" not in "".join(ir.DIRECTION_LABELS.values())
