"""測試共用設定。

TEST_TASK_CONTEXT_BACKEND=supabase_dict：讓整個測試套件改用「經過序列化的 dict-backed 持久化 TaskContext backend」
（與 production 的 Supabase 實作同一份程式碼，僅最底層以 dict 代替 PostgREST），用來回歸驗證持久化路徑不改變任何既有行為。
預設不啟用。
"""

import logging
import os

import pytest

# app.observability.configure_logging（app.main import 時 / test_settings 會呼叫）把這兩個 logger 設成 propagate=False 且從不還原，
# 之後 pytest 的 caplog（掛在 root logger）收不到任何結構化 log（stderr 仍看得到）。runtime 的 logging 設定不變；
# 測試期間讓它們 propagate 到 root，handlers（JSON stderr 輸出）保留。
_PROJECT_LOGGERS = ("nutri_safe_ai", "app")


@pytest.fixture(autouse=True)
def _project_loggers_propagate_to_caplog(monkeypatch):
    from app import observability

    original = observability.configure_logging

    def configure_then_propagate(settings):
        original(settings)
        for name in _PROJECT_LOGGERS:
            logging.getLogger(name).propagate = True

    monkeypatch.setattr(observability, "configure_logging", configure_then_propagate)
    saved = {name: logging.getLogger(name).propagate for name in _PROJECT_LOGGERS}
    for name in _PROJECT_LOGGERS:
        logging.getLogger(name).propagate = True
    yield
    for name, value in saved.items():
        logging.getLogger(name).propagate = value


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
