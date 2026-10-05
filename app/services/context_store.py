"""對話 context 暫存：讓「它」「這個」可以指回先前對話中的產品。

* InMemoryConversationStore：debug API 用（conversation_id → 最近幾回合）。
* TaskContextStore：LINE 用的任務型短期上下文（per user、TTL 30 分鐘、最近 5 題、可重設 / 結束）。
皆為 in-memory（單一 process）；只保存解析指稱所需的最少資訊：使用者訊息文字與系統辨識出的實體。
"""

import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from threading import Lock

from pydantic import BaseModel, Field

from app.schemas.enums import EntityType, Intent
from app.schemas.user_request import (
    ConfirmedProduct,
    ContextEntity,
    ConversationContext,
    ConversationTurn,
    PendingClarification,
)

MAX_TURNS = 5
MAX_CONVERSATIONS = 1000


class InMemoryConversationStore:
    def __init__(self, max_turns: int = MAX_TURNS, max_conversations: int = MAX_CONVERSATIONS):
        self._data: OrderedDict[str, list[ConversationTurn]] = OrderedDict()
        self._max_turns = max_turns
        self._max_conversations = max_conversations
        self._lock = Lock()

    def get(self, conversation_id: str) -> ConversationContext:
        with self._lock:
            turns = list(self._data.get(conversation_id, []))
        return ConversationContext(conversation_id=conversation_id, turns=turns)

    def append(self, conversation_id: str, turn: ConversationTurn) -> None:
        with self._lock:
            turns = self._data.pop(conversation_id, [])
            turns.append(turn)
            self._data[conversation_id] = turns[-self._max_turns :]
            while len(self._data) > self._max_conversations:
                self._data.popitem(last=False)


# ---------------------------------------------------------------- LINE：任務型短期上下文

TASK_CONTEXT_TTL = timedelta(minutes=30)
TASK_MAX_REQUESTS = 5
TASK_MAX_ENTITIES = 10
TASK_MAX_USER_TEXT_CHARS = 300
TASK_MAX_USERS = 5000
# 系統問「是不是這一項」後，使用者的「是 / 不是」只在這段時間內有效（且只對緊接的下一題有效）
PENDING_CLARIFICATION_TTL = timedelta(minutes=10)


class TaskContext(BaseModel):
    """一位 LINE 使用者「目前這次查證」的短期上下文（不是長期 memory）。

    只保存解析「這個 / 它 / 那罐 / 剛才那個」所需資訊；30 分鐘未互動、按「開始新問題」或
    「結束本次查證」即清除。session_id 用來丟棄舊 session 晚到的結果，避免寫進新的對話。
    """

    user_id: str
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    current_product_id: str | None = None
    current_product_name: str | None = None
    # 已確認的 canonical 產品（product_id / permit / 官方品名 / 確認來源 / 時間 / request_id）：
    # 後續指稱的 authoritative referent，由 pipeline 在 Agent B 之前綁定（app/services/context_binding.py）
    confirmed_product: ConfirmedProduct | None = None
    # 上一題正在等使用者回答的產品確認（只對緊接的下一題有效；任何一題寫回 context 時都會被取代 / 清除）
    pending_clarification: PendingClarification | None = None
    recent_confirmed_entities: list[ContextEntity] = Field(default_factory=list)
    last_intent: Intent | None = None
    recent_request_ids: list[str] = Field(default_factory=list)
    turns: list[ConversationTurn] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def conversation_context(self) -> ConversationContext:
        """交給 Agent A 的前文：目前產品排在最前面，其餘依新到舊。"""
        turns = list(self.turns)
        if self.current_product_name:
            current = ContextEntity(entity_type=EntityType.PRODUCT, name=self.current_product_name,
                                    entity_id=self.current_product_id)
            turns.append(ConversationTurn(user_text="", entities=[current]))  # known_entities() 由新到舊，放最後 = 最優先
        return ConversationContext(conversation_id=self.session_id, turns=turns,
                                   confirmed_product=self.confirmed_product)


class TaskContextStore:
    """per-user 任務型 context（in-memory、單一 process；prototype limitation，見 CURRENT_STATE）。"""

    def __init__(self, ttl: timedelta = TASK_CONTEXT_TTL, max_requests: int = TASK_MAX_REQUESTS,
                 max_users: int = TASK_MAX_USERS, clock=lambda: datetime.now(timezone.utc),
                 pending_ttl: timedelta = PENDING_CLARIFICATION_TTL):
        self._data: OrderedDict[str, TaskContext] = OrderedDict()
        self._ttl = ttl
        self._max_requests = max_requests
        self._max_users = max_users
        self._clock = clock
        self._pending_ttl = pending_ttl
        self._lock = Lock()

    def _live(self, user_id: str) -> TaskContext | None:
        ctx = self._data.get(user_id)
        if ctx is not None and self._clock() - ctx.updated_at > self._ttl:
            del self._data[user_id]
            return None
        pending = ctx.pending_clarification if ctx is not None else None
        if pending is not None and self._clock() - pending.created_at > self._pending_ttl:
            ctx.pending_clarification = None
        return ctx

    def snapshot(self, user_id: str) -> TaskContext:
        """取得目前 task context 的複本；沒有或已過期就開一個新的乾淨 session。"""
        with self._lock:
            ctx = self._live(user_id)
            if ctx is None:
                ctx = TaskContext(user_id=user_id, updated_at=self._clock())
                self._data[user_id] = ctx
                while len(self._data) > self._max_users:
                    self._data.popitem(last=False)
            return ctx.model_copy(deep=True)

    def record(self, user_id: str, session_id: str, request_id: str, user_text: str,
               intent: Intent | None, entities: list[ContextEntity],
               confirmed: ConfirmedProduct | None = None, clear_confirmed: bool = False,
               pending: PendingClarification | None = None, clear_products: bool = False) -> bool:
        """把一題的結果寫回 context；session 已被重設 / 過期時丟棄（回傳 False）。

        pending：這一題留下的產品確認問題；每一題都會取代上一題的 pending（None = 清除）。

        confirmed：這一題確認的 canonical 產品（取代先前的）；clear_confirmed：使用者明確改問其他產品但未確認，
        舊的已確認產品不可再當成「它」。兩者皆無時保留原本的 confirmed product。
        """
        with self._lock:
            ctx = self._live(user_id)
            if ctx is None or ctx.session_id != session_id:
                return False
            n = self._max_requests
            ctx.turns = (ctx.turns + [ConversationTurn(user_text=user_text[:TASK_MAX_USER_TEXT_CHARS],
                                                       entities=entities)])[-n:]
            ctx.recent_request_ids = (ctx.recent_request_ids + [request_id])[-n:]
            ctx.last_intent = intent
            products = [e for e in entities if e.entity_type == EntityType.PRODUCT]
            if products:  # 已查證（有 canonical ID）的產品優先
                product = next((e for e in products if e.entity_id), products[0])
                ctx.current_product_id, ctx.current_product_name = product.entity_id, product.name
            merged: list[ContextEntity] = []
            for e in [x for x in entities if x.entity_id] + ctx.recent_confirmed_entities:  # 新的在前，同名去重
                if all((m.entity_type, m.name) != (e.entity_type, e.name) for m in merged):
                    merged.append(e)
            ctx.recent_confirmed_entities = merged[:TASK_MAX_ENTITIES]
            if confirmed is not None:
                ctx.confirmed_product = confirmed
                ctx.current_product_id, ctx.current_product_name = confirmed.product_id, confirmed.official_product_name
            elif clear_confirmed:
                ctx.confirmed_product = None
            if clear_products:  # 兩個以上產品的比較：沒有單一「它」；清掉所有產品指稱（不從中挑一個，也不沿用更早的產品）
                ctx.confirmed_product = None
                ctx.current_product_id = ctx.current_product_name = None
                ctx.recent_confirmed_entities = [e for e in ctx.recent_confirmed_entities if e.entity_type != EntityType.PRODUCT]
                ctx.turns = [t.model_copy(update={"entities": [e for e in t.entities if e.entity_type != EntityType.PRODUCT]})
                             for t in ctx.turns]
            ctx.pending_clarification = (pending.model_copy(update={"created_at": self._clock()})
                                         if pending is not None else None)
            ctx.updated_at = self._clock()
            self._data.move_to_end(user_id)
            return True

    def clear_pending(self, user_id: str, session_id: str) -> None:
        """使用者否定候選 / 改問新產品：清掉 pending clarification（其餘 context 不變）。"""
        with self._lock:
            ctx = self._live(user_id)
            if ctx is not None and ctx.session_id == session_id:
                ctx.pending_clarification = None
                ctx.updated_at = self._clock()

    def reset(self, user_id: str) -> TaskContext:
        """開始新問題：清除目前 task context，建立新的乾淨 session。"""
        with self._lock:
            ctx = TaskContext(user_id=user_id, updated_at=self._clock())
            self._data[user_id] = ctx
            return ctx.model_copy(deep=True)

    def close(self, user_id: str) -> None:
        """結束本次查證：關閉 session 並清除暫存 context。"""
        with self._lock:
            self._data.pop(user_id, None)
