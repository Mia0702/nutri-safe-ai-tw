"""LINE Messaging API 最小 client（reply / push / 下載訊息內容 / loading animation / 驗證訊息格式）。

token 只放在 Authorization header，不寫進 log 或例外訊息。
"""

import logging
import time
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

logger = logging.getLogger("nutri_safe_ai.line.client")

API_BASE = "https://api.line.me"
DATA_API_BASE = "https://api-data.line.me"
MAX_CONTENT_BYTES = 25 * 1024 * 1024
LOADING_SECONDS_MAX = 60
CONTENT_ENDPOINT = "/v2/bot/message/{id}/content"
# 下載圖片 / 語音（含等待 LINE 轉檔）的總時限；超過即視為下載失敗，不讓單一 request 卡住 user queue
CONTENT_DEADLINE_SECONDS = 20.0
TRANSCODING_POLL_SECONDS = 1.0


class LineApiError(Exception):
    def __init__(self, status_code: int, endpoint: str, detail: str = ""):
        super().__init__(f"LINE API {endpoint} 回傳 {status_code}")
        self.status_code = status_code
        self.endpoint = endpoint
        self.detail = detail[:500]  # LINE 的錯誤說明（不含 token）


class LineMessenger(Protocol):
    def reply(self, reply_token: str, messages: list[dict[str, Any]]) -> None: ...

    def push(self, to: str, messages: list[dict[str, Any]], retry_key: str | None = None) -> None: ...

    def get_content(self, message_id: str, deadline_seconds: float = CONTENT_DEADLINE_SECONDS) -> tuple[bytes, str]: ...

    def start_loading(self, chat_id: str, seconds: int = LOADING_SECONDS_MAX) -> None: ...


class LineMessagingClient:
    def __init__(self, access_token: SecretStr, http: httpx.Client | None = None, timeout: float = 15.0):
        self._token = access_token
        self._http = http or httpx.Client(timeout=timeout)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.get_secret_value()}", **(extra or {})}

    def _post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        resp = self._http.post(f"{API_BASE}{path}", json=body, headers=self._headers(headers))
        if resp.status_code >= 400:
            # 409 + retry key = 同一則 push 已送過（冪等），不視為失敗
            if resp.status_code == 409 and headers and "X-Line-Retry-Key" in headers:
                return
            logger.warning("line_api_error", extra={"error_code": f"{path}:{resp.status_code}"})
            raise LineApiError(resp.status_code, path, resp.text)

    def reply(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        self._post("/v2/bot/message/reply", {"replyToken": reply_token, "messages": messages})

    def push(self, to: str, messages: list[dict[str, Any]], retry_key: str | None = None) -> None:
        headers = {"X-Line-Retry-Key": retry_key} if retry_key else None
        self._post("/v2/bot/message/push", {"to": to, "messages": messages}, headers)

    def start_loading(self, chat_id: str, seconds: int = LOADING_SECONDS_MAX) -> None:
        seconds = max(5, min(LOADING_SECONDS_MAX, seconds - seconds % 5))
        self._post("/v2/bot/chat/loading/start", {"chatId": chat_id, "loadingSeconds": seconds})

    def validate_reply(self, messages: list[dict[str, Any]]) -> None:
        """只驗證訊息格式、不會送出（E2E 用來確認 Flex JSON 被 LINE 接受）。"""
        self._post("/v2/bot/message/validate/reply", {"messages": messages})

    def get_content(self, message_id: str, deadline_seconds: float = CONTENT_DEADLINE_SECONDS) -> tuple[bytes, str]:
        """下載使用者傳來的圖片 / 語音；整體有時間上限，失敗一律拋 LineApiError（不會無限等待）。

        語音 / 影片在 LINE 端轉檔完成前，content API 回 202 且沒有內容：
        改查 transcoding 狀態，直到 succeeded 或超過時限（不把 202 的空內容當成成功）。
        """
        deadline = time.monotonic() + deadline_seconds
        url = f"{DATA_API_BASE}/v2/bot/message/{message_id}/content"
        try:
            while True:
                with self._http.stream("GET", url, headers=self._headers(),
                                       timeout=self._remaining_timeout(deadline)) as resp:
                    if resp.status_code == 202:
                        pass  # 仍在轉檔：離開 with 後輪詢狀態
                    elif resp.status_code >= 400:
                        raise LineApiError(resp.status_code, CONTENT_ENDPOINT)
                    else:
                        body = b""
                        for chunk in resp.iter_bytes():
                            body += chunk
                            if len(body) > MAX_CONTENT_BYTES:
                                raise LineApiError(413, CONTENT_ENDPOINT)
                            if time.monotonic() > deadline:
                                raise LineApiError(408, CONTENT_ENDPOINT, "content download deadline exceeded")
                        if not body:
                            raise LineApiError(204, CONTENT_ENDPOINT, "empty content")
                        content_type = resp.headers.get("content-type", "application/octet-stream")
                        return body, content_type.split(";")[0].strip().lower()
                self._wait_transcoding(message_id, deadline)
        except httpx.TimeoutException as exc:
            raise LineApiError(408, CONTENT_ENDPOINT, "content download timeout") from exc
        except httpx.HTTPError as exc:
            raise LineApiError(503, CONTENT_ENDPOINT, type(exc).__name__) from exc

    def _wait_transcoding(self, message_id: str, deadline: float) -> None:
        url = f"{DATA_API_BASE}/v2/bot/message/{message_id}/content/transcoding"
        while True:
            if time.monotonic() + TRANSCODING_POLL_SECONDS > deadline:
                raise LineApiError(408, CONTENT_ENDPOINT, "content still transcoding")
            time.sleep(TRANSCODING_POLL_SECONDS)
            resp = self._http.get(url, headers=self._headers(), timeout=self._remaining_timeout(deadline))
            if resp.status_code >= 400:
                raise LineApiError(resp.status_code, CONTENT_ENDPOINT + "/transcoding")
            status = (resp.json() or {}).get("status")
            if status == "succeeded":
                return
            if status == "failed":
                raise LineApiError(422, CONTENT_ENDPOINT + "/transcoding", "transcoding failed")

    @staticmethod
    def _remaining_timeout(deadline: float) -> float:
        return max(1.0, deadline - time.monotonic())
