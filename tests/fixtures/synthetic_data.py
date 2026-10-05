"""TEST_ONLY — synthetic fixture data.

這裡的產品、公司、成分、許可證字號全部是虛構的測試資料，
只能用於 unit test，絕對不可匯入正式 Supabase / Neo4j。
"""

from app.normalization import normalize_name

TEST_ONLY = True

SOURCE = {
    "source_name": "TEST_ONLY 虛構官方來源",
    "source_url": "https://example.invalid/test-only",
    "is_official": True,
    "retrieved_at": "2026-01-01T00:00:00+00:00",
}


def _product(pid, name, company_id, company, permit_code, status="ACTIVE"):
    return {
        "product_id": pid,
        "country_code": "TW",
        "product_name_zh": name,
        "product_name_en": None,
        "product_name_normalized": normalize_name(name),
        "status": status,
        "company_id": company_id,
        "company_name": company,
        "company_name_normalized": normalize_name(company),
        "permit_id": f"permit-{pid}",
        "permit_number": f"測試健食字第{permit_code}號",
        "permit_code": permit_code,
        "permit_type": "健食字",
        "approval_date": "2020-01-01",
        "source_record_key": f"TEST-{permit_code}",
        **SOURCE,
    }


PRODUCTS = [
    _product("p-1", "虛構牌晶亮測試膠囊", "c-1", "虛構測試生技股份有限公司", "Z00001"),
    _product("p-2", "虛構牌順暢測試優酪乳", "c-1", "虛構測試生技股份有限公司", "Z00002"),
    _product("p-3", "另一虛構保健測試錠", "c-2", "範例測試食品有限公司", "Z00003", status="EXPIRED"),
]

INGREDIENTS = [
    {"id": "i-1", "name_zh": "虛構素甲", "name_en": "fictionin-a", "name_normalized": normalize_name("虛構素甲"), **SOURCE},
    {"id": "i-2", "name_zh": "測試乳酸菌", "name_en": "test lactobacillus", "name_normalized": normalize_name("測試乳酸菌"), **SOURCE},
]

PRODUCT_INGREDIENTS = [
    {"product_id": "p-1", "ingredient_id": "i-1", "raw_text": "虛構素甲 10 mg"},
    {"product_id": "p-2", "ingredient_id": "i-2", "raw_text": "測試乳酸菌"},
]

APPROVED_CLAIMS = [
    {"product_claim_id": "pc-1", "product_id": "p-1", "approved_claim_id": "ac-1",
     "function_name": "測試功效甲", "claim_text": "經測試證實：有助於維持測試機能甲。", **SOURCE},
    {"product_claim_id": "pc-2", "product_id": "p-2", "approved_claim_id": "ac-2",
     "function_name": "胃腸功能改善", "claim_text": "經測試證實：有助於改善胃腸功能。", **SOURCE},
]

WARNINGS = [
    {"product_id": "p-1", "warning_id": "w-1", "warning_type": "PRECAUTION",
     "text": "健康食品不具治療、矯正人類疾病之醫療效能。", **SOURCE},
]

ILLEGAL_ADS = [
    {"id": "ad-1", "product_name": "另一虛構保健測試錠", "product_name_normalized": normalize_name("另一虛構保健測試錠"),
     "company_name": "範例測試食品有限公司", "company_name_normalized": normalize_name("範例測試食品有限公司"),
     "violation_content": "TEST_ONLY 宣稱可治療疾病", "violation_type": "涉及醫療效能", "penalty_date": "2025-01-01", **SOURCE},
]

# 品牌「虛構牌」由同公司產品名稱前綴推導（TEST_ONLY）；低信心別名不應被自動採用
BRANDS = [
    {"id": "b-1", "name": "虛構牌", "company_id": "c-1", "confidence": 0.8},
    {"id": "b-2", "name": "低信心", "company_id": "c-2", "confidence": 0.5},
]

ALIASES = [
    {"id": "a-1", "entity_type": "BRAND", "entity_id": "b-1", "alias": "虛構牌", "alias_normalized": normalize_name("虛構牌"),
     "alias_type": "DERIVED_BRAND_PREFIX", "confidence": 0.8, "brand_company_id": "c-1",
     "evidence_status": "INFERRED", "inference_basis": {"rule": "shared_product_name_prefix", "shared": 2, "total": 2},
     "brand_is_inferred": True, **SOURCE},
    {"id": "a-2", "entity_type": "COMPANY", "entity_id": "c-1", "alias": "虛構測試生技",
     "alias_normalized": normalize_name("虛構測試生技"), "alias_type": "SHORT_NAME", "confidence": 0.9,
     "evidence_status": "INFERRED", "inference_basis": {"rule": "strip_legal_form"}, **SOURCE},
    {"id": "a-3", "entity_type": "INGREDIENT", "entity_id": "i-1", "alias": "fictionin-a",
     "alias_normalized": normalize_name("fictionin-a"), "alias_type": "ENGLISH_NAME", "confidence": 1.0,
     "evidence_status": "OFFICIAL_RECORD", "inference_basis": {"rule": "official_english_name"}, **SOURCE},
    {"id": "a-4", "entity_type": "BRAND", "entity_id": "b-2", "alias": "低信心", "alias_normalized": normalize_name("低信心"),
     "alias_type": "DERIVED_BRAND_PREFIX", "confidence": 0.5, "brand_company_id": "c-2",
     "evidence_status": "INFERRED", "inference_basis": {"rule": "shared_product_name_prefix", "shared": 2, "total": 2},
     "brand_is_inferred": True, **SOURCE},
]

RAG_CHUNKS = [
    {"chunk_id": "rc-1", "document_id": "rd-1", "chunk_index": 0,
     "content": "TEST_ONLY 虛構規範：食品不得宣稱具有治療或預防疾病之效能。",
     "similarity": 0.82, "title": "TEST_ONLY 虛構規範文件", "source_url": "https://example.invalid/rule",
     "source_name": "TEST_ONLY 虛構主管機關", "document_type": "regulation", "is_official": True,
     "retrieved_at": "2026-01-01T00:00:00+00:00"},
]

# Ingredient Bridge（TEST_ONLY）：預設沒有完成匯入的 bridge、沒有 mapping；個別測試以 monkeypatch 提供虛構資料
INGREDIENT_BRIDGE: dict = {}
INGREDIENT_ENTITY_MAPPINGS: list = []
INGREDIENT_STAGING: dict = {}  # product_id → raw_data.ingredient_staging（登記類官方成分原文）
