"""LINE Official Account webhook endpoint。

只做三件事：驗證 X-Line-Signature、解析事件、排進 per-user FIFO queue，然後立刻回 200。
真正的分析與回覆在 queue worker 中進行（app/line/handler.py）。
"""

import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.config.settings import Settings, get_settings
from app.dependencies import get_line_handler
from app.line.events import parse_events
from app.line.signature import verify_signature

logger = logging.getLogger("nutri_safe_ai.line.webhook")

router = APIRouter(tags=["line"])


@router.post("/line/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
):
    if not settings.line_configured:
        raise HTTPException(status_code=503, detail="LINE channel 未設定")
    body = await request.body()
    if not verify_signature(settings.line_channel_secret.get_secret_value(), body, x_line_signature):
        raise HTTPException(status_code=400, detail="invalid signature")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid body") from None
    handler = get_line_handler()
    accepted = [rid for rid in (handler.dispatch(e) for e in parse_events(payload)) if rid]
    logger.info("line_webhook_received", extra={"channel": "line", "result_count": len(accepted)})
    return {"status": "ok"}
