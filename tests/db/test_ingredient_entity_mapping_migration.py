"""在本機空白 PostgreSQL（pgserver）驗證 0005 proposal（不連 Supabase / Neo4j；虛構產品 TEST_ONLY）。

0001 → 0004 → 0005（重跑一次）→ 寫入正式 corpus＋reviewed overlay 的 entity / alias / form→nutrient 關係，
與一筆虛構產品的 mapping → CHECK 擋下「候選卻有正式 entity」「overlay 候選 alias 變成正式 mapping」「SAME_AS 關係」
「沒有 amount basis 卻有 normalized 含量」等組合；既有 ingredients / product_ingredients 欄位不變。
"""

import json
import uuid
from pathlib import Path

import pytest

from app.ingestion import ingredient_entity_mapping as iem
from app.normalization import stable_id
from tests.db.conftest import SUPABASE_PRELUDE

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = [ROOT / "migrations" / name for name in (
    "0001_initial_schema.sql", "0002_brand_relationship_semantics.sql", "0003_claim_policy_corpus.sql",
    "0004_tfda_product_coverage.sql")]
MIGRATION_0005 = ROOT / "migrations" / "0005_product_ingredient_entity_mapping.sql"
PRODUCT = "TEST_ONLY_INGREDIENT_BRIDGE"


def _columns(conn, table):
    return [r[0] for r in conn.execute(
        "select column_name from information_schema.columns where table_name = %s order by ordinal_position", (table,))]


def _load_corpus(conn, corpus):
    ov = corpus.overlay
    for s in ov.sources.values():
        conn.execute("insert into interaction_corpus_sources (source_id, authority, source_tier, title, source_url, "
                     "source_type, sha256, overlay_version) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                     (s["source_id"], s["authority"], s["source_tier"], s["title"], s["source_url"], s["source_type"],
                      s["sha256"], ov.version))
    for e in sorted(corpus.entities.values(), key=lambda e: e.parent_nutrient_entity_id is not None):
        conn.execute(
            "insert into interaction_corpus_entities (entity_id, entity_type, graph_label, canonical_name_en, "
            "display_name_zh, identity_level, overlay_identity_level, parent_nutrient_entity_id, entity_origin, "
            "source_id, source_url, review_status, corpus_version, overlay_version) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (e.entity_id, e.entity_type, e.graph_label, e.canonical_name_en, e.display_name_zh, e.identity_level,
             e.overlay_identity_level, e.parent_nutrient_entity_id, e.origin, e.source_id, e.source_url,
             e.review_status, corpus.version, ov.version))
    for a in corpus.aliases:
        conn.execute("insert into interaction_corpus_entity_aliases (alias_id, entity_id, alias_text, alias_normalized, "
                     "language, alias_relation, match_policy, source_id, review_status, corpus_version) "
                     "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (a.alias_id, a.entity_id, a.alias_text, a.normalized, a.language, a.alias_relation, a.match_policy,
                      a.source_id, a.review_status, corpus.version))
    for a in ov.aliases:
        conn.execute("insert into interaction_corpus_entity_aliases (alias_id, entity_id, candidate_entity_id, alias_text, "
                     "alias_normalized, language, alias_origin, alias_status, identity_level, match_policy, "
                     "mapping_context, precedence, source_id, source_url, source_locator, retrieved_at, review_status, "
                     "corpus_version, overlay_version) values "
                     "(%s, %s, %s, %s, %s, %s, 'REVIEWED_OVERLAY', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (a.alias_id, a.entity_id, a.candidate_entity_id, a.alias_text, a.key, a.language, a.alias_status,
                      a.identity_level, a.match_policy, a.mapping_context, a.precedence, a.source_id, a.source_url,
                      a.source_locator, a.retrieved_at, "READY" if a.is_formal else a.alias_status, corpus.version,
                      a.overlay_version))
    plan = iem.graph_plan(corpus, [])
    for c in plan["contributes_to_nutrient"]:
        conn.execute("insert into interaction_form_nutrient_links (relationship_id, form_entity_id, nutrient_entity_id, "
                     "relationship_type, basis, source_id, source_url, source_locator, review_status, corpus_version, "
                     "overlay_version) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (c["relationship_id"] or f"CORPUS_PARENT:{c['form_entity_id']}", c["form_entity_id"],
                      c["nutrient_entity_id"], c["relationship_type"], c["basis"], c["source_id"], c["source_url"],
                      c["source_locator"], c["review_status"], c["corpus_version"], c["overlay_version"]))


@pytest.fixture(scope="module")
def db(pg_uri):
    import psycopg

    base = pg_uri.rsplit("/", 1)[0]
    name = f"bridge_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"create database {name}")
    with psycopg.connect(f"{base}/{name}", autocommit=True) as conn:
        conn.execute(SUPABASE_PRELUDE)
        for path in MIGRATIONS:
            conn.execute(path.read_text(encoding="utf-8"))
        before = {t: _columns(conn, t) for t in ("ingredients", "product_ingredients", "products")}
        conn.execute(MIGRATION_0005.read_text(encoding="utf-8"))
        conn.execute(MIGRATION_0005.read_text(encoding="utf-8"))          # idempotent
        corpus = iem.load_interaction_corpus(overlay=iem.load_reviewed_overlay())
        _load_corpus(conn, corpus)
        pid = stable_id("product", f"TW:{PRODUCT}")
        conn.execute("insert into products (id, product_name_zh, product_name_normalized, source_record_key) "
                     "values (%s, 'TEST_ONLY 產品', 'testonly產品', %s)", (pid, PRODUCT))
        yield conn, corpus, pid, before
    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")


def _insert(conn, pid, **row):
    base = dict(id=str(uuid.uuid4()), product_id=pid, ingredient_source="TFDA_REGISTRATION_INGREDIENTS_RAW",
                token_index=0, raw_text="TEST_ONLY", mapping_status="NO_MATCH", match_method="NONE", confidence=0,
                review_status="NOT_APPLICABLE", corpus_version="1.0")
    base.update(row)
    cols = list(base)
    conn.execute(f"insert into product_ingredient_entity_mappings ({', '.join(cols)}) "
                 f"values ({', '.join(['%s'] * len(cols))})", [base[c] for c in cols])


def _insert_mapping(conn, pid, m):
    _insert(conn, pid, id=m.mapping_id, token_index=m.token_index, raw_text=m.raw_text,
            mapping_status=m.mapping_status.value, entity_id=m.entity_id, entity_type=m.entity_type,
            via_entity_id=m.via_entity_id, relationship_id=m.relationship_id,
            candidate_entity_ids=list(m.candidate_entity_ids), match_method=m.match_method.value,
            matched_alias_ids=list(m.matched_alias_ids), alias_evidence_status=m.alias_evidence_status,
            overlay_precedence=m.overlay_precedence, confidence=m.confidence, review_status=m.review_status.value,
            corpus_version=m.corpus_version, overlay_version=m.overlay_version)


def test_marker_and_existing_tables_unchanged(db):
    conn, _, _, before = db
    assert conn.execute("select value from app_schema_meta where key = 'migration_0005_product_ingredient_entity_mapping'"
                        ).fetchone()[0] == "applied"
    for table, cols in before.items():
        assert _columns(conn, table) == cols


def test_overlay_entities_aliases_and_links_are_storable(db):
    conn, corpus, _, _ = db
    count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    assert count("select count(*) from interaction_corpus_entities where graph_label = 'IngredientForm'") == 61
    assert count("select count(*) from interaction_corpus_entities where entity_origin = 'REVIEWED_OVERLAY'") == 48
    assert count("select count(*) from interaction_corpus_entity_aliases where alias_origin = 'REVIEWED_OVERLAY'") == 250
    assert count("select count(*) from interaction_corpus_entity_aliases where alias_origin = 'REVIEWED_OVERLAY' "
                 "and entity_id is not null") == 191
    assert count("select count(*) from interaction_form_nutrient_links") == 61
    assert count("select count(*) from interaction_form_nutrient_links "
                 "where relationship_type <> 'CONTRIBUTES_TO_NUTRIENT'") == 0
    # form→nutrient 的來源可追溯
    row = conn.execute("select basis, source_id, source_locator from interaction_form_nutrient_links "
                       "where form_entity_id = 'F_ZN_OXIDE'").fetchone()
    assert row[0] == "REVIEWED_OVERLAY" and row[1] and row[2]


def test_mapping_service_rows_satisfy_constraints(db):
    conn, corpus, pid, _ = db
    src = iem.registration_source({"id": pid, "product_category": "imported_tablet_capsule"}, {
        "parser_draft": {"tokens": ["碳酸鈣", "Riboflavin", "氧化鋅", "硬脂酸鎂", "鹽酸呋喃硫胺", "CALCIUM CARBONATE",
                                    "TEST_ONLY GELATIN"]}})
    result = iem.map_product(corpus, src)
    for m in result.mappings:
        _insert_mapping(conn, pid, m)
    rows = conn.execute("select raw_text, mapping_status, is_formal, entity_id, entity_type, alias_evidence_status "
                        "from product_ingredient_entity_mappings where product_id = %s", (pid,)).fetchall()
    assert ("碳酸鈣", "SUPPORTED_ALIAS", True, "F_CA_CARBONATE", "IngredientForm", "VERIFIED_OFFICIAL") in rows
    assert ("碳酸鈣", "FORM_TO_GROUP", True, "N_CA", "NutrientGroup", "VERIFIED_OFFICIAL") in rows
    assert ("Riboflavin", "SUPPORTED_ALIAS", True, "F_RIBOFLAVIN", "IngredientForm", "VERIFIED_OFFICIAL") in rows
    for text in ("硬脂酸鎂", "鹽酸呋喃硫胺"):                                  # overlay 候選：只存候選
        [(status, formal)] = [(s, f) for t, s, f, *_ in rows if t == text]
        assert status in ("AMBIGUOUS", "NEEDS_REVIEW") and formal is False
    view = conn.execute("select entity_id from product_ingredient_entity_contains where product_id = %s", (pid,)).fetchall()
    assert {v[0] for v in view} == {"F_CA_CARBONATE", "F_RIBOFLAVIN", "F_ZN_OXIDE"}   # 不含 FORM_TO_GROUP / 候選


@pytest.mark.parametrize("row", [
    dict(mapping_status="AMBIGUOUS", entity_id="N_CA", entity_type="NutrientGroup", candidate_entity_ids=["N_CA", "N_ZN"],
         review_status="PENDING_REVIEW", match_method="CONFLICTING_ENTITIES", confidence=0.2),
    dict(mapping_status="NEEDS_REVIEW", candidate_entity_ids=["N_ZN"], review_status="AUTO_ACCEPTED_RULE",
         match_method="ALIAS_CONTAINED_IN_TEXT", confidence=0.3),
    dict(mapping_status="NEEDS_REVIEW", review_status="PENDING_REVIEW", match_method="REVIEWED_OVERLAY_CANDIDATE",
         confidence=0.5),                                                        # 候選沒有 entity 也沒有 alias
    dict(mapping_status="VERIFIED_EXACT", entity_id="N_ZN", entity_type="NutrientGroup", review_status="PENDING_REVIEW",
         confidence=1),
    dict(mapping_status="FORM_TO_GROUP", entity_id="N_CA", entity_type="NutrientGroup", review_status="AUTO_ACCEPTED_RULE",
         confidence=1),
    dict(mapping_status="FORM_TO_GROUP", entity_id="F_CA_CARBONATE", entity_type="IngredientForm",
         via_entity_id="F_CA_CARBONATE", review_status="AUTO_ACCEPTED_RULE", confidence=1),
    dict(mapping_status="SUPPORTED_ALIAS", entity_id="N_MG", entity_type="NutrientGroup", review_status="SOURCE_REVIEWED",
         match_method="REVIEWED_OVERLAY_ALIAS_EXACT", alias_evidence_status="AMBIGUOUS", overlay_version="1.0",
         confidence=0.2),                                                        # overlay 候選 alias 不得變成正式
    dict(mapping_status="VERIFIED_EXACT", entity_id="N_ZN", entity_type="NutrientGroup", review_status="AUTO_ACCEPTED_RULE",
         confidence=1, amount_raw="TEST_ONLY", amount_normalized=10, unit="mg"),
])
def test_constraints_reject_unsafe_rows(db, row):
    import psycopg

    conn, _, pid, _ = db
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(conn, pid, **row)


@pytest.mark.parametrize("sql", [
    # SAME_AS / 含量換算 / interaction 繼承都不允許
    "insert into interaction_form_nutrient_links (relationship_id, form_entity_id, nutrient_entity_id, relationship_type, "
    "basis, review_status, corpus_version) values ('TEST_ONLY_R1', 'TEST_ONLY_F', 'N_CA', 'SAME_AS', 'REVIEWED_OVERLAY', "
    "'READY', '1.0')",
    "insert into interaction_form_nutrient_links (relationship_id, form_entity_id, nutrient_entity_id, relationship_type, "
    "basis, review_status, amount_conversion_allowed, corpus_version) values ('TEST_ONLY_R2', 'TEST_ONLY_F', 'N_CA', "
    "'CONTRIBUTES_TO_NUTRIENT', 'REVIEWED_OVERLAY', 'READY', true, '1.0')",
    # parent 不一致（trigger）
    "insert into interaction_form_nutrient_links (relationship_id, form_entity_id, nutrient_entity_id, relationship_type, "
    "basis, review_status, corpus_version) values ('TEST_ONLY_R3', 'TEST_ONLY_F', 'N_ZN', 'CONTRIBUTES_TO_NUTRIENT', "
    "'REVIEWED_OVERLAY', 'READY', '1.0')",
    # overlay 正式 alias 沒有 entity / 候選 alias 有正式 entity
    "insert into interaction_corpus_entity_aliases (alias_id, alias_text, alias_normalized, language, alias_origin, "
    "alias_status, match_policy, source_id, review_status, corpus_version, overlay_version) values ('TEST_ONLY_A1', "
    "'TEST_ONLY', 'test_only', 'en', 'REVIEWED_OVERLAY', 'VERIFIED_OFFICIAL', "
    "'EXACT_NFKC_CASEFOLD_WHITESPACE_AT_DECLARED_LEVEL', 'TEST_ONLY', 'READY', '1.0', '1.0')",
    "insert into interaction_corpus_entity_aliases (alias_id, entity_id, alias_text, alias_normalized, language, "
    "alias_origin, alias_status, match_policy, source_id, review_status, corpus_version, overlay_version) values "
    "('TEST_ONLY_A2', 'N_MG', 'TEST_ONLY', 'test_only', 'en', 'REVIEWED_OVERLAY', 'AMBIGUOUS', "
    "'CANDIDATE_ONLY_NOT_AUTOMERGE', 'TEST_ONLY', 'AMBIGUOUS', '1.0', '1.0')",
    # form 沒有 parent / 營養素群有 parent
    "insert into interaction_corpus_entities (entity_id, entity_type, graph_label, canonical_name_en, identity_level, "
    "source_id, review_status, corpus_version) values ('F_TEST_ONLY_ORPHAN', 'INGREDIENT', 'IngredientForm', 'TEST_ONLY', "
    "'SALT', 'TEST_ONLY', 'READY', '1.0')",
    "insert into interaction_corpus_entities (entity_id, entity_type, graph_label, canonical_name_en, identity_level, "
    "parent_nutrient_entity_id, source_id, review_status, corpus_version) values ('N_TEST_ONLY', 'INGREDIENT', "
    "'NutrientGroup', 'TEST_ONLY', 'NUTRIENT_GROUP', 'N_CA', 'TEST_ONLY', 'READY', '1.0')",
])
def test_corpus_constraints_reject_unsafe_rows(db, sql):
    import psycopg

    conn, _, _, _ = db
    conn.execute("insert into interaction_corpus_entities (entity_id, entity_type, graph_label, canonical_name_en, "
                 "identity_level, parent_nutrient_entity_id, entity_origin, source_id, review_status, corpus_version, "
                 "overlay_version) values ('TEST_ONLY_F', 'INGREDIENT', 'IngredientForm', 'TEST_ONLY', "
                 "'SALT_FAMILY_UNSPECIFIED_HYDRATION', 'N_CA', 'REVIEWED_OVERLAY', 'TEST_ONLY', 'READY', '1.0', '1.0') "
                 "on conflict do nothing")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(sql)


def test_no_same_as_or_interaction_tables(db):
    conn, _, _, _ = db
    tables = {r[0] for r in conn.execute("select table_name from information_schema.tables where table_schema = 'public'")}
    assert not any("interaction_record" in t or "same_as" in t for t in tables)
    defs = json.dumps([r[0] for r in conn.execute(
        "select pg_get_constraintdef(oid) from pg_constraint where conrelid = "
        "'interaction_form_nutrient_links'::regclass")])
    assert "CONTRIBUTES_TO_NUTRIENT" in defs
