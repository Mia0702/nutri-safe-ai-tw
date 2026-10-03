"""依設定組裝 services / agents / pipeline。

缺少 credentials 的服務以 None 代表，pipeline 仍可執行，相關 tool 會回傳 NOT_CONFIGURED。
"""

import logging
from functools import lru_cache

from app.agents.intake_agent import IntakeAgent
from app.agents.reporter_agent import ReporterAgent
from app.agents.verification_agent import VerificationAgent
from app.config.settings import Settings, get_settings
from app.pipeline import AnalysisPipeline
from app.risk.risk_engine import RiskEngine
from app.schemas.enums import ErrorCode
from app.services.context_store import InMemoryConversationStore
from app.services.errors import ServiceError

logger = logging.getLogger("nutri_safe_ai.dependencies")

LINE_DELIVERY_MARGIN_SECONDS = 30.0


class _UnavailableLLM:
    """OpenAI 未設定時使用：任何呼叫都回報 NOT_CONFIGURED，不假裝成功。"""

    def _fail(self, *args, **kwargs):
        raise ServiceError(ErrorCode.NOT_CONFIGURED, "未設定 OPENAI_API_KEY", "openai")

    parse_structured = generate_text = embed = web_search_structured = plan_step = _fail
    describe_image = transcribe_audio = _fail


def build_pipeline(settings: Settings) -> AnalysisPipeline:
    from app.services.openai_service import OpenAIService

    llm = None
    if settings.openai_configured:
        llm = OpenAIService(settings)

    official_repo = rag_store = log_writer = None
    if settings.supabase_configured:
        from app.services.analysis_log import SupabaseAnalysisLogWriter
        from app.services.supabase_service import (
            SupabaseOfficialDataRepository,
            SupabaseRagStore,
            SupabaseService,
        )

        supabase = SupabaseService(settings)
        official_repo = SupabaseOfficialDataRepository(supabase)
        rag_store = SupabaseRagStore(supabase)
        log_writer = SupabaseAnalysisLogWriter(supabase)

    graph_repo = None
    if settings.neo4j_configured:
        from app.services.neo4j_service import Neo4jService
        from app.tools.graph_tools import Neo4jGraphRepository

        graph_repo = Neo4jGraphRepository(Neo4jService(settings))

    reporter = ReporterAgent(llm)
    return AnalysisPipeline(
        intake=IntakeAgent(llm or _UnavailableLLM()),
        verifier=VerificationAgent(
            official_repo=official_repo,
            graph_repo=graph_repo,
            rag_store=rag_store,
            llm=llm,
            rag_top_k=settings.rag_top_k,
            rag_min_similarity=settings.rag_min_similarity,
        ),
        risk_engine=RiskEngine(),
        reporter=reporter,
        log_writer=log_writer,
        conversation_store=InMemoryConversationStore(),
    )


@lru_cache
def get_pipeline() -> AnalysisPipeline:
    return build_pipeline(get_settings())


@lru_cache
def get_task_context_store():
    from app.services.context_store import TaskContextStore

    return TaskContextStore()


@lru_cache
def get_response_service():
    """LINE webhook 使用：一則 primary Flex + request-scoped 按鈕 + 任務型短期上下文。"""
    from app.response.service import AdaptiveResponseService
    from app.response.store import InMemoryRequestStore

    pipeline = get_pipeline()
    return AdaptiveResponseService(pipeline, pipeline.reporter, InMemoryRequestStore(),
                                   timeout_seconds=get_settings().response_timeout_seconds,
                                   context_store=get_task_context_store())


@lru_cache
def get_line_handler():
    from app.line.client import LineMessagingClient
    from app.line.handler import LineWebhookHandler

    settings = get_settings()
    if not settings.line_configured:
        raise ServiceError(ErrorCode.NOT_CONFIGURED, "未設定 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN", "line")
    from app.line.client import CONTENT_DEADLINE_SECONDS
    from app.line.request_queue import PerUserRequestQueue

    # watchdog：下載 + pipeline 時限 + 送出餘裕；超過即送 ERROR card 並釋放該 user queue
    job_timeout = CONTENT_DEADLINE_SECONDS + settings.response_timeout_seconds + LINE_DELIVERY_MARGIN_SECONDS
    return LineWebhookHandler(get_response_service(), LineMessagingClient(settings.line_channel_access_token),
                              get_task_context_store(), queue=PerUserRequestQueue(job_timeout_seconds=job_timeout))
