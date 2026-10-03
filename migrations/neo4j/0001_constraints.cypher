// nutri_safe_ai（呷安心）Neo4j schema v1
// Neo4j 只存知識關係；節點屬性只放查詢與顯示需要的最小欄位，
// 完整資料以 Supabase 為準，兩邊共用 canonical UUID（*_id）。
//
// 節點
//   (:Product   {product_id, name_zh, status, country_code})
//   (:Ingredient{ingredient_id, name_zh, name_en})
//   (:Company   {company_id, name_zh})
//   (:Brand     {brand_id, name, confidence, evidence_status, is_inferred, inference_basis})  -- = brands.id
//   (:Permit    {permit_id, permit_number, status})
//   (:Claim     {claim_id, function_name})          -- 官方核准保健功效類別（= approved_claims.id）
//   (:Warning   {warning_id, warning_type, text})
//   (:Source    {source_id, source_name, is_official})
//
// 關係（每一種都有明確語意）
//   (Company)-[:OWNS_OR_APPLIES_FOR]->(Product)      公司為該產品的許可申請商
//   (Company)-[:OWNS_BRAND {confidence, evidence_status, is_inferred, inference_basis}]->(Brand)
//                                                    公司擁有該品牌：只限 brands.company_relation = OWNS_BRAND
//                                                    （OFFICIAL_RECORD / MANUAL_REVIEWED 且有 ownership_evidence）
//   (Company)-[:ASSOCIATED_WITH_BRAND {confidence, evidence_status, is_inferred, inference_basis}]->(Brand)
//                                                    公司與品牌有關聯，但未確認擁有（例如由產品名稱推導）
//   一家公司可以連到 0..N 個 Brand。
//   (Product)-[:HAS_BRAND {confidence, evidence_status, is_inferred, inference_basis}]->(Brand)
//                                                    產品屬於該品牌（= product_brands）
//   由產品名稱推導的品牌一律 is_inferred = true，且只能用 ASSOCIATED_WITH_BRAND，不是官方確認的擁有關係。
//   (Product)-[:CONTAINS {raw_text}]->(Ingredient)   官方登記的保健功效相關成分
//   (Product)-[:REGISTERED_AS]->(Permit)             產品對應的官方許可證
//   (Product)-[:HAS_APPROVED_CLAIM {claim_text}]->(Claim)  官方核准的保健功效
//   (Product)-[:HAS_WARNING]->(Warning)              官方登記的警語 / 注意事項
//   (Product)-[:SUPPORTED_BY {source_record_key, source_url}]->(Source)
//   (Claim)-[:SUPPORTED_BY]->(Source)

CREATE CONSTRAINT ja_schema_meta_project IF NOT EXISTS FOR (n:AppSchemaMeta) REQUIRE n.project IS UNIQUE;
CREATE CONSTRAINT ja_product_id IF NOT EXISTS FOR (n:Product) REQUIRE n.product_id IS UNIQUE;
CREATE CONSTRAINT ja_ingredient_id IF NOT EXISTS FOR (n:Ingredient) REQUIRE n.ingredient_id IS UNIQUE;
CREATE CONSTRAINT ja_company_id IF NOT EXISTS FOR (n:Company) REQUIRE n.company_id IS UNIQUE;
CREATE CONSTRAINT ja_permit_id IF NOT EXISTS FOR (n:Permit) REQUIRE n.permit_id IS UNIQUE;
CREATE CONSTRAINT ja_claim_id IF NOT EXISTS FOR (n:Claim) REQUIRE n.claim_id IS UNIQUE;
CREATE CONSTRAINT ja_warning_id IF NOT EXISTS FOR (n:Warning) REQUIRE n.warning_id IS UNIQUE;
CREATE CONSTRAINT ja_brand_id IF NOT EXISTS FOR (n:Brand) REQUIRE n.brand_id IS UNIQUE;
CREATE CONSTRAINT ja_source_id IF NOT EXISTS FOR (n:Source) REQUIRE n.source_id IS UNIQUE;
CREATE INDEX ja_ingredient_name IF NOT EXISTS FOR (n:Ingredient) ON (n.name_zh);

MERGE (m:AppSchemaMeta {project: 'nutri_safe_ai'}) SET m.schema_version = 1, m.updated_at = datetime();
