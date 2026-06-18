"""ToolAgentEngine 结果提取的工程正确性测试。

ReAct(create_agent)的返回要稳健地落成结构化结果:优先用图内置的 structured_response,
拿不到再从末条消息文本抠 JSON,最终兜底为空且不抛断
(见 spec「ReAct 审查结果的结构化与健壮性」)。这里只测确定性的提取逻辑,不调真实 LLM。
"""

from __future__ import annotations

from codeguard_agent.models.schemas import Issue, ReviewResult, Severity
from codeguard_agent.pipeline.engines import (
    DirectEngine,
    GatheredContext,
    ReviewOutcome,
    ToolAgentEngine,
    _extract_gathered_context,
    _extract_json_object,
    _last_message_text,
)
from codeguard_agent.pipeline.stages.reviewer_stage import _dedup_context


def _engine() -> ToolAgentEngine:
    return ToolAgentEngine(tool_client=None)


class _AIMsg:
    """伪 AIMessage:带 tool_calls。"""

    type = "ai"

    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.content = ""


class _ToolMsg:
    """伪 ToolMessage:type='tool' + tool_call_id + content。"""

    type = "tool"

    def __init__(self, tool_call_id, content, name=""):
        self.tool_call_id = tool_call_id
        self.content = content
        self.name = name


class _FakeStructured:
    def __init__(self, result):
        self._result = result

    def invoke(self, _messages):
        return self._result


class _FakeLLM:
    def __init__(self, result):
        self._result = result

    def with_structured_output(self, _schema, method=None):
        return _FakeStructured(self._result)


def test_direct_engine_返回空_gathered_context():
    rr = ReviewResult(summary="x", issues=[])
    outcome = DirectEngine().review(
        _FakeLLM(rr), system_prompt="s", user_prompt="u",
        reviewer_name="logic", max_retries=1, structured_method="function_calling",
    )
    assert isinstance(outcome, ReviewOutcome)
    assert outcome.result is rr
    assert outcome.gathered_context == []


def test_direct_engine_none_结果兜底空信封():
    outcome = DirectEngine().review(
        _FakeLLM(None), system_prompt="s", user_prompt="u",
        reviewer_name="logic", max_retries=1, structured_method="function_calling",
    )
    assert outcome.result.issues == []
    assert outcome.gathered_context == []


def test_抽取_toolmessage_为_gathered_context():
    raw = {
        "messages": [
            _AIMsg([{"id": "c1", "name": "get_file_content", "args": {"path": "A.java"}}]),
            _ToolMsg("c1", "class A { ... }"),
        ]
    }
    got = _extract_gathered_context(raw)
    assert len(got) == 1
    assert got[0].tool == "get_file_content"
    assert "A.java" in got[0].args
    assert got[0].content == "class A { ... }"


def test_抽取_跳过空内容与非工具消息():
    raw = {"messages": [_AIMsg([]), _ToolMsg("c9", "   ")]}
    assert _extract_gathered_context(raw) == []


def test_抽取_异常或非dict_返回空_不抛():
    assert _extract_gathered_context("not a dict") == []
    assert _extract_gathered_context({"messages": None}) == []


def test_dedup_context_按工具与参数去重():
    a = GatheredContext("get_file_content", '{"path":"A.java"}', "AAA")
    a2 = GatheredContext("get_file_content", '{"path":"A.java"}', "AAA-again")
    b = GatheredContext("get_repo_map", "{}", "MAP")
    out = _dedup_context([a, a2, b])
    assert len(out) == 2
    assert out[0] is a and out[1] is b  # 保留首次出现


def test_优先用图内置的_structured_response():
    expected = ReviewResult(
        summary="ok",
        issues=[Issue(severity=Severity.CRITICAL, file="A.java", line=10,
                      type="SQL注入", message="拼接用户输入", confidence=0.9)],
    )
    raw = {"structured_response": expected, "messages": []}
    result = _engine()._extract_result(raw, "security")  # noqa: SLF001
    assert result is expected
    assert result.issues[0].type == "SQL注入"


def test_无结构化_则从末条消息文本抠_json():
    class _Msg:
        content = '收尾:{"summary": "clean", "issues": []}'

    raw = {"structured_response": None, "messages": [_Msg()]}
    result = _engine()._extract_result(raw, "logic")  # noqa: SLF001
    assert result.summary == "clean"
    assert result.issues == []


def test_都拿不到_兜底为空结果_不抛断():
    raw = {"structured_response": None, "messages": [type("M", (), {"content": "纯文字没有 JSON"})()]}
    result = _engine()._extract_result(raw, "quality")  # noqa: SLF001
    assert result.issues == []
    assert result.summary == ""


def test_last_message_text_取末条内容():
    raw = {"messages": [type("M", (), {"content": "first"})(), type("M", (), {"content": "last"})()]}
    assert _last_message_text(raw) == "last"
    assert _last_message_text({"messages": []}) == ""


def test_extract_json_object_花括号配平():
    assert _extract_json_object('prefix {"a": {"b": 1}} suffix') == '{"a": {"b": 1}}'


def test_extract_json_object_代码块():
    assert _extract_json_object('```json\n{"x": 1}\n```') == '{"x": 1}'


def test_extract_json_object_无_json_返回_none():
    assert _extract_json_object("no json at all") is None
    assert _extract_json_object("") is None


def _resolve_tool_names(enabled):
    """复刻 ToolAgentEngine.review 里的工具白名单解析逻辑(不构造真实 agent)。"""
    available = ["get_repo_map", "get_file_content"]
    names = list(available) if enabled is None else enabled
    tools = [n for n in names if n in available]
    if not tools:
        tools = list(available)
    return tools


def test_工具白名单_none_则全开():
    assert _resolve_tool_names(None) == ["get_repo_map", "get_file_content"]


def test_工具白名单_只开_file():
    assert _resolve_tool_names(["get_file_content"]) == ["get_file_content"]


def test_工具白名单_repomap_档保持声明顺序():
    assert _resolve_tool_names(["get_repo_map", "get_file_content"]) == [
        "get_repo_map",
        "get_file_content",
    ]


def test_工具白名单_空或未知_回退全开():
    assert _resolve_tool_names([]) == ["get_repo_map", "get_file_content"]
    assert _resolve_tool_names(["nope"]) == ["get_repo_map", "get_file_content"]
