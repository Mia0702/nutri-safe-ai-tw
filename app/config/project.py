"""專案識別碼（唯一定義處）。

PROJECT_ID 同時用於：
* Supabase app_schema_meta.project marker（migrations/0001_initial_schema.sql）
* Neo4j (:AppSchemaMeta {project}) marker（migrations/neo4j/0001_constraints.cypher）
* logger 名稱與對外 HTTP User-Agent
tests/unit/test_migration_schema.py 會檢查 migration 檔與此常數一致。
"""

PROJECT_ID = "nutri_safe_ai"
USER_AGENT = f"{PROJECT_ID}-seed/0.1"
