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
    describe_image = describe_images_structured = transcribe_audio = _fail


def build_pipeline(settings: Settings) -> AnalysisPipeline:
    from app.services.openai_service import OpenAIService

    llm = None
    if settings.openai_configured:
        llm = OpenAIService(settings)

    official_repo = rag_store = log_writer = claim_policy_repo = interaction_repo = None
    if settings.supabase_configured:
        from app.services.analysis_log import SupabaseAnalysisLogWriter
        from app.services.supabase_service import (
            SupabaseClaimPolicyRepository,
            SupabaseInteractionRepository,
            SupabaseOfficialDataRepository,
            SupabaseRagStore,
            SupabaseService,
        )

        supabase = SupabaseService(settings)
        official_repo = SupabaseOfficialDataRepository(supabase)
        rag_store = SupabaseRagStore(supabase)
        claim_policy_repo = SupabaseClaimPolicyRepository(supabase)
        interaction_repo = SupabaseInteractionRepository(supabase)
        log_writer = SupabaseAnalysisLogWriter(supabase)

    graph_repo = None
    if settings.neo4j_configured:
        from app.services.neo4j_service import Neo4jService
        from app.tools.graph_tools import Neo4jGraphRepository

        graph_repo = Neo4jGraphRepository(Neo4jService(settings))

    reporter = ReporterAgent(llm)
    vocabulary = None
    if official_repo is not None and interaction_repo is not None:  # 語音詞彙提示：只用已審核的 Interaction Corpus 名稱
        from app.services.asr_vocabulary import InteractionVocabularyProvider

        vocabulary = InteractionVocabularyProvider(official_repo.get_ingredient_bridge_reference, interaction_repo.get_drug_reference)
    return AnalysisPipeline(
        intake=IntakeAgent(llm or _UnavailableLLM(), vocabulary_provider=vocabulary),
        verifier=VerificationAgent(
            official_repo=official_repo,
            graph_repo=graph_repo,
            rag_store=rag_store,
            llm=llm,
            rag_top_k=settings.rag_top_k,
            rag_min_similarity=settings.rag_min_similarity,
            claim_policy_repo=claim_policy_repo,
            interaction_repo=interaction_repo,
        ),
        risk_engine=RiskEngine(),
        reporter=reporter,
        log_writer=log_writer,
        conversation_store=InMemoryConversationStore(),
    )


@lru_cache
def get_pipeline() -> AnalysisPipeline:
    return build_pipeline(get_settings())


def build_task_context_store(settings):
    """依 LINE_STATE_BACKEND 選擇 TaskContext 的儲存位置（與 RequestStore / ClaimStore 同一個旗標、同一個 HMAC secret）。"""
    from app.services.context_store import TaskContextStore

    if settings.line_state_backend != "supabase":
        return TaskContextStore()
    from app.state.backend import SupabaseStateBackend
    from app.state.context_backend import SupabaseTaskContextBackend
    from app.state.owner_key import build_owner_key_hasher

    return TaskContextStore(backend=SupabaseTaskContextBackend(SupabaseStateBackend(settings)),
                            owner_key=build_owner_key_hasher(settings))


@lru_cache
def get_task_context_store():
    return build_task_context_store(get_settings())


def build_request_store(settings):
    """依 LINE_STATE_BACKEND 選擇 RequestStore；預設 memory，只有明確設為 supabase 才建立持久化 store。"""
    from app.response.store import InMemoryRequestStore

    if settings.line_state_backend != "supabase":
        return InMemoryRequestStore()
    from app.state.backend import SupabaseStateBackend
    from app.state.owner_key import build_owner_key_hasher
    from app.state.request_store import SupabaseRequestStore

    return SupabaseRequestStore(SupabaseStateBackend(settings), build_owner_key_hasher(settings))


def build_claim_store(settings):
    """依 LINE_STATE_BACKEND 選擇 ClaimStore（與 RequestStore 同一個旗標）；預設 memory。"""
    from app.state.claims import InMemoryClaimStore, SupabaseClaimStore

    if settings.line_state_backend != "supabase":
        return InMemoryClaimStore()
    from app.state.backend import SupabaseStateBackend

    return SupabaseClaimStore(SupabaseStateBackend(settings))


@lru_cache
def get_claim_store():
    return build_claim_store(get_settings())


@lru_cache
def get_request_store():
    return build_request_store(get_settings())


@lru_cache
def get_response_service():
    """LINE webhook 使用：一則 primary Flex + request-scoped 按鈕 + 任務型短期上下文。"""
    from app.response.service import AdaptiveResponseService

    pipeline = get_pipeline()
    return AdaptiveResponseService(pipeline, pipeline.reporter, get_request_store(),
                                   timeout_seconds=get_settings().response_timeout_seconds,
                                   context_store=get_task_context_store(), claim_store=get_claim_store())


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
                              get_task_context_store(), queue=PerUserRequestQueue(job_timeout_seconds=job_timeout),
                              image_burst_window_ms=settings.auto_image_burst_window_ms, claim_store=get_claim_store())
