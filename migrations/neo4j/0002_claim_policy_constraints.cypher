// nutri_safe_ai（呷安心）Neo4j schema 0002：TFDA Claim Policy Corpus（additive）
// 只新增有關係價值的節點；完整內容（條文原文、例示、RAG 區段）以 Supabase 為準，不做成 graph node。
//
// 節點（*_id = Supabase canonical UUID；另保存 corpus 原始 ID）
//   (:ClaimPolicyRule {claim_policy_rule_id, rule_id, policy_effect, rule_category, claim_category,
//                      status, review_status, evidence_use, article_or_attachment})   -- = claim_policy_rules.id
//   (:HealthFunction  {health_function_id, function_id, official_function_name,
//                      status, review_status, evidence_use})                           -- = health_function_catalog.id
//   (:Source          {source_id, ...})  沿用 0001：source_id = source_registry.id（corpus 來源也登記在 source_registry）
//
// 關係
//   (ClaimPolicyRule)-[:SUPPORTED_BY {article_or_attachment}]->(Source)   規則出自該官方來源
//   (HealthFunction)-[:DEFINED_BY {role}]->(Source)                       role = CATALOG（功效項目公告）
//                                                                         / EVALUATION_METHOD（現行評估方法，定義適用範圍）
//   (HealthFunction)-[:HAS_HISTORICAL_METHOD]->(Source)                   已被取代的舊評估方法（版本追溯）
//   (Claim)-[:MAPS_TO_HEALTH_FUNCTION {mapping_method}]->(HealthFunction) 只有 deterministic mapping 才建立：
//                                                                         approved_claims.function_name 與官方功效名稱正規化後完全相同

CREATE CONSTRAINT ja_claim_policy_rule_id IF NOT EXISTS FOR (n:ClaimPolicyRule) REQUIRE n.claim_policy_rule_id IS UNIQUE;
CREATE CONSTRAINT ja_claim_policy_rule_corpus_id IF NOT EXISTS FOR (n:ClaimPolicyRule) REQUIRE n.rule_id IS UNIQUE;
CREATE CONSTRAINT ja_health_function_id IF NOT EXISTS FOR (n:HealthFunction) REQUIRE n.health_function_id IS UNIQUE;
CREATE CONSTRAINT ja_health_function_corpus_id IF NOT EXISTS FOR (n:HealthFunction) REQUIRE n.function_id IS UNIQUE;
