"""開發用 debug endpoint：不經 LINE，直接測試整條 Agent pipeline。

APP_ENV=production 時此 endpoint 回傳 404。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config.settings import Settings, get_settings
from app.dependencies import get_pipeline
from app.pipeline import AnalysisPipeline
from app.schemas.analysis import AnalysisResult
from app.schemas.enums import ErrorCode, InputType
from app.schemas.user_request import RawUserInput

router = APIRouter(prefix="/api/debug", tags=["debug"])

# JSON debug endpoint 只接受文字與網址（網址由 Agent A 的 fetch_url_content 讀取）；
# image / audio 需要二進位內容，由 LINE channel 傳入 RawUserInput.media
SUPPORTED_INPUT_TYPES = {InputType.TEXT, InputType.URL}


class DebugAnalyzeRequest(BaseModel):
    type: InputType
    content: str = Field(min_length=1, max_length=6000)
    conversation_id: str | None = Field(
        default=None, max_length=100, description="同一個 conversation_id 會帶入前文，用來解析「它」「這個」"
    )


def _require_dev(settings: Settings = Depends(get_settings)) -> None:
    if settings.is_production:
        raise HTTPException(status_code=404)


@router.post("/analyze", response_model=AnalysisResult, dependencies=[Depends(_require_dev)])
def analyze(body: DebugAnalyzeRequest, pipeline: AnalysisPipeline = Depends(get_pipeline)):
    if body.type not in SUPPORTED_INPUT_TYPES:
        return JSONResponse(
            status_code=501,
            content={
                "request_id": str(uuid.uuid4()),
                "errors": [{
                    "code": ErrorCode.UNSUPPORTED_INPUT_TYPE.value,
                    "message": f"debug JSON endpoint 不支援 {body.type.value}（只支援 text / url）",
                    "source": "debug_api",
                }],
            },
        )
    raw = (RawUserInput(input_type=body.type, urls=[body.content.strip()]) if body.type == InputType.URL
           else RawUserInput(input_type=body.type, text=body.content))
    return pipeline.run(raw, channel="debug_api", conversation_id=body.conversation_id)
