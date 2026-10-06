"""外部服務共用的結構化錯誤。"""

from app.schemas.enums import ErrorCode
from app.schemas.evidence import ToolError


class ServiceError(Exception):
    """外部服務錯誤，帶結構化錯誤碼，不包含任何 secret。"""

    def __init__(self, code: ErrorCode, message: str, source: str, reason: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.source = source
        self.reason = reason

    def to_tool_error(self) -> ToolError:
        return ToolError(code=self.code, message=self.message, source=self.source, reason=self.reason)
