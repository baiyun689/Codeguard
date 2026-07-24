"""ToolAgentEngine 结果提取的工程正确性测试。

ReAct(create_agent)的返回要稳健地落成结构化结果:优先用图内置的 structured_response,
拿不到再从末条消息文本抠 JSON,最终兜底为空且不抛断
(见 spec「ReAct 审查结果的结构化与健壮性」)。这里只测确定性的提取逻辑,不调真实 LLM。
"""

from __future__ import annotations

from codeguard_agent.models.schemas import Issue, ReviewResult, Severity
from codeguard_agent.pipeline.risk.discovery import COMPLETE_PATCH_RESULT
from codeguard_agent.pipeline.engines import (
    DirectEngine,
    ReviewOutcome,
    ToolAgentEngine,
    _extract_gathered_context,
    _extract_json_object,
    _last_message_text,
)
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


def test_gathered_context_excludes_structured_response_tool_message() -> None:
    raw = {
        "messages": [
            _AIMsg([{"id": "r1", "name": "ReviewResult", "args": {"issues": []}}]),
            _ToolMsg("r1", "Returning structured response", name="ReviewResult"),
        ]
    }
    assert _extract_gathered_context(raw) == []


def test_gathered_context_keeps_first_result_for_duplicate_tool_and_args() -> None:
    raw = {
        "messages": [
            _AIMsg([{"id": "c1", "name": "get_file_content", "args": {"file_path": "A.java"}}]),
            _ToolMsg("c1", "FULL BODY"),
            _AIMsg([{"id": "c2", "name": "get_file_content", "args": {"file_path": "A.java"}}]),
            _ToolMsg("c2", "该工具和参数已经在当前对话中成功返回"),
        ]
    }
    got = _extract_gathered_context(raw)
    assert len(got) == 1
    assert got[0].content == "FULL BODY"


def test_gathered_context_keeps_same_tool_with_different_args() -> None:
    raw = {
        "messages": [
            _AIMsg([{"id": "a", "name": "get_file_content", "args": {"file_path": "A.java"}}]),
            _ToolMsg("a", "A"),
            _AIMsg([{"id": "b", "name": "get_file_content", "args": {"file_path": "B.java"}}]),
            _ToolMsg("b", "B"),
        ]
    }
    assert [item.content for item in _extract_gathered_context(raw)] == ["A", "B"]


def test_gathered_context_dedups_canonical_equivalent_tool_arguments() -> None:
    raw = {
        "messages": [
            _AIMsg([
                {
                    "id": "a",
                    "name": "get_file_content",
                    "args": {"file_path": "src\\.\\A.java"},
                }
            ]),
            _ToolMsg("a", "FULL BODY"),
            _AIMsg([
                {
                    "id": "b",
                    "name": "get_file_content",
                    "args": {"file_path": "src/A.java"},
                }
            ]),
            _ToolMsg("b", "重复调用短标记"),
        ]
    }

    got = _extract_gathered_context(raw)

    assert len(got) == 1
    assert got[0].content == "FULL BODY"


def test_gathered_context_excludes_complete_patch_short_marker() -> None:
    raw = {
        "messages": [
            _AIMsg([
                {
                    "id": "a",
                    "name": "get_file_content",
                    "args": {"file_path": "src/A.java"},
                }
            ]),
            _ToolMsg("a", COMPLETE_PATCH_RESULT),
        ]
    }

    assert _extract_gathered_context(raw) == []


def _resolve_tool_names(enabled):
    """复刻 ToolAgentEngine.review 里的工具白名单解析逻辑(不构造真实 agent)。"""
    available = ["find_sensitive_apis", "find_callers", "get_code_metrics", "get_file_content"]
    names = list(available) if enabled is None else enabled
    tools = [n for n in names if n in available]
    if not tools:
        tools = list(available)
    return tools


def test_工具白名单_none_则全开():
    assert _resolve_tool_names(None) == ["find_sensitive_apis", "find_callers", "get_code_metrics", "get_file_content"]


def test_工具白名单_只开_file():
    assert _resolve_tool_names(["get_file_content"]) == ["get_file_content"]


def test_工具白名单_find_callers_档保持声明顺序():
    assert _resolve_tool_names(["find_callers", "get_file_content"]) == [
        "find_callers",
        "get_file_content",
    ]


def test_工具白名单_空或未知_回退全开():
    assert _resolve_tool_names([]) == ["find_sensitive_apis", "find_callers", "get_code_metrics", "get_file_content"]
    assert _resolve_tool_names(["nope"]) == ["find_sensitive_apis", "find_callers", "get_code_metrics", "get_file_content"]


class _FakeStructLLM:
    def __init__(self, result):
        self._result = result

    def invoke(self, _messages):
        return self._result


class _FakeLLM:
    """伪 LLM:只支持直连降级路径用到的 with_structured_output().invoke()。"""

    def __init__(self, result):
        self._result = result

    def with_structured_output(self, _schema, method):  # noqa: ARG002
        return _FakeStructLLM(self._result)


class _RecursingEngine(ToolAgentEngine):
    """让 ReAct 执行必撞递归上限,用于验证降级路径(不构造真实 agent/不调真实 LLM)。"""

    def _run_agent(self, llm, system_prompt, user_prompt):  # noqa: ARG002
        from langgraph.errors import GraphRecursionError

        raise GraphRecursionError("Recursion limit of 12 reached without hitting a stop condition")


def test_撞递归上限降级为无工具直连_不静默丢弃该域产出():
    # ReAct 撞上限时,该域不应被静默丢弃;而是降级走无工具直连复审,至少产出一份结论。
    eng = _RecursingEngine(tool_client=object())
    salvaged = ReviewResult(summary="降级直连产出", issues=[])
    out = eng.review(
        _FakeLLM(salvaged),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
    )
    assert isinstance(out, ReviewOutcome)
    assert out.result.summary == "降级直连产出"
    # 降级走的是 DirectEngine(无工具),故 gathered_context 恒空。
    assert out.gathered_context == []
