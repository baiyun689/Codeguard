"""追踪模块的确定性单元测试。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import tempfile
from pathlib import Path

from codeguard_agent.observability.collector import (
    _NODE_PHASE_MAP,
    _TraceCollector,
    _phase_for,
    _serialize_messages,
    _summarize_value,
)
from codeguard_agent.observability.dashboard import render_dashboard, render_dashboard_file
from codeguard_agent.observability.models import (
    NodeStats,
    TokenUsage,
    TraceEvent,
    TraceReport,
    TraceSummary,
)
from codeguard_agent.observability.serialization import (
    serialize_llm_response,
    serialize_messages,
    serialize_trace_value,
)


class TestTraceSerialization:
    def test_preserves_long_nested_values(self):
        @dataclass
        class Payload:
            body: str

        value = {"payload": Payload(body="x" * 5000), "items": (1, 2)}

        serialized = serialize_trace_value(value)

        assert serialized["payload"]["body"] == "x" * 5000
        assert serialized["items"] == [1, 2]

    def test_messages_accept_direct_tuple_message_list(self):
        messages = [("system", "system text"), ("human", "user text")]

        assert serialize_messages(messages) == [
            {"role": "system", "content": "system text"},
            {"role": "human", "content": "user text"},
        ]

    def test_messages_flatten_single_batch(self):
        messages = [[("system", "system text"), ("human", "user text")]]

        result = serialize_messages(messages)

        assert [item["role"] for item in result] == ["system", "human"]
        assert result[1]["content"] == "user text"

    def test_llm_response_keeps_tool_calls_when_content_empty(self):
        class FakeAIMessage:
            type = "ai"
            content = ""
            tool_calls = [
                {
                    "id": "call-1",
                    "name": "get_file_content",
                    "args": {"file_path": "src/Foo.java"},
                }
            ]
            invalid_tool_calls = []
            additional_kwargs = {"reasoning_content": "need source"}
            response_metadata = {"finish_reason": "tool_calls"}
            usage_metadata = {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            }

        result = serialize_llm_response(FakeAIMessage())

        assert result["content"] == ""
        assert result["tool_calls"][0]["name"] == "get_file_content"
        assert result["tool_calls"][0]["args"]["file_path"] == "src/Foo.java"
        assert result["additional_kwargs"]["reasoning_content"] == "need source"


class TestTokenUsage:
    def test_defaults(self):
        t = TokenUsage()
        assert t.input_tokens == 0
        assert t.output_tokens == 0
        assert t.total_tokens == 0
        assert t.model == ""

    def test_serialization(self):
        t = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150, model="gpt-4", node_name="discover_threat_model")
        d = t.model_dump()
        assert d["input_tokens"] == 100
        assert d["output_tokens"] == 50
        assert d["total_tokens"] == 150


class TestTraceEvent:
    def test_minimal(self):
        e = TraceEvent(sequence=1, timestamp_ms=0.0, event_type="node_start", node_name="test", phase="outer_graph", depth=0, summary="test")
        assert e.detail == {}
        assert e.tokens is None

    def test_with_tokens(self):
        t = TokenUsage(total_tokens=42)
        e = TraceEvent(sequence=1, timestamp_ms=100.0, event_type="llm_end", node_name="test", phase="outer_graph", depth=1, summary="done", tokens=t)
        assert e.tokens.total_tokens == 42

    def test_detail_default(self):
        e = TraceEvent(sequence=1, timestamp_ms=0.0, event_type="tool_start", node_name="x", phase="outer_graph", depth=0, summary="")
        assert e.detail == {}


class TestNodeStats:
    def test_basic(self):
        s = NodeStats(node_name="discover_threat_model", start_ms=10.0, end_ms=30.0, duration_ms=20.0, llm_calls=2, tool_calls=3)
        assert s.duration_ms == 20.0
        assert s.tokens.total_tokens == 0

    def test_with_tokens(self):
        s = NodeStats(node_name="x", start_ms=0, end_ms=10, duration_ms=10, tokens=TokenUsage(total_tokens=500))
        assert s.tokens.total_tokens == 500


class TestTraceSummary:
    def test_defaults(self):
        s = TraceSummary()
        assert s.total_duration_ms == 0.0
        assert s.total_tokens.total_tokens == 0
        assert s.tokens_by_node == {}
        assert s.event_counts == {}
        assert s.node_timeline == []


class TestTraceReport:
    def test_full_roundtrip(self):
        events = [
            TraceEvent(sequence=1, timestamp_ms=10.0, event_type="node_start", node_name="summary", phase="outer_graph", depth=0, summary="输入: diff_text"),
            TraceEvent(sequence=2, timestamp_ms=20.0, event_type="node_end", node_name="summary", phase="outer_graph", depth=0, summary="输出: diff_summary"),
            TraceEvent(sequence=3, timestamp_ms=30.0, event_type="llm_start", node_name="summary", phase="outer_graph", depth=0, summary="LLM #1", detail={"model": "deepseek"}),
            TraceEvent(sequence=4, timestamp_ms=100.0, event_type="llm_end", node_name="summary", phase="outer_graph", depth=0, summary="完成 (150 tokens)", tokens=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150)),
        ]
        summary = TraceSummary(
            total_duration_ms=200.0,
            total_tokens=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
            tokens_by_node={"summary": TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150, node_name="summary")},
            event_counts={"node_start": 1, "node_end": 1, "llm_start": 1, "llm_end": 1},
            node_timeline=[NodeStats(node_name="summary", start_ms=10, end_ms=100, duration_ms=90)],
        )
        report = TraceReport(run_id="test-1", timestamp="2026-07-09T00:00:00", diff_size=100, events=events, summary=summary)

        d = report.model_dump()
        report2 = TraceReport.model_validate(d)
        assert report2.run_id == "test-1"
        assert len(report2.events) == 4
        assert report2.summary.total_tokens.total_tokens == 150
        assert report2.summary.tokens_by_node["summary"].total_tokens == 150


class TestPhaseMapping:
    def test_all_nodes_have_phase(self):
        expected = {
            "summary", "context_provider", "discover_threat_model", "discover_behavior",
            "discover_maintainability", "council_coordinator", "evidence_agent", "council_judge",
            "prepare", "review", "collect",
        }
        assert set(_NODE_PHASE_MAP.keys()) == expected

    def test_unknown_node_falls_back_to_outer(self):
        assert _phase_for("nonexistent") == "outer_graph"

    def test_known_nodes(self):
        assert _phase_for("discover_threat_model") == "reviewer_subgraph"
        assert _phase_for("council_judge") == "judge"
        assert _phase_for("evidence_agent") == "evidence"
        assert _phase_for("summary") == "outer_graph"


class TestSummarizeValue:
    def test_none(self):
        assert _summarize_value(None) == "None"

    def test_dict(self):
        assert "diff_text" in _summarize_value({"diff_text": "long...", "enabled_tools": None})

    def test_list(self):
        assert "[3 items]" in _summarize_value([1, 2, 3])

    def test_string_truncation(self):
        assert _summarize_value("a" * 300, max_len=50).endswith("...")

    def test_string_no_truncation(self):
        assert _summarize_value("short", max_len=50) == "short"


class TestSerializeMessages:
    def test_empty(self):
        assert _serialize_messages({}) == []

    def test_not_dict(self):
        assert _serialize_messages("not a dict") == []

    def test_list_of_tuples(self):
        msgs = {"messages": [("system", "you are a reviewer"), ("human", "review this diff")]}
        result = _serialize_messages(msgs)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "human"

    def test_message_objects(self):
        """模拟 LangChain 消息对象。"""
        class FakeMsg:
            type = "ai"
            content = "I found an issue"
            tool_calls = [{"name": "get_file_content", "args": {"file_path": "Foo.java"}}]
        msgs = {"messages": [FakeMsg()]}
        result = _serialize_messages(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "ai"
        assert "tool_calls" in result[0]
        assert result[0]["tool_calls"][0]["name"] == "get_file_content"


def _chain_event(
    event_type,
    *,
    name,
    run_id,
    parent_ids,
    node_name,
    data,
    checkpoint_ns="",
):
    return {
        "event": event_type,
        "name": name,
        "run_id": run_id,
        "parent_ids": parent_ids,
        "tags": [],
        "metadata": {
            "langgraph_node": node_name,
            "langgraph_checkpoint_ns": checkpoint_ns,
        },
        "data": data,
    }


class TestCollectorLineage:
    def test_parallel_nodes_are_siblings_and_wrapper_events_are_ignored(self):
        collector = _TraceCollector("diff", "trace-run")
        root = "graph-root"
        for name in (
            "discover_threat_model",
            "discover_behavior",
            "discover_maintainability",
        ):
            collector._handle_event(_chain_event(
                "on_chain_start",
                name=name,
                run_id=f"run-{name}",
                parent_ids=[root],
                node_name=name,
                data={"input": {"diff_text": "full diff"}},
            ))
            collector._handle_event(_chain_event(
                "on_chain_start",
                name="LangGraph",
                run_id=f"wrapper-{name}",
                parent_ids=[root, f"run-{name}"],
                node_name=name,
                data={"input": {"diff_text": "full diff"}},
            ))

        starts = [
            event
            for event in collector.finalize().events
            if event.event_type == "node_start"
        ]

        assert len(starts) == 3
        assert {event.depth for event in starts} == {0}
        assert {event.node_path for event in starts} == {
            "discover_threat_model",
            "discover_behavior",
            "discover_maintainability",
        }

    def test_same_named_subgraph_nodes_keep_distinct_reviewer_paths(self):
        collector = _TraceCollector("diff", "trace-run")
        root = "graph-root"
        for reviewer in ("discover_threat_model", "discover_behavior"):
            reviewer_run = f"run-{reviewer}"
            collector._handle_event(_chain_event(
                "on_chain_start",
                name=reviewer,
                run_id=reviewer_run,
                parent_ids=[root],
                node_name=reviewer,
                data={"input": {}},
            ))
            collector._handle_event(_chain_event(
                "on_chain_start",
                name="prepare",
                run_id=f"prepare-{reviewer}",
                parent_ids=[root, reviewer_run, f"wrapper-{reviewer}"],
                node_name="prepare",
                checkpoint_ns=f"{reviewer}:uuid|prepare:uuid",
                data={"input": {"diff_text": reviewer}},
            ))

        prepares = [
            event
            for event in collector.finalize().events
            if event.event_type == "node_start" and event.node_name == "prepare"
        ]

        assert len(prepares) == 2
        assert {event.depth for event in prepares} == {1}
        assert {event.node_path for event in prepares} == {
            "discover_threat_model/prepare",
            "discover_behavior/prepare",
        }
        assert len({event.invocation_id for event in prepares}) == 2

    def test_node_events_store_complete_input_and_output_values(self):
        collector = _TraceCollector("diff", "trace-run")
        start = _chain_event(
            "on_chain_start",
            name="context_provider",
            run_id="context-run",
            parent_ids=["graph-root"],
            node_name="context_provider",
            data={
                "input": {
                    "diff_text": "actual diff",
                    "enabled_tools": ["get_file_content"],
                }
            },
        )
        end = _chain_event(
            "on_chain_end",
            name="context_provider",
            run_id="context-run",
            parent_ids=["graph-root"],
            node_name="context_provider",
            data={
                "input": start["data"]["input"],
                "output": {
                    "context_bundle": {
                        "facts": [{"content": "fact text"}],
                    }
                },
            },
        )

        collector._handle_event(start)
        collector._handle_event(end)
        events = collector.finalize().events

        assert events[0].detail["input"]["diff_text"] == "actual diff"
        assert (
            events[1].detail["output"]["context_bundle"]["facts"][0]["content"]
            == "fact text"
        )

    def test_llm_and_tool_events_attach_to_nearest_node_and_keep_full_data(self):
        collector = _TraceCollector("diff", "trace-run")
        collector._handle_event(_chain_event(
            "on_chain_start",
            name="review",
            run_id="review-run",
            parent_ids=["root", "discover-run", "subgraph-root"],
            node_name="review",
            checkpoint_ns="discover_threat_model:uuid|review:uuid",
            data={"input": {"user_prompt": "review me"}},
        ))
        collector._handle_event({
            "event": "on_chat_model_start",
            "name": "ChatOpenAI",
            "run_id": "llm-run",
            "parent_ids": [
                "root",
                "discover-run",
                "subgraph-root",
                "review-run",
            ],
            "metadata": {"ls_model_name": "deepseek-v4-pro"},
            "data": {"input": [("human", "prompt" * 1000)]},
        })
        collector._handle_event({
            "event": "on_tool_start",
            "name": "get_file_content",
            "run_id": "tool-run",
            "parent_ids": [
                "root",
                "discover-run",
                "subgraph-root",
                "review-run",
            ],
            "metadata": {},
            "data": {
                "input": {
                    "file_path": "src/Foo.java",
                    "content": "x" * 5000,
                }
            },
        })

        events = collector.finalize().events
        llm = next(event for event in events if event.event_type == "llm_start")
        tool = next(event for event in events if event.event_type == "tool_start")

        assert llm.node_path == "review"
        assert llm.detail["messages"][0]["content"] == "prompt" * 1000
        assert tool.node_path == "review"
        assert tool.detail["input"]["content"] == "x" * 5000


class _FakeGraph:
    def __init__(self):
        self.stream_calls = 0
        self.invoke_calls = 0

    async def astream_events(self, initial_state, *, config, version):
        self.stream_calls += 1
        yield {
            "event": "on_chain_start",
            "name": "LangGraph",
            "run_id": "root-run",
            "parent_ids": [],
            "tags": ["graph:root"],
            "metadata": {},
            "data": {"input": initial_state},
        }
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "run_id": "root-run",
            "parent_ids": [],
            "tags": ["graph:root"],
            "metadata": {},
            "data": {
                "output": {
                    "final_issues": [],
                    "review_summary": "done",
                }
            },
        }

    def invoke(self, initial_state, *, config):
        self.invoke_calls += 1
        raise AssertionError(
            "normal tracing must not invoke graph a second time"
        )


def test_run_with_tracing_returns_root_output_without_second_execution():
    graph = _FakeGraph()
    collector = _TraceCollector("diff", "trace-run")

    result = collector.run_with_tracing(graph, {"diff_text": "diff"}, {})

    assert result["review_summary"] == "done"
    assert graph.stream_calls == 1
    assert graph.invoke_calls == 0


class TestDashboard:
    def test_json_embedding_preserves_script_like_source(self):
        dangerous = (
            "</script><script>window.pwned=true</script>\u2028\u2029"
        )
        report = TraceReport(
            run_id="safe-json",
            timestamp="2026-07-09T00:00:00",
            events=[
                TraceEvent(
                    sequence=1,
                    timestamp_ms=0,
                    event_type="node_start",
                    node_name="review",
                    phase="reviewer_subgraph",
                    depth=1,
                    summary="input",
                    detail={"input": {"diff_text": dangerous}},
                )
            ],
        )

        html = render_dashboard(report)
        match = re.search(
            (
                r'<script id="trace-data" type="application/json">'
                r"(.*?)</script>"
            ),
            html,
            re.DOTALL,
        )

        assert match is not None
        payload = match.group(1)
        assert "</script><script>" not in payload
        parsed = json.loads(payload)
        assert parsed["events"][0]["detail"]["input"]["diff_text"] == dangerous

    def test_template_renders_generic_node_and_raw_details(self):
        template = Path(
            "src/codeguard_agent/observability/dashboard_template.html"
        ).read_text(encoding="utf-8")

        assert "节点输入" in template
        assert "节点输出" in template
        assert "原始 JSON" in template
        assert "renderJsonValue" in template

    def test_render_with_placeholder(self):
        """验证 __TRACE_DATA__ 被替换且产出合法 HTML。"""
        report = TraceReport(
            run_id="test-dash",
            timestamp="2026-07-09T00:00:00",
            diff_size=42,
            events=[
                TraceEvent(sequence=1, timestamp_ms=10.0, event_type="node_start", node_name="summary", phase="outer_graph", depth=0, summary="start"),
                TraceEvent(sequence=2, timestamp_ms=100.0, event_type="node_end", node_name="summary", phase="outer_graph", depth=0, summary="end"),
            ],
            summary=TraceSummary(total_duration_ms=90.0, event_counts={"node_start": 1, "node_end": 1}),
        )
        html = render_dashboard(report)
        assert "__TRACE_DATA__" not in html
        assert '"run_id": "test-dash"' in html
        assert '"events":' in html
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_render_dashboard_file(self):
        """验证写文件功能。"""
        report = TraceReport(
            run_id="test-file",
            timestamp="2026-07-09T00:00:00",
            diff_size=10,
            events=[],
            summary=TraceSummary(),
        )
        with tempfile.TemporaryDirectory() as d:
            path = render_dashboard_file(report, d, "abc12345")
            assert path.exists()
            content = path.read_text(encoding="utf-8")
            assert "test-file" in content
            assert "</html>" in content


class TestEndToEnd:
    def test_orchestrator_passes_trace_max_llm_content(
        self,
        monkeypatch,
        tmp_path,
    ):
        from codeguard_agent.pipeline.orchestrator import (
            PipelineOrchestrator,
        )

        observed = {}

        class FakeCollector:
            def __init__(
                self,
                diff_text,
                run_id,
                max_llm_content=0,
            ):
                observed["max_llm_content"] = max_llm_content

            def run_with_tracing(self, graph, initial, config):
                return graph.invoke(initial, config=config)

            def finalize(self):
                return TraceReport(run_id="fake", timestamp="now")

        monkeypatch.setattr(
            "codeguard_agent.observability.collector._TraceCollector",
            FakeCollector,
        )
        monkeypatch.setattr(
            (
                "codeguard_agent.observability.dashboard."
                "render_dashboard_file"
            ),
            lambda *args, **kwargs: tmp_path / "trace.html",
        )

        PipelineOrchestrator(enable_summary=False).run(
            None,
            "diff --git a/Foo.java b/Foo.java\n-old\n+new\n",
            trace_enabled=True,
            trace_dir=str(tmp_path),
            trace_max_llm_content=1234,
        )

        assert observed["max_llm_content"] == 1234

    def test_mock_review_with_trace(self):
        """跑一次 mock 审查 + trace，验证：
        1. ReviewResult 与无 trace 时一致
        2. trace 文件生成且包含事件
        """
        from codeguard_agent.config import Settings
        from codeguard_agent.llm.client import build_llm
        from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator

        settings = Settings(
            provider="mock", model="", api_key="", api_base_url="",
            max_retries=1, structured_method="function_calling", disable_thinking=False,
        )
        llm = build_llm(settings)
        diff_text = "diff --git a/Foo.java b/Foo.java\n@@ -10,6 +10,8 @@\n+    String password = \"hardcoded123\";\n+    Statement stmt = conn.createStatement();\n"

        with tempfile.TemporaryDirectory() as d:
            orch = PipelineOrchestrator(enable_summary=False)
            r_no_trace = orch.run(llm, diff_text, trace_enabled=False)
            r_trace = orch.run(llm, diff_text, trace_enabled=True, trace_dir=d)
            assert r_no_trace.summary == r_trace.summary
            assert len(r_no_trace.issues) == len(r_trace.issues)

            html_files = list(Path(d).glob("trace-*.html"))
            assert len(html_files) == 1
            content = html_files[0].read_text(encoding="utf-8")
            assert "__TRACE_DATA__" not in content
            assert '"events":' in content
            assert '"node_start"' in content
            assert "</html>" in content

            match = re.search(
                (
                    r'<script id="trace-data" type="application/json">'
                    r"(.*?)</script>"
                ),
                content,
                re.DOTALL,
            )
            assert match is not None
            report_data = json.loads(match.group(1))
            assert report_data["events"]
            assert any(
                event["event_type"] == "node_start"
                and "input" in event["detail"]
                for event in report_data["events"]
            )
            assert all(
                event["depth"] >= 0
                for event in report_data["events"]
            )
            invocation_ids = {
                item["invocation_id"]
                for item in report_data["summary"]["node_timeline"]
            }
            assert len(invocation_ids) == len(
                report_data["summary"]["node_timeline"]
            )
