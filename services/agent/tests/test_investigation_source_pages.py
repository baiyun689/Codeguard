"""验证源码证据按实际内容识别，而不依赖工具名称。"""

import json
import pytest
from codeguard_agent.pipeline.controlled.subtask_react import SubtaskReactEngine
from codeguard_agent.pipeline.execution.discovery import DiscoveryToolRecord


def test_provider_investigation_limits_match_internal_contract():
    from codeguard_agent.pipeline.controlled.llm_contracts import LlmInvestigationResult
    from codeguard_agent.models.tasks import InvestigationResult

    external = LlmInvestigationResult.model_json_schema()
    internal = InvestigationResult.model_json_schema()
    assert (
        external["properties"]["findings"]["maxItems"]
        == internal["properties"]["findings"]["maxItems"]
    )
    assert (
        external["$defs"]["LlmInvestigationFinding"]["properties"]["observations"][
            "maxItems"
        ]
        == 3
    )


@pytest.mark.parametrize("truncated,expected", [(False, True), (True, False)])
def test_validated_relation_source_can_refute_without_another_read(truncated, expected):
    excerpt = {
        "start_line": 1,
        "end_line": 1,
        "text": "void caller() { if (value != null) value.run(); }",
        "truncated": truncated,
    }
    if truncated:
        excerpt["next_cursor"] = 2
    payload = {
        "schema_version": 2,
        "subject_symbol_id": "java:A#run()",
        "source_scope": "MAIN",
        "outcome": "found",
        "coverage": "complete",
        "symbols": [
            {"id": "java:A#run()", "source_set": "MAIN"},
            {
                "id": "java:B#caller()",
                "source_set": "MAIN",
                "startLine": 1,
                "endLine": 2 if truncated else 1,
                "source_excerpt": excerpt,
            },
        ],
        "relationships": [
            {
                "sourceId": "java:B#caller()",
                "targetId": "java:A#run()",
                "kind": "CALLS",
                "resolution": "RESOLVED",
                "source_set": "MAIN",
            }
        ],
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
    }
    record = DiscoveryToolRecord(
        call_id="source",
        tool="query_relations",
        arguments={"subject_symbol_id": "java:A#run()", "relation": "callers"},
        output=json.dumps(payload),
        duration_ms=1,
        status="complete",
        reuse_key="source",
    )
    assert SubtaskReactEngine._record_is_usable_source(record) is expected
