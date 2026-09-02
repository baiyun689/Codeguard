from __future__ import annotations

import json

from codeguard_agent.models.schemas import DiscoveredIssue
from codeguard_agent.models.tasks import SymbolResolutionStatus
from codeguard_agent.pipeline.location import locate_issues
from codeguard_agent.pipeline.evidence.projection import graph_projection_focus
from codeguard_agent.pipeline.symbols.resolver import resolve_task_symbols
from codeguard_agent.pipeline.tasks.task_builder import build_file_tasks
from codeguard_agent.tools.tool_client import ToolResponse


_DELETION_ONLY_DIFF = """diff --git a/src/A.java b/src/A.java
index 1111111..2222222 100644
--- a/src/A.java
+++ b/src/A.java
@@ -10,7 +10,4 @@ class A {
     void run() {
-        if (blocked(value)) {
-            return;
-        }
         execute(value);
     }
 }
"""


def test_file_task_preserves_deleted_text_and_current_anchor() -> None:
    task = build_file_tasks(_DELETION_ONLY_DIFF)[0]

    assert task.changed_lines == []
    assert len(task.deletion_anchors) == 1
    anchor = task.deletion_anchors[0]
    assert anchor.anchor_line == 11
    assert anchor.anchor_kind == "next_surviving"
    assert anchor.deleted_snippet == "        if (blocked(value)) {\n            return;\n        }"


class _GraphClient:
    def __init__(self) -> None:
        self.changes: list[dict] = []

    def resolve_change_context(self, changes):
        self.changes = changes
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
                    "start_line": 9,
                    "end_line": 12,
                    "signature": "void run()",
                    "annotations": [],
                    "control_flow": [],
                    "source_set": "MAIN",
                    "resolution": "resolved",
                }
            ],
            "limitations": [],
        }
        return ToolResponse(True, json.dumps(payload))


def test_deletion_anchor_is_sent_to_symbol_resolution() -> None:
    task = build_file_tasks(_DELETION_ONLY_DIFF)[0]
    client = _GraphClient()

    result = resolve_task_symbols([task], tool_client=client)

    assert client.changes == [{"file": "src/A.java", "lines": [11]}]
    assert result.contexts[task.id].status is SymbolResolutionStatus.RESOLVED
    assert result.contexts[task.id].symbols[0].symbol_id == "java:A#run()"


def test_deletion_anchor_is_a_valid_deterministic_location() -> None:
    task = build_file_tasks(_DELETION_ONLY_DIFF)[0]
    issue = DiscoveredIssue(
        file="src/A.java",
        line=11,
        location_snippet="",
        type="删除守卫",
        message="删除提前返回守卫后，受限输入会继续执行。",
        suggestion="恢复守卫。",
    )

    result = locate_issues(
        [issue], task, llm=None, structured_method="function_calling", max_retries=1
    )

    assert result.issues[0].line == 11
    assert result.records[0].status == "deletion_anchor"
    assert result.records[0].reason == "reported_deletion_anchor"


def test_graph_projection_preserves_new_side_changed_line_semantics() -> None:
    task = build_file_tasks(_DELETION_ONLY_DIFF)[0]

    focus = graph_projection_focus(task)

    assert focus.changed_lines == ()
