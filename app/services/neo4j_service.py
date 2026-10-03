"""Neo4j 連線與 schema 防護。

連線後先確認 (:AppSchemaMeta {project: PROJECT_ID}) 存在，
避免查詢或寫入其他專案的圖資料庫。
"""

import logging
from pathlib import Path
from typing import Any

from app.config.project import PROJECT_ID
from app.config.settings import Settings
from app.schemas.enums import ErrorCode
from app.services.errors import ServiceError

logger = logging.getLogger(__name__)

PROJECT_MARKER = PROJECT_ID
SCHEMA_FILE = Path(__file__).resolve().parents[2] / "migrations" / "neo4j" / "0001_constraints.cypher"
ALLOWED_LABELS = {
    "AppSchemaMeta", "Product", "Ingredient", "Company", "Brand", "Permit", "Claim", "Warning", "Source",
}


def load_schema_statements(path: Path = SCHEMA_FILE) -> list[str]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.strip().startswith("//")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


class Neo4jService:
    def __init__(self, settings: Settings):
        if not settings.neo4j_configured:
            raise ServiceError(ErrorCode.NOT_CONFIGURED, "未設定 Neo4j 連線資訊", "neo4j")
        from neo4j import GraphDatabase

        self._database = settings.neo4j_database
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password.get_secret_value()),
            connection_timeout=15,
        )
        self._schema_checked = False

    def close(self) -> None:
        self._driver.close()

    def _run(self, query: str, params: dict[str, Any] | None = None, write: bool = False) -> list[dict]:
        try:
            records, _, _ = self._driver.execute_query(
                query,
                params or {},
                database_=self._database,
                routing_="w" if write else "r",
            )
        except Exception as exc:  # noqa: BLE001
            raise ServiceError(ErrorCode.UNAVAILABLE, f"Neo4j 查詢失敗：{type(exc).__name__}", "neo4j") from exc
        return [record.data() for record in records]

    def is_project_database(self) -> bool:
        rows = self._run(
            "MATCH (m:AppSchemaMeta {project: $p}) RETURN m.schema_version AS v", {"p": PROJECT_MARKER}
        )
        return bool(rows)

    def foreign_labels(self) -> list[str]:
        """資料庫中不屬於本專案 schema 的 label（用來偵測連到舊 DB）。"""
        rows = self._run("CALL db.labels() YIELD label RETURN label")
        return sorted(r["label"] for r in rows if r["label"] not in ALLOWED_LABELS)

    def ensure_schema(self) -> None:
        if self._schema_checked:
            return
        if not self.is_project_database():
            raise ServiceError(
                ErrorCode.SCHEMA_NOT_INITIALIZED,
                "Neo4j 尚未建立呷安心 schema marker，請先執行 scripts/setup_neo4j.py",
                "neo4j",
            )
        self._schema_checked = True

    def read(self, query: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.ensure_schema()
        return self._run(query, params)

    def write(self, query: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.ensure_schema()
        return self._run(query, params, write=True)

    def apply_schema(self, *, allow_non_empty: bool = False) -> list[str]:
        """建立 constraints 與 schema marker。

        若資料庫已有不屬於本專案的 label，拒絕執行，避免與其他專案資料混在一起。
        """
        foreign = self.foreign_labels()
        if foreign and not allow_non_empty:
            raise ServiceError(
                ErrorCode.SCHEMA_NOT_INITIALIZED,
                f"Neo4j 中有非本專案的 label：{', '.join(foreign[:10])}，請改用全新的 Neo4j instance",
                "neo4j",
            )
        if not self.is_project_database():
            count = self._run("MATCH (n) RETURN count(n) AS c")[0]["c"]
            if count and not allow_non_empty:
                raise ServiceError(
                    ErrorCode.SCHEMA_NOT_INITIALIZED,
                    f"Neo4j 已有 {count} 個節點但沒有本專案 marker，拒絕初始化",
                    "neo4j",
                )
        applied = []
        for stmt in load_schema_statements():
            self._run(stmt, write=True)
            applied.append(stmt.splitlines()[0][:80])
        self._schema_checked = True
        return applied
