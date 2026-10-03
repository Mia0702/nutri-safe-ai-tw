"""TFDA Claim Policy Corpus data layer（不需要資料庫、不呼叫外部服務）。

* 完整 corpus dry-run：筆數、FK、ID、enum、quarantine、RAG 區段 metadata、graph 列。
* Quarantine 規則（虛構列）：SUPERSEDED / NEEDS_REVIEW / OCR。
* Importer：idempotent、version-aware、fail closed、不 hard-code 法規句子。
"""

import copy
import re
from pathlib import Path

import pytest

from app.ingestion import claim_policy_corpus as cpc
from app.ingestion.claim_policy_corpus import (
    BatchStatus,
    EvidenceUse,
    ExampleType,
    PolicyEffect,
    RecordStatus,
    ReviewStatus,
    apply_import,
    build_graph_rows,
    decide_evidence_use,
    load_corpus,
    map_approved_claims_to_functions,
    plan_import,
    validate_corpus,
)
from app.schemas.evidence import CLAIM_KIND_UNCLASSIFIED
from app.services.neo4j_service import ALLOWED_LABELS

ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = ROOT / "data" / "tfda_claim_policy"
MIGRATION = (ROOT / "migrations" / "0003_claim_policy_corpus.sql").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not (CORPUS_DIR / cpc.FILES["validation"]).is_file(), reason="corpus 檔案不存在")


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(CORPUS_DIR)


@pytest.fixture
def fresh(corpus):
    return copy.deepcopy(corpus)


class InMemoryStore:
    """CorpusStore 的記憶體實作（upsert 依 on_conflict 欄位合併）。"""

    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.writes = 0

    def fetch(self, table, columns, eq=None):
        out = []
        for row in self.tables.get(table, []):
            ok = True
            for key, value in (eq or {}).items():
                if "->>" in key:
                    col, sub = key.split("->>")
                    ok &= (row.get(col) or {}).get(sub) == value
                else:
                    ok &= row.get(key) == value
            if ok:
                out.append({c: copy.deepcopy(row.get(c)) for c in columns})
        return out

    def upsert(self, table, rows, on_conflict):
        keys = on_conflict.split(",")
        existing = self.tables.setdefault(table, [])
        for row in rows:
            self.writes += 1
            match = next((r for r in existing if all(r.get(k) == row[k] for k in keys)), None)
            if match is None:
                existing.append(copy.deepcopy(row))
            else:
                match.update(copy.deepcopy(row))

    def update(self, table, values, match):
        for row in self.tables.get(table, []):
            if all(row.get(k) == v for k, v in match.items()):
                self.writes += 1
                row.update(copy.deepcopy(values))


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        return [[float(len(t) % 7), 1.0] for t in texts]


class FakeNeo4j:
    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def write(self, query, params=None):
        self.queries.append((query, params or {}))
        return []


def _apply(plan, store, embedder=None):
    return apply_import(plan, store, embedder=embedder or FakeEmbedder(), embedding_model="test-embedding")


# ---------------------------------------------------------------- 完整 corpus dry-run


def test_full_corpus_dry_run_counts_and_no_errors(corpus):
    plan = plan_import(corpus)
    report = plan.report()
    assert plan.errors == []
    inserts = {t: c["insert"] for t, c in report["tables"].items()}
    assert inserts == {
        "source_registry": 71, "claim_policy_sources": 71, "claim_policy_rules": 163,
        "claim_policy_examples": 267, "health_function_catalog": 14, "health_function_historical_sources": 4,
    }
    assert report["rag"]["source_sections"] == 976
    assert report["illegal_ad_cases_written"] == 0
    assert report["open_data_inventory_sources"] == 5


def test_all_foreign_keys_and_ids_resolve(corpus):
    rows = plan_import(corpus).rows
    registry = {r["id"] for r in rows.source_registry}
    sources = {r["id"] for r in rows.claim_policy_sources}
    rules = {r["id"] for r in rows.claim_policy_rules}
    functions = {r["id"] for r in rows.health_function_catalog}
    for table, key in cpc.VERSIONED_TABLES.items():
        data = getattr(rows, table)
        assert len({r["id"] for r in data}) == len(data), table
        assert len({r[key] for r in data}) == len(data), table
    assert {r["source_registry_id"] for r in rows.claim_policy_sources} == registry
    assert all(r["claim_policy_source_id"] in sources for r in rows.claim_policy_rules)
    assert all(r["claim_policy_rule_id"] in rules and r["claim_policy_source_id"] in sources
               for r in rows.claim_policy_examples)
    assert all(r["catalog_source_id"] in sources and r["evaluation_source_id"] in sources
               for r in rows.health_function_catalog)
    assert all(r["health_function_id"] in functions and r["claim_policy_source_id"] in sources
               for r in rows.health_function_historical_sources)
    # corpus 原始 ID 與原始列完整保留
    assert {r["rule_id"] for r in rows.claim_policy_rules} == {r["rule_id"] for r in corpus.rules}
    assert all(r["raw_data"]["rule_id"] == r["rule_id"] for r in rows.claim_policy_rules)
    assert all(r["raw_manifest"] for r in rows.claim_policy_sources)


def test_status_enums_are_within_migration_checks(corpus):
    def check_values(column):
        found = []
        for m in re.finditer(rf"check\s*\(\s*{column}\s+in\s*\(", MIGRATION):
            depth, i = 1, m.end()
            while depth:
                depth += {"(": 1, ")": -1}.get(MIGRATION[i], 0)
                i += 1
            found.append(set(re.findall(r"'([^']+)'", MIGRATION[m.end(): i - 1])))
        return found

    for column, enums in (("status", (RecordStatus, BatchStatus)), ("review_status", (ReviewStatus,)),
                          ("policy_effect", (PolicyEffect,)), ("example_type", (ExampleType,)),
                          ("evidence_use", (EvidenceUse,))):
        found = check_values(column)
        allowed = [{e.value for e in enum} for enum in enums]
        assert found, column
        assert all(values in allowed for values in found), column
        assert all(a in found for a in allowed), column
    rows = plan_import(corpus).rows
    assert {r["evidence_use"] for t in cpc.VERSIONED_TABLES for r in getattr(rows, t)} <= {e.value for e in EvidenceUse}


# ---------------------------------------------------------------- quarantine


def test_quarantine_counts_on_full_corpus(corpus):
    report = plan_import(corpus).report()
    q = report["quarantine"]
    assert q["claim_policy_sources"] == {"DECISION_ELIGIBLE": 61, "EXCLUDED_SUPERSEDED": 9, "CONTEXT_ONLY": 1}
    assert q["claim_policy_rules"].get("DECISION_ELIGIBLE") == 134
    assert report["review"]["rules_review_status_needs_review"] == 29
    assert report["review"]["rules_policy_effect_needs_review"] == 11
    assert report["review"]["functions_review_status_needs_review"] == 3
    assert report["review"]["rag_sections_needs_review"] == 30


def test_needs_review_rules_never_decision_eligible(corpus):
    rows = plan_import(corpus).rows
    for r in rows.claim_policy_rules:
        if ReviewStatus.NEEDS_REVIEW in (r["review_status"], r["status"]) or r["policy_effect"] == PolicyEffect.NEEDS_REVIEW:
            assert r["evidence_use"] == EvidenceUse.CONTEXT_ONLY, r["rule_id"]
    rule_use = {r["id"]: r["evidence_use"] for r in rows.claim_policy_rules}
    for e in rows.claim_policy_examples:  # 例示不比所屬規則更可信
        if rule_use[e["claim_policy_rule_id"]] != EvidenceUse.DECISION_ELIGIBLE:
            assert e["evidence_use"] != EvidenceUse.DECISION_ELIGIBLE, e["example_id"]


def _src(status="CURRENT"):
    return {"status": status}


def _rule(**kw):
    return {"status": "CURRENT", "review_status": "SOURCE_VERIFIED_CONTEXT_REQUIRED", "policy_effect": "PROHIBITED", **kw}


def test_superseded_rule_or_source_is_excluded_from_current_decision():
    assert decide_evidence_use(cpc.rule_quarantine(_rule(status="SUPERSEDED"), _src())) == EvidenceUse.EXCLUDED_SUPERSEDED
    assert decide_evidence_use(cpc.rule_quarantine(_rule(), _src("SUPERSEDED"))) == EvidenceUse.EXCLUDED_SUPERSEDED
    reasons = cpc.example_quarantine(cpc.rule_quarantine(_rule(), _src("SUPERSEDED")), _src())
    assert decide_evidence_use(reasons) == EvidenceUse.EXCLUDED_SUPERSEDED


def test_needs_review_is_context_only_not_final_conclusion():
    for rule, source in ((_rule(review_status="NEEDS_REVIEW"), _src()), (_rule(policy_effect="NEEDS_REVIEW"), _src()),
                         (_rule(status="NEEDS_REVIEW"), _src()), (_rule(), _src("NEEDS_REVIEW"))):
        assert decide_evidence_use(cpc.rule_quarantine(rule, source)) == EvidenceUse.CONTEXT_ONLY
    assert decide_evidence_use(cpc.rule_quarantine(_rule(), _src())) == EvidenceUse.DECISION_ELIGIBLE


def test_ocr_unverified_section_is_never_sole_decision_evidence():
    section = {"status": "CURRENT", "review_status": "SOURCE_TEXT_VERIFIED", "extraction_method": "OCR_UNVERIFIED"}
    assert decide_evidence_use(cpc.section_quarantine(section)) == EvidenceUse.CORROBORATION_REQUIRED
    section["review_status"] = "NEEDS_REVIEW"
    assert decide_evidence_use(cpc.section_quarantine(section)) == EvidenceUse.CONTEXT_ONLY
    section.update(extraction_method="PDF_TEXT", review_status="SOURCE_TEXT_VERIFIED")
    assert decide_evidence_use(cpc.section_quarantine(section)) == EvidenceUse.DECISION_ELIGIBLE


# ---------------------------------------------------------------- RAG


def test_rag_sections_keep_metadata_and_superseded_is_not_current(corpus):
    plan = plan_import(corpus)
    section_ids = {d["document_id"] for d in corpus.documents}
    first_parts = [c["metadata"] for g in plan.rag_groups for c in g.chunks if c["metadata"]["section_part"] == 0]
    assert {m["corpus_document_id"] for m in first_parts} == section_ids and len(first_parts) == 976
    status_of = {d["document_id"]: d for d in corpus.documents}
    for g in plan.rag_groups:
        assert g.document["is_current"] == (g.document["metadata"]["status"] != "SUPERSEDED")
        assert g.document["document_type"] in {"regulation", "guideline", "announcement", "faq", "other"}
        for c in g.chunks:
            m = c["metadata"]
            src = status_of[m["corpus_document_id"]]
            for key in ("source_id", "status", "review_status", "article_or_section", "extraction_method"):
                assert m[key] == src[key], key
            assert m["effective_date"] == (src["effective_date"] or None)
            if m["section_parts"] > 1:
                assert len(c["content"]) <= cpc.MAX_CHUNK_CHARS
            if not g.document["is_current"]:
                assert m["evidence_use"] == EvidenceUse.EXCLUDED_SUPERSEDED
    superseded = [m for m in first_parts if m["status"] == "SUPERSEDED"]
    assert len(superseded) == 69
    assert plan.report()["rag"]["current_retrieval_sections"] == 976 - 69
    # 一個（來源, 網址）一份文件：current 文件網址不重複（rag_documents_current_url_unique）
    urls = [g.document["source_url_normalized"] for g in plan.rag_groups if g.document["is_current"]]
    assert len(urls) == len(set(urls))
    hashes = [g.document["content_hash"] for g in plan.rag_groups]
    assert len(hashes) == len(set(hashes))


def test_rag_conflict_with_existing_non_corpus_document_blocks(corpus):
    store = InMemoryStore()
    group = plan_import(corpus).rag_groups[0]
    store.tables["rag_documents"] = [{"id": "x", "source_url_normalized": group.document["source_url_normalized"],
                                      "is_current": True, "metadata": {}, "content_hash": "other"}]
    plan = plan_import(corpus, store)
    assert any("同網址" in e for e in plan.errors)
    with pytest.raises(RuntimeError):
        _apply(plan, store)


# ---------------------------------------------------------------- importer：idempotent / version-aware


def test_import_is_idempotent(corpus):
    store, embedder = InMemoryStore(), FakeEmbedder()
    _apply(plan_import(corpus, store), store, embedder)
    assert embedder.calls == sum(len(g.chunks) for g in plan_import(corpus).rag_groups)
    assert len(store.tables["rag_chunks"]) == embedder.calls
    second = plan_import(corpus, store)
    report = second.report()
    assert all(c["insert"] == 0 and c["update"] == 0 for c in report["tables"].values())
    assert report["tables"]["claim_policy_rules"]["unchanged"] == 163
    assert report["rag"]["unchanged"] == report["rag"]["documents"] and report["rag"]["chunks_to_embed"] == 0
    writes_before = store.writes
    _apply(second, store, embedder)
    assert store.writes - writes_before == 2  # 只有批次紀錄（RUNNING → COMPLETED）
    [batch] = store.tables["claim_policy_import_batches"]
    assert batch["status"] == "COMPLETED" and batch["run_count"] == 2


def test_changed_record_is_versioned_update_and_preserves_ids(fresh):
    store = InMemoryStore()
    _apply(plan_import(fresh, store), store)
    first_batch = store.tables["claim_policy_import_batches"][0]["id"]
    target = fresh.rules[0]
    target["conditions"] = target["conditions"] + "（TEST 修訂）"
    fresh.file_hashes = {**fresh.file_hashes, "rules": "changed"}
    plan = plan_import(fresh, store)
    assert plan.report()["tables"]["claim_policy_rules"] == {"insert": 0, "update": 1, "unchanged": 162, "not_in_corpus": 0}
    _apply(plan, store)
    row = next(r for r in store.tables["claim_policy_rules"] if r["rule_id"] == target["rule_id"])
    assert row["record_version"] == 2
    assert row["first_import_batch_id"] == first_batch != row["last_import_batch_id"]
    assert row["id"] == cpc.canonical_id("rule", target["rule_id"])


def test_source_becoming_superseded_updates_rag_without_reembedding(fresh):
    store = InMemoryStore()
    _apply(plan_import(fresh, store), store)
    sid = next(s["source_id"] for s in fresh.sources if s["status"] == "CURRENT")
    for s in fresh.sources:
        if s["source_id"] == sid:
            s["status"] = "SUPERSEDED"
    for d in fresh.documents:
        if d["source_id"] == sid:
            d["status"] = "SUPERSEDED"
    plan = plan_import(fresh, store)
    assert plan.errors == []
    assert plan.rag.metadata_update and not plan.rag.insert and not plan.rag.new_version
    embedder = FakeEmbedder()
    _apply(plan, store, embedder)
    assert embedder.calls == 0
    docs = [d for d in store.tables["rag_documents"] if d["metadata"]["corpus_source_id"] == sid]
    assert docs and not any(d["is_current"] for d in docs)
    rules = [r for r in store.tables["claim_policy_rules"] if r["raw_data"]["source_id"] == sid]
    assert all(r["evidence_use"] == EvidenceUse.EXCLUDED_SUPERSEDED for r in rules)


def test_changed_section_text_creates_new_rag_version(fresh):
    store = InMemoryStore()
    _apply(plan_import(fresh, store), store)
    fresh.documents[0]["content"] += "（TEST 勘誤）"
    plan = plan_import(fresh, store)
    assert len(plan.rag.new_version) == 1 and plan.rag.new_version[0][1]
    _apply(plan, store)
    key = plan.rag.new_version[0][0].group_key
    versions = [d for d in store.tables["rag_documents"] if d["metadata"]["corpus_group_key"] == key]
    assert len(versions) == 2 and sum(d["is_current"] for d in versions) == 1


def test_older_corpus_version_is_refused(fresh):
    store = InMemoryStore()
    _apply(plan_import(fresh, store), store)
    fresh.validation = {**fresh.validation, "research_reference_date": "2020-01-01"}
    fresh.file_hashes = {**fresh.file_hashes, "validation": "older"}
    assert any("降版" in e for e in plan_import(fresh, store).errors)
    assert not any("降版" in e for e in plan_import(fresh, store, allow_older_corpus=True).errors)


@pytest.mark.parametrize("mutate,expected", [
    (lambda c: c.rules[0].__setitem__("source_id", "SRC_TEST_MISSING"), "rules.source_id"),
    (lambda c: c.examples[0].__setitem__("rule_id", "RULE_TEST_MISSING"), "examples.rule_id"),
    (lambda c: c.rules[0].__setitem__("policy_effect", "MAYBE"), "policy_effect"),
    (lambda c: c.sources[0].__setitem__("status", "OLD"), "sources.status"),
    (lambda c: c.rules.append(dict(c.rules[0])), "rules.rule_id 重複"),
    (lambda c: c.rules[0].__setitem__("effective_date", "2024/01/01"), "日期格式"),
    (lambda c: c.documents[0].__setitem__("source_url", "https://example.com/x"), "非台灣官方網域"),
    (lambda c: c.documents[0].__setitem__("document_type", "部落格"), "document_type"),
], ids=["dangling-rule-source", "dangling-example-rule", "bad-effect", "bad-status", "dup-id", "bad-date",
        "non-official-url", "unknown-doc-type"])
def test_invalid_corpus_fails_closed(fresh, mutate, expected):
    mutate(fresh)
    errors = validate_corpus(fresh)
    assert any(expected in e for e in errors), errors
    plan = plan_import(fresh, InMemoryStore())
    with pytest.raises(RuntimeError):
        _apply(plan, InMemoryStore())


def test_dry_run_store_never_writes(corpus):
    plan = plan_import(corpus)
    with pytest.raises(RuntimeError):
        apply_import(plan, cpc.EmptyStore(), embedder=FakeEmbedder(), embedding_model="test-embedding")


def test_importer_hard_codes_no_legal_sentence(corpus):
    source = (ROOT / "app" / "ingestion" / "claim_policy_corpus.py").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "import_claim_policy_corpus.py").read_text(encoding="utf-8")
    texts = {r["official_text"] for r in corpus.rules} | {e["example_text"] for e in corpus.examples}
    fragments = {t.strip()[:12] for t in texts if len(t.strip()) >= 12}
    assert [f for f in fragments if f in source or f in script] == []
    assert not any(r["rule_id"] in source for r in corpus.rules)


# ---------------------------------------------------------------- Neo4j


def test_graph_rows_only_relationship_nodes(corpus):
    plan = plan_import(corpus)
    g = plan.graph
    source_ids = {s["id"] for s in g.sources}
    assert len(g.rules) == 163 and len(g.functions) == 14 and len(g.sources) == 71
    assert all(r["source_id"] in source_ids for r in g.rules)
    assert all(d["source_id"] in source_ids for d in g.defined_by) and len(g.defined_by) == 28
    assert {"ClaimPolicyRule", "HealthFunction", "Source"} <= ALLOWED_LABELS
    neo = FakeNeo4j()
    cpc.write_graph(neo, g)
    text = "\n".join(q for q, _ in neo.queries)
    assert "RagChunk" not in text and "Example" not in text
    assert "MERGE (c:Claim" not in text  # mapping 只連既有 Claim
    assert all("DELETE" not in q for q, _ in neo.queries)


def test_claim_to_function_mapping_is_exact_only():
    functions = [{"id": "f1", "official_function_name": "TEST 功效甲"},
                 {"id": "f2", "official_function_name": "TEST 功效乙"}]
    claims = [
        {"id": "c1", "function_name": "TEST功效甲", "claim_kind": "HEALTH_FOOD_CLAIM"},  # 只差空白 → 正規化後相同
        {"id": "c2", "function_name": "TEST 功效甲輔助", "claim_kind": "HEALTH_FOOD_CLAIM"},  # 包含關係不算
        {"id": "c3", "function_name": "TEST 功效乙", "claim_kind": CLAIM_KIND_UNCLASSIFIED},
        {"id": "c4", "function_name": "", "claim_kind": "HEALTH_FOOD_CLAIM"},
    ]
    assert map_approved_claims_to_functions(claims, functions) == [
        {"claim_id": "c1", "health_function_id": "f1", "mapping_method": "EXACT_OFFICIAL_FUNCTION_NAME"}]


def test_graph_sync_reads_back_from_store(corpus):
    store = InMemoryStore()
    plan = plan_import(corpus, store)
    neo = FakeNeo4j()
    report = apply_import(plan, store, embedder=FakeEmbedder(), embedding_model="test-embedding", neo4j=neo,
                          read_approved_claims=lambda: [])
    assert report["graph"] == build_graph_rows(
        plan.rows.claim_policy_sources, plan.rows.claim_policy_rules, plan.rows.health_function_catalog,
        plan.rows.health_function_historical_sources).counts()
    assert any("CREATE CONSTRAINT" in q for q, _ in neo.queries)
