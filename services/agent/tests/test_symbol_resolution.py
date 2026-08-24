from __future__ import annotations

import json

from codeguard_agent.models.tasks import (
    ReviewTask,
    SymbolResolutionStatus,
)
from codeguard_agent.pipeline.symbols.resolver import resolve_task_symbols
from codeguard_agent.tools.tool_client import ToolResponse


def _task(task_id: str = "task-1", *, lines: list[int] | None = None) -> ReviewTask:
    return ReviewTask(
        id=task_id,
        file="src/A.java",
        patch="+  void run() {}",
        changed_lines=lines or [3],
    )


class _GraphClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.changes: list[dict] = []

    def resolve_change_context(self, changes):
        self.changes = changes
        return ToolResponse(True, json.dumps(self.payload))


def _payload(**overrides) -> dict:
    payload = {
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "source_scope": "MAIN",
        "source_scopes": ["MAIN"],
        "contexts": [
            {
                "file": "src/A.java",
                "symbol_id": "java:A#run()",
                "kind": "method",
                "start_line": 2,
                "end_line": 4,
                "signature": "void run()",
                "annotations": ["Transactional"],
                "control_flow": [],
                "source_set": "MAIN",
                "resolution": "resolved",
            }
        ],
        "limitations": [],
    }
    payload.update(overrides)
    return payload


def test_resolver_maps_changed_lines_to_typed_symbols():
    client = _GraphClient(_payload())

    result = resolve_task_symbols([_task()], tool_client=client)

    assert client.changes == [{"file": "src/A.java", "lines": [3]}]
    context = result.contexts["task-1"]
    assert context.status is SymbolResolutionStatus.RESOLVED
    assert context.symbols[0].symbol_id == "java:A#run()"
    assert context.symbols[0].annotations == ("Transactional",)


def test_resolver_rejects_old_gateway_contract():
    client = _GraphClient({"status": "confirmed", "contexts": []})

    result = resolve_task_symbols([_task()], tool_client=client)

    context = result.contexts["task-1"]
    assert context.status is SymbolResolutionStatus.INVALID
    assert context.limitations == ("invalid_graph_response:graph_protocol_mismatch",)


def test_resolver_distinguishes_complete_miss_from_partial_unavailability():
    complete = _GraphClient(_payload(outcome="not_found", contexts=[]))
    partial = _GraphClient(
        _payload(
            outcome="indeterminate",
            coverage="partial",
            contexts=[],
            limitations=["graph_build_incomplete"],
        )
    )

    complete_result = resolve_task_symbols([_task()], tool_client=complete)
    partial_result = resolve_task_symbols([_task()], tool_client=partial)

    assert (
        complete_result.contexts["task-1"].status
        is SymbolResolutionStatus.NOT_FOUND
    )
    assert (
        partial_result.contexts["task-1"].status
        is SymbolResolutionStatus.UNAVAILABLE
    )


def test_resolver_never_cuts_a_symbol_json_payload():
    second = dict(_payload()["contexts"][0])
    second.update(symbol_id="java:A#other()", start_line=5, end_line=6)
    client = _GraphClient(_payload(contexts=[_payload()["contexts"][0], second]))
    task = _task(lines=[3, 5])

    result = resolve_task_symbols(
        [task], tool_client=client, max_chars_per_task=300
    )

    context = result.contexts["task-1"]
    assert context.truncated is True
    assert len(context.symbols) == 1
    assert context.symbols[0].symbol_id == "java:A#run()"


def test_resolver_deduplicates_a_symbol_shared_by_multiple_hunk_queries():
    duplicate = dict(_payload()["contexts"][0])
    client = _GraphClient(_payload(contexts=[duplicate, duplicate]))

    result = resolve_task_symbols([_task()], tool_client=client)

    assert len(result.contexts["task-1"].symbols) == 1


def test_resolver_without_tool_server_is_explicitly_unavailable():
    result = resolve_task_symbols([_task()], tool_client=None)

    context = result.contexts["task-1"]
    assert context.status is SymbolResolutionStatus.UNAVAILABLE
    assert context.limitations == ("tool_server_not_configured",)
