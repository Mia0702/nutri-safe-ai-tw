"""Migration 與程式碼的一致性檢查（不需要資料庫）。

確保 SQL CHECK 列舉值、schema marker、RLS 範圍與 Python 端定義一致，
避免 migration 與程式各改各的。
"""

import re
from enum import StrEnum
from pathlib import Path

import pytest

from app.config.project import PROJECT_ID
from app.schemas.enums import (
    AliasType,
    ClaimAssessmentStatus,
    ClaimOrigin,
    ClaimSemanticCategory,
    EntityType,
    EvidenceStatus,
    EvidenceStrength,
    InputType,
    ProductStatus,
    ProductVerificationStatus,
    RiskLevel,
)
from app.services import neo4j_service, supabase_service

SQL = Path("migrations/0001_initial_schema.sql").read_text(encoding="utf-8")
CYPHER = Path("migrations/neo4j/0001_constraints.cypher").read_text(encoding="utf-8")


def check_values(column: str) -> list[set[str]]:
    """取出所有 `check (<column> in (...))` 的值集合。"""
    results = []
    for m in re.finditer(rf"check\s*\(\s*{column}\s+in\s*\(", SQL):
        depth, i = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(SQL[i], 0)
            i += 1
        results.append(set(re.findall(r"'([^']+)'", SQL[m.end(): i - 1])))
    return results


@pytest.mark.parametrize("column,enum", [
    ("origin", ClaimOrigin),
    ("semantic_category", ClaimSemanticCategory),
    ("assessment_status", ClaimAssessmentStatus),
    ("entity_type", EntityType),
    ("alias_type", AliasType),
    ("status", ProductStatus),
    ("product_verification", ProductVerificationStatus),
    ("evidence_strength", EvidenceStrength),
    ("risk_level", RiskLevel),
    ("input_type", InputType),
    ("evidence_status", EvidenceStatus),
])
def test_sql_check_values_match_python_enums(column: str, enum: type[StrEnum]):
    found = check_values(column)
    assert found, f"migration 缺少 {column} 的 CHECK constraint"
    for values in found:
        assert values == {e.value for e in enum}, column


def test_observed_claims_origin_distinguishes_question_from_product_claim():
    [origins] = check_values("origin")
    assert {"USER_QUESTION", "USER_ASSERTION", "PRODUCT_LABEL_CLAIM", "ADVERTISEMENT_CLAIM", "THIRD_PARTY_CLAIM"} <= origins
    assert "is_product_side_assertion boolean generated always as" in SQL
    assert "origin in ('PRODUCT_LABEL_CLAIM', 'ADVERTISEMENT_CLAIM')" in SQL


def test_project_marker_is_shared_by_migrations_and_code():
    assert PROJECT_ID == "nutri_safe_ai"
    assert supabase_service.PROJECT_MARKER == PROJECT_ID
    assert neo4j_service.PROJECT_MARKER == PROJECT_ID
    assert f"('project', '{PROJECT_ID}')" in SQL
    assert f"{{project: '{PROJECT_ID}'}}" in CYPHER
    assert "ji_anxin" not in SQL and "ji_anxin" not in CYPHER


def _created_tables() -> set[str]:
    return set(re.findall(r"create table if not exists (\w+)", SQL))


def test_every_table_has_rls_and_service_role_grant():
    match = re.search(r"tables text\[\] := array\[(.*?)\];", SQL, re.S)
    rls_tables = set(re.findall(r"'(\w+)'", match.group(1)))
    assert rls_tables == _created_tables()


def test_updated_at_is_maintained_by_generic_trigger():
    assert "create or replace function set_updated_at()" in SQL
    assert "c.column_name = 'updated_at'" in SQL
    assert "execute function set_updated_at()" in SQL


def test_line_user_reference_is_unambiguous():
    assert "line_user_record_id uuid references line_users(id)" in SQL
    assert not re.search(r"line_user_id\s+uuid", SQL)
    assert "line_user_id text not null unique" in SQL


def test_rag_dedupe_does_not_rely_on_nullable_url():
    assert "unique (source_url, content_hash)" not in SQL
    assert "constraint rag_documents_content_hash_unique unique (content_hash)" in SQL
    assert "where is_current and source_url_normalized is not null" in SQL


def test_product_identity_is_not_name_based():
    products = SQL[SQL.index("create table if not exists products"):SQL.index("create index if not exists idx_products_name_norm")]
    assert "unique (permit_id)" in products and "unique (source_id, source_record_key)" in products
    assert "unique (product_name" not in products


def test_entity_aliases_has_one_fk_target_per_row():
    for column in ("product_id", "brand_id", "company_id", "ingredient_id"):
        assert f"{column} uuid references" in SQL[SQL.index("create table if not exists entity_aliases"):]
    assert "num_nonnulls(product_id, brand_id, company_id, ingredient_id) = 1" in SQL


def test_inferred_brand_and_alias_require_basis_and_are_flagged():
    for table in ("brands", "product_brands", "entity_aliases"):
        block = SQL[SQL.index(f"create table if not exists {table}"):]
        block = block[: block.index(");\n")]
        assert "evidence_status text not null" in block, table
        assert "is_inferred boolean generated always as (evidence_status = 'INFERRED') stored" in block, table
        assert "inference_basis jsonb not null" in block, table
        assert "evidence_status <> 'INFERRED' or (inference_basis <> '{}'::jsonb and confidence < 1)" in block, table
    assert "alias_type <> 'DERIVED_BRAND_PREFIX' or evidence_status = 'INFERRED'" in SQL
    assert "'source_is_official', s.is_official" in SQL  # 資料集官方 ≠ 別名官方確認


def test_brand_company_relation_owns_requires_ownership_evidence():
    assert "add column if not exists company_relation text not null default 'ASSOCIATED_WITH_BRAND'" in SQL
    assert "check (company_relation in ('OWNS_BRAND', 'ASSOCIATED_WITH_BRAND'))" in SQL
    assert "(evidence_status <> 'INFERRED' and company_id is not null and inference_basis ? 'ownership_evidence')" in SQL
    assert "'brand_company_relation', b.company_relation" in SQL


def test_preserved_requirements():
    assert "vector(1536)" in SQL
    assert "enable row level security" in SQL
    assert "to service_role" in SQL
    assert "raw_record_id uuid references raw_import_records(id)" in SQL


def test_evidence_kinds_written_by_app_are_allowed():
    [kinds] = check_values("evidence_kind")
    written = Path("app/services/analysis_log.py").read_text(encoding="utf-8")
    for kind in re.findall(r'"evidence_kind": "(\w+)"', written) + re.findall(r'\("(\w+)", bundle\.', written):
        assert kind in kinds, kind
