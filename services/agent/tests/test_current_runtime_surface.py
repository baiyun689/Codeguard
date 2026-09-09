"""Offline contracts for the sole bounded runtime and its reporting surface."""

import pytest

from codeguard_agent.models.evidence import (
    ArtifactAvailability,
    EvidenceArtifact,
    EvidenceCaptureMode,
    EvidenceSourceKind,
)
from codeguard_agent.observability.models import TraceEvent, TraceReport
from codeguard_agent.observability.view_model import build_trace_view
from codeguard_agent.pipeline.orchestration.graph import build_review_graph
from codeguard_agent.pipeline.orchestration.orchestrator import _artifact_tool_profile


def test_graph_has_no_historical_planner_or_reviewer_entries():
    nodes = set(build_review_graph().get_graph().nodes)
    assert nodes == {
        "__start__",
        "__end__",
        "classify_mode",
        "file_task_builder",
        "diff_task_builder",
        "task_route",
        "direct_task_review",
        "task_selection",
        "symbol_resolution",
        "controlled_review",
        "council_coordinator",
        "evidence_verifier",
        "council_judge",
        "causal_merge",
    }
    with pytest.raises(ValueError, match="historical engines were removed"):
        build_review_graph(discovery_mode="react")


def test_eval_tool_profile_exports_executed_artifacts_without_retired_engine():
    artifact = EvidenceArtifact.build(
        task_id="task",
        reviewer="behavior",
        revision="rev",
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool="read_symbol",
        arguments={"symbol_id": "java:A#run()"},
        payload="real source",
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )
    reused = artifact.model_copy(update={"capture_mode": EvidenceCaptureMode.REUSED})
    rows = _artifact_tool_profile({"first": artifact, "repeat": reused})
    assert len(rows) == 1
    assert rows[0].tool == "read_symbol"
    assert rows[0].content == "real source"
    assert "java:A#run()" in rows[0].args


def test_trace_shows_real_change_groups_without_fake_planning_stages():
    output = {
        "controlled_subtask_plans": {
            "task": {
                "task_id": "task",
                "subtasks": [
                    {
                        "subtask_id": "group-1",
                        "objective": "Inspect changed behavior",
                        "initial_symbol_ids": ["java:A#run()"],
                    },
                ],
            }
        },
        "controlled_subtask_outcomes": {"task:group-1": "no_finding"},
    }
    report = TraceReport(
        run_id="offline",
        timestamp="2026-09-09",
        events=[
            TraceEvent(
                sequence=1,
                timestamp_ms=0,
                event_type="node_start",
                node_name="controlled_review",
                node_path="controlled_review",
                run_id="node",
                phase="reviewer_subgraph",
                depth=0,
                summary="review",
            ),
            TraceEvent(
                sequence=2,
                timestamp_ms=10,
                event_type="node_end",
                node_name="controlled_review",
                node_path="controlled_review",
                run_id="node",
                phase="reviewer_subgraph",
                depth=0,
                summary="review",
                detail={"output": output},
            ),
        ],
    )
    view = build_trace_view(report)
    assert [stage["code_name"] for stage in view["main_stages"]] == [
        "controlled_review"
    ]
    assert len(view["controlled_sections"]) == 1
    group = view["steps"]["investigation:task:group-1"]
    assert group["subtask_id"] == "group-1"
    assert group["summary"] == "no_finding"
    assert not any(
        key.startswith(("placeholder:", "group:review_council"))
        for key in view["steps"]
    )
