"""Frozen contract 補充回歸測試（docs/FROZEN_CONTRACTS.md 中原本沒有直接測試的通用行為）。

* FC-NOHC：production code 不含任何測試 fixture 的產品名稱 / 字號 / TEST_ONLY 標記（不 hard-code 測試答案）。
* FC-SEC：.env 不進版控；.env.example 只有變數名稱或安全 placeholder。
* FC-AGC：Agent C 不新增 EvidenceBundle 中不存在的官方結論（查無產品時沒有官方來源、不說已登記 / 已核准）。

全部使用虛構資料，不呼叫外部服務。
"""

import re
from pathlib import Path

import pytest

from tests.builders import request
from tests.fixtures import synthetic_data as data
from tests.unit.test_adaptive_response import compose

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DIRS = ("app", "scripts")
SECRET_VARS = ("OPENAI_API_KEY", "SUPABASE_SECRET_KEY", "NEO4J_PASSWORD", "LINE_CHANNEL_SECRET",
               "LINE_CHANNEL_ACCESS_TOKEN")


def _production_sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text(encoding="utf-8")) for d in PRODUCTION_DIRS for p in (ROOT / d).rglob("*.py")]


def test_production_code_contains_no_fixture_identities():
    markers = {"TEST_ONLY"}
    markers |= {p["product_name_zh"] for p in data.PRODUCTS}
    markers |= {p["permit_code"] for p in data.PRODUCTS}
    markers |= {p["company_name"] for p in data.PRODUCTS}
    hits = [f"{path.relative_to(ROOT)}:{m}" for path, text in _production_sources() for m in markers if m in text]
    assert hits == []


def test_env_file_is_ignored_and_example_has_no_secret_values():
    ignored = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert ".env" in ignored
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z0-9_]+)=(.*)$", example, re.MULTILINE))
    for var in SECRET_VARS:
        assert var in values
        value = values[var].strip()
        # 只允許空值或明顯的 placeholder（不可有像真的 token / key 的長字串）
        assert value == "" or value.startswith("<") or not re.search(r"[A-Za-z0-9_\-]{16,}", value), var


@pytest.mark.parametrize("phrase", ["已登記", "官方核准", "已核准", "合法產品"])
def test_reporter_adds_no_official_conclusion_without_official_evidence(phrase):
    payload = compose(request(product_candidates=["完全查不到的虛構名字"]))
    assert payload.source_count == 0 and payload.sources == []
    text = " ".join([payload.title, payload.summary, *payload.key_points, *payload.warnings])
    assert phrase not in text
    # 提到假貨只能是否定句（NOT_FOUND 不等於假貨）
    assert all("不代表" in sentence for sentence in re.split(r"[。；]", text) if "假貨" in sentence)
