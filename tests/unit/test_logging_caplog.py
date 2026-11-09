"""configure_logging（propagate=False）不得讓 pytest caplog 收不到專案的結構化 log（見 tests/conftest.py 的 autouse fixture）。"""

import logging


def test_caplog_still_receives_project_logs_after_app_main_is_imported():
    import app.main  # noqa: F401  測試內才 import 會呼叫 configure_logging（propagate=False）


def test_caplog_receives_records_after_configure_logging(caplog):
    from app.config.settings import Settings
    from app.observability import configure_logging

    configure_logging(Settings(_env_file=None))
    with caplog.at_level(logging.INFO):
        logging.getLogger("nutri_safe_ai.line.handler").info("structured_event", extra={"stage": "probe"})
    rec = next(r for r in caplog.records if r.message == "structured_event")
    assert rec.stage == "probe"
    # runtime 設定沒變：JSON stderr handler 仍在
    assert logging.getLogger("nutri_safe_ai").handlers


def test_caplog_receives_records_when_app_main_is_first_imported_inside_the_test(caplog):
    import importlib
    import sys

    sys.modules.pop("app.main", None)  # 強制在測試內重新 import → 再次呼叫 configure_logging
    importlib.import_module("app.main")
    with caplog.at_level(logging.INFO):
        logging.getLogger("nutri_safe_ai.agents.interaction").info("hello")
    assert [r.message for r in caplog.records] == ["hello"]
