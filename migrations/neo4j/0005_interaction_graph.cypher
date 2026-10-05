// nutri_safe_ai（呷安心）Neo4j schema 0005：Interaction graph（PROPOSAL，未執行）
//
// Supabase 是 canonical；Neo4j 只放 traversal 需要的 identity / 分類欄位，不放 recommendation / clinical_effect 全文。
//   (:DrugSubstance {entity_id})                       D_*；不是藥品商品
//   (:Interaction {interaction_id, ...})               25 筆 pairwise 臨床 / 生理交互作用
//   (:DuplicateIntakeRule {interaction_id, ...})       17 筆重複攝取模板（獨立 label，不是有害交互作用）
//   (:InteractionEvidence {evidence_id, ...})
//   (:InteractionSource {source_id, ...})              與既有 (:Source)（Product / Claim 來源）分開，避免 ID namespace 碰撞
// 關係：
//   (NutrientGroup | IngredientForm | DrugSubstance)-[:PARTICIPATES_IN {role}]->(Interaction | DuplicateIntakeRule)
//   (Interaction | DuplicateIntakeRule)-[:HAS_EVIDENCE {supports_direction, evidence_use}]->(InteractionEvidence)
//     中性關係：evidence 可能 SUPPORTS / NO_SIGNIFICANT_EFFECT / MIXED，方向只由 supports_direction 結構化欄位表達（不拆關係類型）。
//   (InteractionEvidence)-[:FROM_SOURCE]->(InteractionSource)
//   NutrientGroup / IngredientForm 節點沿用 0004（Ingredient Bridge）；本檔不建立它們。
// 不建立：SAME_AS、Product-[:INTERACTS_WITH]->Product、Product→Interaction、藥品商品節點。

CREATE CONSTRAINT ja_drug_substance_id IF NOT EXISTS FOR (n:DrugSubstance) REQUIRE n.entity_id IS UNIQUE;
CREATE CONSTRAINT ja_interaction_id IF NOT EXISTS FOR (n:Interaction) REQUIRE n.interaction_id IS UNIQUE;
CREATE CONSTRAINT ja_duplicate_intake_rule_id IF NOT EXISTS FOR (n:DuplicateIntakeRule) REQUIRE n.interaction_id IS UNIQUE;
CREATE CONSTRAINT ja_interaction_evidence_id IF NOT EXISTS FOR (n:InteractionEvidence) REQUIRE n.evidence_id IS UNIQUE;
CREATE CONSTRAINT ja_interaction_source_id IF NOT EXISTS FOR (n:InteractionSource) REQUIRE n.source_id IS UNIQUE;
