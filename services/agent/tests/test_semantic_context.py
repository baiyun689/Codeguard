from __future__ import annotations

import json

from codeguard_agent.pipeline.context.base import PipelineContext
from codeguard_agent.pipeline.context.provider import ContextProviderStage
from codeguard_agent.tools.tool_client import ToolResponse


_DIFF = """\
diff --git a/src/A.java b/src/A.java
--- a/src/A.java
+++ b/src/A.java
@@ -2,1 +2,2 @@
 class A {
+  void run() {}
 }
"""


class _GraphClient:
    def __init__(self) -> None:
        self.changes = []

    def resolve_change_context(self, changes):
        self.changes = changes
        return ToolResponse(
            True,
            json.dumps(
                {
                    "status": "confirmed",
                    "coverage": "complete",
                    "contexts": [
                        {
                            "file": "src/A.java",
                            "file_id": "file:src/A.java",
                            "symbol_id": "java:A#run()",
                            "kind": "method",
                            "start_line": 3,
                            "end_line": 3,
                            "signature": "void run()",
                            "annotations": [],
                            "control_flow": [],
                            "resolution": "resolved",
                        }
                    ],
                    "limitations": [],
                }
            ),
        )


def test_context_provider_prefetches_structured_symbol_context():
    client = _GraphClient()
    context = PipelineContext(diff_text=_DIFF, tool_client=client)

    ContextProviderStage().execute(context)

    assert client.changes == [{"file": "src/A.java", "lines": [3]}]
    assert [fact.kind for fact in context.context_bundle.facts] == ["symbol_context"]
    assert "java:A#run()" in context.context_bundle.facts[0].content
