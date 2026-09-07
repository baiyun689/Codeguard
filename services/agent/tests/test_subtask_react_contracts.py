from __future__ import annotations

from types import SimpleNamespace

from codeguard_agent.models.tasks import (
    EvidenceNeed,
    InvestigationFinding,
    InvestigationObservation,
    InvestigationResult,
    InvestigationSeed,
    ReviewerKind,
)
from codeguard_agent.pipeline.controlled.subtask_plan import run_subtask_plan
from codeguard_agent.pipeline.execution.discovery import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
)
from codeguard_agent.tools.tool_client import ToolResponse


def _seed() -> InvestigationSeed:
    return InvestigationSeed(
        seed_id="investigation-behavior-1",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-task-1",
        observed_change="返回表达式发生变化",
        investigation_question="检查相关调用方是否依赖原返回行为",
        location_file="src/A.java",
        location_line=12,
        initial_symbol_ids=("java:A#run()",),
        evidence_need=EvidenceNeed.INSPECT_CHANGE_IMPACT,
        allowed_tools=("inspect_change_impact", "get_file_content"),
    )


def test_subtask_plan_without_llm_keeps_each_seed_as_bounded_neutral_task():
    plan, diagnostics = run_subtask_plan(
        reviewer=ReviewerKind.BEHAVIOR,
        task=SimpleNamespace(id="task-1", file="src/A.java", patch="+return run();"),
        seeds=(_seed(),),
        symbol_context=SimpleNamespace(symbols=()),
        llm=None,
        max_retries=1,
        structured_method="function_calling",
        max_tool_calls=4,
        max_rounds=3,
        max_subtasks=2,
        max_path_depth=3,
    )
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].seed_id == "investigation-behavior-1"
    assert plan.subtasks[0].primary_tool == "inspect_change_impact"
    assert "subtask_plan_deterministic_fallback" in diagnostics


def test_investigation_result_can_confirm_only_with_local_observation_ids():
    result = InvestigationResult(
        subtask_id="subtask-1",
        outcome="findings",
        findings=(
            InvestigationFinding(
                claim="调用方继续修改返回对象，导致返回行为不兼容",
                mechanism="返回对象的使用方式与变更后的实现不一致",
                location_file="src/A.java",
                location_line=12,
                observations=(
                    InvestigationObservation(observation_id="T01", role="relation"),
                    InvestigationObservation(observation_id="T02", role="mechanism"),
                ),
            ),
        ),
    )
    assert result.outcome == "findings"
    assert [item.observation_id for item in result.findings[0].observations] == ["T01", "T02"]


def test_subtask_tool_budget_is_enforced_before_delegate_call():
    calls: list[tuple[str, str, int]] = []

    class Delegate:
        def inspect_path(self, symbol_id, path_kind, max_depth):
            calls.append((symbol_id, path_kind, max_depth))
            return ToolResponse(success=True, result='{"outcome":"found"}')

    client = CoordinatedDiscoveryToolClient(
        Delegate(),
        DiscoveryToolCoordinator(),
        max_tool_calls=1,
        max_path_depth=2,
    )
    first = client.inspect_path("java:A#run()", "behavior", 2)
    second = client.inspect_path("java:A#run()", "behavior", 2)
    rejected_depth = client.inspect_path("java:A#run()", "behavior", 3)

    assert first.success
    assert not second.success
    assert second.error == "subtask_tool_budget_exceeded"
    assert not rejected_depth.success
    assert rejected_depth.error == "invalid_max_depth"
    assert calls == [("java:A#run()", "behavior", 2)]
    assert client.tool_calls == 1

    depth_limited = CoordinatedDiscoveryToolClient(
        Delegate(),
        DiscoveryToolCoordinator(),
        max_tool_calls=1,
        max_path_depth=2,
    )
    invalid = depth_limited.inspect_path("java:A#run()", "behavior", 3)
    assert not invalid.success
    assert invalid.error == "invalid_max_depth"
    assert depth_limited.tool_calls == 0
    depth_limited.close()
    closed = depth_limited.inspect_path("S01", "behavior", 1)
    assert not closed.success
    assert closed.error == "subtask_execution_closed"


def test_subtask_tools_accept_only_symbol_aliases_and_alias_graph_results():
    calls: list[str] = []

    class Delegate:
        def inspect_path(self, symbol_id, path_kind, max_depth):
            calls.append(symbol_id)
            return ToolResponse(
                success=True,
                result=(
                    '{"symbols":[{"id":"java:B#run()"}],'
                    '"relationships":[{"sourceId":"java:A#run()",'
                    '"targetId":"java:B#run()"}]}'
                ),
            )

    client = CoordinatedDiscoveryToolClient(
        Delegate(),
        DiscoveryToolCoordinator(),
        initial_symbol_ids={"java:A#run()"},
        lossless_payload=True,
    )
    response = client.inspect_path("S01", "behavior", 1)
    unknown = client.inspect_path("java:Other#run()", "behavior", 1)

    assert response.success
    assert calls == ["java:A#run()"]
    assert '"id":"R01"' in response.result
    assert '"targetId":"R01"' in response.result
    assert not unknown.success
    assert unknown.error == "symbol_ref_not_in_review_context"
    assert client.symbol_aliases["S01"] == "java:A#run()"
    assert client.symbol_aliases["R01"] == "java:B#run()"
