"""Tool 共用的結果型別與執行包裝。

每個 Tool：
- 輸入是明確的參數
- 輸出是 ToolResult[T]（ok / data / error）
- 不產生最終使用者回答
"""

import logging
import time
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel

from app.schemas.enums import ErrorCode
from app.schemas.evidence import ToolCallRecord, ToolError
from app.services.errors import ServiceError

logger = logging.getLogger("nutri_safe_ai.tools")

T = TypeVar("T")


class ToolResult(BaseModel, Generic[T]):
    tool_name: str
    ok: bool
    data: T | None = None
    error: ToolError | None = None
    duration_ms: int = 0


def _result_count(data: Any) -> int:
    if isinstance(data, tuple) and data:  # registry handler 回傳 (data, summary)
        data = data[0]
    if data is None:
        return 0
    if isinstance(data, list):
        return len(data)
    count = getattr(data, "result_count", None)
    if isinstance(count, int):
        return count
    return 1


def execute_tool(tool_name: str, fn: Callable[[], T]) -> ToolResult[T]:
    start = time.perf_counter()
    try:
        data = fn()
        return ToolResult[Any](
            tool_name=tool_name, ok=True, data=data, duration_ms=int((time.perf_counter() - start) * 1000)
        )
    except ServiceError as exc:
        error = exc.to_tool_error()
    except Exception as exc:  # noqa: BLE001 - tool 不可讓整條 pipeline crash
        logger.exception("tool %s 發生未預期錯誤", tool_name)
        error = ToolError(code=ErrorCode.INTERNAL_ERROR, message=f"{type(exc).__name__}", source=tool_name)
    return ToolResult[Any](
        tool_name=tool_name, ok=False, error=error, duration_ms=int((time.perf_counter() - start) * 1000)
    )


class ToolCallRecorder:
    """Agent 用來記錄每一次 tool 呼叫（tools_called / observability）。"""

    def __init__(self, request_id: str, agent: str = "verification"):
        self.request_id = request_id
        self.agent = agent
        self.records: list[ToolCallRecord] = []

    def call(
        self, tool_name: str, arguments: dict[str, Any], fn: Callable[[], T], planned_by: str = "agent"
    ) -> ToolResult[T]:
        result = execute_tool(tool_name, fn)
        record = ToolCallRecord(
            tool_name=tool_name,
            arguments=arguments,
            planned_by=planned_by,
            ok=result.ok,
            duration_ms=result.duration_ms,
            result_count=_result_count(result.data),
            error=result.error,
        )
        self.records.append(record)
        logger.info(
            "tool_called",
            extra={
                "request_id": self.request_id,
                "agent": self.agent,
                "tool_called": f"{tool_name}[{planned_by}]",
                "tool_duration_ms": result.duration_ms,
                "result_count": record.result_count,
                "error_code": result.error.code if result.error else None,
            },
        )
        return result

    @property
    def errors(self) -> list[ToolError]:
        return [r.error for r in self.records if r.error is not None]
