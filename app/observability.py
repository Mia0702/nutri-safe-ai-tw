"""結構化 logging。

* 每筆 log 以 JSON 輸出，只保留白名單欄位，避免意外把 token / key 寫進 log。
* 另外對訊息做 secret 遮蔽：任何已設定的 secret 值出現在訊息中都會被替換。
"""

import json
import logging
from datetime import datetime, timezone

from app.config.settings import Settings

LOG_FIELDS = (
    "request_id",
    "input_type",
    "intent",
    "agent",
    "tool_called",
    "tool_duration_ms",
    "result_count",
    "evidence_count",
    "product_verification",
    "evidence_strength",
    "risk_result",
    "error_code",
    "claim_count",
    "duration_ms",
    "channel",
    "stage",
    # deterministic product resolver / fast path（只有代碼與 ID，不含使用者原文）
    "candidate_count",
    "selected_product_id",
    "match_type",
    "match_confidence",
    "ambiguity_reason",
    "fast_path_eligible",
    "fast_path_reason",
    "planner_mode",
    # modality parity（語音轉文字後是否走與文字相同的語意契約）
    "semantic_input_type",
    "claim_roles",
    "parity_path_used",
    "transcription_completed",
    # URL source role / stage latency（只有代碼與毫秒數）
    "url_source_roles",
    "official_statement_count",
    "assessment_count",
    "fetch_ms",
    "vision_ms",
    "transcription_ms",
    "claim_count_observed",
    "semantic_extraction_ms",
    "product_resolution_ms",
    "db_kg_ms",
    "rag_ms",
    "planner_ms",
    "assessment_ms",
    "official_web_ms",
    "claim_policy_ms",
    # pending clarification（只有代碼）
    "dialogue_act",
)


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***REDACTED***")
        return text

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": self._redact(record.getMessage()),
        }
        for field in LOG_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value if isinstance(value, (int, float, bool)) else self._redact(str(value))
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    secrets = [
        s.get_secret_value()
        for s in (
            settings.openai_api_key,
            settings.supabase_secret_key,
            settings.neo4j_password,
            settings.line_channel_secret,
            settings.line_channel_access_token,
        )
        if s is not None
    ]
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(secrets))
    root = logging.getLogger("nutri_safe_ai")
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    root.propagate = False
    for name in ("app",):
        logging.getLogger(name).handlers = [handler]
        logging.getLogger(name).setLevel(settings.log_level)
        logging.getLogger(name).propagate = False
    # 第三方套件可能在 debug log 中印出 header，固定在 WARNING
    for noisy in ("httpx", "httpcore", "openai", "neo4j", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
