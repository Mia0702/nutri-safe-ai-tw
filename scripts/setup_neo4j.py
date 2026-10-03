"""建立呷安心 Neo4j constraints 與 schema marker。

用法：python -m scripts.setup_neo4j

若資料庫中已有其他專案的節點 / label，會拒絕執行（請使用全新的 Neo4j instance）。
"""

import sys

from app.config.settings import get_settings
from app.services.errors import ServiceError
from app.services.neo4j_service import Neo4jService


def main() -> int:
    service = Neo4jService(get_settings())
    try:
        applied = service.apply_schema()
    except ServiceError as exc:
        print(f"[neo4j] 拒絕初始化：{exc.message}")
        return 1
    finally:
        service.close()
    for stmt in applied:
        print(f"[neo4j] applied: {stmt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
