"""TaskContext <-> JSON-safe dict（明確 schema；不使用 pickle）。

* 原始 LINE user id（TaskContext.user_id）不放進 JSON；擁有者由 owner_key 欄位承載，讀回時由呼叫端帶入。
* 不含圖片 / 語音 bytes（TaskContext 本來就不保存）。
* 讀到未知版本 / 多餘或缺少欄位 / 型別錯誤 → StateSerializationError（fail closed），不回傳部分資料。
"""

from typing import Any

from pydantic import BaseModel, ValidationError

from app.state.errors import StateSerializationError

SCHEMA_VERSION = 1
_BODY = "context"


def serialize_context(ctx: BaseModel) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, _BODY: ctx.model_dump(mode="json", exclude={"user_id"})}


def deserialize_context(payload: Any, model: type[BaseModel], user_id: str) -> BaseModel:
    try:
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION or set(payload) != {"schema_version", _BODY}:
            raise ValueError("unknown schema")
        body = payload[_BODY]
        if not isinstance(body, dict) or "user_id" in body:
            raise ValueError("bad body")
        return model.model_validate({**body, "user_id": user_id})
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateSerializationError(f"持久化的 task context 無法還原：{type(exc).__name__}") from exc
