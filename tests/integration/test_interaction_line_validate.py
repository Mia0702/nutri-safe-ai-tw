"""Interaction Reporter 的 Flex 全部送 LINE 官方 validate API（只驗證、不送出）。

資料全部是 TEST_ONLY 虛構產品 / entity（不連 Supabase / Neo4j / OpenAI）；只需要 LINE_CHANNEL_ACCESS_TOKEN。
沒有 token 時以「SKIPPED_EXTERNAL」skip，不假裝成功。

執行：pytest tests/integration/test_interaction_line_validate.py -m integration -s
"""

import pytest

from app.line.client import LineMessagingClient
from app.schemas.enums import ResponseAction
from app.schemas.interaction import InteractionMode
from tests.integration.conftest import skip_external
from tests.unit.test_interaction_reporter import (  # noqa: F401（autouse fixture：虛構產品 / Bridge mapping）
    DOM_NAME,
    DRUG,
    EPA_NAME,
    HF_ZINC,
    IMP_NAME,
    ING,
    MIX_NAME,
    PRODUCT,
    TYPO_ZINC,
    Service,
    inheriting_repo,
    ingredient_data,
    mix_product,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def line_client(live_settings):
    token = live_settings.line_channel_access_token
    if token is None:
        skip_external("未設定 LINE_CHANNEL_ACCESS_TOKEN")
    return LineMessagingClient(token)


def _cases():
    dup = InteractionMode.DUPLICATE_COMPARISON
    return {
        "eligible 成分↔成分": Service([("鋅", ING), ("銅", ING)]),
        "eligible 產品↔藥品（lineage 可繼承）": Service([(IMP_NAME, PRODUCT), ("Testdrug-B", DRUG)], repo=inheriting_repo()),
        "evidence conflict": Service([("魚油", ING), ("Testdrug-Anticoag", DRUG)]),
        "lineage not inherited": Service([(IMP_NAME, PRODUCT), ("Testdrug-B", DRUG)]),
        "needs review": Service([("鎂", ING), ("Testdrug-A", DRUG)]),
        "no corpus record": Service([("鎂", ING), ("銅", ING)]),
        "duplicate": Service([(HF_ZINC, PRODUCT), (DOM_NAME, PRODUCT)], mode=dup),
        "duplicate 形式不同": Service([(IMP_NAME, PRODUCT), (EPA_NAME, PRODUCT)], mode=dup),
        "pairwise + duplicate": Service([(HF_ZINC, PRODUCT), (MIX_NAME, PRODUCT)], repo=inheriting_repo()),
        "drug unmapped": Service([("鋅", ING), ("某某牌藥品商品名", DRUG)]),
        "candidate card": Service([(TYPO_ZINC, PRODUCT), ("Testdrug-A", DRUG)]),
    }


def test_all_interaction_flex_messages_pass_line_validate_api(line_client, mix_product):
    validated = 0
    for name, service in _cases().items():
        text = f"{TYPO_ZINC} 跟 Testdrug-A 可以一起吃嗎" if name == "candidate card" else None
        flex, record = service.send(text=text)
        line_client.validate_reply([flex])
        validated += 1
        for action in record.response.available_actions:
            if action in (ResponseAction.WHY, ResponseAction.INTERACTION_EVIDENCE, ResponseAction.INTERACTION_MAPPING):
                line_client.validate_reply([service.action(record, action)])
                validated += 1
                if action == ResponseAction.INTERACTION_EVIDENCE:  # 證據卡 → 可點擊的來源卡
                    line_client.validate_reply([service.action(record, ResponseAction.SOURCES)])
                    validated += 1
    print(f"validated {validated} flex messages")
    assert validated >= 25
