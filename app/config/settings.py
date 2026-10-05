"""應用程式設定。

所有 secrets 只從環境變數 / .env 讀取，不得 hard-code。
驗證錯誤訊息不得包含任何 secret 的值。
"""

import re
from functools import lru_cache

from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Supabase project URL 只接受 https://<project-ref>.supabase.co（不含 /rest/v1 或任何路徑）
SUPABASE_URL_RE = re.compile(r"^https://[a-z0-9]+\.supabase\.co$")
# Supabase 新式後端金鑰（secret key）一律以 sb_secret_ 開頭
SUPABASE_SECRET_KEY_PREFIX = "sb_secret_"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # 驗證錯誤不顯示輸入值，避免把錯誤格式的 key / token 印到 console 或 log
        hide_input_in_errors=True,
    )

    app_env: str = "development"
    log_level: str = "INFO"

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-6-luna"
    openai_transcription_model: str = "gpt-transcribe"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_timeout_seconds: float = 60.0
    # 官方網站 fallback 單次上限（不重試）：必須遠小於 response_timeout_seconds，慢的搜尋只讓該 tool 逾時，不拖垮整則回覆
    openai_web_search_timeout_seconds: float = 40.0
    # 語音轉文字單次上限（不重試）：轉錄卡住只讓這題回 ERROR card，不拖住 user queue
    openai_transcription_timeout_seconds: float = 30.0

    line_channel_secret: SecretStr | None = None
    line_channel_access_token: SecretStr | None = None
    # LINE 一題超過此秒數即回 ERROR card、晚到結果丟棄（實測 live pipeline 約 25–75 秒）
    response_timeout_seconds: float = Field(default=120.0, gt=0, le=300)
    # Automatic Image Burst Gate：LINE 相簿一次選多張照片時，連續到達的圖片在這個 window（毫秒）內合成一組；0 = 停用
    auto_image_burst_window_ms: int = Field(default=1800, ge=0, le=10000)

    supabase_url: str | None = None
    # 後端專用 Supabase secret key（sb_secret_...）；不得回傳給 client
    supabase_secret_key: SecretStr | None = None
    # legacy SUPABASE_SERVICE_ROLE_KEY 不再使用：只偵測是否存在以便提示，值會立即丟棄
    supabase_service_role_key: SecretStr | None = Field(default=None, exclude=True, repr=False)

    neo4j_uri: str | None = None
    neo4j_username: str | None = None
    neo4j_password: SecretStr | None = None
    neo4j_database: str | None = None

    rag_top_k: int = Field(default=5, ge=1, le=20)
    rag_min_similarity: float = Field(default=0.3, ge=0.0, le=1.0)

    _legacy_supabase_key_detected: bool = PrivateAttr(default=False)

    @model_validator(mode="before")
    @classmethod
    def _drop_blank_values(cls, data):
        # .env 中空白的變數視為未設定，讓 default 生效。
        if isinstance(data, dict):
            return {
                key: (value.strip() if isinstance(value, str) else value)
                for key, value in data.items()
                if not (isinstance(value, str) and not value.strip())
            }
        return data

    @field_validator("supabase_url")
    @classmethod
    def _validate_supabase_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not SUPABASE_URL_RE.match(value):
            raise ValueError("SUPABASE_URL 必須是 https://<project-ref>.supabase.co（不可包含 /rest/v1 或其他路徑）")
        return value

    @field_validator("supabase_secret_key")
    @classmethod
    def _validate_supabase_secret_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        if not value.get_secret_value().startswith(SUPABASE_SECRET_KEY_PREFIX):
            # 不在訊息中帶出任何 key 內容
            raise ValueError(
                "SUPABASE_SECRET_KEY 格式不正確：必須是 Supabase 後端 secret key（sb_secret_ 開頭），"
                "不可使用 publishable key 或 legacy service_role JWT"
            )
        return value

    @model_validator(mode="after")
    def _discard_legacy_supabase_key(self):
        self._legacy_supabase_key_detected = self.supabase_service_role_key is not None
        object.__setattr__(self, "supabase_service_role_key", None)
        return self

    @property
    def is_production(self) -> bool:
        return (self.app_env or "").lower() == "production"

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.get_secret_value())

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key)

    @property
    def legacy_supabase_key_detected(self) -> bool:
        """環境中仍設定 legacy SUPABASE_SERVICE_ROLE_KEY（不會被使用）。"""
        return self._legacy_supabase_key_detected

    @property
    def neo4j_configured(self) -> bool:
        return bool(self.neo4j_uri and self.neo4j_username and self.neo4j_password)

    @property
    def line_configured(self) -> bool:
        return bool(self.line_channel_secret and self.line_channel_access_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
