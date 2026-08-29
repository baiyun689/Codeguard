from __future__ import annotations

import json

from codeguard_agent.pipeline.evidence.projection import (
    GraphProjectionFocus,
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


def _deep_behavior_payload(edge_count: int = 128) -> str:
    subject = "java:retry.RetryTemplate#doExecute()"
    first = "java:retry.RetryTemplate#doOpenInterceptors()"
    listener = "java:retry.RetryListener#open()"
    internal = "java:retry.RetryTemplate#doOpenInternal()"
    context = "java:retry.RetrySynchronizationManager#getContext()"
    relationships = [
        {
            "sourceId": subject,
            "targetId": first,
            "kind": "CALLS",
            "file": "src/RetryTemplate.java",
            "line": 101,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        },
        {
            "sourceId": first,
            "targetId": listener,
            "kind": "LISTENS_TO_EVENT",
            "file": "src/RetryTemplate.java",
            "line": 115,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        },
        {
            "sourceId": first,
            "targetId": internal,
            "kind": "CALLS",
            "file": "src/RetryTemplate.java",
            "line": 116,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        },
        {
            "sourceId": internal,
            "targetId": context,
            "kind": "CALLS",
            "file": "src/RetryTemplate.java",
            "line": 121,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        },
    ]
    for index in range(edge_count - len(relationships)):
        source = f"java:retry.Noise#{index}()"
        relationships.append({
            "sourceId": source,
            "targetId": f"java:retry.Noise#{index + 1}()",
            "kind": "CALLS",
            "file": "src/Noise.java",
            "line": index + 1,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        })
    symbols = []
    for symbol_id in {subject, first, listener, internal, context}:
        symbols.append({
            "id": symbol_id,
            "kind": "method",
            "file": "src/RetryTemplate.java",
            "startLine": 1,
            "endLine": 200,
            "source_set": "MAIN",
        })
    return json.dumps({
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "source_scope": "MAIN",
        "subject_symbol_id": subject,
        "symbols": symbols,
        "relationships": relationships,
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
    }, ensure_ascii=False)


def test_behavior_projection_preserves_complete_deep_path_and_focuses_changed_lines():
    raw = _deep_behavior_payload()
    focus = GraphProjectionFocus(
        changed_file="src/RetryTemplate.java",
        changed_lines=(101, 115, 116, 121),
        changed_symbol_ids=("java:retry.RetryTemplate#doExecute()",),
    )

    projection = project_tool_payload(
        "inspect_path",
        raw,
        ProjectionAudience.REVIEWER,
        arguments={
            "symbol_id": "java:retry.RetryTemplate#doExecute()",
            "path_kind": "behavior",
            "max_depth": 3,
        },
        focus=focus,
    )

    content = json.loads(projection.content)
    edges = {
        (item["sourceId"], item["targetId"], item["kind"])
        for item in content["relationships"]
    }
    assert (
        "java:retry.RetryTemplate#doExecute()",
        "java:retry.RetryTemplate#doOpenInterceptors()",
        "CALLS",
    ) in edges
    assert (
        "java:retry.RetryTemplate#doOpenInterceptors()",
        "java:retry.RetryTemplate#doOpenInternal()",
        "CALLS",
    ) in edges
    assert (
        "java:retry.RetryTemplate#doOpenInternal()",
        "java:retry.RetrySynchronizationManager#getContext()",
        "CALLS",
    ) in edges
    assert (
        "java:retry.RetryTemplate#doOpenInterceptors()",
        "java:retry.RetryListener#open()",
        "LISTENS_TO_EVENT",
    ) in edges
    assert content["omitted_count"] > 0
    assert "projection_truncated" in content["limitations"]


def test_behavior_projection_keeps_real_listener_and_state_branches_under_budget_pressure():
    subject = "java:retry.RetryTemplate#doExecute()"
    open_method = "java:retry.RetryTemplate#open()"
    interceptors = "java:retry.RetryTemplate#doOpenInterceptors()"
    listener = "java:retry.RetryListener#open()"
    internal = "java:retry.RetryTemplate#doOpenInternal()"
    context = "java:retry.RetrySynchronizationManager#getContext()"
    relationships = [
        {
            "sourceId": subject,
            "targetId": open_method,
            "kind": "CALLS",
            "file": "src/RetryTemplate.java",
            "line": 278,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        },
        {
            "sourceId": subject,
            "targetId": interceptors,
            "kind": "CALLS",
            "file": "src/RetryTemplate.java",
            "line": 291,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        },
        {
            "sourceId": interceptors,
            "targetId": listener,
            "kind": "CALLS",
            "file": "src/RetryTemplate.java",
            "line": 585,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        },
        {
            "sourceId": open_method,
            "targetId": internal,
            "kind": "CALLS",
            "file": "src/RetryTemplate.java",
            "line": 500,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        },
        {
            "sourceId": internal,
            "targetId": context,
            "kind": "CALLS",
            "file": "src/RetryTemplate.java",
            "line": 505,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        },
    ]
    # Many connected ordinary CALLS branches compete for the same path budget.
    for branch in range(80):
        branch_nodes = [
            f"java:retry.RetryTemplate#aaaNoiseComponent{branch}_{depth}"
            + ("x" * 48)
            + "()"
            for depth in range(4)
        ]
        for depth in range(3):
            relationships.append({
                "sourceId": subject if depth == 0 else branch_nodes[depth - 1],
                "targetId": branch_nodes[depth],
                "kind": "CALLS",
                "file": "src/RetryTemplate.java",
                "line": 600 + branch * 3 + depth,
                "source_set": "MAIN",
                "resolution": "RESOLVED",
            })
    # Callback paths use ordinary CALLS too; the target name is the only
    # stable semantic signal available from this Gateway response shape.
    for branch in range(40):
        callback = f"java:retry.Callback{branch}#callback()"
        callback_leaf = f"java:retry.Callback{branch}#invoke()"
        relationships.extend([
            {
                "sourceId": subject,
                "targetId": callback,
                "kind": "CALLS",
                "file": "src/RetryTemplate.java",
                "line": 800 + branch * 2,
                "source_set": "MAIN",
                "resolution": "RESOLVED",
            },
            {
                "sourceId": callback,
                "targetId": callback_leaf,
                "kind": "CALLS",
                "file": "src/RetryTemplate.java",
                "line": 801 + branch * 2,
                "source_set": "MAIN",
                "resolution": "RESOLVED",
            },
        ])
    # Subject-level field facts are intentionally noisy: they must not make
    # every traversal branch look semantic merely because it shares the root.
    for index in range(30):
        relationships.append({
            "sourceId": subject,
            "targetId": f"java:retry.RetryContext#attribute{index}",
            "kind": "READS_FIELD",
            "file": "src/RetryTemplate.java",
            "line": 700 + index,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        })
    symbol_ids = {item["sourceId"] for item in relationships}
    symbol_ids.update(item["targetId"] for item in relationships)
    raw = json.dumps({
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "source_scope": "MAIN",
        "subject_symbol_id": subject,
        "symbols": [
            {
                "id": symbol_id,
                "kind": "method",
                "file": "src/RetryTemplate.java",
                "startLine": 1,
                "endLine": 700,
                "source_set": "MAIN",
            }
            for symbol_id in sorted(symbol_ids)
        ],
        "relationships": relationships,
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
    }, ensure_ascii=False)
    content = json.loads(project_tool_payload(
        "inspect_path",
        raw,
        ProjectionAudience.REVIEWER,
        arguments={"symbol_id": subject, "path_kind": "behavior", "max_depth": 3},
        focus=GraphProjectionFocus(
            changed_file="src/RetryTemplate.java",
            changed_lines=(298, 299, 500),
            changed_symbol_ids=(subject, open_method),
        ),
    ).content)
    edges = {
        (item["sourceId"], item["targetId"])
        for item in content["relationships"]
    }
    assert (subject, interceptors) in edges
    assert (interceptors, listener) in edges
    assert (internal, context) in edges
    callback_edges = {
        edge for edge in edges if edge[1].startswith("java:retry.Callback")
    }
    assert callback_edges
    callback_branches = {
        edge[1] for edge in callback_edges if edge[0] == subject
    }
    assert len(callback_branches) <= 4


def test_path_family_prefers_changed_callsite_before_deduplication():
    subject = "java:demo.Service#run()"
    target = "java:demo.Service#helper()"
    raw = json.dumps({
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "source_scope": "MAIN",
        "subject_symbol_id": subject,
        "symbols": [
            {"id": subject, "kind": "method", "file": "src/Service.java",
             "startLine": 1, "endLine": 100, "source_set": "MAIN"},
            {"id": target, "kind": "method", "file": "src/Service.java",
             "startLine": 1, "endLine": 100, "source_set": "MAIN"},
        ],
        "relationships": [
            {"sourceId": subject, "targetId": target, "kind": "CALLS",
             "file": "src/Service.java", "line": 12, "source_set": "MAIN",
             "resolution": "RESOLVED"},
            {"sourceId": subject, "targetId": target, "kind": "CALLS",
             "file": "src/Service.java", "line": 88, "source_set": "MAIN",
             "resolution": "RESOLVED"},
        ],
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
    }, ensure_ascii=False)
    content = json.loads(project_tool_payload(
        "inspect_path",
        raw,
        ProjectionAudience.REVIEWER,
        arguments={"symbol_id": subject, "path_kind": "behavior"},
        focus=GraphProjectionFocus(
            changed_file="src/Service.java",
            changed_lines=(88,),
            changed_symbol_ids=(subject,),
        ),
    ).content)
    assert content["relationships"] == [{
        "sourceId": subject,
        "targetId": target,
        "kind": "CALLS",
        "file": "src/Service.java",
        "line": 88,
        "source_set": "MAIN",
        "resolution": "RESOLVED",
    }]


def test_reviewer_and_judge_projection_are_byte_identical_with_focus():
    raw = _deep_behavior_payload(24)
    focus = GraphProjectionFocus(
        changed_file="src/RetryTemplate.java",
        changed_lines=(101, 116, 121),
        changed_symbol_ids=("java:retry.RetryTemplate#doExecute()",),
    )
    kwargs = {
        "arguments": {
            "symbol_id": "java:retry.RetryTemplate#doExecute()",
            "path_kind": "behavior",
            "max_depth": 3,
        },
        "focus": focus,
    }
    reviewer = project_tool_payload(
        "inspect_path", raw, ProjectionAudience.REVIEWER, **kwargs
    )
    judge = project_tool_payload(
        "inspect_path", raw, ProjectionAudience.JUDGE, **kwargs
    )
    assert reviewer.content == judge.content
    assert reviewer.summary == judge.summary
    assert reviewer.truncated == judge.truncated


def test_behavior_attached_relationship_does_not_create_a_pseudo_path():
    subject = "java:demo.Root#m()"
    attached_target = "java:demo.State#value"
    unreachable = "java:demo.Unreachable#run()"
    raw = json.dumps({
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "source_scope": "MAIN",
        "subject_symbol_id": subject,
        "symbols": [],
        "relationships": [
            {
                "sourceId": subject,
                "targetId": attached_target,
                "kind": "READS_FIELD",
                "file": "src/Root.java",
                "line": 10,
                "source_set": "MAIN",
                "resolution": "RESOLVED",
            },
            {
                "sourceId": attached_target,
                "targetId": unreachable,
                "kind": "CALLS",
                "file": "src/State.java",
                "line": 11,
                "source_set": "MAIN",
                "resolution": "RESOLVED",
            },
        ],
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
    })

    content = json.loads(project_tool_payload(
        "inspect_path",
        raw,
        ProjectionAudience.REVIEWER,
        arguments={"symbol_id": subject, "path_kind": "behavior"},
    ).content)

    assert [item["kind"] for item in content["relationships"]] == ["READS_FIELD"]
    assert all(
        item["kind"] != "CALLS" for item in content["relationships"]
    )


def test_change_impact_reverses_calls_without_following_attached_facts():
    subject = "java:demo.Service#m()"
    caller = "java:demo.Controller#run()"
    callee = "java:demo.Repository#load()"
    raw = json.dumps({
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "source_scope": "MAIN",
        "subject_symbol_id": subject,
        "symbols": [],
        "relationships": [
            {
                "sourceId": caller,
                "targetId": subject,
                "kind": "CALLS",
                "file": "src/Controller.java",
                "line": 20,
                "source_set": "MAIN",
                "resolution": "RESOLVED",
            },
            {
                "sourceId": subject,
                "targetId": callee,
                "kind": "CALLS",
                "file": "src/Service.java",
                "line": 21,
                "source_set": "MAIN",
                "resolution": "RESOLVED",
            },
        ],
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
    })

    content = json.loads(project_tool_payload(
        "inspect_change_impact",
        raw,
        ProjectionAudience.REVIEWER,
        arguments={"symbol_id": subject},
    ).content)

    assert len(content["relationships"]) == 1
    assert content["relationships"][0]["sourceId"] == caller


def test_projection_hard_limit_returns_empty_relationships_without_invalid_json():
    subject = "java:demo.Root#m()"
    raw = json.dumps({
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "source_scope": "MAIN",
        "subject_symbol_id": subject,
        "symbols": [],
        "relationships": [{
            "sourceId": subject,
            "targetId": "java:demo.Target#" + ("x" * 20000),
            "kind": "CALLS",
            "file": "src/Root.java",
            "line": 10,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        }],
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
    })

    content = json.loads(project_tool_payload(
        "inspect_path",
        raw,
        ProjectionAudience.REVIEWER,
        arguments={"symbol_id": subject, "path_kind": "behavior"},
    ).content)

    assert content["relationships"] == []
    assert content["omitted_count"] == 1
    assert "projection_hard_limit_exceeded" in content["limitations"]


def test_security_projection_does_not_invent_path_from_omitted_intermediate_calls():
    subject = "java:demo.Controller#handle()"
    intermediate = "java:demo.Service#load()"
    sink = "java:demo.Repository#executeQuery()"
    raw = json.dumps({
        "schema_version": 2,
        "outcome": "found",
        "coverage": "partial",
        "source_scope": "MAIN",
        "subject_symbol_id": subject,
        "symbols": [],
        "relationships": [{
            "sourceId": intermediate,
            "targetId": sink,
            "kind": "CALLS",
            "file": "src/Service.java",
            "line": 30,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        }],
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
    })

    content = json.loads(project_tool_payload(
        "inspect_path",
        raw,
        ProjectionAudience.REVIEWER,
        arguments={"symbol_id": subject, "path_kind": "security"},
    ).content)

    assert content["relationships"][0]["sourceId"] == intermediate
    assert content["relationships"][0]["targetId"] == sink
    assert content["omitted_path_count"] == 0
