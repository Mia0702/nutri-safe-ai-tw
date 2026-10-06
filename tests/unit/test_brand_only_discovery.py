"""Brand-only 查詢的產品探索（phone smoke：「某品牌之前查得到、現在查不到」的調查結果）。

調查結論：資料與程式都沒有變；同一句話只因 Agent A 把名稱放進 product_candidates 或只放進 brand_candidates，
結果就不同——只有 brand_candidates 時 guardrail 不呼叫 lookup_product（官方品名常以品牌開頭）→ NOT_FOUND。
修正是一般性的（brand-only → 品名探索），結果仍由 resolver 決定，只會是候選。

fixture：tests/fixtures/readonly_brand_only_products.json 是正式資料庫的唯讀快照（TFDA 公開紀錄，只作迴歸測試資料）。
"""

import json
import re
from pathlib import Path

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import InputType, Intent, ProductVerificationStatus
from app.schemas.user_request import IntakeExtraction, RawUserInput
from tests.builders import extraction
from tests.fakes import FakeLLM, InMemoryGraphRepo, InMemoryOfficialRepo, InMemoryRagStore

ROOT = Path(__file__).resolve().parents[2]
ROWS = json.loads((ROOT / "tests/fixtures/readonly_brand_only_products.json").read_text(encoding="utf-8"))["rows"]
BRAND = "澳佳寶"  # 只出現在測試 / fixture；production code 不得出現


class SnapshotRepo(InMemoryOfficialRepo):
    """依唯讀快照回答 search_products（與正式 RPC 相同：任一搜尋詞出現在品名 / 公司名即命中）。"""

    def search_products(self, terms, limit):
        self._check("search_products")
        hits = [dict(r) for r in ROWS if any(t and t in (r["product_name_normalized"] or "") for t in terms)]
        return hits[:limit]

    def get_products_by_permit_code(self, permit_code):
        self._check("get_products_by_permit_code")
        return [dict(r) for r in ROWS if r["permit_code"] == permit_code]


def _run(ex, text):
    llm = FakeLLM(structured={IntakeExtraction: ex})
    pipeline = AnalysisPipeline(
        IntakeAgent(llm), VerificationAgent(SnapshotRepo(), InMemoryGraphRepo(), InMemoryRagStore(), llm),
        RiskEngine(), ReporterAgent(llm))
    return pipeline.run(RawUserInput(input_type=InputType.TEXT, text=text))


def test_fixture_is_real_snapshot_with_brand_products():
    assert len(ROWS) >= 5 and all(BRAND in r["product_name_zh"] for r in ROWS)


def test_brand_only_extraction_discovers_products_instead_of_not_found():
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, brand_candidates=[BRAND], needs_product_lookup=True)
    result = _run(ex, BRAND)
    match = result.product_verification
    assert match.status == ProductVerificationStatus.PARTIAL_MATCH  # 候選，不自動確認
    assert match.candidates and all(BRAND in c.product.product_name_zh for c in match.candidates)
    assert match.best_match is None or match.needs_confirmation or match.is_ambiguous
    assert "lookup_product" in [t.tool_name for t in result.tools_called]
    assert result.response.product_choices  # 使用者可以選


def test_brand_only_and_product_candidate_extractions_agree_on_discovery():
    """Agent A 把名稱放 product 或 brand 欄位，不應得到「查不到」vs「有候選」兩種結果。"""
    as_brand = _run(extraction(intent=Intent.PRODUCT_VERIFICATION, brand_candidates=[BRAND], needs_product_lookup=True), BRAND)
    as_product = _run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[BRAND], needs_product_lookup=True), BRAND)
    assert as_brand.product_verification.status == as_product.product_verification.status
    assert {c.product.product_id for c in as_brand.product_verification.candidates} \
        == {c.product.product_id for c in as_product.product_verification.candidates}


def test_unknown_brand_only_is_still_not_found():
    ex = extraction(intent=Intent.PRODUCT_VERIFICATION, brand_candidates=["TEST_ONLY不存在牌"], needs_product_lookup=True)
    assert _run(ex, "TEST_ONLY不存在牌").product_verification.status == ProductVerificationStatus.NOT_FOUND


def test_no_brand_specific_mapping_in_runtime_code():
    pattern = re.compile(r"澳佳寶|blackmores", re.IGNORECASE)
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_exact_product_name_and_permit_resolution_unchanged():
    row = ROWS[0]
    by_name = _run(extraction(intent=Intent.PRODUCT_VERIFICATION, product_candidates=[row["product_name_zh"]],
                              brand_candidates=[BRAND], needs_product_lookup=True), row["product_name_zh"])
    assert by_name.product_verification.status == ProductVerificationStatus.VERIFIED
    assert by_name.product_verification.best_match.product.product_id == row["product_id"]
    by_permit = _run(extraction(intent=Intent.PRODUCT_VERIFICATION, permit_number_candidates=[row["permit_number"]],
                                needs_product_lookup=True), row["permit_number"])
    assert by_permit.product_verification.status == ProductVerificationStatus.VERIFIED
    assert by_permit.product_verification.best_match.product.product_id == row["product_id"]
