"""OpenAI API 的唯一出入口。

Agent 與 Tool 只透過這個 service 呼叫 OpenAI，方便 mock、統一錯誤處理與 timeout。
"""

import base64
import logging
from typing import Any, Protocol, TypeVar

import openai
from openai import OpenAI
from pydantic import BaseModel

from app.config.settings import Settings
from app.schemas.enums import ErrorCode
from app.services.errors import ServiceError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class WebSearchSource(BaseModel):
    url: str
    title: str | None = None


class WebSearchResult(BaseModel):
    parsed: Any
    sources: list[WebSearchSource]


class PlannedCall(BaseModel):
    """LLM 提出的一個 function call（尚未經過 allowlist / 參數驗證）。"""

    call_id: str
    name: str
    arguments: str  # JSON 字串


class PlanStepResult(BaseModel):
    response_id: str | None = None
    calls: list[PlannedCall] = []
    text: str = ""


class LLMClient(Protocol):
    """Agent / Tool 依賴的介面；測試用 fake 實作同一介面。"""

    def parse_structured(self, *, instructions: str, input_text: str, output_model: type[T]) -> T: ...

    def generate_text(self, *, instructions: str, input_text: str) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def plan_step(
        self,
        *,
        instructions: str,
        input_items: str | list[dict[str, Any]],
        tools: list[dict[str, Any]],
        previous_response_id: str | None = None,
        tool_choice: str = "auto",
    ) -> PlanStepResult: ...

    def web_search_structured(
        self,
        *,
        instructions: str,
        input_text: str,
        output_model: type[T],
        allowed_domains: list[str],
    ) -> WebSearchResult: ...


class MediaClient(Protocol):
    """Agent A 的 Input Tools（analyze_image / transcribe_audio）依賴的介面。"""

    def describe_image(self, *, instructions: str, image: bytes, mime_type: str) -> str: ...

    def describe_images_structured(
        self, *, instructions: str, images: list[tuple[bytes, str]], output_model: type[T]
    ) -> T: ...

    def transcribe_audio(self, *, audio: bytes, filename: str) -> str: ...


def _wrap_openai_error(exc: Exception) -> ServiceError:
    if isinstance(exc, openai.APITimeoutError):
        return ServiceError(ErrorCode.TIMEOUT, "OpenAI 請求逾時", "openai")
    if isinstance(exc, openai.APIConnectionError):
        return ServiceError(ErrorCode.UNAVAILABLE, "無法連線到 OpenAI", "openai")
    if isinstance(exc, openai.APIStatusError):
        return ServiceError(
            ErrorCode.UPSTREAM_ERROR, f"OpenAI 回傳錯誤狀態 {exc.status_code}", "openai"
        )
    return ServiceError(ErrorCode.UPSTREAM_ERROR, f"OpenAI 呼叫失敗：{type(exc).__name__}", "openai")


class OpenAIService:
    def __init__(self, settings: Settings):
        if not settings.openai_configured:
            raise ServiceError(ErrorCode.NOT_CONFIGURED, "未設定 OPENAI_API_KEY", "openai")
        self._settings = settings
        self._client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.openai_timeout_seconds,
            max_retries=1,
        )

    @property
    def raw_client(self) -> OpenAI:
        return self._client

    def parse_structured(self, *, instructions: str, input_text: str, output_model: type[T]) -> T:
        try:
            response = self._client.responses.parse(
                model=self._settings.openai_model,
                instructions=instructions,
                input=input_text,
                text_format=output_model,
            )
        except Exception as exc:  # noqa: BLE001 - 統一轉成 ServiceError
            raise _wrap_openai_error(exc) from exc
        parsed = response.output_parsed
        if parsed is None:
            raise ServiceError(ErrorCode.UPSTREAM_ERROR, "OpenAI 沒有回傳可解析的結構化結果", "openai")
        return parsed

    def generate_text(self, *, instructions: str, input_text: str) -> str:
        try:
            response = self._client.responses.create(
                model=self._settings.openai_model,
                instructions=instructions,
                input=input_text,
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap_openai_error(exc) from exc
        text = (response.output_text or "").strip()
        if not text:
            raise ServiceError(ErrorCode.UPSTREAM_ERROR, "OpenAI 回傳空白內容", "openai")
        return text

    def describe_image(self, *, instructions: str, image: bytes, mime_type: str) -> str:
        data_url = f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"
        try:
            response = self._client.responses.create(
                model=self._settings.openai_model,
                instructions=instructions,
                input=[{"role": "user", "content": [{"type": "input_image", "image_url": data_url}]}],
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap_openai_error(exc) from exc
        text = (response.output_text or "").strip()
        if not text:
            raise ServiceError(ErrorCode.UPSTREAM_ERROR, "OpenAI 圖片理解回傳空白內容", "openai")
        return text

    def describe_images_structured(
        self, *, instructions: str, images: list[tuple[bytes, str]], output_model: type[T]
    ) -> T:
        """一次 Vision request 傳入所有圖片（每張前面標示 Image k），回傳結構化觀察。"""
        content: list[dict[str, Any]] = []
        for index, (image, mime_type) in enumerate(images, start=1):
            content.append({"type": "input_text", "text": f"Image {index}"})
            content.append({"type": "input_image",
                            "image_url": f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"})
        try:
            response = self._client.responses.parse(
                model=self._settings.openai_model, instructions=instructions,
                input=[{"role": "user", "content": content}], text_format=output_model)
        except Exception as exc:  # noqa: BLE001
            raise _wrap_openai_error(exc) from exc
        parsed = response.output_parsed
        if parsed is None:
            raise ServiceError(ErrorCode.UPSTREAM_ERROR, "OpenAI 沒有回傳可解析的圖片觀察", "openai")
        return parsed

    def transcribe_audio(self, *, audio: bytes, filename: str) -> str:
        try:
            # 不重試：預設 timeout × 重試會讓語音一題卡到整題逾時
            response = self._client.with_options(max_retries=0).audio.transcriptions.create(
                model=self._settings.openai_transcription_model, file=(filename, audio),
                timeout=self._settings.openai_transcription_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap_openai_error(exc) from exc
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            raise ServiceError(ErrorCode.UPSTREAM_ERROR, "OpenAI 語音轉文字回傳空白內容", "openai")
        return text

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._client.embeddings.create(
                model=self._settings.openai_embedding_model, input=texts
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap_openai_error(exc) from exc
        return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]

    def plan_step(
        self,
        *,
        instructions: str,
        input_items: str | list[dict[str, Any]],
        tools: list[dict[str, Any]],
        previous_response_id: str | None = None,
        tool_choice: str = "auto",
    ) -> PlanStepResult:
        """Agent B 的一輪 tool planning：只回傳 LLM 想呼叫的 function，不執行任何東西。"""
        try:
            response = self._client.responses.create(
                model=self._settings.openai_model,
                instructions=instructions,
                input=input_items,
                tools=tools,
                tool_choice=tool_choice,
                previous_response_id=previous_response_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap_openai_error(exc) from exc
        calls = [
            PlannedCall(call_id=item.call_id, name=item.name, arguments=item.arguments or "{}")
            for item in response.output
            if getattr(item, "type", None) == "function_call"
        ]
        return PlanStepResult(response_id=response.id, calls=calls, text=response.output_text or "")

    def web_search_structured(
        self,
        *,
        instructions: str,
        input_text: str,
        output_model: type[T],
        allowed_domains: list[str],
    ) -> WebSearchResult:
        """使用 Responses API web_search，限制只能搜尋 allowed_domains。"""
        try:
            # 不重試：逾時重試會讓最壞情況變成 2 倍，超過 LINE 一題的處理時限
            response = self._client.with_options(max_retries=0).responses.parse(
                model=self._settings.openai_model,
                instructions=instructions,
                input=input_text,
                text_format=output_model,
                tools=[
                    {
                        "type": "web_search",
                        "filters": {"allowed_domains": allowed_domains},
                        "user_location": {"type": "approximate", "country": "TW"},
                    }
                ],
                include=["web_search_call.action.sources"],
                timeout=self._settings.openai_web_search_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap_openai_error(exc) from exc
        return WebSearchResult(parsed=response.output_parsed, sources=_collect_web_sources(response))


def _collect_web_sources(response: Any) -> list[WebSearchSource]:
    """從 web_search_call 與 url_citation annotation 收集模型實際看過的來源。"""
    sources: dict[str, WebSearchSource] = {}
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "web_search_call":
            action = getattr(item, "action", None)
            for src in getattr(action, "sources", None) or []:
                url = getattr(src, "url", None)
                if url:
                    sources.setdefault(url, WebSearchSource(url=url))
        elif item_type == "message":
            for content in getattr(item, "content", None) or []:
                for ann in getattr(content, "annotations", None) or []:
                    if getattr(ann, "type", None) == "url_citation" and getattr(ann, "url", None):
                        sources[ann.url] = WebSearchSource(url=ann.url, title=getattr(ann, "title", None))
    return list(sources.values())
