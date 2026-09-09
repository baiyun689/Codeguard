"""ReviewEngine 公开行为的工程正确性测试。"""

from __future__ import annotations
import pytest
from codeguard_agent.models.evidence import EvidenceCatalog
from codeguard_agent.models.schemas import DiscoveryReviewResult, ReviewResult
from codeguard_agent.pipeline.execution.discovery import (
    REPEATED_TOOL_RESULT,
    DiscoveryToolRecord,
)
from codeguard_agent.pipeline.execution.engines import (
    DirectEngine,
    ReviewExecutionStatus,
    ReviewOutcome,
)


class _AIMsg:
    """伪 AIMessage:带 tool_calls。"""

    type = "ai"

    def __init__(self, tool_calls, content=""):
        self.tool_calls = tool_calls
        self.content = content


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


def test_direct_engine_返回评审信封():
    rr = ReviewResult(summary="x", issues=[])
    outcome = DirectEngine().review(
        _FakeLLM(rr),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
    )
    assert isinstance(outcome, ReviewOutcome)
    assert outcome.status is ReviewExecutionStatus.COMPLETE
    assert outcome.result is rr
    assert outcome.tool_trace_records == []


def test_direct_engine_none_结果标记协议失败():
    outcome = DirectEngine().review(
        _FakeLLM(None),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
    )
    assert outcome.status is ReviewExecutionStatus.PROTOCOL_FAILED
    assert outcome.result is None
    assert outcome.failure_reason == "structured_output_missing"
    assert outcome.tool_trace_records == []


def _resolve_tool_names(enabled):
    """复刻 ToolAgentEngine.review 里的工具白名单解析逻辑(不构造真实 agent)。"""
    available = [
        "read_symbol",
        "inspect_structure",
        "inspect_change_impact",
        "inspect_path",
    ]
    names = list(available) if enabled is None else enabled
    tools = [n for n in names if n in available]
    if not tools:
        tools = list(available)
    return tools


def test_工具白名单_none_则全开():
    assert _resolve_tool_names(None) == [
        "read_symbol",
        "inspect_structure",
        "inspect_change_impact",
        "inspect_path",
    ]


def test_工具白名单_只开_file():
    assert _resolve_tool_names(["read_symbol"]) == ["read_symbol"]


def test_工具白名单_档保持声明顺序():
    assert _resolve_tool_names(["inspect_structure", "read_symbol"]) == [
        "inspect_structure",
        "read_symbol",
    ]


def test_工具白名单_空或未知_回退全开():
    assert _resolve_tool_names([]) == [
        "read_symbol",
        "inspect_structure",
        "inspect_change_impact",
        "inspect_path",
    ]
    assert _resolve_tool_names(["nope"]) == [
        "read_symbol",
        "inspect_structure",
        "inspect_change_impact",
        "inspect_path",
    ]


class _FakeStructLLM:
    def __init__(self, result):
        self._result = result

    def invoke(self, _messages):
        return self._result


class _FakeLLM:
    """伪 LLM:只支持直连降级路径用到的 with_structured_output().invoke()。"""

    def __init__(self, result):
        self._result = result

    def with_structured_output(self, _schema, method):
        return _FakeStructLLM(self._result)


class _FailIfStructuredLLM:
    def with_structured_output(self, _schema, method):
        raise AssertionError("合法 ReAct 最终结果不应再次调用结构化 LLM")


class _CountingLLM:
    def __init__(self, result):
        self.result = result
        self.structured_calls = 0

    def with_structured_output(self, _schema, method):
        self.structured_calls += 1
        return _FakeStructLLM(self.result)


class _RaisingStructuredLLM:
    def __init__(self):
        self.structured_calls = 0

    def with_structured_output(self, _schema, method):
        self.structured_calls += 1

        class RaisingStructured:
            def invoke(self, _messages):
                raise RuntimeError("fallback failed")

        return RaisingStructured()
