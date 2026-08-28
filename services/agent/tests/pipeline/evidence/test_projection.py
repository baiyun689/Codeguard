from __future__ import annotations

import json

from codeguard_agent.pipeline.evidence.projection import (
    ProjectionAudience,
    project_tool_payload,
)


def _graph_payload() -> str:
    return json.dumps({
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "source_scope": "MAIN",
        "subject_symbol_id": "java:demo.Service#m()",
        "symbols": [{
            "id": "java:demo.Service#m()",
            "kind": "method",
            "file": "src/Service.java",
            "startLine": 10,
            "endLine": 15,
            "source_set": "MAIN",
            "signature": "void m()",
            "annotations": ["Transactional"],
        }],
        "relationships": [{
            "sourceId": "java:demo.Controller#run()",
            "targetId": "java:demo.Service#m()",
            "kind": "calls",
            "file": "src/Controller.java",
            "line": 20,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
            "diagnostic": "not for reviewer",
        }],
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
        "snapshot_main_coverage": "partial",
    }, ensure_ascii=False)


def test_reviewer_graph_projection_keeps_reasoning_fields_not_raw_diagnostics():
    raw = _graph_payload()

    projection = project_tool_payload(
        "inspect_change_impact", raw, ProjectionAudience.REVIEWER
    )

    content = json.loads(projection.content)
    assert content["outcome"] == "found"
    assert content["relationships"][0]["targetId"] == "java:demo.Service#m()"
    assert content["symbols"][0]["source_set"] == "MAIN"
    assert "snapshot_main_coverage" not in content
    assert "diagnostic" not in content["relationships"][0]
    assert projection.summary == "found/complete · 已解析 1 · 未解析 0"


def test_reviewer_file_projection_preserves_complete_content():
    raw = "文件: src/A.java\n" + "class A {}\n" * 100

    projection = project_tool_payload(
        "get_file_content", raw, ProjectionAudience.REVIEWER
    )

    assert projection.content == raw
    assert projection.truncated is False


def test_evidence_projection_never_changes_original_payload():
    raw = _graph_payload()

    projection = project_tool_payload(
        "inspect_structure", raw, ProjectionAudience.EVIDENCE
    )

    assert projection.content == raw
    assert projection.truncated is False


def test_reviewer_invalid_graph_projection_never_falls_back_to_raw_payload():
    raw = "BROKEN_GRAPH_SECRET"

    projection = project_tool_payload(
        "inspect_path", raw, ProjectionAudience.REVIEWER
    )

    assert raw not in projection.content
    content = json.loads(projection.content)
    assert content["outcome"] == "indeterminate"
    assert content["limitations"] == ["graph_projection_unavailable"]


def test_reviewer_malformed_v2_collections_fail_closed():
    raw = json.dumps({
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "symbols": [],
        "relationships": 42,
        "unresolved_relationships": [],
        "limitations": [],
    })

    projection = project_tool_payload(
        "inspect_structure", raw, ProjectionAudience.REVIEWER
    )

    content = json.loads(projection.content)
    assert content["outcome"] == "indeterminate"
    assert content["limitations"] == ["graph_projection_unavailable"]


def test_reviewer_invalid_v2_contract_fails_closed():
    raw = json.dumps({
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "source_scope": "MAIN",
        "subject_symbol_id": "java:A#m()",
        "symbols": [],
        "relationships": [],
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
    })

    projection = project_tool_payload(
        "inspect_change_impact", raw, ProjectionAudience.REVIEWER
    )

    content = json.loads(projection.content)
    assert content["outcome"] == "indeterminate"
    assert content["limitations"] == ["graph_projection_unavailable"]


def test_reviewer_subject_mismatch_fails_closed_when_arguments_are_known():
    raw = _graph_payload()

    projection = project_tool_payload(
        "inspect_change_impact",
        raw,
        ProjectionAudience.REVIEWER,
        arguments={"symbol_id": "java:other.Service#run()"},
    )

    content = json.loads(projection.content)
    assert content["outcome"] == "indeterminate"
    assert content["limitations"] == ["graph_projection_unavailable"]
