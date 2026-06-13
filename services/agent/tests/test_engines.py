"""ToolAgentEngine 结果提取的工程正确性测试。

ReAct(create_agent)的返回要稳健地落成结构化结果:优先用图内置的 structured_response,
拿不到再从末条消息文本抠 JSON,最终兜底为空且不抛断
(见 spec「ReAct 审查结果的结构化与健壮性」)。这里只测确定性的提取逻辑,不调真实 LLM。
"""

from __future__ import annotations

from codeguard_agent.models.schemas import Issue, ReviewResult, Severity
from codeguard_agent.pipeline.engines import _extract_json_object, _last_message_text
from codeguard_agent.pipeline.engines import ToolAgentEngine


def _engine() -> ToolAgentEngine:
    return ToolAgentEngine(tool_client=None)


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
