// nutri_safe_ai（呷安心）Neo4j schema 0004：Product Ingredient Mapping Bridge（PROPOSAL，未執行）
//
// Interaction Corpus＋reviewed overlay 的 ingredient entity：
//   (:NutrientGroup {entity_id})   N_*（營養素群，不是單一化學 substance）
//   (:IngredientForm {entity_id, entity_origin})  F_*（特定 salt / vitamer / substance / mixture；
//                                                  entity_origin = BASE_CORPUS / REVIEWED_OVERLAY）
// 關係：
//   (Product)-[:CONTAINS {source, raw_text, mapping_status, match_method, confidence, review_status, alias_ids,
//                        alias_evidence_status, amount_raw, amount_normalized, unit, amount_basis,
//                        corpus_version, overlay_version}]->(IngredientForm|NutrientGroup)
//     只來自 VERIFIED_EXACT / SUPPORTED_ALIAS（Supabase view product_ingredient_entity_contains）；
//     AMBIGUOUS / NEEDS_REVIEW 不建立任何邊。
//   (IngredientForm)-[:CONTRIBUTES_TO_NUTRIENT {basis, relationship_id, source_id, source_url, source_locator,
//                                              review_status, corpus_version, overlay_version}]->(NutrientGroup)
//     basis = CORPUS_PARENT_NUTRIENT / REVIEWED_OVERLAY；不是 SAME_AS、不是等量換算、不繼承 interaction。
// 不建立：SAME_AS、Product-[:INTERACTS_WITH]->Product、Product→Interaction、Interaction / Evidence node（下一輪）。
// 既有 (Product)-[:CONTAINS]->(Ingredient)（TFDA canonical 成分）不變；以目標 label 區分。

CREATE CONSTRAINT ja_nutrient_group_id IF NOT EXISTS FOR (n:NutrientGroup) REQUIRE n.entity_id IS UNIQUE;
CREATE CONSTRAINT ja_ingredient_form_id IF NOT EXISTS FOR (n:IngredientForm) REQUIRE n.entity_id IS UNIQUE;
