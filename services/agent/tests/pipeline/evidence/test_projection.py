from __future__ import annotations
import json
from codeguard_agent.models.evidence import (
    ArtifactAvailability,
    EvidenceArtifact,
    EvidenceCaptureMode,
    EvidenceSourceKind,
    payload_digest,
)
from codeguard_agent.pipeline.execution.discovery import DiscoveryToolRecord
from codeguard_agent.pipeline.evidence.projection import (
    GraphProjectionFocus,
    ProjectionAudience,
    project_tool_payload,
)


def _graph_payload() -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "outcome": "found",
            "coverage": "complete",
            "source_scope": "MAIN",
            "subject_symbol_id": "java:demo.Service#m()",
            "symbols": [
                {
                    "id": "java:demo.Service#m()",
                    "kind": "method",
                    "file": "src/Service.java",
                    "startLine": 10,
                    "endLine": 15,
                    "source_set": "MAIN",
                    "signature": "void m()",
                    "annotations": ["Transactional"],
                }
            ],
            "relationships": [
                {
                    "sourceId": "java:demo.Controller#run()",
                    "targetId": "java:demo.Service#m()",
                    "kind": "calls",
                    "file": "src/Controller.java",
                    "line": 20,
                    "source_set": "MAIN",
                    "resolution": "RESOLVED",
                    "diagnostic": "not for reviewer",
                }
            ],
            "unresolved_relationships": [],
            "unresolved_count": 0,
            "limitations": [],
            "snapshot_main_coverage": "partial",
        },
        ensure_ascii=False,
    )


def _graph_with_source_excerpt():
    payload = json.loads(_graph_payload())
    payload["symbols"].append(
        {
            "id": "java:demo.Controller#run()",
            "kind": "METHOD",
            "file": "src/Controller.java",
            "startLine": 20,
            "endLine": 20,
            "source_set": "MAIN",
            "source_excerpt": {
                "start_line": 20,
                "end_line": 20,
                "text": "void run() { service.m(); }\n",
                "truncated": False,
            },
        }
    )
    return payload


def test_relation_source_excerpt_survives_judge_projection_and_artifact_capture():
    from codeguard_agent.models.council import CandidateIssue
    from codeguard_agent.models.evidence import EvidenceRef
    from codeguard_agent.models.schemas import EvidenceRole
    from codeguard_agent.models.tasks import ReviewTask
    from codeguard_agent.pipeline.evidence.planner import CandidateDossier
    from codeguard_agent.pipeline.evidence.verifier import verify_evidence
    from codeguard_agent.pipeline.council.verdict import _evidence_item_payload

    payload = _graph_with_source_excerpt()
    raw = json.dumps(payload)
    assert (
        project_tool_payload(
            "query_relations", raw, ProjectionAudience.EVIDENCE
        ).content
        == raw
    )
    projected = project_tool_payload("query_relations", raw, ProjectionAudience.JUDGE)
    content = json.loads(projected.content)
    endpoint = next(
        (
            item
            for item in content["symbols"]
            if item["id"] == "java:demo.Controller#run()"
        )
    )
    assert endpoint["source_excerpt"] == payload["symbols"][-1]["source_excerpt"]
    assert content["relationships"]
    assert not projected.truncated
    artifact = EvidenceArtifact.build(
        task_id="task",
        reviewer="behavior",
        revision="rev",
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool="query_relations",
        arguments={
            "subject_symbol_id": payload["subject_symbol_id"],
            "relation": "callers",
        },
        payload=raw,
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )
    candidate = CandidateIssue(
        id="candidate",
        task_id="task",
        source_agent="behavior",
        file="src/Service.java",
        line=12,
        type="logic",
        claim="Caller depends on the changed method",
        confidence=0.8,
        evidence_refs=[
            EvidenceRef(artifact_id=artifact.id, declared_role=EvidenceRole.MECHANISM)
        ],
    )
    dossier = CandidateDossier(
        candidate=candidate,
        symbol_context=None,
        task=ReviewTask(
            id="task", file="src/Service.java", patch="+change();", changed_lines=[12]
        ),
    )
    verified = verify_evidence(
        [dossier],
        artifacts={artifact.id: artifact},
        revision="rev",
        tool_client=None,
        enabled_replay_tools=None,
    )
    assert not verified.replayed_artifact_ids
    items, mapping = _evidence_item_payload(dossier, verified.candidates[candidate.id])
    assert mapping == [("F001", artifact.id)]
    assert "void run() { service.m(); }" in items[0]["content"]


def test_relation_source_excerpt_cannot_escape_its_declaration():
    from codeguard_agent.pipeline.evidence.graph_response import validate_graph_payload
    from codeguard_agent.models.evidence import EvidenceValidationStatus

    payload = _graph_with_source_excerpt()
    payload["symbols"][-1]["source_excerpt"]["end_line"] = 40
    validation = validate_graph_payload(json.dumps(payload), tool="query_relations")
    assert validation.status is EvidenceValidationStatus.INVALID
    assert "invalid_graph_source_excerpt" in validation.limitations


def test_excerpt_budget_never_removes_an_otherwise_visible_relationship():
    from codeguard_agent.pipeline.evidence.graph_response import summarize_graph

    payload = _graph_with_source_excerpt()
    payload["symbols"][-1]["source_excerpt"]["text"] = "x" * 999 + "\n"
    plain = json.loads(json.dumps(payload))
    del plain["symbols"][-1]["source_excerpt"]
    baseline = json.loads(
        summarize_graph(json.dumps(plain), tool="query_relations", max_chars=1300)
    )
    content = json.loads(
        summarize_graph(json.dumps(payload), tool="query_relations", max_chars=1300)
    )
    assert content["relationships"] == baseline["relationships"]
    assert content["omitted_source_excerpt_count"] == 1
    assert "source_excerpts_omitted" in content["limitations"]


def test_reviewer_graph_projection_keeps_reasoning_fields_not_raw_diagnostics():
    raw = _graph_payload()
    projection = project_tool_payload(
        "query_relations", raw, ProjectionAudience.REVIEWER
    )
    content = json.loads(projection.content)
    assert content["outcome"] == "found"
    assert content["relationships"][0]["targetId"] == "java:demo.Service#m()"
    assert content["symbols"][0]["source_set"] == "MAIN"
    assert "snapshot_main_coverage" not in content
    assert "diagnostic" not in content["relationships"][0]
    assert projection.summary == "found/complete · 已解析 1 · 未解析 0"


def test_reviewer_graph_projection_preserves_unresolved_relationship_facts():
    payload = json.loads(_graph_payload())
    payload["coverage"] = "partial"
    payload["unresolved_relationships"] = [
        {
            "sourceId": "java:demo.Service#m()",
            "targetId": "java:demo.Dynamic#run()",
            "kind": "CALLS",
            "file": "src/Service.java",
            "line": 14,
            "source_set": "MAIN",
            "resolution": "UNRESOLVED",
            "reason": "dynamic dispatch",
        }
    ]
    payload["unresolved_count"] = 1
    content = json.loads(
        project_tool_payload(
            "query_relations",
            json.dumps(payload, ensure_ascii=False),
            ProjectionAudience.REVIEWER,
            arguments={
                "subject_symbol_id": "java:demo.Service#m()",
                "path_kind": "behavior",
                "relation": "callees",
            },
        ).content
    )
    assert content["unresolved_count"] == 1
    assert content["unresolved_relationships"] == [
        {
            "sourceId": "java:demo.Service#m()",
            "targetId": "java:demo.Dynamic#run()",
            "kind": "CALLS",
            "file": "src/Service.java",
            "line": 14,
            "source_set": "MAIN",
            "resolution": "UNRESOLVED",
        }
    ]
    assert "reason" not in content["unresolved_relationships"][0]


def test_projection_reports_omitted_unresolved_relationships_under_budget_pressure():
    payload = json.loads(_graph_payload())
    payload["coverage"] = "partial"
    payload["unresolved_relationships"] = [
        {
            "sourceId": "java:demo.Service#m()",
            "targetId": f"java:demo.Dynamic#target{index}-{'x' * 80}()",
            "kind": "CALLS",
            "file": "src/Service.java",
            "line": index,
            "source_set": "MAIN",
            "resolution": "UNRESOLVED",
        }
        for index in range(100)
    ]
    payload["unresolved_count"] = 100
    projection = project_tool_payload(
        "query_relations",
        json.dumps(payload, ensure_ascii=False),
        ProjectionAudience.REVIEWER,
        arguments={
            "subject_symbol_id": "java:demo.Service#m()",
            "path_kind": "behavior",
            "relation": "callees",
        },
    )
    content = json.loads(projection.content)
    assert content["unresolved_count"] == 100
    assert content["omitted_unresolved_count"] > 0
    assert len(content["unresolved_relationships"]) < 100
    assert "unresolved_relationships_truncated" in content["limitations"]
    assert projection.truncated is True


def test_reviewer_file_projection_preserves_complete_content():
    raw = "文件: src/A.java\n" + "class A {}\n" * 100
    projection = project_tool_payload("read_symbol", raw, ProjectionAudience.REVIEWER)
    assert projection.content == raw
    assert projection.truncated is False


def test_evidence_projection_never_changes_original_payload():
    raw = _graph_payload()
    projection = project_tool_payload(
        "query_relations", raw, ProjectionAudience.EVIDENCE
    )
    assert projection.content == raw
    assert projection.truncated is False


def test_reviewer_invalid_graph_projection_never_falls_back_to_raw_payload():
    raw = "BROKEN_GRAPH_SECRET"
    projection = project_tool_payload(
        "query_relations", raw, ProjectionAudience.REVIEWER
    )
    assert raw not in projection.content
    content = json.loads(projection.content)
    assert content["outcome"] == "indeterminate"
    assert content["limitations"] == ["graph_projection_unavailable"]


def test_reviewer_malformed_v2_collections_fail_closed():
    raw = json.dumps(
        {
            "schema_version": 2,
            "outcome": "found",
            "coverage": "complete",
            "symbols": [],
            "relationships": 42,
            "unresolved_relationships": [],
            "limitations": [],
        }
    )
    projection = project_tool_payload(
        "query_relations", raw, ProjectionAudience.REVIEWER
    )
    content = json.loads(projection.content)
    assert content["outcome"] == "indeterminate"
    assert content["limitations"] == ["graph_projection_unavailable"]


def test_reviewer_invalid_v2_contract_fails_closed():
    raw = json.dumps(
        {
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
        }
    )
    projection = project_tool_payload(
        "query_relations", raw, ProjectionAudience.REVIEWER
    )
    content = json.loads(projection.content)
    assert content["outcome"] == "indeterminate"
    assert content["limitations"] == ["graph_projection_unavailable"]


def test_reviewer_subject_mismatch_fails_closed_when_arguments_are_known():
    raw = _graph_payload()
    projection = project_tool_payload(
        "query_relations",
        raw,
        ProjectionAudience.REVIEWER,
        arguments={
            "subject_symbol_id": "java:other.Service#run()",
            "relation": "callees",
        },
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
        relationships.append(
            {
                "sourceId": source,
                "targetId": f"java:retry.Noise#{index + 1}()",
                "kind": "CALLS",
                "file": "src/Noise.java",
                "line": index + 1,
                "source_set": "MAIN",
                "resolution": "RESOLVED",
            }
        )
    symbols = []
    for symbol_id in {subject, first, listener, internal, context}:
        symbols.append(
            {
                "id": symbol_id,
                "kind": "method",
                "file": "src/RetryTemplate.java",
                "startLine": 1,
                "endLine": 200,
                "source_set": "MAIN",
            }
        )
    return json.dumps(
        {
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
        },
        ensure_ascii=False,
    )


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
    for branch in range(80):
        branch_nodes = [
            f"java:retry.RetryTemplate#aaaNoiseComponent{branch}_{depth}"
            + "x" * 48
            + "()"
            for depth in range(4)
        ]
        for depth in range(3):
            relationships.append(
                {
                    "sourceId": subject if depth == 0 else branch_nodes[depth - 1],
                    "targetId": branch_nodes[depth],
                    "kind": "CALLS",
                    "file": "src/RetryTemplate.java",
                    "line": 600 + branch * 3 + depth,
                    "source_set": "MAIN",
                    "resolution": "RESOLVED",
                }
            )
    for branch in range(40):
        callback = f"java:retry.Callback{branch}#callback()"
        callback_leaf = f"java:retry.Callback{branch}#invoke()"
        relationships.extend(
            [
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
            ]
        )
    for index in range(30):
        relationships.append(
            {
                "sourceId": subject,
                "targetId": f"java:retry.RetryContext#attribute{index}",
                "kind": "READS_FIELD",
                "file": "src/RetryTemplate.java",
                "line": 700 + index,
                "source_set": "MAIN",
                "resolution": "RESOLVED",
            }
        )
    symbol_ids = {item["sourceId"] for item in relationships}
    symbol_ids.update((item["targetId"] for item in relationships))
    raw = json.dumps(
        {
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
        },
        ensure_ascii=False,
    )
    content = json.loads(
        project_tool_payload(
            "query_relations",
            raw,
            ProjectionAudience.REVIEWER,
            arguments={
                "subject_symbol_id": subject,
                "path_kind": "behavior",
                "depth": 3,
                "relation": "callees",
            },
            focus=GraphProjectionFocus(
                changed_file="src/RetryTemplate.java",
                changed_lines=(298, 299, 500),
                changed_symbol_ids=(subject, open_method),
            ),
        ).content
    )
    edges = {(item["sourceId"], item["targetId"]) for item in content["relationships"]}
    assert (subject, interceptors) in edges
    assert (interceptors, listener) in edges
    assert (internal, context) in edges
    callback_edges = {
        edge for edge in edges if edge[1].startswith("java:retry.Callback")
    }
    assert callback_edges
    callback_branches = {edge[1] for edge in callback_edges if edge[0] == subject}
    assert len(callback_branches) <= 4


def test_path_family_prefers_changed_callsite_before_deduplication():
    subject = "java:demo.Service#run()"
    target = "java:demo.Service#helper()"
    raw = json.dumps(
        {
            "schema_version": 2,
            "outcome": "found",
            "coverage": "complete",
            "source_scope": "MAIN",
            "subject_symbol_id": subject,
            "symbols": [
                {
                    "id": subject,
                    "kind": "method",
                    "file": "src/Service.java",
                    "startLine": 1,
                    "endLine": 100,
                    "source_set": "MAIN",
                },
                {
                    "id": target,
                    "kind": "method",
                    "file": "src/Service.java",
                    "startLine": 1,
                    "endLine": 100,
                    "source_set": "MAIN",
                },
            ],
            "relationships": [
                {
                    "sourceId": subject,
                    "targetId": target,
                    "kind": "CALLS",
                    "file": "src/Service.java",
                    "line": 12,
                    "source_set": "MAIN",
                    "resolution": "RESOLVED",
                },
                {
                    "sourceId": subject,
                    "targetId": target,
                    "kind": "CALLS",
                    "file": "src/Service.java",
                    "line": 88,
                    "source_set": "MAIN",
                    "resolution": "RESOLVED",
                },
            ],
            "unresolved_relationships": [],
            "unresolved_count": 0,
            "limitations": [],
        },
        ensure_ascii=False,
    )
    content = json.loads(
        project_tool_payload(
            "query_relations",
            raw,
            ProjectionAudience.REVIEWER,
            arguments={
                "subject_symbol_id": subject,
                "path_kind": "behavior",
                "relation": "callees",
            },
            focus=GraphProjectionFocus(
                changed_file="src/Service.java",
                changed_lines=(88,),
                changed_symbol_ids=(subject,),
            ),
        ).content
    )
    assert content["relationships"] == [
        {
            "sourceId": subject,
            "targetId": target,
            "kind": "CALLS",
            "file": "src/Service.java",
            "line": 88,
            "source_set": "MAIN",
            "resolution": "RESOLVED",
        }
    ]


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
        "query_relations", raw, ProjectionAudience.REVIEWER, **kwargs
    )
    judge = project_tool_payload(
        "query_relations", raw, ProjectionAudience.JUDGE, **kwargs
    )
    assert reviewer.content == judge.content
    assert reviewer.summary == judge.summary
    assert reviewer.truncated == judge.truncated


def test_projection_preserves_artifact_payload_hash_and_graph_headers():
    raw = _deep_behavior_payload()
    artifact = EvidenceArtifact.build(
        task_id="deep-task",
        reviewer="behavior",
        revision="deep-revision",
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool="query_relations",
        arguments={
            "subject_symbol_id": "java:retry.RetryTemplate#doExecute()",
            "path_kind": "behavior",
            "depth": "3",
            "relation": "callees",
        },
        payload=raw,
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
        call_id="deep-call",
    )
    projected = json.loads(
        project_tool_payload(
            "query_relations",
            raw,
            ProjectionAudience.REVIEWER,
            arguments={
                "subject_symbol_id": "java:retry.RetryTemplate#doExecute()",
                "path_kind": "behavior",
                "depth": 3,
                "relation": "callees",
            },
        ).content
    )
    assert artifact.payload == raw
    assert artifact.payload_hash == payload_digest(raw)
    raw_headers = json.loads(raw)
    for key in ("outcome", "coverage", "source_scope"):
        assert projected[key] == raw_headers[key]


def test_projection_hard_limit_returns_empty_relationships_without_invalid_json():
    subject = "java:demo.Root#m()"
    raw = json.dumps(
        {
            "schema_version": 2,
            "outcome": "found",
            "coverage": "complete",
            "source_scope": "MAIN",
            "subject_symbol_id": subject,
            "symbols": [],
            "relationships": [
                {
                    "sourceId": subject,
                    "targetId": "java:demo.Target#" + "x" * 20000,
                    "kind": "CALLS",
                    "file": "src/Root.java",
                    "line": 10,
                    "source_set": "MAIN",
                    "resolution": "RESOLVED",
                }
            ],
            "unresolved_relationships": [],
            "unresolved_count": 0,
            "limitations": [],
        }
    )
    content = json.loads(
        project_tool_payload(
            "query_relations",
            raw,
            ProjectionAudience.REVIEWER,
            arguments={
                "subject_symbol_id": subject,
                "path_kind": "behavior",
                "relation": "callees",
            },
        ).content
    )
    assert content["relationships"] == []
    assert content["omitted_count"] == 1
    assert "projection_hard_limit_exceeded" in content["limitations"]


def test_projection_hard_limit_does_not_repopulate_relationships_with_attached_facts():
    subject = "java:demo.Root#m()"
    raw = json.dumps(
        {
            "schema_version": 2,
            "outcome": "found",
            "coverage": "complete",
            "source_scope": "MAIN",
            "subject_symbol_id": subject,
            "symbols": [],
            "relationships": [
                {
                    "sourceId": subject,
                    "targetId": "java:demo.Target#" + "x" * 20000,
                    "kind": "CALLS",
                    "file": "src/Root.java",
                    "line": 10,
                    "source_set": "MAIN",
                    "resolution": "RESOLVED",
                },
                {
                    "sourceId": subject,
                    "targetId": "java:demo.State#value",
                    "kind": "READS_FIELD",
                    "file": "src/Root.java",
                    "line": 11,
                    "source_set": "MAIN",
                    "resolution": "RESOLVED",
                },
            ],
            "unresolved_relationships": [],
            "unresolved_count": 0,
            "limitations": [],
        }
    )
    content = json.loads(
        project_tool_payload(
            "query_relations",
            raw,
            ProjectionAudience.REVIEWER,
            arguments={
                "subject_symbol_id": subject,
                "path_kind": "behavior",
                "relation": "callees",
            },
        ).content
    )
    assert content["relationships"] == []
    assert content["symbols"] == []
    assert "projection_hard_limit_exceeded" in content["limitations"]


def test_projection_hard_limit_after_an_earlier_path_fails_closed():
    subject = "java:demo.Root#m()"
    raw = json.dumps(
        {
            "schema_version": 2,
            "outcome": "found",
            "coverage": "complete",
            "source_scope": "MAIN",
            "subject_symbol_id": subject,
            "symbols": [],
            "relationships": [
                {
                    "sourceId": subject,
                    "targetId": "java:demo.Small#run()",
                    "kind": "CALLS",
                    "file": "src/Root.java",
                    "line": 10,
                    "source_set": "MAIN",
                    "resolution": "RESOLVED",
                },
                {
                    "sourceId": subject,
                    "targetId": "java:demo.Large#" + "x" * 20000,
                    "kind": "CALLS",
                    "file": "src/Root.java",
                    "line": 11,
                    "source_set": "MAIN",
                    "resolution": "RESOLVED",
                },
            ],
            "unresolved_relationships": [],
            "unresolved_count": 0,
            "limitations": [],
        }
    )
    content = json.loads(
        project_tool_payload(
            "query_relations",
            raw,
            ProjectionAudience.REVIEWER,
            arguments={
                "subject_symbol_id": subject,
                "path_kind": "behavior",
                "relation": "callees",
            },
        ).content
    )
    assert content["relationships"] == []
    assert "projection_hard_limit_exceeded" in content["limitations"]
