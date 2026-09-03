from __future__ import annotations

from codeguard_agent.models.tasks import (
    CandidateSeed,
    AssessmentStatus,
    CoverageDeclaration,
    DirectTriageResult,
    EvidenceNeed,
    EvidenceStep,
    GraphQuestion,
    ProofScope,
    ProofMatch,
    ProofMatchStatus,
    ReviewerGraphPlan,
    ReviewerKind,
    ReviewTask,
    TaskSelection,
    TaskSymbolContext,
    WorkItem,
)
from codeguard_agent.models.evidence import EvidenceCatalog
from codeguard_agent.models.tasks.symbols import ResolvedSymbol, SymbolResolutionStatus
from codeguard_agent.pipeline.controlled.executor import ControlledEvidenceExecutor
from codeguard_agent.pipeline.controlled.graph_plan import validate_graph_plan
from codeguard_agent.pipeline.controlled.assessment import (
    match_execution_proof,
    run_evidence_assessment,
)
from codeguard_agent.pipeline.controlled.assessment import visible_symbol_ids
from codeguard_agent.pipeline.controlled.triage import run_direct_triage
from codeguard_agent.pipeline.orchestration.graph import _controlled_review_node
from codeguard_agent.tools.tool_client import ToolResponse


class _GraphClient:
    def __init__(self) -> None:
        self.path_calls: list[tuple[str, str, int]] = []

    def inspect_path(self, symbol_id: str, path_kind: str, max_depth: int = 3) -> ToolResponse:
        self.path_calls.append((symbol_id, path_kind, max_depth))
        return ToolResponse(
            True,
            '{"schema_version":2,"outcome":"found","coverage":"complete",'
            '"source_scope":"MAIN","subject_symbol_id":"s1",'
            '"symbols":[{"id":"s1","kind":"METHOD","source_set":"MAIN"},'
            '{"id":"s2","kind":"METHOD","source_set":"MAIN"}],'
            '"relationships":[{"sourceId":"s1","targetId":"s2",'
            '"kind":"CALLS","source_set":"MAIN","resolution":"RESOLVED"}],'
            '"unresolved_relationships":[],"unresolved_count":0,"limitations":[]}',
        )

    def inspect_structure(self, symbol_id: str) -> ToolResponse:  # noqa: ARG002
        return ToolResponse(False, error="not_used")


class _Structured:
    def __init__(self, result):
        self.result = result

    def invoke(self, _messages):
        return self.result


class _TriageLLM:
    def __init__(self, result):
        self.result = result

    def with_structured_output(self, _schema, method=None):  # noqa: ARG002
        return _Structured(self.result)

    def inspect_change_impact(self, symbol_id: str) -> ToolResponse:  # noqa: ARG002
        return ToolResponse(False, error="not_used")

    def get_file_content(self, symbol_id: str) -> ToolResponse:  # noqa: ARG002
        return ToolResponse(False, error="not_used")


def _context() -> TaskSymbolContext:
    return TaskSymbolContext(
        task_id="A.java#h0",
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="A.java",
                symbol_id="s1",
                kind="METHOD",
                start_line=1,
                end_line=5,
                source_set="MAIN",
            ),
        ),
    )


def _plan() -> ReviewerGraphPlan:
    return ReviewerGraphPlan(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        work_items=(
            WorkItem(
                seed_id="seed-1",
                reviewer=ReviewerKind.BEHAVIOR,
                hypothesis="下游调用丢失",
                expected_mechanism="调用链被截断",
                evidence_steps=(
                    EvidenceStep(
                        tool="inspect_path",
                        subject_ref="s1",
                        path_kind="behavior",
                        max_depth=3,
                        purpose="验证下游调用",
                        expected_fact="到达 s2",
                    ),
                ),
                candidate_criteria="存在完整路径",
                rejection_criteria="无路径",
            ),
        ),
    )


def test_graph_plan_binds_step_ids_and_executor_deduplicates_calls():
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="下游调用丢失",
        mechanism="调用链被截断",
        location_file="A.java",
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("s2",),
            required_relationships=("CALLS",),
            question="是否到达 s2",
        ),
        seed_id="seed-1",
    )
    normalized, diagnostics = validate_graph_plan(
        _plan(),
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        seeds=(seed,),
        symbol_context=_context(),
        max_path_depth=3,
    )
    assert not diagnostics
    assert normalized.work_items[0].work_item_id.startswith("wi-behavior-")
    task = ReviewTask(id="A.java#h0", file="A.java", patch="+call", changed_lines=[2])
    client = _GraphClient()
    batch = ControlledEvidenceExecutor(
        tool_client=client,
        task=task,
        symbol_context=_context(),
        revision="r1",
        initial_budget=1,
    ).execute((normalized, normalized))
    assert client.path_calls == [("s1", "behavior", 3)]
    assert len(batch.trace_refs) == 1
    assert batch.catalog.tool_aliases() == ["T01"]
    assert batch.steps[0].alias == "T01"


def test_reused_step_keeps_fact_for_proof_matching():
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="下游调用存在",
        mechanism="调用链事实需要确认",
        location_file="A.java",
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("s2",),
            required_relationships=("CALLS",),
            question="是否到达 s2",
        ),
        seed_id="seed-1",
    )
    normalized, _ = validate_graph_plan(
        _plan(),
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        seeds=(seed,),
        symbol_context=_context(),
        max_path_depth=3,
    )
    batch = ControlledEvidenceExecutor(
        tool_client=_GraphClient(),
        task=ReviewTask(id="A.java#h0", file="A.java", patch="+call", changed_lines=[2]),
        symbol_context=_context(),
        revision="r1",
        initial_budget=1,
        extra_symbol_ids={"s2"},
    ).execute((normalized, normalized))
    reused = [step for step in batch.steps if step.status == "reused"]
    assert reused and reused[0].raw_payload
    proof = match_execution_proof(
        work_item=normalized.work_items[0],
        seed=seed,
        steps=tuple(batch.steps),
        subject_symbol_id="s1",
    )
    assert proof.status.value == "proved"


def test_assessment_binds_graph_artifact_when_model_cites_source_only():
    from codeguard_agent.pipeline.controlled.executor import ExecutionBatch, StepExecution
    from codeguard_agent.models.tasks import EvidenceAssessment, EvidenceAssessmentBatch

    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="下游调用存在",
        mechanism="调用链事实需要确认",
        location_file="A.java",
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("s2",),
            required_relationships=("CALLS",),
            question="是否到达 s2",
        ),
        seed_id="seed-1",
    )
    work_item = _plan().work_items[0].model_copy(update={"work_item_id": "wi-1"})
    graph_payload = (
        '{"schema_version":2,"outcome":"found","coverage":"complete",'
        '"subject_symbol_id":"s1","symbols":[{"id":"s1"},{"id":"s2"}],'
        '"relationships":[{"sourceId":"s1","targetId":"s2","kind":"CALLS"}]}'
    )
    execution = ExecutionBatch(
        catalog=EvidenceCatalog(task_id="A.java#h0", reviewer="controlled", revision="r1"),
        artifacts={},
        trace_refs=(),
        steps=(
            StepExecution(
                work_item_id=work_item.work_item_id,
                step=work_item.evidence_steps[0].model_copy(update={"tool": "get_file_content"}),
                status="complete",
                alias="T01",
                raw_payload="class A {}",
                projected_payload="class A {}",
            ),
            StepExecution(
                work_item_id=work_item.work_item_id,
                step=work_item.evidence_steps[0],
                status="complete",
                alias="T02",
                raw_payload=graph_payload,
                projected_payload=graph_payload,
            ),
        ),
    )
    assessment = EvidenceAssessment(
        work_item_id=work_item.work_item_id,
        status=AssessmentStatus.CANDIDATE,
        claim=seed.claim,
        mechanism=seed.mechanism,
        proof_scope=seed.proof_scope,
        supporting_refs=("T01",),
    )
    result, diagnostics = run_evidence_assessment(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        work_items=(work_item,),
        seeds={seed.seed_id: seed},
        execution=execution,
        proof_matches={
            work_item.work_item_id: ProofMatch(
                work_item_id=work_item.work_item_id,
                status=ProofMatchStatus.PROVED,
            )
        },
        llm=_TriageLLM(EvidenceAssessmentBatch(assessments=(assessment,))),
        max_retries=1,
        structured_method="function_calling",
    )

    assert result[work_item.work_item_id].supporting_refs == ("T02", "T01")
    assert any("assessment_ref_bound" in item for item in diagnostics)


def test_graph_plan_rejects_dependency_cycle():
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="下游调用存在",
        mechanism="调用链事实需要确认",
        location_file="A.java",
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("s2",),
            required_relationships=("CALLS",),
            question="是否到达 s2",
        ),
        seed_id="seed-1",
    )
    cycle_plan = _plan().model_copy(
        update={
            "work_items": (
                _plan().work_items[0].model_copy(
                    update={
                        "evidence_steps": (
                            EvidenceStep(
                                tool="inspect_path",
                                subject_ref="s1",
                                path_kind="behavior",
                                max_depth=3,
                                purpose="第一步",
                                expected_fact="s2",
                                step_id="a",
                                depends_on=("b",),
                            ),
                            EvidenceStep(
                                tool="inspect_structure",
                                subject_ref="s1",
                                purpose="第二步",
                                expected_fact="结构",
                                step_id="b",
                                depends_on=("a",),
                            ),
                        )
                    }
                ),
            )
        }
    )
    normalized, diagnostics = validate_graph_plan(
        cycle_plan,
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        seeds=(seed,),
        symbol_context=_context(),
        max_path_depth=3,
    )
    assert not normalized.work_items
    assert any("dependency_cycle" in item for item in diagnostics)


def test_graph_plan_rejects_steps_outside_enabled_tool_budget():
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="下游调用存在",
        mechanism="调用链事实需要确认",
        location_file="A.java",
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("s2",),
            required_relationships=("CALLS",),
            question="是否到达 s2",
        ),
        seed_id="seed-1",
    )
    normalized, diagnostics = validate_graph_plan(
        _plan(),
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        seeds=(seed,),
        symbol_context=_context(),
        max_path_depth=3,
        enabled_tools={"get_file_content"},
    )
    assert not normalized.work_items
    assert any("tool_disabled" in item for item in diagnostics)


def test_direct_triage_is_structured_and_does_not_route_by_confidence():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+if (value == null) return;",
        changed_lines=[2],
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="local_only",
                reason="条件和影响均在新增行中可见",
            ),
        ),
        issues=(
            CandidateSeed(
                reviewer=ReviewerKind.BEHAVIOR,
                change_unit_id="CU-A.java#h0",
                claim="空值分支改变了原有处理",
                mechanism="新增提前返回",
                location_file="A.java",
                location_line=2,
                proof_scope=ProofScope.LOCAL,
                evidence_basis=("changed_lines",),
                confidence=0.01,
            ),
        ),
    )
    triage, diagnostics = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=None,
        llm=_TriageLLM(result),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )
    assert triage is not None
    assert triage.issues[0].confidence == 0.01
    assert any(item.startswith("seed_route:") for item in diagnostics)


def test_controlled_review_node_runs_three_triage_reviewers_without_tools():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+if (value == null) return;",
        changed_lines=[2],
    )
    from codeguard_agent.models.tasks import PlanUnit, TaskRoute

    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="空值分支改变了原有处理",
        mechanism="新增提前返回",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.LOCAL,
        evidence_basis=("changed_lines",),
        confidence=0.9,
    )
    triage = DirectTriageResult(
        coverage=(CoverageDeclaration(change_unit_id="CU-A.java#h0", decision="local_only", reason="局部足够"),),
        issues=(seed,),
    )
    node = _controlled_review_node(_TriageLLM(triage), tool_client=None)
    state = {
        "review_tasks": [task],
        "task_selection": TaskSelection(selected_task_ids=[task.id]),
        "task_routes": {task.id: TaskRoute(task_id=task.id, route="full", reason="test")},
        "plan_units": [PlanUnit(id="A.java", file="A.java", task_ids=(task.id,))],
        "knowledge_route_plan": {},
        "task_symbol_contexts": {},
        "evidence_revision": "r1",
        "max_retries": 1,
        "structured_method": "function_calling",
    }
    output = node(state)
    assert len(output["raw_candidate_issues"]) == 1
    assert output["raw_candidate_issues"][0].source_agent == "behavior"
    assert output["tool_trace_records"] == []


def test_delta_visibility_uses_projected_payload_not_raw_artifact():
    from codeguard_agent.pipeline.controlled.executor import ExecutionBatch, StepExecution

    step = EvidenceStep(
        tool="inspect_path",
        subject_ref="s1",
        path_kind="behavior",
        max_depth=3,
        purpose="验证下游调用",
        expected_fact="到达 s2",
    )
    execution = ExecutionBatch(
        catalog=EvidenceCatalog(task_id="A.java#h0", reviewer="controlled", revision="r1"),
        artifacts={},
        trace_refs=(),
        steps=(
            StepExecution(
                work_item_id="wi-1",
                step=step,
                status="complete",
                raw_payload=(
                    '{"schema_version":2,"symbols":[{"id":"raw-only"}],'
                    '"relationships":[]}'
                ),
                projected_payload=(
                    '{"schema_version":2,"symbols":[{"id":"visible"}],'
                    '"relationships":[]}'
                ),
            ),
        ),
    )
    assert visible_symbol_ids(execution) == {"visible"}


def test_direct_triage_normalizes_invalid_seed_location():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+if (value == null) return;",
        changed_lines=[2],
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="local_only",
                reason="局部足够",
            ),
        ),
        issues=(
            CandidateSeed(
                reviewer=ReviewerKind.BEHAVIOR,
                change_unit_id="CU-A.java#h0",
                claim="错误行号候选",
                mechanism="机制",
                location_file="A.java",
                location_line=99,
                proof_scope=ProofScope.LOCAL,
                evidence_basis=("changed_lines",),
            ),
        ),
    )
    triage, diagnostics = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=None,
        llm=_TriageLLM(result),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )
    assert triage is not None
    assert triage.issues[0].location_line == 0
    assert any("candidate_location_unresolved" in item for item in diagnostics)


def test_direct_triage_rejects_seed_for_another_file():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+if (value == null) return;",
        changed_lines=[2],
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="local_only",
                reason="局部足够",
            ),
        ),
        issues=(
            CandidateSeed(
                reviewer=ReviewerKind.BEHAVIOR,
                change_unit_id="CU-A.java#h0",
                claim="错误文件候选",
                mechanism="机制",
                location_file="B.java",
                location_line=2,
                proof_scope=ProofScope.LOCAL,
                evidence_basis=("changed_lines",),
            ),
        ),
    )
    triage, diagnostics = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=None,
        llm=_TriageLLM(result),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )
    assert triage is not None
    assert triage.issues == ()
    assert "location_file_mismatch" in " ".join(diagnostics)
