"""Data freeze 前的資料層一般化測試：
1. 官方紀錄缺「保健功效」類別但有「保健功效宣稱」原文 → 仍保留為官方核准宣稱證據（未分類，不推測類別）。
2. graph sync 的 ID 過濾與 Neo4j 寫入一律分批，批次失敗有明確錯誤，重跑 idempotent。

所有紀錄皆為 synthetic（TEST_ONLY），不對應真實產品。
"""

from datetime import datetime, timezone

import pytest

from app.ingestion.graph_sync import GraphSyncBatchError, _fetch_all, _run, sync_graph_from_supabase
from app.ingestion.tfda_health_food import (
    CLAIM_KIND_FUNCTION,
    UNCLASSIFIED_FUNCTION_KEY,
    UNCLASSIFIED_FUNCTION_NAME,
    normalize_record,
)
from app.schemas.enums import ClaimEvidenceRelation, ClaimOrigin, ClaimSemanticCategory
from app.schemas.evidence import CLAIM_KIND_UNCLASSIFIED, ClaimEvidenceLink
from app.schemas.user_request import ExtractedClaim
from app.risk.assessment_rules import assess_claim
from app.schemas.enums import ClaimAssessmentStatus
from app.tools.product_tools import lookup_approved_claim

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def _record(permit: str, function: str | None, claim: str | None) -> dict:
    return {
        "許可證字號": f"衛部健食字第{permit}號",
        "中文品名": f"TEST_ONLY 測試膠囊{permit}",
        "申請商": "TEST_ONLY 測試生技股份有限公司",
        "證況": "核可",
        "核可日期": "2020/01/01",
        "保健功效相關成分": "測試成分甲",
        "保健功效": function,
        "保健功效宣稱": claim,
        "網址": "https://consumer.fda.gov.tw/test-only",
    }


# ------------------------------------------------------------------ A1：未分類官方宣稱


@pytest.mark.parametrize("empty", [None, "", "  "])
def test_claim_text_without_function_category_kept_as_unclassified_official_claim(empty):
    rec = _record("Z90001", empty, "「根據測試結果，有助於維持測試機能。」")
    b = normalize_record(rec, NOW)
    assert b is not None
    [claim] = b.approved_claims
    assert claim["claim_kind"] == CLAIM_KIND_UNCLASSIFIED
    assert claim["function_key"] == UNCLASSIFIED_FUNCTION_KEY
    assert claim["function_name"] == UNCLASSIFIED_FUNCTION_NAME  # 不推測類別
    [pc] = b.product_claims
    assert pc["claim_type"] == "APPROVED_OFFICIAL"
    assert pc["claim_text"] == "根據測試結果,有助於維持測試機能。"  # 與既有 claim 正規化一致（NFKC、去外層引號）
    assert pc["approved_claim_id"] == claim["id"]
    assert pc["source_record_key"] == b.source_record_key and pc["source_url"]
    assert b.raw_data["保健功效宣稱"] == rec["保健功效宣稱"]  # raw source 保留


def test_unclassified_claim_id_is_shared_and_stable_across_records():
    a = normalize_record(_record("Z90001", None, "宣稱甲"), NOW)
    b = normalize_record(_record("Z90002", None, "宣稱乙"), NOW)
    assert a.approved_claims[0]["id"] == b.approved_claims[0]["id"]
    assert a.product_claims[0]["id"] != b.product_claims[0]["id"]


def test_classified_function_unchanged_and_no_unclassified_added():
    b = normalize_record(_record("Z90003", "測試功效甲, 測試功效乙", "宣稱原文"), NOW)
    assert [c["function_name"] for c in b.approved_claims] == ["測試功效甲", "測試功效乙"]
    assert {c["claim_kind"] for c in b.approved_claims} == {CLAIM_KIND_FUNCTION}
    assert len(b.product_claims) == 2


def test_no_claim_text_and_no_function_creates_no_claim():
    b = normalize_record(_record("Z90004", None, None), NOW)
    assert b.approved_claims == [] and b.product_claims == []


class _ClaimRepo:
    def __init__(self, rows):
        self.rows = rows

    def get_approved_claims(self, product_ids):
        return [r for r in self.rows if r["product_id"] in product_ids]


def _claim_row(kind):
    return {
        "product_claim_id": "pc1", "product_id": "p1", "approved_claim_id": "c1",
        "function_name": UNCLASSIFIED_FUNCTION_NAME if kind == CLAIM_KIND_UNCLASSIFIED else "測試功效甲",
        "claim_kind": kind, "claim_text": "有助於維持測試機能。",
        "source_id": "s1", "source_name": "TEST_ONLY 官方", "source_type": "open_data", "is_official": True,
        "source_url": "https://data.fda.gov.tw/test", "source_record_key": "k1", "retrieved_at": NOW.isoformat(),
    }


def test_unclassified_claim_is_still_official_approved_evidence_for_agent_b():
    [ev] = lookup_approved_claim(_ClaimRepo([_claim_row(CLAIM_KIND_UNCLASSIFIED)]), ["p1"])
    assert ev.function_classified is False
    assert ev.source.is_official and ev.claim_text
    claim = ExtractedClaim(
        claim_text="有助於維持測試機能", origin=ClaimOrigin.USER_QUESTION,
        semantic_category=ClaimSemanticCategory.HEALTH_FUNCTION, target_condition=None,
        target_function="測試機能", uses_absolute_language=False,
    )
    link = ClaimEvidenceLink(evidence_id=ev.evidence_id, relation=ClaimEvidenceRelation.SUPPORTED, rationale="官方宣稱涵蓋")
    result = assess_claim(0, claim, [link], {ev.evidence_id}, {ev.evidence_id}, True)
    assert result.status == ClaimAssessmentStatus.CONSISTENT_WITH_OFFICIAL


def test_classified_claim_flag_default_true():
    [ev] = lookup_approved_claim(_ClaimRepo([_claim_row(CLAIM_KIND_FUNCTION)]), ["p1"])
    assert ev.function_classified is True


# ------------------------------------------------------------------ A2：graph sync 分批


class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.ids, self.start, self.end = db, table, None, 0, 0

    def select(self, _cols):
        return self

    def in_(self, _col, ids):
        self.db.in_sizes.append(len(ids))
        self.ids = list(ids)
        return self

    def range(self, start, end):
        self.start, self.end = start, end
        return self


class _FakeSupabase:
    def __init__(self, tables, fail_table=None):
        self.tables, self.fail_table, self.in_sizes = tables, fail_table, []

    def table(self, name):
        return _Query(self, name)

    def select(self, q):
        if q.table == self.fail_table:
            raise RuntimeError("HTTP 414")
        rows = self.tables.get(q.table, [])
        if q.ids is not None:
            key = "id" if q.table in ("products", "companies", "permits") else "product_id"
            rows = [r for r in rows if r[key] in q.ids]
        return [dict(r) for r in rows[q.start : q.end + 1]]


class _FakeNeo4j:
    def __init__(self, fail_on_call=None):
        self.calls, self.fail_on_call = [], fail_on_call

    def write(self, query, params):
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("neo4j unavailable")
        self.calls.append(len(params["rows"]))


def _tables(n):
    products = [{"id": f"p{i}", "product_name_zh": f"TEST_ONLY {i}", "status": "ACTIVE", "country_code": "TW",
                 "company_id": f"c{i % 7}", "permit_id": f"pm{i}", "source_id": "s1", "source_url": None,
                 "source_record_key": f"k{i}"} for i in range(n)]
    return {
        "products": products,
        "companies": [{"id": f"c{i}", "name_zh": f"公司{i}"} for i in range(7)],
        "permits": [{"id": f"pm{i}", "permit_number": f"N{i}", "status": "ACTIVE"} for i in range(n)],
        "source_registry": [{"id": "s1", "source_name": "TEST_ONLY", "is_official": True, "base_url": "x"}],
        "product_ingredients": [{"product_id": f"p{i}", "ingredient_id": "i1", "raw_text": "x",
                                 "ingredients": {"name_zh": "甲", "name_en": None}} for i in range(n)],
        "product_claims": [], "product_warnings": [], "product_brands": [],
    }


def test_graph_sync_chunks_id_filters_and_never_sends_all_ids_in_one_url():
    sb = _FakeSupabase(_tables(565))
    neo = _FakeNeo4j()
    counts = sync_graph_from_supabase(sb, neo, [f"p{i}" for i in range(565)], id_batch_size=100, write_batch_size=200)
    assert sb.in_sizes and max(sb.in_sizes) <= 100
    assert counts["Product"] == 565 and counts["Permit"] == 565 and counts["CONTAINS"] == 565
    assert max(neo.calls) <= 200


def test_graph_sync_rerun_is_idempotent_in_counts():
    ids = [f"p{i}" for i in range(250)]
    first = sync_graph_from_supabase(_FakeSupabase(_tables(250)), _FakeNeo4j(), ids, id_batch_size=40)
    second = sync_graph_from_supabase(_FakeSupabase(_tables(250)), _FakeNeo4j(), ids, id_batch_size=40)
    assert first == second


def test_graph_sync_queries_are_merge_based():
    from pathlib import Path

    src = Path("app/ingestion/graph_sync.py").read_text(encoding="utf-8")
    assert " CREATE " not in src.upper().replace("MERGE", "")  # 只用 MERGE，重跑不產生重複 node / relationship


def test_read_batch_failure_raises_explicit_error():
    sb = _FakeSupabase(_tables(10), fail_table="permits")
    with pytest.raises(GraphSyncBatchError) as err:
        sync_graph_from_supabase(sb, _FakeNeo4j(), [f"p{i}" for i in range(10)], id_batch_size=3)
    assert err.value.stage == "read permits" and err.value.batch_index == 0 and err.value.batch_count == 4


def test_write_batch_failure_raises_explicit_error_with_batch_position():
    neo = _FakeNeo4j(fail_on_call=2)
    with pytest.raises(GraphSyncBatchError) as err:
        _run(neo, "UNWIND $rows AS r MERGE (n:X {id: r.id})", [{"id": i} for i in range(10)], "Product", batch_size=3)
    assert err.value.stage == "write Product" and err.value.batch_index == 2 and err.value.batch_count == 4
    assert "3/4" in str(err.value)


def test_invalid_batch_size_rejected():
    with pytest.raises(ValueError):
        _fetch_all(_FakeSupabase(_tables(1)), "products", "id", ("id", ["p0"]), id_batch_size=0)
