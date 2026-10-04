// nutri_safe_ai（呷安心）Neo4j schema 0003：Product.product_category（additive）
// 不會自動執行：只由 scripts/import_tfda_product_coverage.py --apply（app/ingestion/tfda_product_coverage.py
// prepare_coverage_graph）在寫入新類別之前執行。
//
// Product.product_category = Supabase products.product_category
//   health_food / domestic_vitamin_tablet_capsule / imported_tablet_capsule
// 既有 health_food Product（由 graph_sync 寫入、當時沒有此屬性）依其 SUPPORTED_BY 的健康食品資料集 Source 補上
// product_category = 'health_food'；只補 NULL，不覆寫已有值。$health_food_source_id = source_registry.id
// （source_key = tw_fda_health_food_dataset），由 importer 從 Supabase 讀取後傳入。

CREATE INDEX ja_product_category IF NOT EXISTS FOR (n:Product) ON (n.product_category);

MATCH (p:Product)-[:SUPPORTED_BY]->(:Source {source_id: $health_food_source_id})
WHERE p.product_category IS NULL
SET p.product_category = 'health_food';
