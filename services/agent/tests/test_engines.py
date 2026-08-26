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
    ReviewOutcome,
    ToolAgentEngine,
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
        _FakeLLM(rr), system_prompt="s", user_prompt="u",
        reviewer_name="logic", max_retries=1, structured_method="function_calling",
    )
    assert isinstance(outcome, ReviewOutcome)
    assert outcome.result is rr
    assert outcome.tool_trace_records == []


def test_direct_engine_none_结果兜底空信封():
    outcome = DirectEngine().review(
        _FakeLLM(None), system_prompt="s", user_prompt="u",
        reviewer_name="logic", max_retries=1, structured_method="function_calling",
    )
    assert outcome.result.issues == []
    assert outcome.tool_trace_records == []


def _resolve_tool_names(enabled):
    """复刻 ToolAgentEngine.review 里的工具白名单解析逻辑(不构造真实 agent)。"""
    available = ["get_file_content", "inspect_security_path", "inspect_change_impact", "inspect_structure"]
    names = list(available) if enabled is None else enabled
    tools = [n for n in names if n in available]
    if not tools:
        tools = list(available)
    return tools


def test_工具白名单_none_则全开():
    assert _resolve_tool_names(None) == [
        "get_file_content", "inspect_security_path", "inspect_change_impact", "inspect_structure",
    ]


def test_工具白名单_只开_file():
    assert _resolve_tool_names(["get_file_content"]) == ["get_file_content"]


def test_工具白名单_档保持声明顺序():
    assert _resolve_tool_names(["inspect_structure", "get_file_content"]) == [
        "inspect_structure",
        "get_file_content",
    ]


def test_工具白名单_空或未知_回退全开():
    assert _resolve_tool_names([]) == [
        "get_file_content", "inspect_security_path", "inspect_change_impact", "inspect_structure",
    ]
    assert _resolve_tool_names(["nope"]) == [
        "get_file_content", "inspect_security_path", "inspect_change_impact", "inspect_structure",
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

    def with_structured_output(self, _schema, method):  # noqa: ARG002
        return _FakeStructLLM(self._result)


class _RecursingEngine(ToolAgentEngine):
    """让 ReAct 执行必撞递归上限,用于验证降级路径(不构造真实 agent/不调真实 LLM)。"""

    def _run_agent(self, llm, system_prompt, user_prompt):  # noqa: ARG002
        from langgraph.errors import GraphRecursionError

        raise GraphRecursionError("Recursion limit of 12 reached without hitting a stop condition")


class _SuccessfulAgentEngine(ToolAgentEngine):
    """返回一次成功工具探索和同轨迹最终结构化结果。"""

    def _run_agent(self, llm, system_prompt, user_prompt):  # noqa: ARG002
        return {
            "messages": [
                _AIMsg([
                    {
                        "id": "tool-1",
                        "name": "get_file_content",
                        "args": {"file_path": "src/A.java"},
                    }
                ]),
                _ToolMsg("tool-1", "class A {}"),
                _AIMsg(
                    [],
                    '{"summary":"同轨迹完成","issues":[{'
                    '"file":"src/A.java","line":1,'
                    '"location_snippet":"class A {}","type":"结构问题",'
                    '"message":"新增结构导致问题","suggestion":"调整结构",'
                    '"confidence":0.9,"evidence_refs":['
                    '{"alias":"T01","role":"mechanism"}]}]}',
                ),
            ]
        }


class _FailIfStructuredLLM:
    def with_structured_output(self, _schema, method):  # noqa: ARG002
        raise AssertionError("合法 ReAct 最终结果不应再次调用结构化 LLM")


class _RawFinalEngine(ToolAgentEngine):
    def __init__(self, content):
        super().__init__(tool_client=type("Client", (), {"trace_records": []})())
        self._content = content

    def _run_agent(self, llm, system_prompt, user_prompt):  # noqa: ARG002
        return {"messages": [_AIMsg([], self._content)]}


class _RawMessagesEngine(ToolAgentEngine):
    def __init__(self, messages):
        super().__init__(tool_client=type("Client", (), {"trace_records": []})())
        self._messages = messages

    def _run_agent(self, llm, system_prompt, user_prompt):  # noqa: ARG002
        return {"messages": self._messages}


class _CountingLLM:
    def __init__(self, result):
        self.result = result
        self.structured_calls = 0

    def with_structured_output(self, _schema, method):  # noqa: ARG002
        self.structured_calls += 1
        return _FakeStructLLM(self.result)


class _RaisingStructuredLLM:
    def __init__(self):
        self.structured_calls = 0

    def with_structured_output(self, _schema, method):  # noqa: ARG002
        self.structured_calls += 1

        class RaisingStructured:
            def invoke(self, _messages):
                raise RuntimeError("fallback failed")

        return RaisingStructured()


def test_react_成功_直接使用同轨迹结构化结果():
    engine = _SuccessfulAgentEngine(
        tool_client=type("Client", (), {"trace_records": []})(),
    )

    outcome = engine.review(
        _FailIfStructuredLLM(),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        result_schema=DiscoveryReviewResult,
    )

    assert isinstance(outcome.result, DiscoveryReviewResult)
    assert outcome.result.summary == "同轨迹完成"
    assert outcome.result.issues[0].evidence_refs[0].alias == "T01"
    assert outcome.execution_events == ["react_inline_structured"]


def test_react_明确clean_不触发结构化降级():
    engine = _RawFinalEngine('{"summary":"clean","issues":[]}')

    outcome = engine.review(
        _FailIfStructuredLLM(),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        result_schema=DiscoveryReviewResult,
    )

    assert outcome.result.summary == "clean"
    assert outcome.result.issues == []
    assert outcome.execution_events == ["react_inline_structured"]


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        '{"summary":"missing issues"}',
        '{"summary":"null issues","issues":null}',
        '{"summary":"extra field","issues":[],"unexpected":true}',
    ],
)
def test_react_非法schema_只触发一次结构化降级(content):
    engine = _RawFinalEngine(content)
    llm = _CountingLLM(DiscoveryReviewResult(summary="fallback", issues=[]))

    outcome = engine.review(
        llm,
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        result_schema=DiscoveryReviewResult,
    )

    assert llm.structured_calls == 1
    assert outcome.result.summary == "fallback"
    assert outcome.execution_events == ["react_synthesis_fallback_invalid_output"]


def test_react_结构化降级失败后返回失败事件且不启动新阶段(monkeypatch):
    monkeypatch.setattr("codeguard_agent.pipeline.execution.engines.sleep", lambda _s: None)
    engine = _RawFinalEngine("not json")
    llm = _CountingLLM(None)

    outcome = engine.review(
        llm,
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        result_schema=DiscoveryReviewResult,
    )

    assert llm.structured_calls == 1
    assert outcome.result.issues == []
    assert outcome.execution_events == [
        "structured_output_missing",
        "react_synthesis_fallback_invalid_output",
    ]


def test_react_结构化降级抛异常后返回失败事件且不逸出(monkeypatch):
    monkeypatch.setattr("codeguard_agent.llm.client.time.sleep", lambda _s: None)
    engine = _RawFinalEngine("not json")
    llm = _RaisingStructuredLLM()

    outcome = engine.review(
        llm,
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        result_schema=DiscoveryReviewResult,
    )

    assert llm.structured_calls == 1
    assert outcome.result.issues == []
    assert outcome.execution_events == [
        "react_synthesis_fallback_invalid_output",
        "react_synthesis_fallback_failed",
    ]


@pytest.mark.parametrize(
    "content",
    [
        "```json\n{\"summary\":\"fenced\",\"issues\":[]}\n```",
        [{"type": "text", "text": '{"summary":"blocks","issues":[]}'}],
        ["{\"summary\":\"string-block\",\"issues\":[]}"],
    ],
)
def test_react_支持单一json围栏和文本内容块(content):
    engine = _RawFinalEngine(content)

    outcome = engine.review(
        _FailIfStructuredLLM(),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        result_schema=DiscoveryReviewResult,
    )

    assert isinstance(outcome.result, DiscoveryReviewResult)
    assert outcome.execution_events == ["react_inline_structured"]


def test_react_拒绝夹杂解释的json并降级():
    engine = _RawFinalEngine('结果如下：{"summary":"mixed","issues":[]}')
    llm = _CountingLLM(DiscoveryReviewResult(summary="fallback", issues=[]))

    outcome = engine.review(
        llm,
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        result_schema=DiscoveryReviewResult,
    )

    assert llm.structured_calls == 1
    assert outcome.result.summary == "fallback"


@pytest.mark.parametrize(
    "messages",
    [
        [_ToolMsg("tool-1", '{"summary":"tool","issues":[]}')],
        [
            _AIMsg(
                [{"id": "tool-1", "name": "get_file_content", "args": {}}],
                '{"summary":"unfinished","issues":[]}',
            )
        ],
    ],
)
def test_react_不把工具消息或未结束工具调用中的json当最终结果(messages):
    engine = _RawMessagesEngine(messages)
    llm = _CountingLLM(DiscoveryReviewResult(summary="fallback", issues=[]))

    outcome = engine.review(
        llm,
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        result_schema=DiscoveryReviewResult,
    )

    assert llm.structured_calls == 1
    assert outcome.result.summary == "fallback"


def test_react_内联结果仍捕获全部工具调用用于审计():
    records = [
        DiscoveryToolRecord(
            call_id="call-1",
            tool="get_file_content",
            arguments={"file_path": "src/A.java"},
            output="class A {}",
            duration_ms=1.0,
            status="complete",
            reuse_key="get_file_content:src/A.java",
        ),
        DiscoveryToolRecord(
            call_id="call-2",
            tool="inspect_change_impact",
            arguments={"symbol_id": "method:A#m()"},
            output="Error: timeout",
            duration_ms=2.0,
            status="failed",
            reuse_key="inspect_change_impact:method:A#m()",
        ),
    ]
    engine = _SuccessfulAgentEngine(
        tool_client=type("Client", (), {"trace_records": records})(),
    )
    catalog = EvidenceCatalog(task_id="task-1", reviewer="behavior", revision="rev-1")

    outcome = engine.review(
        _FailIfStructuredLLM(),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        evidence_catalog=catalog,
        result_schema=DiscoveryReviewResult,
    )

    assert outcome.evidence_catalog.tool_aliases() == ["T01", "T02"]
    assert [ref.call_id for ref in outcome.tool_trace_records] == ["call-1", "call-2"]
    assert [ref.status for ref in outcome.tool_trace_records] == ["complete", "failed"]


def test_react_未知证据别名不触发结构化降级():
    content = (
        '{"summary":"unknown ref","issues":[{'
        '"file":"src/A.java","line":1,"location_snippet":"return value;",'
        '"type":"行为问题","message":"返回了错误值","suggestion":"修复返回值",'
        '"confidence":0.8,"evidence_refs":['
        '{"alias":"T99","role":"mechanism"}]}]}'
    )
    engine = _RawFinalEngine(content)

    outcome = engine.review(
        _FailIfStructuredLLM(),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        result_schema=DiscoveryReviewResult,
    )

    assert outcome.result.issues[0].evidence_refs[0].alias == "T99"
    assert outcome.execution_events == ["react_inline_structured"]


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
    assert out.execution_events == ["react_degraded_recursion"]
    # 降级走的是 DirectEngine(无工具),工具记录恒空。
    assert out.tool_trace_records == []


def test_递归无可用事实的直连降级失败不再次逸出且保留审计(monkeypatch):
    monkeypatch.setattr("codeguard_agent.llm.client.time.sleep", lambda _s: None)
    record = DiscoveryToolRecord(
        call_id="call-failed",
        tool="inspect_change_impact",
        arguments={"symbol_id": "method:A#m()"},
        output=REPEATED_TOOL_RESULT,
        duration_ms=2.0,
        status="reused",
        reuse_key="inspect_change_impact:method:A#m()",
    )
    engine = _RecursingEngine(
        tool_client=type("Client", (), {"trace_records": [record]})(),
    )

    outcome = engine.review(
        _RaisingStructuredLLM(),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        result_schema=DiscoveryReviewResult,
    )

    assert outcome.result.issues == []
    assert [ref.call_id for ref in outcome.tool_trace_records] == ["call-failed"]
    assert outcome.execution_events == [
        "react_degraded_recursion",
        "react_direct_fallback_failed",
    ]


def test_严格工具档递归失败不混入无工具直连结果():
    engine = _RecursingEngine(
        tool_client=type(
            "Client",
            (),
            {
                "trace_records": [
                    type(
                        "Record",
                        (),
                        {
                            "tool": "inspect_change_impact",
                            "arguments": {"symbol_id": "method:A#m()"},
                            "output": "A#m() 被 Controller 调用",
                            "duration_ms": 3.0,
                            "status": "complete",
                        },
                    )()
                ]
            },
        )(),
        allow_direct_fallback=False,
    )

    outcome = engine.review(
        _FakeLLM(ReviewResult(summary="基于图谱事实收束")),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
    )

    assert outcome.result.summary == "基于图谱事实收束"
    assert outcome.execution_events == ["react_synthesis_fallback_recursion"]
    trace_ref = outcome.tool_trace_records[0]
    assert trace_ref.tool == "inspect_change_impact"
    assert trace_ref.status == "complete"
    assert "output" not in trace_ref.model_dump()


def test_严格工具档递归且无事实时仍失败():
    from langgraph.errors import GraphRecursionError

    engine = _RecursingEngine(
        tool_client=type("Client", (), {"trace_records": []})(),
        allow_direct_fallback=False,
    )

    with pytest.raises(GraphRecursionError):
        engine.review(
            _FakeLLM(ReviewResult(summary="不应使用")),
            system_prompt="s",
            user_prompt="u",
            reviewer_name="logic",
            max_retries=1,
            structured_method="function_calling",
        )
