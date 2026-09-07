from __future__ import annotations

from types import SimpleNamespace

import pytest

from codeguard_agent.models.tasks import (
    EvidenceNeed,
    InvestigationFinding,
    InvestigationObservation,
    InvestigationResult,
    InvestigationSeed,
    ReviewerKind,
    SubtaskInstruction,
    SubtaskPlan,
)
from codeguard_agent.models.tasks.symbols import ResolvedSymbol
from codeguard_agent.pipeline.controlled.subtask_plan import run_subtask_plan
from codeguard_agent.pipeline.controlled.subtask_grouping import group_investigation_seeds
from codeguard_agent.pipeline.controlled.subtask_capabilities import (
    coherent_tool_bundle,
    normalize_investigation_seed,
    requested_graph_tools,
)
from codeguard_agent.pipeline.orchestration.graph import _allocate_subtask_budgets
from codeguard_agent.pipeline.controlled.subtask_react import SubtaskReactEngine
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


def test_structure_subtask_fallback_exposes_source_reader_for_method_body():
    seed = _seed().model_copy(
        update={
            "evidence_need": EvidenceNeed.INSPECT_STRUCTURE,
            "allowed_tools": ("inspect_structure",),
        }
    )
    plan, _ = run_subtask_plan(
        reviewer=ReviewerKind.BEHAVIOR,
        task=SimpleNamespace(id="task-1", file="src/A.java", patch="+return run();"),
        seeds=(seed,),
        symbol_context=SimpleNamespace(symbols=()),
        llm=None,
        max_retries=1,
        structured_method="function_calling",
        max_tool_calls=4,
        max_rounds=3,
        max_subtasks=2,
        max_path_depth=3,
    )
    assert plan.subtasks[0].allowed_tools == (
        "inspect_structure",
        "get_file_content",
    )


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
        allowed_path_kind="behavior",
    )
    first = client.inspect_path("java:A#run()", "behavior", 2)
    second = client.inspect_path("java:A#run()", "behavior", 2)
    rejected_depth = client.inspect_path("java:A#run()", "behavior", 3)

    assert first.success
    assert not second.success
    assert second.error == "subtask_tool_budget_exceeded"
    assert not rejected_depth.success
    assert rejected_depth.error == "invalid_max_depth"
    assert client.budget_exhausted
    assert calls == [("java:A#run()", "behavior", 2)]
    assert client.tool_calls == 1

    wrong_domain = client.inspect_path("java:A#run()", "security", 1)
    assert not wrong_domain.success
    assert wrong_domain.error == "path_kind_not_allowed"

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

        def get_file_content(self, symbol_id, start_line=None, end_line=None):
            calls.append(f"source:{symbol_id}")
            return ToolResponse(success=True, result=f"source for {symbol_id}")

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
    source = client.get_file_content("R01")
    assert source.success
    assert calls == ["java:A#run()", "source:java:B#run()"]
    assert not unknown.success
    assert unknown.error == "symbol_ref_not_in_review_context"
    assert client.symbol_aliases["S01"] == "java:A#run()"
    assert client.symbol_aliases["R01"] == "java:B#run()"


def test_equivalent_seeds_from_fixed_reviewers_are_grouped_before_react():
    threat = _seed().model_copy(
        update={
            "seed_id": "investigation-threat-1",
            "reviewer": ReviewerKind.THREAT_MODEL,
            "confidence": 0.6,
            "observed_change": "返回表达式发生变化，调用方可能继续修改结果",
        }
    )
    behavior = _seed().model_copy(
        update={
            "seed_id": "investigation-behavior-1",
            "reviewer": ReviewerKind.BEHAVIOR,
            "confidence": 0.8,
            "observed_change": "返回值的使用方式发生变化",
        }
    )
    other = _seed().model_copy(
        update={
            "seed_id": "investigation-behavior-2",
            "location_line": 30,
        }
    )

    groups = group_investigation_seeds({
        ReviewerKind.THREAT_MODEL: (threat,),
        ReviewerKind.BEHAVIOR: (behavior, other),
    })

    assert len(groups) == 2
    merged = next(group for group in groups if len(group.seed_ids) == 2)
    assert merged.seed.seed_id == "investigation-behavior-1"
    assert merged.reviewers == (ReviewerKind.BEHAVIOR, ReviewerKind.THREAT_MODEL)
    assert set(merged.seed_ids) == {
        "investigation-behavior-1",
        "investigation-threat-1",
    }


def test_behavior_and_security_path_seeds_are_not_merged():
    behavior = _seed().model_copy(
        update={
            "evidence_need": EvidenceNeed.INSPECT_PATH,
            "allowed_tools": ("inspect_path", "get_file_content"),
            "path_kind": "behavior",
            "direction": "downstream",
        }
    )
    security = behavior.model_copy(
        update={
            "seed_id": "investigation-threat-security-1",
            "reviewer": ReviewerKind.THREAT_MODEL,
            "path_kind": "security",
        }
    )
    groups = group_investigation_seeds({
        ReviewerKind.BEHAVIOR: (behavior,),
        ReviewerKind.THREAT_MODEL: (security,),
    })
    assert len(groups) == 2
    assert {group.seed.path_kind for group in groups} == {"behavior", "security"}


def test_security_hint_on_neutral_structure_seed_does_not_split_investigation():
    behavior = _seed().model_copy(update={
        "seed_id": "investigation-neutral-behavior",
        "evidence_need": EvidenceNeed.INSPECT_STRUCTURE,
        "allowed_tools": ("inspect_structure", "get_file_content"),
        "path_kind": None,
        "direction": None,
        "observed_change": "删除 super.parseFromLocalFileData 调用",
        "investigation_question": "检查父类初始化状态是否丢失",
    })
    threat = behavior.model_copy(update={
        "seed_id": "investigation-neutral-threat",
        "reviewer": ReviewerKind.THREAT_MODEL,
        "path_kind": "security",
    })
    groups = group_investigation_seeds({
        ReviewerKind.BEHAVIOR: (behavior,),
        ReviewerKind.THREAT_MODEL: (threat,),
    })
    assert len(groups) == 1
    assert groups[0].seed.path_kind is None


def test_reviewer_local_risk_labels_do_not_duplicate_same_investigation():
    behavior = _seed().model_copy(update={
        "evidence_need": EvidenceNeed.INSPECT_STRUCTURE,
        "allowed_tools": ("inspect_structure", "get_file_content"),
        "risk_dimension": "state_consistency",
        "observed_change": "删除 super.parseFromLocalFileData 调用",
        "investigation_question": "检查父类初始化状态是否丢失",
    })
    threat = behavior.model_copy(update={
        "seed_id": "investigation-threat-risk-label",
        "reviewer": ReviewerKind.THREAT_MODEL,
        "risk_dimension": "input_validation",
    })
    groups = group_investigation_seeds({
        ReviewerKind.BEHAVIOR: (behavior,),
        ReviewerKind.THREAT_MODEL: (threat,),
    })
    assert len(groups) == 1
    assert set(groups[0].seed_ids) == {
        behavior.seed_id,
        threat.seed_id,
    }


def test_same_anchor_structure_and_path_seeds_merge_into_one_coherent_investigation():
    path_seed = _seed().model_copy(
        update={
            "seed_id": "investigation-behavior-path",
            "location_line": 373,
            "initial_symbol_ids": ("java:A#parse()",),
            "evidence_need": EvidenceNeed.INSPECT_PATH,
            "allowed_tools": ("inspect_path",),
            "path_kind": "behavior",
            "direction": "downstream",
            "observed_change": "删除 super.parse() 调用",
            "investigation_question": "检查 override 的调用关系",
        }
    )
    structure_seed = path_seed.model_copy(
        update={
            "seed_id": "investigation-behavior-structure",
            "evidence_need": EvidenceNeed.INSPECT_STRUCTURE,
            "allowed_tools": ("get_file_content",),
            "path_kind": None,
            "direction": "upstream",
            "investigation_question": "检查父类 parse() 是否写入状态",
        }
    )

    groups = group_investigation_seeds({
        ReviewerKind.BEHAVIOR: (path_seed, structure_seed),
    })

    assert len(groups) == 1
    merged = groups[0].seed
    assert set(merged.allowed_tools) == {"inspect_path", "get_file_content"}
    assert merged.evidence_need is EvidenceNeed.INSPECT_PATH
    assert merged.direction == "downstream"


def test_subtask_capability_bundle_rejects_zero_budget_and_opposite_direction():
    seed = _seed().model_copy(
        update={
            "evidence_need": EvidenceNeed.INSPECT_CHANGE_IMPACT,
            "direction": "upstream",
            "allowed_tools": (
                "inspect_change_impact",
                "inspect_path",
                "get_file_content",
            ),
        }
    )
    assert coherent_tool_bundle(
        seed,
        seed.allowed_tools,
        domain_tools=(
            "inspect_change_impact",
            "inspect_path",
            "get_file_content",
        ),
        max_tools=0,
    ) == ()
    assert coherent_tool_bundle(
        seed,
        seed.allowed_tools,
        domain_tools=(
            "inspect_change_impact",
            "inspect_path",
            "get_file_content",
        ),
    ) == ("inspect_change_impact", "get_file_content")

    contradictory = seed.model_copy(update={"direction": "downstream"})
    assert coherent_tool_bundle(
        contradictory,
        contradictory.allowed_tools,
        domain_tools=(
            "inspect_change_impact",
            "inspect_path",
            "get_file_content",
        ),
    ) == ("get_file_content",)


def test_requested_graph_tools_never_returns_both_traversal_directions():
    seed = _seed().model_copy(update={
        "evidence_need": EvidenceNeed.INSPECT_PATH,
        "direction": "downstream",
        "path_kind": "behavior",
    })
    assert requested_graph_tools(
        seed,
        ("inspect_path", "inspect_change_impact", "inspect_structure"),
    ) == ("inspect_path", "inspect_structure")


def test_discovery_client_rejects_invalid_allowed_path_kind():
    with pytest.raises(ValueError, match="allowed_path_kind"):
        CoordinatedDiscoveryToolClient(
            object(),
            DiscoveryToolCoordinator(),
            allowed_path_kind="not-a-path-kind",
        )


def test_investigation_seed_path_contract_uses_reviewer_domain_and_direction():
    omitted = _seed().model_copy(
        update={
            "evidence_need": EvidenceNeed.INSPECT_PATH,
            "allowed_tools": ("inspect_path",),
            "path_kind": None,
            "direction": None,
            "reviewer": ReviewerKind.THREAT_MODEL,
        }
    )
    normalized = normalize_investigation_seed(omitted)
    assert normalized.path_kind == "security"
    assert normalized.direction == "downstream"

    contradictory = omitted.model_copy(update={"direction": "upstream"})
    normalized = normalize_investigation_seed(contradictory)
    assert normalized.evidence_need is EvidenceNeed.INSPECT_CHANGE_IMPACT
    assert normalized.path_kind is None
    assert normalized.direction == "upstream"


def test_opposite_direction_seeds_at_same_anchor_stay_separate():
    downstream = _seed().model_copy(
        update={
            "seed_id": "investigation-downstream",
            "evidence_need": EvidenceNeed.INSPECT_PATH,
            "allowed_tools": ("inspect_path", "get_file_content"),
            "path_kind": "behavior",
            "direction": "downstream",
        }
    )
    upstream = downstream.model_copy(
        update={
            "seed_id": "investigation-upstream",
            "evidence_need": EvidenceNeed.INSPECT_CHANGE_IMPACT,
            "allowed_tools": ("inspect_change_impact", "get_file_content"),
            "path_kind": None,
            "direction": "upstream",
        }
    )
    groups = group_investigation_seeds({ReviewerKind.BEHAVIOR: (downstream, upstream)})
    assert len(groups) == 2


def test_security_path_does_not_merge_with_neutral_structure_companion():
    security = _seed().model_copy(
        update={
            "seed_id": "investigation-security",
            "evidence_need": EvidenceNeed.INSPECT_PATH,
            "allowed_tools": ("inspect_path", "get_file_content"),
            "path_kind": "security",
            "direction": "downstream",
        }
    )
    structure = security.model_copy(
        update={
            "seed_id": "investigation-structure",
            "evidence_need": EvidenceNeed.INSPECT_STRUCTURE,
            "allowed_tools": ("inspect_structure", "get_file_content"),
            "path_kind": None,
            "direction": None,
        }
    )
    groups = group_investigation_seeds({ReviewerKind.THREAT_MODEL: (security, structure)})
    assert len(groups) == 2


def test_subtask_plan_merges_duplicate_instructions_and_keeps_graph_reader_bundle():
    seed = _seed().model_copy(
        update={
            "seed_id": "investigation-behavior-parent",
            "location_line": 373,
            "initial_symbol_ids": ("java:A#parse()",),
            "evidence_need": EvidenceNeed.INSPECT_STRUCTURE,
            "allowed_tools": ("inspect_structure", "get_file_content"),
        }
    )

    class _Structured:
        def invoke(self, _messages):
            return SubtaskPlan(
                reviewer=ReviewerKind.BEHAVIOR,
                task_id="task-1",
                subtasks=(
                    SubtaskInstruction(
                        subtask_id="provider-1",
                        seed_id=seed.seed_id,
                        reviewer=ReviewerKind.BEHAVIOR,
                        change_unit_id=seed.change_unit_id,
                        objective=seed.investigation_question,
                        observed_change=seed.observed_change,
                        initial_symbol_ids=("S01",),
                        allowed_tools=("inspect_structure",),
                        primary_tool="inspect_structure",
                        max_tool_calls=2,
                        max_rounds=2,
                    ),
                    SubtaskInstruction(
                        subtask_id="provider-2",
                        seed_id=seed.seed_id,
                        reviewer=ReviewerKind.BEHAVIOR,
                        change_unit_id=seed.change_unit_id,
                        objective="读取父类实现",
                        observed_change=seed.observed_change,
                        initial_symbol_ids=("S01",),
                        allowed_tools=("get_file_content",),
                        primary_tool="get_file_content",
                        max_tool_calls=4,
                        max_rounds=3,
                    ),
                ),
            )

    class _LLM:
        def with_structured_output(self, _schema, method=None):  # noqa: ARG002
            return _Structured()

    context = SimpleNamespace(symbols=(ResolvedSymbol(
        file="A.java", symbol_id="java:A#parse()", kind="METHOD",
        start_line=1, end_line=5, source_set="MAIN",
    ),))
    plan, diagnostics = run_subtask_plan(
        reviewer=ReviewerKind.BEHAVIOR,
        task=SimpleNamespace(id="task-1", file="src/A.java", patch="+super.parse();"),
        seeds=(seed,),
        symbol_context=context,
        llm=_LLM(),
        max_retries=1,
        structured_method="function_calling",
        max_tool_calls=6,
        max_rounds=4,
        max_subtasks=4,
        max_path_depth=3,
    )

    assert len(plan.subtasks) == 1
    assert set(plan.subtasks[0].allowed_tools) == {
        "inspect_structure", "get_file_content",
    }
    assert plan.subtasks[0].max_tool_calls == 4
    assert any(item.startswith("subtask_duplicate_seed_merged:") for item in diagnostics)


def test_subtask_budget_split_uses_remainder_without_zero_budget_tasks():
    assert _allocate_subtask_budgets(
        5,
        total_budget=16,
        per_subtask_limit=4,
    ) == (4, 3, 3, 3, 3)
    assert _allocate_subtask_budgets(
        8,
        total_budget=3,
        per_subtask_limit=4,
    ) == (1, 1, 1, 0, 0, 0, 0, 0)


def test_graph_recursion_is_reported_as_inconclusive_not_task_failure():
    class GraphRecursionError(Exception):
        pass

    class Client:
        trace_records = ()

    instruction = SubtaskInstruction(
        subtask_id="subtask-1",
        seed_id="seed-1",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-task-1",
        objective="检查返回行为",
        observed_change="返回表达式发生变化",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("get_file_content",),
    )
    engine = SubtaskReactEngine(Client(), max_tool_calls=2, max_rounds=2)
    engine._run_agent = lambda *args: (_ for _ in ()).throw(GraphRecursionError())

    outcome = engine.run(
        object(),
        task=SimpleNamespace(file="src/A.java", patch="+return run();"),
        symbol_context=SimpleNamespace(symbols=()),
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )

    assert outcome.status == "inconclusive"
    assert outcome.reason == "subtask_recursion_limit"
    assert outcome.events == ["subtask_inconclusive"]


def test_graph_recursion_after_budget_rejection_is_labeled_as_budget_gap():
    class GraphRecursionError(Exception):
        pass

    class Client:
        trace_records = ()
        budget_exhausted = True

    instruction = SubtaskInstruction(
        subtask_id="subtask-budget-recursion",
        seed_id="seed-budget-recursion",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-task-1",
        objective="检查返回行为",
        observed_change="返回表达式发生变化",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("get_file_content",),
    )
    engine = SubtaskReactEngine(Client(), max_tool_calls=2, max_rounds=2)
    engine._run_agent = lambda *args: (_ for _ in ()).throw(GraphRecursionError())

    outcome = engine.run(
        object(),
        task=SimpleNamespace(file="src/A.java", patch="+return run();"),
        symbol_context=SimpleNamespace(symbols=()),
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )

    assert outcome.status == "inconclusive"
    assert outcome.reason == "tool_budget_exceeded"
    assert outcome.events == ["subtask_tool_budget_exceeded"]


def test_rejected_budget_call_cannot_be_reported_as_no_finding():
    class Client:
        trace_records = ()
        tool_calls = 1
        budget_exhausted = True

    instruction = SubtaskInstruction(
        subtask_id="subtask-budget",
        seed_id="seed-budget",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-task-1",
        objective="检查返回行为",
        observed_change="返回表达式发生变化",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("get_file_content",),
    )
    engine = SubtaskReactEngine(Client(), max_tool_calls=2, max_rounds=2)
    engine._run_agent = lambda *args: {
        "structured_response": InvestigationResult(
            subtask_id="subtask-budget",
            outcome="no_finding",
        )
    }

    outcome = engine.run(
        object(),
        task=SimpleNamespace(file="src/A.java", patch="+return run();"),
        symbol_context=SimpleNamespace(symbols=()),
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )

    assert outcome.status == "inconclusive"
    assert outcome.reason == "tool_budget_exceeded"
    assert outcome.events == ["subtask_tool_budget_exceeded"]
