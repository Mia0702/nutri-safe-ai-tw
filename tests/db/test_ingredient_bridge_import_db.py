"""在本機空白 PostgreSQL（pgserver）驗證 Ingredient Bridge importer（不連 Supabase / Neo4j；虛構產品 TEST_ONLY）。

0001 → 0005 → 虛構 health_food / 國產 / 輸入產品＋既有 ProductIngredient → 正式 corpus＋reviewed overlay 的 importer apply →
重跑（0 寫入、0 重複）→ 既有 health_food 列不變 → 版本切換拒絕 → 中途失敗沒有完成標記且狀態安全、重跑補齊 →
transaction rollback 不留任何列 → Neo4j 只在完成標記後同步、只有 MERGE。
"""

import dataclasses
import uuid
from pathlib import Path

import pytest

from app.ingestion import ingredient_bridge_import as bi
from app.ingestion import ingredient_entity_mapping as iem
from app.normalization import stable_id
from scripts.dry_run_ingredient_entity_mapping import map_all
from tests.db.conftest import SUPABASE_PRELUDE
from tests.db.test_tfda_product_coverage_db import BulkPsycopgStore

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = [ROOT / "migrations" / name for name in (
    "0001_initial_schema.sql", "0002_brand_relationship_semantics.sql", "0003_claim_policy_corpus.sql",
    "0004_tfda_product_coverage.sql", "0005_product_ingredient_entity_mapping.sql")]
BRIDGE_TABLES = ("interaction_corpus_entities", "interaction_corpus_sources", "interaction_corpus_entity_aliases",
                 "interaction_form_nutrient_links", "product_ingredient_entity_mappings")
PROTECTED = ("products", "ingredients", "product_ingredients")

HF, DOM, IMP = (stable_id("product", f"TW:TEST_ONLY_BRIDGE_{k}") for k in ("HF", "DOM", "IMP"))
ING_CA, ING_OTHER = stable_id("ingredient", "testonly碳酸鈣"), stable_id("ingredient", "testonly成分")
REG_TOKENS = {
    DOM: ["碳酸鈣", "葡萄糖酸鋅", "硬脂酸鎂", "生物素", "TEST_ONLY 色素"],
    IMP: ["VITAMIN B12 (CYANOCOBALAMIN)", "MAGNESIUM STEARATE", "Riboflavin", "FISH OIL", "EPA", "DHA", "Folic Acid"],
}


def _data():
    return {
        "products": [{"id": HF, "product_category": "health_food"},
                     {"id": DOM, "product_category": "domestic_vitamin_tablet_capsule", "raw_record_id": "R_DOM"},
                     {"id": IMP, "product_category": "imported_tablet_capsule", "raw_record_id": "R_IMP"}],
        "ingredients": [{"id": ING_CA, "name_zh": "碳酸鈣", "name_en": "Calcium carbonate"},
                        {"id": ING_OTHER, "name_zh": "TEST_ONLY成分", "name_en": None}],
        "product_ingredients": [
            {"product_id": HF, "ingredient_id": ING_CA, "raw_text": "TEST_ONLY 碳酸鈣 500毫克", "amount_text": "500毫克"},
            {"product_id": HF, "ingredient_id": ING_OTHER, "raw_text": "TEST_ONLY 成分", "amount_text": None}],
        "aliases": [],
        "raw_staging": [{"id": f"R_{k}", "staging": {"parser_draft": {"tokens": REG_TOKENS[pid]}}}
                        for k, pid in (("DOM", DOM), ("IMP", IMP))],
    }


def _seed(conn):
    for pid, cat in ((HF, "health_food"), (DOM, "domestic_vitamin_tablet_capsule"), (IMP, "imported_tablet_capsule")):
        conn.execute("insert into products (id, product_name_zh, product_name_normalized, product_category, "
                     "source_record_key) values (%s, %s, %s, %s, %s)",
                     (pid, f"TEST_ONLY {cat}", f"testonly{cat}", cat, f"TEST_ONLY_{cat}"))
    for iid, zh, en in ((ING_CA, "碳酸鈣", "Calcium carbonate"), (ING_OTHER, "TEST_ONLY成分", None)):
        conn.execute("insert into ingredients (id, name_zh, name_en, name_normalized) values (%s, %s, %s, %s)",
                     (iid, zh, en, f"testonly{zh}"))
    for pi in _data()["product_ingredients"]:
        conn.execute("insert into product_ingredients (product_id, ingredient_id, raw_text, amount_text) "
                     "values (%s, %s, %s, %s)", (pi["product_id"], pi["ingredient_id"], pi["raw_text"], pi["amount_text"]))


def _snapshot(conn, tables):
    return {t: sorted(map(str, conn.execute(f"select * from {t}").fetchall())) for t in tables}


def _new_db(pg_uri):
    import psycopg

    base = pg_uri.rsplit("/", 1)[0]
    name = f"bridge_imp_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"create database {name}")
    conn = psycopg.connect(f"{base}/{name}", autocommit=True)
    conn.execute(SUPABASE_PRELUDE)
    for path in MIGRATIONS:
        conn.execute(path.read_text(encoding="utf-8"))
    _seed(conn)
    return conn, name


def _drop(pg_uri, conn, name):
    import psycopg

    conn.close()
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")


@pytest.fixture(scope="module")
def corpus():
    return iem.load_interaction_corpus(overlay=iem.load_reviewed_overlay())


def _plan(corpus, store, data=None):
    data = data or _data()
    return bi.plan_bridge_import(corpus, map_all(corpus, data), store,
                                 product_ids={p["id"] for p in data["products"]},
                                 ingredient_ids={i["id"] for i in data["ingredients"]})


@pytest.fixture(scope="module")
def applied(pg_uri, corpus):
    conn, name = _new_db(pg_uri)
    before = _snapshot(conn, PROTECTED)
    store = BulkPsycopgStore(conn)
    plan = _plan(corpus, store)
    assert plan.errors == []
    first = bi.apply_bridge_import(plan, store)
    yield conn, store, plan, first, before
    _drop(pg_uri, conn, name)


def _q(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


def test_rerun_is_idempotent_with_no_duplicates(applied, corpus):
    conn, store, plan, first, _ = applied
    assert first["written"]["D_formal_mappings"] > 0 and first["written"]["E_candidate_mappings"] > 0
    again = _plan(corpus, store)
    assert again.errors == []
    for stage, diff in again.diffs.items():
        assert diff.counts() == {"insert": 0, "update": 0, "unchanged": len(plan.stages[stage]), "stale_would_delete": 0}
    writes = store.writes
    report = bi.apply_bridge_import(again, store)
    assert sum(report["written"].values()) == 0 and store.writes == writes + 1   # 只有完成標記 upsert
    assert _q(conn, "select count(*) from (select product_id, coalesce(source_ingredient_id::text, ''), "
                    "coalesce(token_index, -1), raw_text, mapping_status, coalesce(entity_id, ''), "
                    "coalesce(via_entity_id, ''), count(*) from product_ingredient_entity_mappings "
                    "group by 1, 2, 3, 4, 5, 6, 7 having count(*) > 1) d")[0][0] == 0
    assert _q(conn, "select count(*) from (select alias_normalized, overlay_version from interaction_corpus_entity_aliases "
                    "where alias_origin = 'REVIEWED_OVERLAY' and entity_id is not null group by 1, 2 "
                    "having count(*) > 1) d")[0][0] == 0
    assert _q(conn, "select count(*) from (select form_entity_id from interaction_form_nutrient_links "
                    "group by 1 having count(*) > 1) d")[0][0] == 0
    counts = {t: _q(conn, f"select count(*) from {t}")[0][0] for t in BRIDGE_TABLES}
    assert counts["interaction_corpus_entities"] == len(corpus.entities)
    assert counts["interaction_form_nutrient_links"] == 61
    assert counts["product_ingredient_entity_mappings"] == len(plan.stages["D_formal_mappings"]) + \
        len(plan.stages["E_candidate_mappings"])
    assert _q(conn, "select value from app_schema_meta where key = %s", plan.marker_key)[0][0] == plan.marker_value


def test_existing_health_food_and_protected_tables_unchanged(applied):
    conn, _, _, _, before = applied
    assert _snapshot(conn, PROTECTED) == before
    rows = _q(conn, "select source_ingredient_id::text, raw_text, entity_id, mapping_status "
                    "from product_ingredient_entity_mappings where product_id = %s", HF)
    assert (ING_CA, "TEST_ONLY 碳酸鈣 500毫克", "F_CA_CARBONATE", "VERIFIED_EXACT") in rows   # 只新增 bridge


def test_accepted_alias_formal_and_candidate_stays_candidate(applied):
    conn, *_ = applied
    contains = {r[0] for r in _q(conn, "select entity_id from product_ingredient_entity_contains where product_id = %s", DOM)}
    assert contains == {"F_CA_CARBONATE", "F_ZN_GLUCONATE", "F_BIOTIN"}
    for text in ("硬脂酸鎂", "MAGNESIUM STEARATE"):                     # 不因名稱含 magnesium 變成營養用 Magnesium
        [(status, formal, entity)] = _q(conn, "select mapping_status, is_formal, entity_id from "
                                              "product_ingredient_entity_mappings where raw_text = %s", text)
        assert status in ("AMBIGUOUS", "NEEDS_REVIEW") and formal is False and entity is None
    assert _q(conn, "select count(*) from product_ingredient_entity_mappings where entity_id = 'N_MG'")[0][0] == 0


def test_overlay_precedence_form_then_contributes_only(applied):
    conn, *_ = applied
    rows = _q(conn, "select mapping_status, entity_id, entity_type, via_entity_id, relationship_id from "
                    "product_ingredient_entity_mappings where raw_text = 'VITAMIN B12 (CYANOCOBALAMIN)' order by 1")
    assert [r[:4] for r in rows] == [("FORM_TO_GROUP", "N_B12", "NutrientGroup", "F_CYANOCOBALAMIN"),
                                     ("SUPPORTED_ALIAS", "F_CYANOCOBALAMIN", "IngredientForm", None)]
    [(rtype, basis, src)] = _q(conn, "select relationship_type, basis, source_id from interaction_form_nutrient_links "
                                     "where relationship_id = %s", rows[0][4])
    assert (rtype, basis) == ("CONTRIBUTES_TO_NUTRIENT", "REVIEWED_OVERLAY") and src
    assert {r[0] for r in _q(conn, "select distinct relationship_type from interaction_form_nutrient_links")} == {
        "CONTRIBUTES_TO_NUTRIENT"}
    # CONTAINS 只到 form，不同時 CONTAINS 營養素群
    assert _q(conn, "select count(*) from product_ingredient_entity_contains where product_id = %s and entity_id = 'N_B12'",
              IMP)[0][0] == 0


def test_identity_separation_and_raw_text(applied):
    conn, *_ = applied
    contains = {r[0] for r in _q(conn, "select entity_id from product_ingredient_entity_contains where product_id = %s", IMP)}
    assert {"F_FISH_OIL", "F_EPA", "F_DHA", "F_FOLIC_ACID"} <= contains and not {"N_OMEGA3", "N_B9"} & contains
    raw = [r[0] for r in _q(conn, "select raw_text from product_ingredient_entity_mappings where product_id = %s "
                                  "and mapping_status <> 'FORM_TO_GROUP' order by token_index", IMP)]
    assert raw == REG_TOKENS[IMP]


def test_source_provenance_complete(applied):
    conn, *_ = applied
    assert _q(conn, "select count(*) from interaction_corpus_entity_aliases where alias_origin = 'REVIEWED_OVERLAY' and "
                    "(source_id is null or source_url is null or source_locator is null or retrieved_at is null "
                    "or overlay_version is null)")[0][0] == 0
    assert _q(conn, "select count(*) from interaction_form_nutrient_links where source_id is null")[0][0] == 0
    # 正式 overlay mapping 的 alias 都存在於 alias 表；正式 mapping 都有版本
    assert _q(conn, "select count(*) from product_ingredient_entity_mappings m where alias_evidence_status is not null "
                    "and not (matched_alias_ids <@ (select array_agg(alias_id) from interaction_corpus_entity_aliases))"
              )[0][0] == 0
    assert _q(conn, "select count(*) from product_ingredient_entity_mappings where corpus_version is null "
                    "or overlay_version is null")[0][0] == 0
    assert _q(conn, "select count(*) from product_ingredient_entity_mappings where amount_normalized is not null")[0][0] == 0


def test_version_replay_refuses_without_writing(applied, corpus):
    conn, store, *_ = applied
    newer = dataclasses.replace(corpus, overlay=dataclasses.replace(corpus.overlay, version="TEST_ONLY-2"))
    before = _snapshot(conn, BRIDGE_TABLES)
    plan = _plan(newer, store)
    assert any("不在本次計畫" in e for e in plan.errors) and any("其他版本" in e for e in plan.errors)
    assert plan.report()["would_delete"] > 0
    with pytest.raises(RuntimeError, match="拒絕寫入"):
        bi.apply_bridge_import(plan, store)
    assert _snapshot(conn, BRIDGE_TABLES) == before


class FailingStore(BulkPsycopgStore):
    def __init__(self, conn, fail_table):
        super().__init__(conn)
        self.fail_table = fail_table

    def upsert(self, table, rows, on_conflict):
        if table == self.fail_table:
            raise RuntimeError("TEST_ONLY injected failure")
        super().upsert(table, rows, on_conflict)


def test_failure_mid_apply_leaves_safe_state_and_rerun_completes(pg_uri, corpus):
    conn, name = _new_db(pg_uri)
    try:
        store = FailingStore(conn, "product_ingredient_entity_mappings")
        plan = _plan(corpus, store)
        with pytest.raises(RuntimeError, match="injected"):
            bi.apply_bridge_import(plan, store)
        assert not bi.bridge_marker_present(store, plan)                   # 沒有完成標記
        assert _q(conn, "select count(*) from product_ingredient_entity_mappings")[0][0] == 0
        assert _q(conn, "select count(*) from interaction_corpus_entities")[0][0] == len(corpus.entities)
        good = BulkPsycopgStore(conn)
        retry = _plan(corpus, good)
        assert retry.errors == [] and retry.diffs["A_entities"].counts()["insert"] == 0
        bi.apply_bridge_import(retry, good)
        assert bi.bridge_marker_present(good, retry)
        assert _q(conn, "select count(*) from product_ingredient_entity_mappings where is_formal and "
                        "mapping_status in ('AMBIGUOUS', 'NEEDS_REVIEW')")[0][0] == 0
    finally:
        _drop(pg_uri, conn, name)


def test_transaction_rollback_leaves_no_rows(pg_uri, corpus):
    conn, name = _new_db(pg_uri)
    try:
        conn.autocommit = False
        store = FailingStore(conn, "app_schema_meta")                       # 最後一步（完成標記）失敗
        plan = _plan(corpus, store)
        with pytest.raises(RuntimeError, match="injected"):
            bi.apply_bridge_import(plan, store)
        conn.rollback()
        conn.autocommit = True
        assert all(_q(conn, f"select count(*) from {t}")[0][0] == 0 for t in BRIDGE_TABLES)
    finally:
        _drop(pg_uri, conn, name)


def test_database_rejects_candidate_promoted_to_formal(applied):
    import psycopg

    conn, *_ = applied
    [(mid,)] = _q(conn, "select id from product_ingredient_entity_mappings where raw_text = 'MAGNESIUM STEARATE'")
    promote = ("update product_ingredient_entity_mappings set mapping_status = 'SUPPORTED_ALIAS', "
               "entity_id = 'N_MG', entity_type = 'NutrientGroup', review_status = 'SOURCE_REVIEWED', "
               "candidate_entity_ids = '{}'")
    for extra in ("", ", match_method = 'REVIEWED_OVERLAY_ALIAS_EXACT'",
                  ", match_method = 'CORPUS_SOURCED_SYNONYM_EXACT'"):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(promote + extra + " where id = %s", (mid,))
    # 新列：正式 mapping 引用 overlay 候選 alias（硬脂酸鎂 = AMBIGUOUS）
    [(cand_alias,)] = _q(conn, "select alias_id from interaction_corpus_entity_aliases where alias_origin = "
                               "'REVIEWED_OVERLAY' and alias_text = '硬脂酸鎂'")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into product_ingredient_entity_mappings (id, product_id, ingredient_source, token_index, "
                     "raw_text, mapping_status, entity_id, entity_type, match_method, matched_alias_ids, confidence, "
                     "review_status, corpus_version, overlay_version) values (%s, %s, "
                     "'TFDA_REGISTRATION_INGREDIENTS_RAW', 99, 'TEST_ONLY', 'SUPPORTED_ALIAS', 'N_MG', 'NutrientGroup', "
                     "'CORPUS_SOURCED_SYNONYM_EXACT', %s, 0.9, 'AUTO_ACCEPTED_RULE', '1.0', '1.0')",
                     (str(uuid.uuid4()), DOM, [cand_alias]))
    assert _q(conn, "select mapping_status from product_ingredient_entity_mappings where id = %s", mid)[0][0] == \
        "NEEDS_REVIEW"


class FakeNeo4j:
    def __init__(self):
        self.queries = []

    def write(self, query, params=None):
        self.queries.append((query, params or {}))
        return []


def test_neo4j_local_preview_only_after_marker_and_merge_only(pg_uri, corpus):
    conn, name = _new_db(pg_uri)
    try:
        store, neo = BulkPsycopgStore(conn), FakeNeo4j()
        plan = _plan(corpus, store)
        report = bi.apply_bridge_import(plan, store, neo4j=neo)
        assert bi.bridge_marker_present(store, plan)
        text = "\n".join(q for q, _ in neo.queries)
        for token in bi.FORBIDDEN_GRAPH_TOKENS:
            assert token not in text
        written = report["graph_written"]
        preview = plan.graph_preview()
        assert written["nutrient_groups"] == preview["nodes"]["NutrientGroup"] == 17
        assert written["ingredient_forms"] == preview["nodes"]["IngredientForm"] == 61
        assert written["contributes"] == 61
        assert written["contains_nutrient_group"] + written["contains_ingredient_form"] == \
            preview["relationships"]["CONTAINS"]
        contains_rows = [r for q, p in neo.queries if "CONTAINS" in q for r in p.get("rows", [])]
        assert all(r["mapping_status"] in ("VERIFIED_EXACT", "SUPPORTED_ALIAS") and r["raw_text"]
                   and r["review_status"] and r["corpus_version"] and r["overlay_version"] for r in contains_rows)
    finally:
        _drop(pg_uri, conn, name)
