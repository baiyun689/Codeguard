"""追踪模块的确定性单元测试。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import tempfile
from pathlib import Path

from codeguard_agent.observability.collector import (
    _NODE_PHASE_MAP,
    _TraceCollector,
    _phase_for,
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
from codeguard_agent.observability.view_model import build_trace_view


def _flow_event(
    sequence: int,
    event_type: str,
    node_name: str,
    node_path: str,
    run_id: str,
    *,
    detail: dict | None = None,
    invocation_id: str = "",
) -> TraceEvent:
    return TraceEvent(
        sequence=sequence,
        timestamp_ms=float(sequence * 10),
        event_type=event_type,
        node_name=node_name,
        node_path=node_path,
        phase="reviewer_subgraph",
        depth=node_path.count("/"),
        summary=f"{event_type}: {node_name}",
        detail=detail or {},
        run_id=run_id,
        invocation_id=invocation_id or run_id,
    )


def _flow_report_fixture() -> TraceReport:
    events = [
        _flow_event(1, "node_start", "summary", "summary", "summary-run"),
        _flow_event(
            2,
            "node_end",
            "summary",
            "summary",
            "summary-run",
            detail={"output": {"diff_summary": "summary"}},
        ),
        _flow_event(
            3,
            "node_start",
            "context_provider",
            "context_provider",
            "context-run",
        ),
        _flow_event(
            4,
            "node_end",
            "context_provider",
            "context_provider",
            "context-run",
            detail={"output": {"context_bundle": {"facts": []}}},
        ),
        _flow_event(
            5,
            "node_start",
            "discover_threat_model",
            "discover_threat_model",
            "discover-run",
        ),
        _flow_event(
            6,
            "node_start",
            "prepare",
            "discover_threat_model/prepare",
            "prepare-run",
        ),
        _flow_event(
            7,
            "node_end",
            "prepare",
            "discover_threat_model/prepare",
            "prepare-run",
            detail={"output": {"messages": [{"role": "human"}]}},
        ),
        _flow_event(
            8,
            "llm_start",
            "model",
            "discover_threat_model/review/model",
            "llm-run",
            detail={"messages": [{"role": "human", "content": "review"}]},
            invocation_id="model-run",
        ),
        _flow_event(
            9,
            "llm_end",
            "model",
            "discover_threat_model/review/model",
            "llm-run",
            detail={"response": {"tool_calls": [{"name": "get_file_content"}]}},
            invocation_id="model-run",
        ),
        _flow_event(
            10,
            "tool_start",
            "tools",
            "discover_threat_model/review/tools",
            "tool-run",
            detail={
                "tool_name": "get_file_content",
                "input": {"file_path": "src/Foo.java"},
            },
            invocation_id="tools-run",
        ),
        _flow_event(
            11,
            "tool_end",
            "tools",
            "discover_threat_model/review/tools",
            "tool-run",
            detail={
                "tool_name": "get_file_content",
                "output": {"content": "class Foo {}"},
            },
            invocation_id="tools-run",
        ),
        _flow_event(
            12,
            "node_start",
            "collect",
            "discover_threat_model/collect",
            "collect-run",
        ),
        _flow_event(
            13,
            "node_end",
            "collect",
            "discover_threat_model/collect",
            "collect-run",
            detail={"output": {"raw_candidate_issues": [{"type": "security"}]}},
        ),
        _flow_event(
            14,
            "node_end",
            "discover_threat_model",
            "discover_threat_model",
            "discover-run",
        ),
        _flow_event(
            15,
            "node_start",
            "council_coordinator",
            "council_coordinator",
            "coordinator-run",
        ),
        _flow_event(
            16,
            "node_end",
            "council_coordinator",
            "council_coordinator",
            "coordinator-run",
            detail={"output": {"council_route": "evidence_agent"}},
        ),
        _flow_event(
            17,
            "node_start",
            "evidence_agent",
            "evidence_agent",
            "evidence-run",
        ),
        _flow_event(
            18,
            "node_end",
            "evidence_agent",
            "evidence_agent",
            "evidence-run",
            detail={"output": {"evidence_notes": [{"status": "supported"}]}},
        ),
        _flow_event(
            19,
            "node_start",
            "council_coordinator",
            "council_coordinator",
            "coordinator-run-2",
        ),
        _flow_event(
            20,
            "node_end",
            "council_coordinator",
            "council_coordinator",
            "coordinator-run-2",
            detail={"output": {"council_route": "council_judge"}},
        ),
        _flow_event(
            21,
            "node_start",
            "council_judge",
            "council_judge",
            "judge-run",
        ),
        _flow_event(
            22,
            "node_end",
            "council_judge",
            "council_judge",
            "judge-run",
            detail={"output": {"final_issues": [], "summary": "done"}},
        ),
    ]
    return TraceReport(
        run_id="flow-run",
        timestamp="2026-07-09T00:00:00",
        events=events,
    )


def _extract_trace_payload(html: str) -> dict:
    match = re.search(
        (
            r'<script id="trace-data" type="application/json">'
            r"(.*?)</script>"
        ),
        html,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _dashboard_template() -> str:
    return Path(
        "src/codeguard_agent/observability/dashboard_template.html"
    ).read_text(encoding="utf-8")


def test_trace_view_groups_reviewer_react_steps_and_state_writes():
    report = _flow_report_fixture()

    view = build_trace_view(report)

    assert [item["code_name"] for item in view["main_stages"]] == [
        "summary",
        "context_provider",
        "review_council",
        "coordination_loop",
        "council_judge",
    ]
    assert view["main_stages"][3]["summary"] == (
        "协调 2 次，证据补充 1 次，路由 2 次"
    )
    threat = next(
        item
        for item in view["reviewer_sections"]
        if item["key"] == "threat_model"
    )
    assert [
        view["steps"][step_id]["kind"]
        for step_id in threat["step_ids"]
    ] == ["node", "llm", "tool", "node"]
    assert threat["tool_call_count"] == 1
    assert threat["tool_step_ids"] == ["tool:tool-run"]
    tool_step = view["steps"][threat["tool_step_ids"][0]]
    assert tool_step["start_sequence"] == 10
    assert tool_step["end_sequence"] == 11
    assert tool_step["duration_ms"] == 10.0
    assert view["state_writes"]["raw_candidate_issues"][0]["step_id"]
    assert view["integrity"]["missing_end_count"] == 0


def test_trace_view_main_stages_resolve_without_copying_state_values():
    view = build_trace_view(_flow_report_fixture())

    assert all(
        stage["step_id"] in view["steps"]
        for stage in view["main_stages"]
    )
    assert view["steps"]["group:review_council"]["kind"] == "group"
    assert {"diff_summary", "context_bundle", "evidence_notes", "final_issues"} <= set(
        view["state_writes"]
    )
    assert all(
        "value" not in write
        for writes in view["state_writes"].values()
        for write in writes
    )


def test_trace_view_renders_phase5_task_chain_and_direct_discoverers():
    events = []
    sequence = 0

    def node(name: str, output: dict) -> None:
        nonlocal sequence
        sequence += 1
        run_id = f"{name}-run"
        events.append(_flow_event(sequence, "node_start", name, name, run_id))
        sequence += 1
        events.append(
            _flow_event(
                sequence,
                "node_end",
                name,
                name,
                run_id,
                detail={"output": output},
            )
        )

    node(
        "classify_mode",
        {
            "review_mode": "large",
            "review_route": {
                "initial_mode": "large",
                "effective_mode": "large",
                "selected_node": "diff_task_builder",
                "fallback": False,
                "metrics": {"file_count": 16, "hunk_count": 20, "diff_chars": 70000},
            },
        },
    )
    node("diff_task_builder", {"review_tasks": [{"id": "task-1"}]})
    node("risk_triage", {"risk_priors": {"task-1": {}}})
    node("task_rank", {"task_selection": {"selected_task_ids": ["task-1"]}})
    node("review_coverage", {"review_coverage_plan": {"assignments": []}})
    node("summary", {"diff_summary": "summary"})
    node("context_provider", {"task_context_bundles": {"task-1": {}}})
    for reviewer in (
        "discover_threat_model",
        "discover_behavior",
        "discover_maintainability",
    ):
        node(reviewer, {"raw_candidate_issues": []})
    node("council_coordinator", {"council_trace": []})
    node("evidence_planner", {"evidence_requests": []})
    node("evidence_agent", {"evidence_notes": []})
    node("council_judge", {"final_issues": [], "council_stats": {}})

    view = build_trace_view(
        TraceReport(
            run_id="phase5-run",
            timestamp="2026-07-14T00:00:00",
            events=events,
        )
    )

    assert [stage["code_name"] for stage in view["main_stages"]] == [
        "classify_mode",
        "diff_task_builder",
        "risk_triage",
        "task_rank",
        "review_coverage",
        "summary",
        "context_provider",
        "review_council",
        "coordination_loop",
        "council_judge",
    ]
    assert view["routing"] == {
        "initial_mode": "large",
        "effective_mode": "large",
        "selected_node": "diff_task_builder",
        "fallback": False,
        "metrics": {"file_count": 16, "hunk_count": 20, "diff_chars": 70000},
    }
    assert all(section["step_ids"] for section in view["reviewer_sections"])
    assert all(
        section["tool_call_count"] == 0
        and section["tool_step_ids"] == []
        for section in view["reviewer_sections"]
    )
    assert {
        view["steps"][step_id]["code_name"]
        for step_id in view["coordination_steps"]
    } == {
        "council_coordinator",
        "evidence_planner",
        "evidence_agent",
        "council_judge",
    }
    assert {"review_tasks", "risk_priors", "task_selection", "evidence_requests"} <= set(
        view["state_writes"]
    )


def test_trace_view_marks_small_pipeline_stages_as_intentionally_skipped():
    events = [
        _flow_event(1, "node_start", "classify_mode", "classify_mode", "classify"),
        _flow_event(
            2,
            "node_end",
            "classify_mode",
            "classify_mode",
            "classify",
            detail={
                "output": {
                    "review_mode": "small",
                    "review_route": {
                        "initial_mode": "small",
                        "effective_mode": "small",
                        "selected_node": "direct_review",
                        "fallback": False,
                        "metrics": {
                            "file_count": 1,
                            "hunk_count": 1,
                            "diff_chars": 1200,
                        },
                    },
                }
            },
        ),
        _flow_event(3, "node_start", "direct_review", "direct_review", "direct"),
        _flow_event(
            4,
            "node_end",
            "direct_review",
            "direct_review",
            "direct",
            detail={
                "output": {
                    "direct_review_status": "completed",
                    "final_issues": [],
                }
            },
        ),
    ]

    view = build_trace_view(
        TraceReport(
            run_id="small-route",
            timestamp="2026-07-29T00:00:00",
            events=events,
        )
    )

    assert [stage["code_name"] for stage in view["main_stages"][:2]] == [
        "classify_mode",
        "direct_review",
    ]
    skipped = view["main_stages"][2:]
    assert skipped
    assert all(stage["status"] == "skipped" for stage in skipped)
    assert all("small 模式按设计跳过" in stage["summary"] for stage in skipped)
    assert view["routing"]["selected_node"] == "direct_review"
    assert view["integrity"]["status"] == "complete"


def test_trace_view_does_not_treat_unstarted_small_direct_as_success():
    report = TraceReport(
        run_id="small-before-direct",
        timestamp="2026-07-29T00:00:00",
        events=[
            _flow_event(
                1,
                "node_start",
                "classify_mode",
                "classify_mode",
                "classify",
            ),
            _flow_event(
                2,
                "node_end",
                "classify_mode",
                "classify_mode",
                "classify",
                detail={"output": {"review_mode": "small"}},
            ),
        ],
    )

    view = build_trace_view(report)

    direct = next(
        stage
        for stage in view["main_stages"]
        if stage["code_name"] == "direct_review"
    )
    assert direct["status"] == "missing"
    assert all(
        stage["status"] != "skipped"
        for stage in view["main_stages"]
        if stage["code_name"] != "classify_mode"
    )


def test_trace_view_infers_medium_file_route_from_pre_structured_trace():
    events = [
        _flow_event(1, "node_start", "classify_mode", "classify_mode", "classify"),
        _flow_event(
            2,
            "node_end",
            "classify_mode",
            "classify_mode",
            "classify",
            detail={"output": {"review_mode": "medium"}},
        ),
        _flow_event(3, "node_start", "file_task_builder", "file_task_builder", "file"),
        _flow_event(
            4,
            "node_end",
            "file_task_builder",
            "file_task_builder",
            "file",
            detail={"output": {"review_tasks": []}},
        ),
    ]
    view = build_trace_view(
        TraceReport(
            run_id="legacy-medium-route",
            timestamp="2026-07-29T00:00:00",
            events=events,
        )
    )

    assert view["routing"]["initial_mode"] == "medium"
    assert view["routing"]["selected_node"] == "file_task_builder"
    assert [stage["code_name"] for stage in view["main_stages"][:2]] == [
        "classify_mode",
        "file_task_builder",
    ]


def test_trace_view_shows_small_direct_fallback_to_file_pipeline():
    def pair(sequence: int, name: str, output: dict) -> list[TraceEvent]:
        return [
            _flow_event(sequence, "node_start", name, name, f"{name}-run"),
            _flow_event(
                sequence + 1,
                "node_end",
                name,
                name,
                f"{name}-run",
                detail={"output": output},
            ),
        ]

    events = [
        *pair(
            1,
            "classify_mode",
            {
                "review_mode": "small",
                "review_route": {
                    "initial_mode": "small",
                    "effective_mode": "small",
                    "selected_node": "direct_review",
                    "fallback": False,
                    "metrics": {"file_count": 2, "hunk_count": 2, "diff_chars": 3000},
                },
            },
        ),
        *pair(
            3,
            "direct_review",
            {
                "direct_review_status": "fallback",
                "review_mode": "medium",
                "review_route": {
                    "initial_mode": "small",
                    "effective_mode": "medium",
                    "selected_node": "file_task_builder",
                    "fallback": True,
                    "fallback_reason": "structured_output_missing",
                    "metrics": {"file_count": 2, "hunk_count": 2, "diff_chars": 3000},
                },
            },
        ),
        *pair(5, "file_task_builder", {"review_tasks": [{"id": "file-task"}]}),
        *pair(7, "risk_triage", {"risk_priors": {}}),
        *pair(9, "task_rank", {"task_selection": {}}),
        *pair(11, "review_coverage", {"review_coverage_plan": {}}),
        *pair(13, "context_provider", {"task_context_bundles": {}}),
    ]
    view = build_trace_view(
        TraceReport(
            run_id="fallback-route",
            timestamp="2026-07-29T00:00:00",
            events=events,
        )
    )

    assert [stage["code_name"] for stage in view["main_stages"][:6]] == [
        "classify_mode",
        "direct_review",
        "file_task_builder",
        "risk_triage",
        "task_rank",
        "review_coverage",
    ]
    assert view["routing"]["initial_mode"] == "small"
    assert view["routing"]["effective_mode"] == "medium"
    assert view["routing"]["fallback"] is True
    assert view["routing"]["fallback_reason"] == "structured_output_missing"


def test_trace_view_shows_discovery_only_terminal_and_skips_judge():
    events = [
        _flow_event(
            1,
            "node_start",
            "discovery_collector",
            "discovery_collector",
            "collector",
        ),
        _flow_event(
            2,
            "node_end",
            "discovery_collector",
            "discovery_collector",
            "collector",
            detail={"output": {"final_issues": []}},
        ),
    ]
    view = build_trace_view(
        TraceReport(
            run_id="discovery-only",
            timestamp="2026-07-29T00:00:00",
            events=events,
        )
    )

    assert any(
        stage["code_name"] == "discovery_collector"
        and stage["status"] == "complete"
        for stage in view["main_stages"]
    )
    judge = next(
        stage
        for stage in view["main_stages"]
        if stage["code_name"] == "council_judge"
    )
    assert judge["status"] == "skipped"
    assert "discovery_only" in judge["summary"]


def test_trace_view_indexes_state_writes_from_hidden_discover_nodes():
    report = TraceReport(
        run_id="discover-state-run",
        timestamp="2026-07-10T00:00:00",
        events=[
            _flow_event(
                1,
                "node_start",
                "discover_behavior",
                "discover_behavior",
                "discover-behavior-run",
            ),
            _flow_event(
                2,
                "node_end",
                "discover_behavior",
                "discover_behavior",
                "discover-behavior-run",
                detail={
                    "output": {
                        "raw_candidate_issues": [{"type": "null-deref"}],
                        "evidence_requests": [{"candidate_id": "c1"}],
                    }
                },
            ),
        ],
    )

    view = build_trace_view(report)

    assert "raw_candidate_issues" in view["state_writes"]
    candidate_write = view["state_writes"]["raw_candidate_issues"][0]
    assert candidate_write["node_path"] == "discover_behavior"
    assert candidate_write["step_id"] in view["steps"]


def test_trace_view_reports_missing_and_unassociated_events():
    report = TraceReport(
        run_id="incomplete-run",
        timestamp="2026-07-09T00:00:00",
        events=[
            _flow_event(
                1,
                "node_start",
                "summary",
                "summary",
                "missing-end-run",
            ),
            _flow_event(
                2,
                "llm_start",
                "unknown",
                "unknown",
                "unassociated-run",
            ),
            _flow_event(
                3,
                "llm_end",
                "unknown",
                "unknown",
                "unassociated-run",
            ),
        ],
    )

    view = build_trace_view(report)

    assert view["integrity"]["missing_end_count"] == 1
    assert view["integrity"]["unassociated_count"] == 2
    assert view["integrity"]["status"] == "incomplete"
    assert all(
        stage["step_id"] in view["steps"]
        for stage in view["main_stages"]
    )
    assert view["steps"]["placeholder:council_judge"]["status"] == "missing"


def test_trace_view_treats_tool_error_as_a_completed_failed_call():
    report = TraceReport(
        run_id="tool-error-run",
        timestamp="2026-07-26T00:00:00",
        events=[
            _flow_event(
                1,
                "tool_start",
                "review",
                "discover_behavior/review",
                "tool-run",
                detail={
                    "tool_name": "inspect_structure",
                    "input": {"symbol": "execute"},
                },
            ),
            _flow_event(
                2,
                "tool_error",
                "review",
                "discover_behavior/review",
                "tool-run",
                detail={
                    "tool_name": "inspect_structure",
                    "output": {
                        "type": "RuntimeError",
                        "message": "gateway unavailable",
                    },
                },
            ),
        ],
    )

    view = build_trace_view(report)
    behavior = next(
        section
        for section in view["reviewer_sections"]
        if section["key"] == "behavior"
    )
    tool_step = view["steps"][behavior["tool_step_ids"][0]]

    assert behavior["tool_call_count"] == 1
    assert tool_step["status"] == "failed"
    assert tool_step["start_sequence"] == 1
    assert tool_step["end_sequence"] == 2
    assert view["integrity"]["status"] == "complete"


def test_trace_view_builds_reviewer_tool_steps_from_node_output_without_native_events():
    report = TraceReport(
        run_id="application-tool-run",
        timestamp="2026-07-26T00:00:00",
        events=[
            _flow_event(
                1,
                "node_start",
                "discover_behavior",
                "discover_behavior",
                "discover-run",
            ),
            _flow_event(
                2,
                "node_end",
                "discover_behavior",
                "discover_behavior",
                "discover-run",
                detail={
                    "output": {
                        "gathered_context": [
                            {
                                "tool": "inspect_change_impact",
                                "args": '{"symbol_id":"java:demo.OrderService"}',
                                "content": '{"status":"confirmed"}',
                            }
                        ]
                    }
                },
            ),
        ],
    )

    view = build_trace_view(report)
    behavior = next(
        section
        for section in view["reviewer_sections"]
        if section["key"] == "behavior"
    )
    tool_step = view["steps"][behavior["tool_step_ids"][0]]

    assert behavior["tool_call_count"] == 1
    assert tool_step["code_name"] == "inspect_change_impact"
    assert tool_step["input"] == {"symbol_id": "java:demo.OrderService"}
    assert tool_step["output"] == '{"status":"confirmed"}'
    assert tool_step["status"] == "complete"


def test_trace_view_keeps_each_reviewer_tool_record_including_reuse():
    report = TraceReport(
        run_id="reviewer-tool-reuse",
        timestamp="2026-07-26T00:00:00",
        events=[
            _flow_event(
                1,
                "node_start",
                "discover_behavior",
                "discover_behavior",
                "discover-run",
            ),
            _flow_event(
                2,
                "node_end",
                "discover_behavior",
                "discover_behavior",
                "discover-run",
                detail={
                    "output": {
                        "tool_trace_records": [
                            {
                                "call_id": "call-1",
                                "tool": "inspect_change_impact",
                                "arguments": {"symbol_id": "java:demo.Service"},
                                "output": '{"status":"confirmed"}',
                                "duration_ms": 4.0,
                                "status": "complete",
                                "reuse_key": "impact:service",
                                "reused_from_call_id": "",
                            },
                            {
                                "call_id": "call-2",
                                "tool": "inspect_change_impact",
                                "arguments": {"symbol_id": "java:demo.Service"},
                                "output": "reuse marker",
                                "duration_ms": 0.1,
                                "status": "reused",
                                "reuse_key": "impact:service",
                                "reused_from_call_id": "call-1",
                            },
                        ]
                    }
                },
            ),
        ],
    )

    view = build_trace_view(report)
    behavior = next(
        section
        for section in view["reviewer_sections"]
        if section["key"] == "behavior"
    )
    tool_steps = [
        view["steps"][step_id] for step_id in behavior["tool_step_ids"]
    ]

    assert behavior["tool_call_count"] == 2
    assert [step["status"] for step in tool_steps] == ["complete", "reused"]
    assert tool_steps[1]["reuse_key"] == "impact:service"
    assert tool_steps[1]["reused_from_call_id"] == "call-1"


def test_trace_view_shows_evidence_tool_reuse_as_a_separate_step():
    report = TraceReport(
        run_id="evidence-tool-reuse",
        timestamp="2026-07-26T00:00:00",
        events=[
            _flow_event(
                1,
                "node_start",
                "evidence_agent",
                "evidence_agent",
                "evidence-run",
            ),
            _flow_event(
                2,
                "node_end",
                "evidence_agent",
                "evidence_agent",
                "evidence-run",
                detail={
                    "output": {
                        "gathered_context": [
                            {
                                "tool": "inspect_security_path",
                                "args": '{"symbol_id":"java:demo.Service"}',
                                "content": '{"status":"confirmed"}',
                                "duration_ms": 4.5,
                                "status": "complete",
                            }
                        ],
                        "council_trace": [
                            {
                                "node": "evidence_agent",
                                "event": "evidence_tool_called",
                                "detail": json.dumps(
                                    {
                                        "call_id": "evidence-call-1",
                                        "tool": "inspect_security_path",
                                        "arguments": {
                                            "symbol_id": "java:demo.Service"
                                        },
                                        "reuse_key": "security:service",
                                    }
                                ),
                            },
                            {
                                "node": "evidence_agent",
                                "event": "evidence_tool_reused",
                                "detail": json.dumps(
                                    {
                                        "tool": "inspect_security_path",
                                        "arguments": {
                                            "symbol_id": "java:demo.Service"
                                        },
                                        "evidence_id": "evidence-1",
                                        "reuse_key": "security:service",
                                        "reused_from_call_id": "evidence-call-1",
                                        "output": '{"status":"confirmed"}',
                                    }
                                ),
                            }
                        ],
                    }
                },
            ),
        ],
    )

    view = build_trace_view(report)
    tool_steps = [
        view["steps"][step_id]
        for step_id in view["coordination_steps"]
        if view["steps"][step_id]["kind"] == "tool"
    ]

    assert [step["status"] for step in tool_steps] == ["complete", "reused"]
    assert tool_steps[0]["duration_ms"] == 4.5
    assert tool_steps[0]["pair_id"] == "evidence-call-1"
    assert tool_steps[0]["reuse_key"] == "security:service"
    assert tool_steps[1]["input"] == {"symbol_id": "java:demo.Service"}
    assert tool_steps[1]["output"] == '{"status":"confirmed"}'
    assert tool_steps[1]["reuse_key"] == "security:service"
    assert tool_steps[1]["reused_from_call_id"] == "evidence-call-1"


def test_trace_view_exposes_evidence_batch_phase_metrics():
    metrics = {
        "request_count": 68,
        "fact_count": 121,
        "llm_analysis_calls": 68,
        "tool_unique_calls": 7,
        "tool_reused_calls": 16,
        "tool_collection_ms": 5.0,
        "fact_preparation_ms": 2.0,
        "fact_analysis_ms": 32000.0,
    }
    report = TraceReport(
        run_id="evidence-metrics",
        timestamp="2026-07-26T00:00:00",
        events=[
            _flow_event(
                1,
                "node_start",
                "evidence_agent",
                "evidence_agent",
                "evidence-run",
            ),
            _flow_event(
                2,
                "node_end",
                "evidence_agent",
                "evidence_agent",
                "evidence-run",
                detail={
                    "output": {
                        "council_trace": [
                            {
                                "node": "evidence_agent",
                                "event": "evidence_batch_metrics",
                                "detail": json.dumps(metrics),
                            }
                        ]
                    }
                },
            ),
        ],
    )

    view = build_trace_view(report)
    step = next(
        item
        for item in view["steps"].values()
        if item["code_name"] == "evidence_agent"
    )

    assert step["metrics"] == metrics
    assert "68 次 LLM" in step["summary"]
    assert "32.000s" in step["summary"]


def test_dashboard_payload_keeps_raw_report_and_adds_flow_view():
    report = _flow_report_fixture()

    payload = _extract_trace_payload(render_dashboard(report))

    assert payload["events"] == report.model_dump(mode="json")["events"]
    assert payload["view"]["reviewer_sections"][0]["step_ids"]
    assert payload["view"]["integrity"]["event_count"] == len(report.events)


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
            "summary", "classify_mode", "direct_review", "file_task_builder",
            "diff_task_builder", "risk_triage", "task_rank", "review_coverage",
            "context_provider",
            "discover_threat_model", "discover_behavior", "discover_maintainability",
            "discovery_collector", "council_coordinator", "concern_analyzer",
            "evidence_planner", "evidence_agent", "council_judge",
            "evidence_strategist", "evidence_researcher", "impact_assessor",
            "prepare", "review", "collect",
        }
        assert set(_NODE_PHASE_MAP.keys()) == expected

    def test_unknown_node_falls_back_to_outer(self):
        assert _phase_for("nonexistent") == "outer_graph"

    def test_known_nodes(self):
        assert _phase_for("discover_threat_model") == "reviewer_subgraph"
        assert _phase_for("council_judge") == "judge"
        assert _phase_for("evidence_agent") == "evidence"
        assert _phase_for("evidence_strategist") == "evidence"
        assert _phase_for("evidence_researcher") == "evidence"
        assert _phase_for("impact_assessor") == "judge"
        assert _phase_for("summary") == "outer_graph"


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

        assert llm.node_path == "discover_threat_model/review"
        assert llm.detail["messages"][0]["content"] == "prompt" * 1000
        assert tool.node_path == "discover_threat_model/review"
        assert tool.detail["input"]["content"] == "x" * 5000

    def test_tool_errors_are_collected_as_visible_outputs(self):
        collector = _TraceCollector("diff", "trace-run")
        collector._handle_event(_chain_event(
            "on_chain_start",
            name="review",
            run_id="review-run",
            parent_ids=["root", "discover-run", "subgraph-root"],
            node_name="review",
            checkpoint_ns="discover_behavior:uuid|review:uuid",
            data={"input": {"user_prompt": "review me"}},
        ))
        collector._handle_event({
            "event": "on_tool_error",
            "name": "inspect_structure",
            "run_id": "tool-run",
            "parent_ids": ["root", "review-run"],
            "metadata": {},
            "data": {"error": RuntimeError("gateway unavailable")},
        })

        error = next(
            event
            for event in collector.finalize().events
            if event.event_type == "tool_error"
        )

        assert error.node_path == "discover_behavior/review"
        assert error.detail["output"]["type"] == "RuntimeError"
        assert error.detail["output"]["message"] == "gateway unavailable"

    def test_all_reviewers_keep_multiple_tool_inputs_and_outputs_in_dashboard(
        self,
    ):
        collector = _TraceCollector("diff", "trace-run")
        reviewers = (
            "discover_threat_model",
            "discover_behavior",
            "discover_maintainability",
        )
        for reviewer in reviewers:
            reviewer_run = f"{reviewer}-run"
            review_run = f"{reviewer}-review-run"
            collector._handle_event(_chain_event(
                "on_chain_start",
                name=reviewer,
                run_id=reviewer_run,
                parent_ids=["root"],
                node_name=reviewer,
                data={"input": {}},
            ))
            collector._handle_event(_chain_event(
                "on_chain_start",
                name="review",
                run_id=review_run,
                parent_ids=["root", reviewer_run, "subgraph-root"],
                node_name="review",
                checkpoint_ns=f"{reviewer}:uuid|review:uuid",
                data={"input": {"reviewer": reviewer}},
            ))
            for index, tool_name in enumerate(
                ("get_file_content", "inspect_structure"),
                start=1,
            ):
                tool_run = f"{reviewer}-tool-{index}"
                collector._handle_event({
                    "event": "on_tool_start",
                    "name": tool_name,
                    "run_id": tool_run,
                    "parent_ids": ["root", reviewer_run, review_run],
                    "metadata": {},
                    "data": {
                        "input": {
                            "reviewer": reviewer,
                            "call": index,
                        }
                    },
                })
                terminal_event = (
                    {
                        "event": "on_tool_end",
                        "data": {
                            "output": {
                                "reviewer": reviewer,
                                "result": index,
                            }
                        },
                    }
                    if index == 1
                    else {
                        "event": "on_tool_error",
                        "data": {
                            "error": RuntimeError(
                                f"{reviewer} gateway unavailable"
                            )
                        },
                    }
                )
                collector._handle_event({
                    **terminal_event,
                    "name": tool_name,
                    "run_id": tool_run,
                    "parent_ids": ["root", reviewer_run, review_run],
                    "metadata": {},
                })
            collector._handle_event(_chain_event(
                "on_chain_end",
                name="review",
                run_id=review_run,
                parent_ids=["root", reviewer_run, "subgraph-root"],
                node_name="review",
                checkpoint_ns=f"{reviewer}:uuid|review:uuid",
                data={"output": {}},
            ))
            collector._handle_event(_chain_event(
                "on_chain_end",
                name=reviewer,
                run_id=reviewer_run,
                parent_ids=["root"],
                node_name=reviewer,
                data={"output": {}},
            ))

        report = collector.finalize()
        payload = _extract_trace_payload(render_dashboard(report))
        view = payload["view"]
        events_by_sequence = {
            event["sequence"]: event for event in payload["events"]
        }

        for section in view["reviewer_sections"]:
            assert section["tool_call_count"] == 2
            tool_steps = [
                view["steps"][step_id]
                for step_id in section["tool_step_ids"]
            ]
            assert [step["code_name"] for step in tool_steps] == [
                "get_file_content",
                "inspect_structure",
            ]
            assert [step["status"] for step in tool_steps] == [
                "complete",
                "failed",
            ]
            first_input = events_by_sequence[
                tool_steps[0]["start_sequence"]
            ]["detail"]["input"]
            first_output = events_by_sequence[
                tool_steps[0]["end_sequence"]
            ]["detail"]["output"]
            failed_output = events_by_sequence[
                tool_steps[1]["end_sequence"]
            ]["detail"]["output"]
            assert first_input["call"] == 1
            assert first_output["result"] == 1
            assert failed_output["type"] == "RuntimeError"
            assert "gateway unavailable" in failed_output["message"]
        assert view["integrity"]["status"] == "complete"


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
        template = _dashboard_template()

        assert "节点输入" in template
        assert "节点输出" in template
        assert "原始 JSON" in template
        assert "renderJsonValue" in template

    def test_uses_narrative_layout_and_stable_step_identity(self):
        template = _dashboard_template()

        assert 'id="trace-outline"' in template
        assert 'id="trace-story"' in template
        assert 'id="trace-inspector"' in template
        assert "renderMainFlow" in template
        assert "renderReviewerSection" in template
        assert "renderStateEvolution" in template
        assert "renderRawEvents" in template

    def test_reviewer_cards_render_tool_counts_inputs_and_outputs(self):
        template = _dashboard_template()

        assert "tool_call_count" in template
        assert "renderToolPayloads" in template
        assert "工具入参" in template
        assert "工具输出" in template

    def test_preserves_reading_position_for_local_updates(self):
        template = _dashboard_template()

        assert "captureReadingPosition" in template
        assert "restoreReadingPosition" in template
        assert "renderPreservingReadingPosition" in template
        assert "selectStep" in template
        assert "stepMatchesSearch" in template
        assert "firstAnomalousStep" in template
        assert "stepRelationClass" in template

    def test_json_details_render_as_collapsed_tree(self):
        template = _dashboard_template()

        assert "renderJsonTree" in template
        assert "renderJsonBranch" in template
        assert "json-tree" in template
        assert "json-summary" in template
        assert "json-long-string" in template

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

    def test_render_dashboard_file_includes_timestamp(self, tmp_path):
        report = TraceReport(
            run_id="abc12345",
            timestamp="2026-07-09T20:30:45",
        )

        path = render_dashboard_file(
            report,
            str(tmp_path),
            report.run_id,
        )

        assert path.name == "trace-20260709-203045-abc12345.html"

    def test_template_displays_report_timestamp(self):
        template = _dashboard_template()

        assert "生成时间" in template
        assert "DATA.timestamp" in template


class TestCliTraceConfig:
    def test_review_uses_trace_setting_when_cli_flag_is_omitted(
        self,
        monkeypatch,
        caplog,
    ):
        from codeguard_agent import cli
        from codeguard_agent.config import Settings
        from codeguard_agent.models.schemas import ReviewResult

        observed = {}
        caplog.set_level(logging.INFO, logger="codeguard")

        class FakeOrchestrator:
            def __init__(self, **kwargs):
                pass

            def run(self, *args, **kwargs):
                observed["trace_enabled"] = kwargs["trace_enabled"]
                return ReviewResult(summary="done")

        monkeypatch.setattr(
            cli.Settings,
            "from_env",
            lambda: Settings(
                provider="mock",
                model="",
                api_key="",
                api_base_url="",
                max_retries=1,
                structured_method="function_calling",
                disable_thinking=False,
                trace_enabled=False,
            ),
        )
        monkeypatch.setattr(cli, "collect_diff", lambda repo, base: "diff --git a/Foo.java b/Foo.java\n+x\n")
        monkeypatch.setattr(cli, "build_llm", lambda settings, temperature=None: None)
        monkeypatch.setattr(cli, "PipelineOrchestrator", FakeOrchestrator)

        assert cli.main(["review", "--repo", "."]) == 0

        assert observed["trace_enabled"] is False
        assert "coordinator → planner → evidence → judge" in caplog.text

    def test_review_trace_flag_overrides_environment_setting(
        self,
        monkeypatch,
    ):
        from codeguard_agent import cli
        from codeguard_agent.config import Settings
        from codeguard_agent.models.schemas import ReviewResult

        observed = {}

        class FakeOrchestrator:
            def __init__(self, **kwargs):
                pass

            def run(self, *args, **kwargs):
                observed["trace_enabled"] = kwargs["trace_enabled"]
                return ReviewResult(summary="done")

        monkeypatch.setattr(
            cli.Settings,
            "from_env",
            lambda: Settings(
                provider="mock",
                model="",
                api_key="",
                api_base_url="",
                max_retries=1,
                structured_method="function_calling",
                disable_thinking=False,
                trace_enabled=False,
            ),
        )
        monkeypatch.setattr(cli, "collect_diff", lambda repo, base: "diff --git a/Foo.java b/Foo.java\n+x\n")
        monkeypatch.setattr(cli, "build_llm", lambda settings, temperature=None: None)
        monkeypatch.setattr(cli, "PipelineOrchestrator", FakeOrchestrator)

        assert cli.main(["review", "--repo", ".", "--trace"]) == 0

        assert observed["trace_enabled"] is True


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
            assert report_data["view"]["main_stages"]
            assert len(report_data["view"]["reviewer_sections"]) == 3
            assert "integrity" in report_data["view"]
            assert 'id="trace-story"' in content
            assert "_prototype_trace_flow" not in content
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
