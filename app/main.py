from fastapi import FastAPI

from app.api import debug, line_webhook
from app.config.settings import get_settings
from app.observability import configure_logging

configure_logging(get_settings())

app = FastAPI(
    title="呷安心",
    description="基於 Multi-Agent 協作架構之保健食品安全辨識平台",
    version="0.1.0",
)
app.include_router(debug.router)
app.include_router(line_webhook.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "project": "呷安心",
        "phase": "greenfield",
    }
