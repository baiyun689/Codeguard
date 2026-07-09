"""追踪模块的确定性单元测试。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import tempfile
from pathlib import Path

import pytest

from codeguard_agent.observability.collector import (
    _NODE_PHASE_MAP,
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


class TestDashboard:
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
