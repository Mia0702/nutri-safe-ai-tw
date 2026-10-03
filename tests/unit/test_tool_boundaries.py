"""Agent A Input Tools / Agent B Evidence Tools / Agent C / Renderer 的責任邊界。"""

import ast
from pathlib import Path

import httpx
import pytest

from app.agents.evidence_tools import EVIDENCE_TOOLS, OFFICIAL_WEB_TOOLS
from app.agents.intake_agent import IntakeAgent
from app.schemas.enums import ErrorCode, InputType
from app.schemas.user_request import IntakeExtraction, MediaInput, RawUserInput
from app.services.errors import ServiceError
from app.tools.input_tools import (
    INPUT_TOOL_NAMES,
    NOTE_OFFICIAL_URL,
    NOTE_UNOFFICIAL_URL,
    InputToolOutput,
    fetch_url_content,
)
from tests.builders import extraction
from tests.fakes import FakeLLM

REQUIRED_EVIDENCE_TOOLS = {
    "lookup_product", "lookup_company", "lookup_permit", "lookup_ingredient", "lookup_approved_claim",
    "search_illegal_ad_cases", "query_knowledge_graph", "search_rag", "search_tw_official_web",
}


def _imports(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


# ---------------------------------------------------------------- 邊界


def test_input_and_evidence_tools_are_disjoint_and_complete():
    evidence = set(EVIDENCE_TOOLS) | set(OFFICIAL_WEB_TOOLS)
    assert set(INPUT_TOOL_NAMES) == {"analyze_image", "transcribe_audio", "fetch_url_content", "reinspect_image_identity"}
    assert not set(INPUT_TOOL_NAMES) & evidence
    assert REQUIRED_EVIDENCE_TOOLS <= evidence


def test_agent_a_cannot_reach_evidence_tools_or_data_services():
    imports = _imports("app/agents/intake_agent.py")
    forbidden = {"app.agents.evidence_tools", "app.tools.product_tools", "app.tools.graph_tools", "app.tools.rag_tools",
                 "app.tools.official_web_tools", "app.services.supabase_service", "app.services.neo4j_service"}
    assert not imports & forbidden


def test_agent_b_does_not_use_input_tools():
    for path in ("app/agents/verification_agent.py", "app/agents/evidence_tools.py"):
        assert "app.tools.input_tools" not in _imports(path)


@pytest.mark.parametrize("path", ["app/agents/reporter_agent.py", "app/response/line_renderer.py"])
def test_agent_c_and_renderer_do_not_query_data(path):
    imports = _imports(path)
    assert not any(m.startswith("app.tools") for m in imports)
    assert not imports & {"app.services.supabase_service", "app.services.neo4j_service", "app.agents.evidence_tools"}


def test_renderer_does_not_call_agents():
    assert not any(m.startswith("app.agents") for m in _imports("app/response/line_renderer.py"))


# ---------------------------------------------------------------- Agent A + Input Tools


def _intake(llm=None, **kw):
    llm = llm or FakeLLM(structured={IntakeExtraction: extraction()})
    return llm, IntakeAgent(llm, **kw)


def _content(llm) -> str:
    import json

    return json.loads([i for n, i in llm.structured_inputs if n == "IntakeExtraction"][-1])["content"]


def test_text_input_uses_no_input_tool():
    llm, agent = _intake()
    req = agent.understand("r1", RawUserInput(input_type=InputType.TEXT, text="TEST_ONLY 問題"))
    assert req.input_tools_called == [] and _content(llm) == "TEST_ONLY 問題"


def test_image_and_audio_go_through_input_tools_into_one_request():
    llm, agent = _intake()
    raw = RawUserInput(input_type=InputType.MIXED, text="TEST_ONLY 這個呢", media=[
        MediaInput(kind="image", content=b"img", mime_type="image/png"),
        MediaInput(kind="audio", content=b"aud", mime_type="audio/m4a"),
    ])
    req = agent.understand("r1", raw)
    assert [c.tool_name for c in req.input_tools_called] == ["analyze_image", "transcribe_audio"]
    assert all(c.ok and c.planned_by == "intake" for c in req.input_tools_called)
    content = _content(llm)
    # 語音轉錄是使用者自己說的話：與使用者文字同一標示（modality parity）；圖片是第三方文字，保留來源標示
    assert "【使用者文字】" in content and "【圖片內容】" in content and "【語音內容】" not in content
    assert content.count("【使用者文字】") == 2
    assert llm.calls.count(("parse_structured", "IntakeExtraction")) == 1  # 單一語意理解，不分三條 pipeline


def test_url_input_uses_fetch_tool_and_keeps_unofficial_note():
    fetched = InputToolOutput(tool_name="fetch_url_content", text="TEST_ONLY 廣告內容",
                              source_url="https://ad.example.invalid/p", notes=[NOTE_UNOFFICIAL_URL])
    llm, agent = _intake(url_fetcher=lambda url: fetched)
    req = agent.understand("r1", RawUserInput(input_type=InputType.URL, urls=["https://ad.example.invalid/p"]))
    assert req.source_url == "https://ad.example.invalid/p" and NOTE_UNOFFICIAL_URL in req.adapter_notes
    assert "【網頁內容】" in _content(llm)


def test_failed_input_tool_with_text_continues_with_note():
    llm = FakeLLM(structured={IntakeExtraction: extraction()}, fail={"describe_image"})
    _, agent = _intake(llm)
    raw = RawUserInput(input_type=InputType.MIXED, text="TEST_ONLY 問題",
                       media=[MediaInput(kind="image", content=b"img", mime_type="image/png")])
    req = agent.understand("r1", raw)
    assert req.input_tools_called[0].ok is False and any("圖片內容無法讀取" in n for n in req.adapter_notes)


def test_all_input_tools_failed_without_text_raises():
    _, agent = _intake(FakeLLM(structured={IntakeExtraction: extraction()}, fail={"transcribe_audio"}))
    raw = RawUserInput(input_type=InputType.AUDIO, media=[MediaInput(kind="audio", content=b"a", mime_type="audio/m4a")])
    with pytest.raises(ServiceError) as exc:
        agent.understand("r1", raw)
    assert exc.value.code == ErrorCode.TIMEOUT


def test_unsupported_image_type_is_rejected_by_tool():
    llm, agent = _intake()
    raw = RawUserInput(input_type=InputType.MIXED, text="x",
                       media=[MediaInput(kind="image", content=b"x", mime_type="application/pdf")])
    req = agent.understand("r1", raw)
    assert req.input_tools_called[0].error.code == ErrorCode.INVALID_INPUT
    assert not llm.called("describe_image")


# ---------------------------------------------------------------- fetch_url_content


def _client(routes: dict[str, httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda req: routes[str(req.url)]))


HTML = "<html><head><title>TEST_ONLY 標題</title></head><body><p>TEST_ONLY 宣稱可以降血糖</p></body></html>"
PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


def test_fetch_url_extracts_text_and_marks_unofficial():
    url = "https://ad.example.invalid/p"
    out = fetch_url_content(url, client=_client({url: httpx.Response(200, html=HTML)}), resolve=PUBLIC)
    assert "TEST_ONLY 宣稱可以降血糖" in out.text and out.notes == [NOTE_UNOFFICIAL_URL]


def test_fetch_url_marks_official_domain():
    url = "https://www.fda.gov.tw/tc/test"
    out = fetch_url_content(url, client=_client({url: httpx.Response(200, html=HTML)}), resolve=PUBLIC)
    assert out.notes == [NOTE_OFFICIAL_URL]


@pytest.mark.parametrize("url,ips", [
    ("http://localhost/x", ["127.0.0.1"]),
    ("http://intranet.example.invalid/x", ["10.0.0.5"]),
    ("http://metadata.example.invalid/x", ["169.254.169.254"]),
    ("file:///etc/passwd", ["93.184.216.34"]),
])
def test_fetch_url_blocks_non_public_targets(url, ips):
    with pytest.raises(ServiceError) as exc:
        fetch_url_content(url, client=_client({}), resolve=lambda host: ips)
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_fetch_url_rechecks_host_on_redirect():
    url = "https://ad.example.invalid/p"
    client = _client({url: httpx.Response(302, headers={"location": "http://internal.example.invalid/admin"})})
    resolve = lambda host: ["10.0.0.1"] if host.startswith("internal") else ["93.184.216.34"]  # noqa: E731
    with pytest.raises(ServiceError):
        fetch_url_content(url, client=client, resolve=resolve)


def test_fetch_url_rejects_non_text_content():
    url = "https://ad.example.invalid/file"
    client = _client({url: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})})
    with pytest.raises(ServiceError) as exc:
        fetch_url_content(url, client=client, resolve=PUBLIC)
    assert exc.value.code == ErrorCode.INVALID_INPUT
