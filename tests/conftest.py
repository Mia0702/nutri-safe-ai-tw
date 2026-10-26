"""測試共用設定。

TEST_TASK_CONTEXT_BACKEND=supabase_dict：讓整個測試套件改用「經過序列化的 dict-backed 持久化 TaskContext backend」
（與 production 的 Supabase 實作同一份程式碼，僅最底層以 dict 代替 PostgREST），用來回歸驗證持久化路徑不改變任何既有行為。
預設不啟用。
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _task_context_backend_switch(monkeypatch):
    if os.environ.get("TEST_TASK_CONTEXT_BACKEND") != "supabase_dict":
        return
    from app.services import context_store
    from app.state.context_backend import SupabaseTaskContextBackend
    from tests.context_flows import HASH
    from tests.state_fixtures import DictStateBackend

    original = context_store.TaskContextStore.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("backend", SupabaseTaskContextBackend(DictStateBackend()))
        kwargs.setdefault("owner_key", HASH)
        original(self, *args, **kwargs)

    monkeypatch.setattr(context_store.TaskContextStore, "__init__", init)
