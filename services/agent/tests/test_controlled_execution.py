from __future__ import annotations

from types import SimpleNamespace

from codeguard_agent.models.tasks import (
    CandidateSeed,
    AssessmentStatus,
    CoverageDeclaration,
    CoverageDecision,
    DirectTriageResult,
    EvidenceAssessment,
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
from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks.symbols import ResolvedSymbol, SymbolResolutionStatus
from codeguard_agent.pipeline.controlled.executor import (
    ControlledEvidenceExecutor,
    _fair_plan_steps,
)
from codeguard_agent.pipeline.controlled.graph_plan import (
    _baseline_graph_plan,
    run_graph_plan,
    validate_graph_plan,
)
from codeguard_agent.pipeline.controlled.assessment import (
    build_evidence_pack,
    candidate_from_seed,
    collapse_candidate_duplicates,
    match_execution_proof,
    run_evidence_assessment,
)
from codeguard_agent.pipeline.controlled.assessment import visible_symbol_ids
from codeguard_agent.pipeline.controlled.triage import run_direct_triage
from codeguard_agent.pipeline.controlled.triage import (
    _merge_protocol_repair_issues,
    _needs_claim_consequence_repair,
    _needs_claim_scope_repair,
    _needs_claim_self_call_repair,
    _normalize_graph_question,
    _invoke_once,
)
from codeguard_agent.pipeline.controlled.llm_contracts import (
    LlmCandidateSeed,
    LlmDirectTriageResult,
)
from codeguard_agent.pipeline.orchestration.graph import (
    _assemble_state_dossiers,
    _controlled_review_node,
    _select_delta_work_items,
)
from codeguard_agent.pipeline.orchestration.graph import _auto_context_delta_step
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

    def get_file_content(self, symbol_id: str) -> ToolResponse:  # noqa: ARG002
        return ToolResponse(True, "class A { void changed() { call(); } }")


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
    assert not any("unknown_" in item or "invalid" in item for item in diagnostics)
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


def test_executor_fairly_schedules_primary_step_for_each_work_item():
    """A small task budget must not starve later WorkItems of graph proof."""

    def item(seed_id: str, symbol_id: str) -> WorkItem:
        return WorkItem(
            seed_id=seed_id,
            reviewer=ReviewerKind.BEHAVIOR,
            hypothesis="跨符号行为需要确认",
            expected_mechanism="调用路径需要确认",
            evidence_steps=(
                EvidenceStep(
                    tool="inspect_path",
                    subject_ref=symbol_id,
                    path_kind="behavior",
                    max_depth=3,
                    purpose="验证下游路径",
                    expected_fact="存在下游关系",
                ),
                EvidenceStep(
                    tool="get_file_content",
                    subject_ref=symbol_id,
                    purpose="补充局部实现",
                    expected_fact="确认局部机制",
                ),
            ),
            candidate_criteria="存在关系",
            rejection_criteria="无关系",
        )

    plan = ReviewerGraphPlan(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        work_items=(item("seed-a", "s1"), item("seed-b", "s2")),
    )
    client = _GraphClient()
    batch = ControlledEvidenceExecutor(
        tool_client=client,
        task=ReviewTask(id="A.java#h0", file="A.java", patch="+call", changed_lines=[2]),
        symbol_context=_context(),
        revision="r1",
        initial_budget=2,
    ).execute((plan,))

    assert client.path_calls == [("s1", "behavior", 3), ("s2", "behavior", 3)]
    assert [step.status for step in batch.steps] == [
        "complete",
        "complete",
        "budget_exhausted",
        "budget_exhausted",
    ]


def test_executor_prioritizes_distinct_graph_subjects_before_source_round():
    """Independent graph questions are not starved by an eager source pair."""

    def seed(seed_id: str, line: int) -> CandidateSeed:
        return CandidateSeed(
            reviewer=ReviewerKind.BEHAVIOR,
            change_unit_id="CU-A.java#file",
            claim=f"changed behavior at line {line} needs evidence",
            mechanism="state behavior needs confirmation",
            location_file="A.java",
            location_line=line,
            proof_scope=ProofScope.CROSS_FILE,
            evidence_basis=("changed_lines",),
            evidence_need=EvidenceNeed.INSPECT_PATH,
            graph_question=GraphQuestion(
                subject_ref="s1",
                direction="downstream",
                path_kind="behavior",
                required_relationships=("CALLS",),
                question="verify downstream path",
            ),
            seed_id=seed_id,
        )

    def item(seed_id: str, source: str) -> WorkItem:
        return WorkItem(
            seed_id=seed_id,
            reviewer=ReviewerKind.BEHAVIOR,
            hypothesis="verify downstream path",
            expected_mechanism="confirm state",
            evidence_steps=(
                EvidenceStep(
                    tool="inspect_path",
                    subject_ref=source,
                    path_kind="behavior",
                    max_depth=3,
                ),
                EvidenceStep(tool="get_file_content", subject_ref=source),
            ),
        )

    plans = (
        ReviewerGraphPlan(
            reviewer=ReviewerKind.BEHAVIOR,
            task_id="A.java#file",
            work_items=(
                item("seed-a", "s1"),
                item("seed-a2", "s2"),
                item("seed-b", "s3"),
            ),
        ),
    )
    task = ReviewTask(
        id="A.java#file",
        file="A.java",
        patch="+a\n+b\n+c",
        changed_lines=[10, 30, 50],
    )
    ordered = _fair_plan_steps(
        plans,
        task=task,
        seed_by_id={
            "seed-a": seed("seed-a", 10),
            "seed-a2": seed("seed-a2", 10),
            "seed-b": seed("seed-b", 30),
        },
    )
    assert [(item.seed_id, step.tool) for item, step in ordered[:3]] == [
        ("seed-a", "inspect_path"),
        ("seed-b", "inspect_path"),
        ("seed-a2", "inspect_path"),
    ]
    assert ("seed-a", "get_file_content") in [
        (item.seed_id, step.tool) for item, step in ordered[3:]
    ]


def test_delta_reservation_covers_distinct_locations_before_reviewer_order():
    """One reviewer cannot consume every Delta slot for one changed hunk."""

    def seed(seed_id: str, line: int, reviewer: ReviewerKind) -> CandidateSeed:
        return CandidateSeed(
            reviewer=reviewer,
            change_unit_id="CU-A.java#file",
            claim=f"changed behavior at line {line} needs doOpenInternal evidence",
            mechanism="state/context timing or return behavior needs confirmation",
            location_file="A.java",
            location_line=line,
            proof_scope=ProofScope.CROSS_FILE,
            evidence_basis=("changed_lines",),
            evidence_need=EvidenceNeed.INSPECT_PATH,
            graph_question=GraphQuestion(
                subject_ref="s1",
                direction="downstream",
                path_kind="behavior",
                required_relationships=("CALLS",),
                question="verify downstream path",
            ),
            seed_id=seed_id,
        )

    first = seed("seed-first", 297, ReviewerKind.BEHAVIOR)
    second = seed("seed-second", 500, ReviewerKind.BEHAVIOR)
    duplicate_first = seed("seed-duplicate", 297, ReviewerKind.THREAT_MODEL)

    def item(seed_id: str, suffix: str) -> WorkItem:
        return WorkItem(
            work_item_id=f"wi-{suffix}",
            seed_id=seed_id,
            reviewer=ReviewerKind.BEHAVIOR,
            hypothesis="verify downstream path",
            expected_mechanism="confirm state",
            evidence_steps=(EvidenceStep(tool="inspect_path", subject_ref="s1", path_kind="behavior", max_depth=3),),
            candidate_criteria="path",
            rejection_criteria="none",
        )

    payload = '{"symbols":[{"id":"s1","file":"A.java"},{"id":"s2","file":"A.java"}]}'
    execution = SimpleNamespace(
        steps=(
            SimpleNamespace(
                step=SimpleNamespace(tool="inspect_path"),
                projected_payload=payload,
            ),
        )
    )
    selected = _select_delta_work_items(
        graph_plans=[
            ReviewerGraphPlan(
                reviewer=ReviewerKind.THREAT_MODEL,
                task_id="A.java#file",
                work_items=(item("seed-duplicate", "duplicate"),),
            ),
            ReviewerGraphPlan(
                reviewer=ReviewerKind.BEHAVIOR,
                task_id="A.java#file",
                work_items=(item("seed-first", "first"), item("seed-second", "second")),
            ),
        ],
        seeds={candidate.seed_id: candidate for candidate in (first, second, duplicate_first)},
        execution=execution,
        budget=2,
    )

    assert selected == {"wi-first", "wi-second"}


def test_graph_plan_keeps_required_graph_fact_when_provider_overproduces():
    """Provider ordering must not evict the GraphQuestion's canonical query."""

    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="下游调用需要确认",
        mechanism="候选依赖跨符号路径",
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
        seed_id="seed-overproduced",
    )
    overproduced = ReviewerGraphPlan(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        work_items=(
            WorkItem(
                seed_id=seed.seed_id,
                reviewer=ReviewerKind.BEHAVIOR,
                hypothesis=seed.claim,
                expected_mechanism=seed.mechanism,
                evidence_steps=(
                    EvidenceStep(
                        tool="get_file_content",
                        subject_ref="s1",
                        purpose="源码",
                        expected_fact="局部机制",
                    ),
                    EvidenceStep(
                        tool="inspect_change_impact",
                        subject_ref="s1",
                        purpose="错误方向的额外查询",
                        expected_fact="调用者",
                    ),
                    EvidenceStep(
                        tool="inspect_path",
                        subject_ref="s1",
                        path_kind="behavior",
                        max_depth=3,
                        purpose="正确的下游查询",
                        expected_fact="s2",
                    ),
                    EvidenceStep(
                        tool="inspect_structure",
                        subject_ref="s1",
                        purpose="额外结构查询",
                        expected_fact="结构",
                    ),
                ),
            ),
        ),
    )
    normalized, diagnostics = validate_graph_plan(
        overproduced,
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        seeds=(seed,),
        symbol_context=_context(),
        max_path_depth=3,
        enabled_tools={
            "get_file_content",
            "inspect_structure",
            "inspect_change_impact",
            "inspect_path",
        },
    )
    assert [step.tool for step in normalized.work_items[0].evidence_steps] == [
        "inspect_path",
        "get_file_content",
    ]
    assert any("too_many_steps_trimmed" in item for item in diagnostics)


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


def test_assessment_keeps_subject_and_endpoint_sources_after_delta():
    """A Delta source must not evict the changed subject's mechanism excerpt."""

    from codeguard_agent.pipeline.controlled.executor import ExecutionBatch, StepExecution
    from codeguard_agent.models.tasks import EvidenceAssessmentBatch

    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="状态注册顺序变化可能影响下游观察者",
        mechanism="注册调用从变更方法的前置位置移动到后续分支",
        location_file="A.java",
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("s3",),
            required_relationships=("CALLS",),
            question="s1 是否到达下游观察者 s3",
        ),
        seed_id="seed-source-pair",
    )
    work_item = _plan().work_items[0].model_copy(
        update={"work_item_id": "wi-source-pair", "seed_id": seed.seed_id}
    )
    graph_step = work_item.evidence_steps[0]
    source_subject = EvidenceStep(tool="get_file_content", subject_ref="s1")
    source_endpoint = EvidenceStep(tool="get_file_content", subject_ref="s3")
    graph_payload = (
        '{"schema_version":2,"outcome":"found","coverage":"partial",'
        '"subject_symbol_id":"s1","symbols":[{"id":"s1"},{"id":"s3"}],'
        '"relationships":[{"sourceId":"s1","targetId":"s3","kind":"CALLS"}],'
        '"unresolved_relationships":[],"unresolved_count":1,"limitations":[]}'
    )
    execution = ExecutionBatch(
        catalog=EvidenceCatalog(task_id="A.java#h0", reviewer="controlled", revision="r1"),
        artifacts={},
        trace_refs=(),
        steps=(
            StepExecution(work_item_id=work_item.work_item_id, step=graph_step, status="complete", alias="T01", raw_payload=graph_payload, projected_payload=graph_payload),
            StepExecution(work_item_id=work_item.work_item_id, step=source_subject, status="complete", alias="T02", raw_payload="subject", projected_payload="subject"),
            StepExecution(work_item_id=work_item.work_item_id, step=source_endpoint, status="complete", alias="T03", raw_payload="endpoint", projected_payload="endpoint"),
        ),
    )
    assessment = EvidenceAssessment(
        work_item_id=work_item.work_item_id,
        status=AssessmentStatus.NEEDS_EVIDENCE,
        claim=seed.claim,
        mechanism=seed.mechanism,
        proof_scope=seed.proof_scope,
        supporting_refs=("T01", "T03"),
    )
    result, _ = run_evidence_assessment(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        work_items=(work_item,),
        seeds={seed.seed_id: seed},
        execution=execution,
        proof_matches={work_item.work_item_id: ProofMatch(work_item_id=work_item.work_item_id, status=ProofMatchStatus.PARTIAL)},
        llm=_TriageLLM(EvidenceAssessmentBatch(assessments=(assessment,))),
        max_retries=1,
        structured_method="function_calling",
    )

    assert result[work_item.work_item_id].supporting_refs == ("T01", "T02", "T03")


def test_assessment_recovers_plain_json_after_invalid_structured_tool_call():
    """A provider transport error must not erase an otherwise valid assessment."""

    from codeguard_agent.pipeline.controlled.executor import ExecutionBatch, StepExecution
    from codeguard_agent.models.tasks import EvidenceAssessmentBatch

    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="下游调用可能观察到错误的注册时机",
        mechanism="注册调用从方法前置位置移动到运行分支之后",
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
            question="s1 是否到达 s2",
        ),
        seed_id="seed-assessment-fallback",
    )
    work_item = _plan().work_items[0].model_copy(
        update={"work_item_id": "wi-assessment-fallback", "seed_id": seed.seed_id}
    )
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
                step=work_item.evidence_steps[0],
                status="complete",
                alias="T01",
                raw_payload=graph_payload,
                projected_payload=graph_payload,
            ),
            StepExecution(
                work_item_id=work_item.work_item_id,
                step=EvidenceStep(tool="get_file_content", subject_ref="s1"),
                status="complete",
                alias="T02",
                raw_payload="class A {}",
                projected_payload="class A {}",
            ),
        ),
    )
    assessment = EvidenceAssessment(
        work_item_id=work_item.work_item_id,
        status=AssessmentStatus.PARTIAL,
        claim=seed.claim,
        mechanism=seed.mechanism,
        proof_scope=seed.proof_scope,
        supporting_refs=("T01", "T02"),
    )

    class _InvalidAssessmentLLM:
        def with_structured_output(self, _schema, method=None):  # noqa: ARG002
            return _Structured(
                SimpleNamespace(
                    content="",
                    invalid_tool_calls=[
                        {"name": "LlmEvidenceAssessmentBatch", "args": "not-json"}
                    ],
                )
            )

        def invoke(self, _messages):
            return SimpleNamespace(content=EvidenceAssessmentBatch(assessments=(assessment,)).model_dump_json())

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
        llm=_InvalidAssessmentLLM(),
        max_retries=1,
        structured_method="function_calling",
    )

    assert result[work_item.work_item_id].supporting_refs == ("T01", "T02")
    assert "assessment_text_fallback_used" in diagnostics


def test_candidate_binding_keeps_direct_triage_claim_over_terse_assessment_echo():
    """Evidence assessment must not erase the concrete candidate hypothesis."""

    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="将已有缓存对象替换为新的工厂调用，可能丢失该对象中已累计的重试状态。",
        mechanism="返回值从已有对象变为重新创建的对象",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.LOCAL,
        evidence_basis=("changed_lines",),
        seed_id="seed-claim-owner",
    )
    assessment = EvidenceAssessment(
        work_item_id="wi-claim-owner",
        status=AssessmentStatus.CANDIDATE,
        claim="返回值变化",
        mechanism="对象变化",
        proof_scope=ProofScope.LOCAL,
    )
    candidate = candidate_from_seed(
        seed=seed,
        task=ReviewTask(
            id="A.java#h0",
            file="A.java",
            patch="@@ -1 +1 @@\n-old\n+new",
            changed_lines=[2],
        ),
        catalog=EvidenceCatalog(task_id="A.java#h0", reviewer="controlled", revision="r1"),
        reviewer=ReviewerKind.BEHAVIOR.value,
        candidate_index=1,
        assessment=assessment,
    )
    assert candidate.claim == seed.claim


def test_candidate_binding_names_observable_state_for_return_change():
    """Source-backed context prevents a return-only candidate from staying abstract."""

    from codeguard_agent.models.evidence import (
        ArtifactAvailability,
        EvidenceArtifact,
        EvidenceCaptureMode,
        EvidenceSourceKind,
    )

    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="return context 改为调用内部 open 方法，改变返回值来源。",
        mechanism="返回责任从局部 context 转移到内部调用",
        impact="调用方可能获得不同的返回对象。",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        seed_id="seed-return-observable",
    )
    source = EvidenceArtifact.build(
        task_id="A.java#h0",
        reviewer="controlled",
        revision="r1",
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool="get_file_content",
        arguments={"symbol_id": "s1"},
        payload="cache.containsKey(key); context.removeAttribute(EXHAUSTED);",
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )
    catalog = EvidenceCatalog(
        task_id="A.java#h0",
        reviewer="controlled",
        revision="r1",
        artifacts={source.id: source},
        alias_to_artifact_id={"T01": source.id},
    )
    assessment = EvidenceAssessment(
        work_item_id="wi-return-observable",
        status=AssessmentStatus.PARTIAL,
        claim=seed.claim,
        mechanism=seed.mechanism,
        impact=seed.impact,
        proof_scope=seed.proof_scope,
        supporting_refs=("T01",),
    )

    candidate = candidate_from_seed(
        seed=seed,
        task=ReviewTask(
            id="A.java#h0",
            file="A.java",
            patch="@@ -1 +1 @@\n-old\n+new",
            changed_lines=[2],
        ),
        catalog=catalog,
        reviewer=ReviewerKind.BEHAVIOR.value,
        candidate_index=1,
        assessment=assessment,
    )

    assert "缓存读写/命中" in candidate.impact
    assert "状态属性访问" in candidate.impact
    assert candidate.claim == seed.claim


def test_candidate_binding_names_verified_observer_before_state_registration():
    """Timing output is grounded only when graph and source artifacts agree."""

    from codeguard_agent.models.evidence import (
        ArtifactAvailability,
        EvidenceArtifact,
        EvidenceCaptureMode,
        EvidenceSourceKind,
    )

    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="context register 调用从检查之前移动到之后，改变状态可见时序。",
        mechanism="注册操作位于同一方法的分支检查之后",
        impact="依赖同步上下文的调用方可能观察到不同状态。",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.IMPACT,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="验证状态注册前的执行顺序",
        ),
        seed_id="seed-timing-observable",
    )
    graph = EvidenceArtifact.build(
        task_id="A.java#h0",
        reviewer="controlled",
        revision="r1",
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool="inspect_path",
        arguments={"symbol_id": "s1", "path_kind": "behavior", "max_depth": "3"},
        payload=(
            '{"schema_version":2,"outcome":"found","coverage":"complete",'
            '"source_scope":"MAIN","subject_symbol_id":"s1",'
            '"symbols":[{"id":"s1","kind":"METHOD","source_set":"MAIN"},'
            '{"id":"java:p.RetryListener#open()","kind":"METHOD","source_set":"MAIN"}],'
            '"relationships":[{"sourceId":"s1","targetId":"java:p.RetryListener#open()",'
            '"kind":"CALLS"}],"unresolved_relationships":[],"unresolved_count":0,'
            '"limitations":[]}'
        ),
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )
    source = EvidenceArtifact.build(
        task_id="A.java#h0",
        reviewer="controlled",
        revision="r1",
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool="get_file_content",
        arguments={"symbol_id": "s1"},
        payload="doOpenInterceptors(callback); RetrySynchronizationManager.register(context);",
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )
    catalog = EvidenceCatalog(
        task_id="A.java#h0",
        reviewer="controlled",
        revision="r1",
        artifacts={graph.id: graph, source.id: source},
        alias_to_artifact_id={"T01": graph.id, "T02": source.id},
    )
    assessment = EvidenceAssessment(
        work_item_id="wi-timing-observable",
        status=AssessmentStatus.PARTIAL,
        claim=seed.claim,
        mechanism=seed.mechanism,
        impact=seed.impact,
        proof_scope=seed.proof_scope,
        supporting_refs=("T01", "T02"),
    )

    candidate = candidate_from_seed(
        seed=seed,
        task=ReviewTask(
            id="A.java#h0",
            file="A.java",
            patch="@@ -1 +1 @@\n-old\n+new",
            changed_lines=[2],
        ),
        catalog=catalog,
        reviewer=ReviewerKind.BEHAVIOR.value,
        candidate_index=1,
        assessment=assessment,
    )

    assert "RetryListener.open" in candidate.evidence_observation
    assert "注册之前" in candidate.evidence_observation
    issue = candidate.to_issue(Severity.WARNING)
    assert "RetryListener" in issue.message
    assert "新同步上下文注册之前" in issue.message


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


def test_graph_plan_restores_baseline_for_partially_rejected_seed():
    seed_one = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="第一个候选",
        mechanism="需要调用路径",
        location_file="A.java",
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="验证第一个候选",
        ),
        seed_id="seed-one",
    )
    seed_two = seed_one.model_copy(update={"seed_id": "seed-two", "claim": "第二个候选"})
    cyclic = _plan().work_items[0].model_copy(
        update={"seed_id": "seed-one", "evidence_steps": (
            _plan().work_items[0].evidence_steps[0].model_copy(
                update={"step_id": "a", "depends_on": ("a",)}
            ),
        )}
    )
    valid = _plan().work_items[0].model_copy(update={"seed_id": "seed-two"})
    model_plan = ReviewerGraphPlan(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        work_items=(cyclic, valid),
    )
    plan, diagnostics = run_graph_plan(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        seeds=(seed_one, seed_two),
        symbol_context=_context(),
        llm=_TriageLLM(model_plan),
        max_retries=1,
        structured_method="function_calling",
    )
    assert {item.seed_id for item in plan.work_items} == {"seed-one", "seed-two"}
    assert any("baseline_for_missing_seed:seed-one" in item for item in diagnostics)


def test_baseline_graph_plan_is_total_for_unknown_subject():
    """Malformed provider symbols are isolated instead of crashing fallback."""

    seed = CandidateSeed(
        seed_id="seed-invalid-subject",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号候选",
        mechanism="需要图谱验证",
        location_file="A.java",
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="验证下游路径",
        ),
    )

    plan, diagnostics = _baseline_graph_plan(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        seeds=(seed,),
        symbol_context=_context(),
        max_path_depth=3,
        enabled_tools={"inspect_path", "get_file_content"},
    )

    assert plan.work_items == ()
    assert any("baseline_unknown_subject" in item for item in diagnostics)


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


def test_claim_consequence_repair_requires_observer_for_ordering_claims():
    graph_seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="register 调用移动后全局状态不一致",
        mechanism="调用顺序发生变化",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="是否存在下游影响",
        ),
    )
    explicit_observer = graph_seed.model_copy(
        update={"claim": "register 移动后下游 listener 读取到不一致状态"}
    )
    assert _needs_claim_consequence_repair((graph_seed,))
    assert not _needs_claim_consequence_repair((explicit_observer,))


def test_claim_scope_repair_detects_cross_hunk_candidate_without_deciding_bug():
    task = ReviewTask(
        id="A.java#file",
        file="A.java",
        patch=(
            "diff --git a/A.java b/A.java\n"
            "--- a/A.java\n+++ b/A.java\n"
            "@@ -1,1 +1,1 @@\n"
            "-oldRegister();\n+register(context);\n"
            "@@ -20,1 +20,1 @@\n"
            "-return context;\n+return doOpenInternal(state);\n"
        ),
        changed_lines=[2, 21],
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#file",
        claim="register 移动后与 doOpenInternal 返回 context 的时序可能不一致",
        mechanism="register 与 doOpenInternal 两处改动共同改变状态生命周期",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            path_kind="behavior",
            required_relationships=("CALLS",),
        ),
    )
    assert _needs_claim_scope_repair((seed,), task=task)

    local_seed = seed.model_copy(
        update={
            "claim": "register 移动后下游观察到不同状态",
            "mechanism": "register 的调用顺序改变",
        }
    )
    assert not _needs_claim_scope_repair((local_seed,), task=task)


def test_self_call_wording_repair_requires_a_verified_self_target():
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#file",
        claim="方法 A 调用方法 B 后发生递归",
        mechanism="return 表达式改为调用 B",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="java:pkg.Type#A()",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("java:pkg.Type#B()",),
            required_relationships=("CALLS",),
            question="检查下游路径",
        ),
    )
    assert _needs_claim_self_call_repair((seed,))

    self_call = seed.model_copy(
        update={
            "graph_question": seed.graph_question.model_copy(
                update={"expected_targets": ("java:pkg.Type#A()",)}
            )
        }
    )
    assert not _needs_claim_self_call_repair((self_call,))


def test_claim_grounding_repair_flags_unseen_control_flow_construct():
    from codeguard_agent.pipeline.controlled.triage import _needs_claim_grounding_repair

    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+return delegate(state);",
        changed_lines=[2],
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="finally 块清理后返回值被替换",
        mechanism="return 调用 delegate",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查路径",
        ),
    )
    assert _needs_claim_grounding_repair((seed,), task=task, symbol_context=None)


def test_partial_graph_proof_can_enrich_even_when_assessment_says_proved():
    """Provider overconfidence must not suppress a bounded endpoint read."""

    task = ReviewTask(id="A.java#h0", file="A.java", patch="+call();", changed_lines=[2])
    seed = CandidateSeed(
        seed_id="seed-partial",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号状态变化",
        mechanism="调用顺序改变",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("s2",),
            required_relationships=("CALLS",),
        ),
    )
    assessment = EvidenceAssessment(
        work_item_id="wi-partial",
        status=AssessmentStatus.PROVED,
        claim=seed.claim,
        proof_scope=seed.proof_scope,
    )
    graph_step = EvidenceStep(
        tool="inspect_path",
        subject_ref="s1",
        path_kind="behavior",
        max_depth=3,
    )
    source_step = EvidenceStep(tool="get_file_content", subject_ref="s1")
    execution = SimpleNamespace(
        steps=(
            SimpleNamespace(
                step=graph_step,
                projected_payload=(
                    '{"symbols":[{"id":"s1","file":"A.java"},'
                    '{"id":"s2","file":"B.java"}],'
                    '"relationships":[{"sourceId":"s1","targetId":"s2"}]}'
                ),
            ),
            SimpleNamespace(step=source_step, projected_payload="source"),
        )
    )
    delta = _auto_context_delta_step(
        seed=seed,
        assessment=assessment,
        execution=execution,
        task=task,
        allow_partial_proof_enrichment=True,
    )
    assert delta is not None
    assert delta.tool == "get_file_content"
    assert delta.subject_ref == "s2"


def test_delta_prefers_candidate_named_endpoint_over_unrelated_graph_symbols():
    """Delta reads the endpoint named by the hypothesis before a nearby field."""

    task = ReviewTask(id="A.java#h0", file="A.java", patch="+return next();", changed_lines=[2])
    seed = CandidateSeed(
        seed_id="seed-endpoint-priority",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="return 改为调用 doOpenInternal，状态传播可能改变",
        mechanism="局部状态清理后返回表达式改为 doOpenInternal",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
        ),
    )
    assessment = EvidenceAssessment(
        work_item_id="wi-endpoint-priority",
        status=AssessmentStatus.PARTIAL,
        claim=seed.claim,
        mechanism=seed.mechanism,
        proof_scope=seed.proof_scope,
    )
    execution = SimpleNamespace(
        steps=(
            SimpleNamespace(
                step=EvidenceStep(tool="inspect_path", subject_ref="s1", path_kind="behavior", max_depth=3),
                projected_payload=(
                    '{"symbols":['
                    '{"id":"s1","kind":"METHOD","file":"A.java"},'
                    '{"id":"s2","kind":"METHOD","file":"B.java"},'
                    '{"id":"doOpenInternal","kind":"METHOD","file":"B.java"},'
                    '{"id":"RetryContextCache#containsKey","kind":"METHOD","file":"B.java"}],'
                    '"relationships":['
                    '{"sourceId":"s1","targetId":"s2","kind":"CALLS"},'
                    '{"sourceId":"s1","targetId":"doOpenInternal","kind":"CALLS"},'
                    '{"sourceId":"s1","targetId":"RetryContextCache#containsKey","kind":"CALLS"}]}'
                ),
            ),
        )
    )

    delta = _auto_context_delta_step(
        seed=seed,
        assessment=assessment,
        execution=execution,
        task=task,
    )

    assert delta is not None
    assert delta.subject_ref == "doOpenInternal"


def test_provider_envelope_treats_null_optional_fields_as_omitted():
    parsed = LlmCandidateSeed.model_validate(
        {
            "reviewer": "behavior",
            "change_unit_id": "CU-A.java#h0",
            "claim": "changed behavior",
            "location_file": "A.java",
            "proof_scope": "local",
            "mechanism_note2": None,
            "suggestion": None,
            "confidence": None,
        }
    )
    assert parsed.mechanism_note2 == ""
    assert parsed.suggestion == ""
    assert parsed.confidence == 0.5


def test_graph_needed_evidence_label_defers_to_graph_question_direction():
    """Coverage vocabulary must not guess an executable graph tool."""

    assert EvidenceNeed("graph_needed") is EvidenceNeed.NONE
    assert EvidenceNeed("graph") is EvidenceNeed.NONE
    assert EvidenceNeed("inspect_graph") is EvidenceNeed.NONE
    assert EvidenceNeed("inspire_path") is EvidenceNeed.INSPECT_PATH


def test_controlled_review_binds_provider_assessment_into_candidate():
    """A valid assessment must create a candidate instead of being dropped."""

    from codeguard_agent.models.tasks import PlanUnit, TaskRoute
    from codeguard_agent.pipeline.controlled.llm_contracts import (
        LlmEvidenceAssessmentBatch,
        LlmReviewerGraphPlan,
    )

    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+call();",
        changed_lines=[2],
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="变更后的调用链仍应到达下游方法",
        mechanism="新增调用改变了下游可达性",
        location_file="A.java",
        location_line=2,
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
        seed_id="seed-provider-assessment",
    )
    triage_empty = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision=CoverageDecision.LOCAL_ONLY,
                reason="无候选",
            ),
        ),
        issues=(),
    )
    triage_behavior = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision=CoverageDecision.GRAPH_NEEDED,
                reason="需要确认调用事实",
            ),
        ),
        issues=(seed,),
    )
    plan = _plan().model_copy(update={"work_items": (
        _plan().work_items[0].model_copy(
            update={"seed_id": seed.seed_id, "work_item_id": ""}
        ),
    )})
    assessment = EvidenceAssessment(
        work_item_id="wi-behavior-A.java#h0-1",
        status=AssessmentStatus.CANDIDATE,
        claim=seed.claim,
        mechanism=seed.mechanism,
        proof_scope=seed.proof_scope,
        supporting_refs=("T01",),
    )

    class _NodeLLM:
        def with_structured_output(self, schema, method=None):  # noqa: ARG002
            class _DynamicStructured:
                def invoke(_, messages):  # noqa: ANN001
                    name = schema.__name__
                    prompt = " ".join(str(item) for item in messages)
                    if name == "LlmDirectTriageResult":
                        result = (
                            triage_empty
                            if "ThreatModelAgent" in prompt
                            or "MaintainabilityAgent" in prompt
                            else triage_behavior
                        )
                    elif name == "LlmReviewerGraphPlan":
                        result = LlmReviewerGraphPlan.model_validate(plan.model_dump())
                    elif name == "LlmEvidenceAssessmentBatch":
                        result = LlmEvidenceAssessmentBatch(assessments=(assessment,))
                    else:  # pragma: no cover - protects this test from hidden calls
                        raise AssertionError(f"unexpected schema: {name}")
                    return result

            return _DynamicStructured()

    output = _controlled_review_node(_NodeLLM(), tool_client=_GraphClient())({
        "review_tasks": [task],
        "task_selection": TaskSelection(selected_task_ids=[task.id]),
        "task_routes": {task.id: TaskRoute(task_id=task.id, route="full", reason="test")},
        "plan_units": [PlanUnit(id="A.java", file="A.java", task_ids=(task.id,))],
        "knowledge_route_plan": {},
        "task_symbol_contexts": {task.id: _context()},
        "evidence_revision": "r1",
        "max_retries": 1,
        "structured_method": "function_calling",
        "enabled_tools": {
            "get_file_content",
            "inspect_structure",
            "inspect_change_impact",
            "inspect_path",
        },
    })

    assert len(output["raw_candidate_issues"]) == 1
    assert output["raw_candidate_issues"][0].source_agent == "behavior"
    assert any(
        item.event == "completed" and "candidates=1" in item.detail
        for item in output["council_trace"]
    )


def test_candidate_context_is_rehydrated_after_state_serialization():
    """Excluded CandidateIssue fields remain available to the final Judge."""

    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+return value;",
        changed_lines=[2],
    )
    candidate = CandidateIssue(
        id="seed-context",
        task_id=task.id,
        source_agent="behavior",
        file=task.file,
        line=2,
        type="return-contract",
        claim="返回结果的状态传播可能改变",
        mechanism="局部状态更新后返回表达式改变",
        impact="调用方可观察的状态属性可能改变",
        impact_locale="返回值",
        claim_type="behavior",
    )
    # This mirrors the shell that can arrive after a LangGraph checkpoint:
    # Field(exclude=True) explanatory values are absent from model_dump.
    serialized_shell = CandidateIssue.model_validate(candidate.model_dump())
    assembly = _assemble_state_dossiers(
        {
            "candidate_issues": [serialized_shell],
            "review_tasks": [task],
            "task_symbol_contexts": {},
            "controlled_candidate_contexts": {
                candidate.id: {
                    "mechanism": candidate.mechanism,
                    "impact": candidate.impact,
                    "impact_locale": candidate.impact_locale,
                    "claim_type": candidate.claim_type,
                }
            },
        }
    )

    assert len(assembly.dossiers) == 1
    assert assembly.dossiers[0].candidate.mechanism == candidate.mechanism
    assert assembly.dossiers[0].candidate.impact == candidate.impact
    assert assembly.dossiers[0].candidate.impact_locale == candidate.impact_locale
    assert assembly.dossiers[0].candidate.claim_type == candidate.claim_type


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


def test_direct_triage_normalizes_unresolved_graph_aliases_to_canonical_kind():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+call();",
        changed_lines=[2],
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号行为变化",
        mechanism="调用顺序改变",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("UnknownListener",),
            required_relationships=("listener_before_register",),
            question="检查下游调用",
        ),
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="graph_needed",
                reason="需要图谱",
            ),
        ),
        issues=(seed,),
    )
    triage, _ = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=_context(),
        llm=_TriageLLM(result),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )
    assert triage is not None
    question = triage.issues[0].graph_question
    assert question is not None
    # Preserve a precise provider alias for proof-time resolution against the
    # projected graph; it is not executable until a visible endpoint matches.
    assert question.expected_targets == ("UnknownListener",)
    assert question.required_relationships == ("CALLS",)


def test_direct_triage_drops_prose_from_graph_targets_but_keeps_symbol_aliases():
    """GraphQuestion targets are identifiers, not a second natural-language claim."""

    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+call();",
        changed_lines=[2],
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号行为变化",
        mechanism="调用顺序改变",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            expected_targets=(
                "RetrySynchronizationManager",
                "当前方法终止早退分支抛异常路径已无下游",
            ),
            required_relationships=("CALLS",),
            question="检查下游调用",
        ),
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="graph_needed",
                reason="需要图谱",
            ),
        ),
        issues=(seed,),
    )

    triage, _ = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=_context(),
        llm=_TriageLLM(result),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )

    assert triage is not None
    question = triage.issues[0].graph_question
    assert question is not None
    assert question.expected_targets == ("RetrySynchronizationManager",)


def test_direct_triage_promotes_local_seed_with_explicit_graph_question():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+callChangedSymbol();",
        changed_lines=[2],
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号行为变化",
        mechanism="调用方观察到的状态可能改变",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.LOCAL,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查调用路径",
        ),
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="graph_needed",
                reason="需要路径证据",
            ),
        ),
        issues=(seed,),
    )
    triage, diagnostics = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=_context(),
        llm=_TriageLLM(result),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )
    assert triage is not None
    assert triage.issues[0].proof_scope is ProofScope.CROSS_FILE
    assert triage.issues[0].graph_question is not None
    assert any("local_scope_promoted_for_graph" in item for item in diagnostics)


def test_direct_triage_promotes_local_coverage_when_seed_requests_graph():
    task = ReviewTask(id="A.java#h0", file="A.java", patch="+call();", changed_lines=[2])
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号行为变化",
        mechanism="调用顺序改变",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_basis=("changed_lines",),
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查下游调用",
        ),
    )
    result = DirectTriageResult(
        coverage=(CoverageDeclaration(change_unit_id="CU-A.java#h0", decision="local_only", reason="局部足够"),),
        issues=(seed,),
    )

    triage, diagnostics = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=_context(),
        llm=_TriageLLM(result),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )

    assert triage is not None
    assert len(triage.issues) == 1
    assert triage.coverage[0].decision is CoverageDecision.GRAPH_NEEDED
    assert any("coverage_promoted_for_graph_seed" in item for item in diagnostics)


def test_evidence_pack_distinguishes_omitted_secondary_step_from_empty_pack():
    from codeguard_agent.pipeline.controlled.executor import StepExecution

    work_item = _plan().work_items[0].model_copy(update={"work_item_id": "wi-pack"})
    graph = StepExecution(
        work_item_id="wi-pack",
        step=work_item.evidence_steps[0],
        status="complete",
        alias="T01",
        projected_payload='{"relationships": [{"sourceId": "s1", "targetId": "s2"}]}',
    )
    source = StepExecution(
        work_item_id="wi-pack",
        step=EvidenceStep(
            tool="get_file_content",
            subject_ref="s2",
            purpose="源码",
            expected_fact="读取源码",
        ),
        status="complete",
        alias="T02",
        projected_payload="x" * 500,
    )
    pack = build_evidence_pack(
        work_item=work_item,
        steps=(graph, source),
        max_chars=220,
    )
    assert "T01" in pack
    assert "evidence_step_omitted" in pack
    assert "evidence_pack_truncated_at_step_boundary" not in pack


def test_controlled_candidate_reducer_collapses_same_lifecycle_seam_across_reviewers():
    common = {
        "task_id": "A.java#h0",
        "file": "A.java",
        "line": 4,
        "type": "controlled_review_finding",
        "confidence": 0.5,
    }
    behavior = CandidateIssue(
        id="behavior-1",
        source_agent="behavior",
        claim="register context 在 doOpenInterceptors listener 之前可见性改变",
        **common,
    )
    maintainability = CandidateIssue(
        id="maint-1",
        source_agent="maintainability",
        claim="register context 从 doOpenInterceptable 主干移到 running 检查之后",
        **common,
    )
    reduced, collapsed = collapse_candidate_duplicates([maintainability, behavior])
    assert collapsed == 1
    assert [item.id for item in reduced] == ["behavior-1"]


def test_controlled_candidate_reducer_collapses_cross_type_same_changed_expression():
    common = {
        "task_id": "A.java#h0",
        "file": "A.java",
        "line": 4,
        "confidence": 0.5,
    }
    first = CandidateIssue(
        id="threat-1",
        source_agent="threat_model",
        type="state_cleanup_effective_change",
        claim="return context 改为 return doOpenInternal，removeAttribute 后的状态可能被新对象丢弃",
        **common,
    )
    second = CandidateIssue(
        id="behavior-1",
        source_agent="behavior",
        type="controlled_review_finding",
        claim="return context 改为 return doOpenInternal，可能丢失已有状态",
        **common,
    )
    reduced, collapsed = collapse_candidate_duplicates([first, second])
    assert collapsed == 1
    assert [item.id for item in reduced] == ["behavior-1"]


def test_direct_triage_normalizes_duplicate_coverage_instead_of_dropping_seeds():
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
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="local_only",
                reason="重复声明",
            ),
        ),
        issues=(),
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
    assert len(triage.coverage) == 1
    assert "coverage_normalized" in diagnostics


def test_direct_triage_resolves_file_line_graph_subject_to_stable_symbol():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+register(context);",
        changed_lines=[2],
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="register context 需要检查 listener 时序",
        mechanism="调用顺序可能改变",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="A.java:2",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查下游路径",
        ),
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="graph_needed",
                reason="需要图谱",
            ),
        ),
        issues=(seed,),
    )
    triage, _ = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=_context(),
        llm=_TriageLLM(result),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )
    assert triage is not None
    assert triage.issues[0].graph_question is not None
    assert triage.issues[0].graph_question.subject_ref == "s1"


def test_direct_triage_resolves_unique_signatureless_symbol_alias():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+callChangedSymbol();",
        changed_lines=[3],
    )
    full_id = "java:pkg.Type#run(java.lang.String)"
    context = TaskSymbolContext(
        task_id=task.id,
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="A.java",
                symbol_id=full_id,
                kind="METHOD",
                start_line=1,
                end_line=8,
                source_set="MAIN",
            ),
        ),
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号调用行为改变",
        mechanism="调用顺序可能改变",
        location_file="A.java",
        location_line=3,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="java:pkg.Type#run",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查下游调用",
        ),
    )

    normalized = _normalize_graph_question(seed, context, task=task)

    assert normalized.graph_question is not None
    assert normalized.graph_question.subject_ref == full_id


def test_direct_triage_resolves_bare_method_symbol_alias():
    task = ReviewTask(id="A.java#h0", file="A.java", patch="+callChangedSymbol();", changed_lines=[3])
    context = TaskSymbolContext(
        task_id=task.id,
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="A.java",
                symbol_id="java:pkg.Type#run(java.lang.String)",
                kind="METHOD",
                start_line=1,
                end_line=8,
                source_set="MAIN",
            ),
        ),
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号调用行为改变",
        mechanism="调用顺序可能改变",
        location_file="A.java",
        location_line=3,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="run",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查下游调用",
        ),
    )

    normalized = _normalize_graph_question(seed, context, task=task)

    assert normalized.graph_question is not None
    assert normalized.graph_question.subject_ref == "java:pkg.Type#run(java.lang.String)"


def test_graph_question_direction_follows_explicit_tool_need():
    """A provider's contradictory direction cannot disable its chosen tool."""

    task = ReviewTask(id="A.java#h0", file="A.java", patch="+callChangedSymbol();", changed_lines=[3])
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号调用行为改变",
        mechanism="调用顺序可能改变",
        location_file="A.java",
        location_line=3,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="upstream",
            path_kind=None,
            required_relationships=("CALLS",),
            question="检查下游调用",
        ),
    )

    normalized = _normalize_graph_question(seed, _context(), task=task)

    assert normalized.graph_question is not None
    assert normalized.graph_question.direction == "downstream"
    assert normalized.graph_question.path_kind == "behavior"


def test_graph_question_text_repairs_contradictory_impact_route():
    """An explicit downstream question must not be executed as an upstream query."""

    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+register(context);",
        changed_lines=[3],
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="注册时序变化可能影响下游监听器",
        mechanism="注册移动到监听器调用之后",
        location_file="A.java",
        location_line=3,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_CHANGE_IMPACT,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="upstream",
            path_kind=None,
            required_relationships=("CALLS",),
            question="沿下游调用路径确认监听器是否观察到该上下文",
        ),
    )

    normalized = _normalize_graph_question(seed, _context(), task=task)

    assert normalized.graph_question is not None
    assert normalized.graph_question.direction == "downstream"
    assert normalized.graph_question.path_kind == "behavior"
    assert normalized.evidence_need is EvidenceNeed.INSPECT_PATH


def test_explicit_graph_path_overrides_contradictory_structure_label():
    """A multi-hop question must not be shortened to inspect_structure."""

    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+register(context);",
        changed_lines=[3],
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="注册时序变化可能影响下游监听器",
        mechanism="注册移动到监听器调用之前或之后",
        location_file="A.java",
        location_line=3,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_STRUCTURE,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="确认下游 callback 是否可达",
        ),
    )

    normalized = _normalize_graph_question(seed, _context(), task=task)

    assert normalized.evidence_need is EvidenceNeed.INSPECT_PATH


def test_graph_question_callee_alias_falls_back_to_unique_changed_enclosing_symbol():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+register(context);",
        changed_lines=[3],
    )
    context = TaskSymbolContext(
        task_id=task.id,
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="A.java",
                symbol_id="java:pkg.Type#run(java.lang.String)",
                kind="METHOD",
                start_line=1,
                end_line=8,
                source_set="MAIN",
            ),
        ),
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="注册时序发生变化",
        mechanism="调用点的状态可见性改变",
        location_file="A.java",
        location_line=3,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="RetrySynchronizationManager.register",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查下游路径",
        ),
    )

    normalized = _normalize_graph_question(seed, context, task=task)

    assert normalized.graph_question is not None
    assert normalized.graph_question.subject_ref == "java:pkg.Type#run(java.lang.String)"


def test_graph_question_subject_uses_candidate_line_in_multi_symbol_task():
    task = ReviewTask(
        id="A.java#file",
        file="A.java",
        patch="+change();",
        changed_lines=[3, 20],
    )
    context = TaskSymbolContext(
        task_id=task.id,
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(file="A.java", symbol_id="java:pkg.Type#first()", kind="METHOD", start_line=1, end_line=8, source_set="MAIN"),
            ResolvedSymbol(file="A.java", symbol_id="java:pkg.Type#second()", kind="METHOD", start_line=15, end_line=25, source_set="MAIN"),
        ),
    )
    seed = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#file",
        claim="跨符号调用行为改变",
        mechanism="调用顺序可能改变",
        location_file="A.java",
        location_line=20,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="RetrySynchronizationManager.register",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查下游调用",
        ),
    )

    normalized = _normalize_graph_question(seed, context, task=task)

    assert normalized.graph_question is not None
    assert normalized.graph_question.subject_ref == "java:pkg.Type#second()"


def test_direct_triage_recovers_candidates_embedded_in_coverage_metadata():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+callChangedSymbol();",
        changed_lines=[2],
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="graph_needed",
                reason="需要路径证据",
                issues=(
                    {
                        "reviewer": "behavior",
                        "claim": "跨符号行为变化",
                        "mechanism": "调用方观察到的状态可能改变",
                        "location_file": "A.java",
                        "location_line": 2,
                        "proof_scope": "cross_file",
                        "evidence_need": "inspect_path",
                        "graph_question": {
                            "subject_ref": "s1",
                            "direction": "downstream",
                            "path_kind": "behavior",
                            "required_relationships": ["CALLS"],
                            "question": "检查调用路径",
                        },
                    },
                ),
            ),
        ),
    )
    triage, diagnostics = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=_context(),
        llm=_TriageLLM(result),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )
    assert triage is not None
    assert len(triage.issues) == 1
    assert triage.issues[0].claim == "跨符号行为变化"
    assert any("coverage_embedded_candidates_recovered:1" in item for item in diagnostics)


def test_direct_triage_rechecks_empty_graph_needed_result_once():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+callChangedSymbol();",
        changed_lines=[2],
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="graph_needed",
                reason="需要路径证据",
            ),
        ),
        issues=(),
    )
    triage, diagnostics = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=_context(),
        llm=_TriageLLM(result),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )
    assert triage is not None
    assert triage.issues == ()
    assert "empty_graph_recheck_confirmed_no_candidates" in diagnostics


def test_direct_triage_rechecks_empty_response_with_missing_coverage():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="+changedCall();",
        changed_lines=[2],
    )
    triage, diagnostics = run_direct_triage(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        symbol_context=_context(),
        llm=_TriageLLM(DirectTriageResult()),
        diff_summary="",
        task_knowledge="",
        max_retries=1,
        structured_method="function_calling",
    )
    assert triage is not None
    assert triage.issues == ()
    assert any(item == "coverage_normalized" for item in diagnostics)
    assert any(item == "empty_graph_recheck_confirmed_no_candidates" for item in diagnostics)


def test_direct_triage_repair_keeps_candidates_omitted_by_provider():
    first = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="第一处变更改变了状态注册时序",
        mechanism="注册操作被移动到条件判断之后",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.LOCAL,
    )
    second = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="第二处变更重新创建了返回状态",
        mechanism="返回值改为再次调用工厂方法",
        location_file="A.java",
        location_line=20,
        proof_scope=ProofScope.LOCAL,
    )
    repaired_first = first.model_copy(
        update={"claim": "第一处变更把状态注册移到条件判断之后"}
    )
    merged = _merge_protocol_repair_issues((first, second), (repaired_first,))
    assert len(merged) == 2
    assert merged[0].claim == "第一处变更把状态注册移到条件判断之后"
    assert merged[1].claim == second.claim


def test_direct_triage_ignores_empty_provider_issue_shell():
    valid = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="具体的变更机制",
        mechanism="新增分支改变状态",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.LOCAL,
    )
    raw = LlmDirectTriageResult.model_validate(
        {"issues": [{}, valid.model_dump()]}
    )
    result, diagnostic = _invoke_once(
        llm=_TriageLLM(raw),
        system_prompt="system",
        user_prompt="user",
        max_retries=1,
        structured_method="function_calling",
    )
    assert diagnostic == ""
    assert result is not None
    assert len(result.issues) == 1
    assert result.issues[0].claim == valid.claim


def test_direct_triage_uses_bounded_json_fallback_when_structured_output_is_missing():
    valid = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="具体的变更机制",
        mechanism="新增分支改变状态",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.LOCAL,
    )

    class _MissingStructuredTextLlm:
        def with_structured_output(self, _schema, method=None):  # noqa: ARG002
            return _Structured(None)

        def invoke(self, _messages):
            return SimpleNamespace(
                content=DirectTriageResult(
                    coverage=(
                        CoverageDeclaration(
                            change_unit_id="CU-A.java#h0",
                            decision=CoverageDecision.LOCAL_ONLY,
                            reason="局部事实足够",
                        ),
                    ),
                    issues=(valid,),
                ).model_dump_json()
            )

    result, diagnostic = _invoke_once(
        llm=_MissingStructuredTextLlm(),
        system_prompt="system",
        user_prompt="user",
        max_retries=1,
        structured_method="function_calling",
    )
    assert result is not None
    assert result.issues[0].claim == valid.claim
    assert diagnostic == "triage_text_fallback_used"


def test_direct_triage_uses_json_fallback_after_structured_transport_error(monkeypatch):
    """A transient structured-call failure must not erase a reviewer."""

    monkeypatch.setattr("codeguard_agent.llm.client.time.sleep", lambda _seconds: None)

    valid = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="结构化请求失败后仍保留的候选",
        mechanism="状态分支改变",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.LOCAL,
    )

    class _FailStructuredLlm:
        def with_structured_output(self, _schema, method=None):  # noqa: ARG002
            class _FailingStructured:
                def invoke(self, _messages):
                    raise RuntimeError("temporary provider failure")

            return _FailingStructured()

        def invoke(self, _messages):
            return SimpleNamespace(
                content=DirectTriageResult(
                    coverage=(
                        CoverageDeclaration(
                            change_unit_id="CU-A.java#h0",
                            decision=CoverageDecision.LOCAL_ONLY,
                            reason="局部事实足够",
                        ),
                    ),
                    issues=(valid,),
                ).model_dump_json()
            )

    result, diagnostic = _invoke_once(
        llm=_FailStructuredLlm(),
        system_prompt="system",
        user_prompt="user",
        max_retries=1,
        structured_method="function_calling",
    )
    assert result is not None
    assert result.issues[0].claim == valid.claim
    assert diagnostic == "triage_text_fallback_after_error"


def test_direct_triage_binds_blank_task_envelope_fields():
    """Provider blanks for task-owned fields must not discard a valid claim."""

    raw = LlmDirectTriageResult.model_validate(
        {
            "issues": [
                {
                    "reviewer": None,
                    "change_unit_id": "",
                    "claim": "具体的变更机制",
                    "mechanism": "新增分支改变状态",
                    "location_file": "",
                    "location_line": 2,
                    "proof_scope": "local",
                }
            ]
        }
    )
    result, diagnostic = _invoke_once(
        llm=_TriageLLM(raw),
        system_prompt="system",
        user_prompt="user",
        max_retries=1,
        structured_method="function_calling",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        location_file="A.java",
    )

    assert diagnostic == ""
    assert result is not None
    assert result.issues[0].reviewer is ReviewerKind.BEHAVIOR
    assert result.issues[0].change_unit_id == "CU-A.java#h0"
    assert result.issues[0].location_file == "A.java"


def test_direct_triage_treats_null_optional_provider_fields_as_omitted():
    """Null display fields from compatible providers must not drop the row."""

    raw = {
        "issues": [
            {
                "reviewer": "behavior",
                "change_unit_id": "CU-A.java#h0",
                "claim": "局部候选",
                "mechanism": "条件变化",
                "mechanism_note": None,
                "confidence_note": None,
                "location_file": "A.java",
                "location_line": 2,
                "proof_scope": "local",
                "confidence": None,
            }
        ]
    }
    result, diagnostic = _invoke_once(
        llm=_TriageLLM(raw),
        system_prompt="system",
        user_prompt="user",
        max_retries=1,
        structured_method="function_calling",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        location_file="A.java",
    )

    assert result is not None
    assert result.issues[0].claim == "局部候选"
    assert result.issues[0].confidence == 0.5
    assert diagnostic in {"", "triage_provider_rows_salvaged"}


def test_direct_triage_accepts_common_graph_tool_alias_from_provider():
    """Tool-name vocabulary drift must not discard a valid graph candidate."""

    raw = {
        "issues": [
            {
                "reviewer": "behavior",
                "change_unit_id": "CU-A.java#h0",
                "claim": "跨符号候选",
                "mechanism": "调用顺序改变",
                "location_file": "A.java",
                "location_line": 2,
                "proof_scope": "cross_file",
                "evidence_need": "inspect_call_graph",
                "graph_question": {
                    "subject_ref": "s1",
                    "direction": "downstream",
                    "path_kind": "behavior",
                    "expected_targets": ["s2"],
                    "required_relationships": ["CALLS"],
                    "question": "检查下游路径",
                },
            }
        ]
    }
    result, diagnostic = _invoke_once(
        llm=_TriageLLM(raw),
        system_prompt="system",
        user_prompt="user",
        max_retries=1,
        structured_method="function_calling",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        location_file="A.java",
    )

    assert result is not None
    assert result.issues[0].evidence_need is EvidenceNeed.INSPECT_PATH
    assert diagnostic == ""


def test_direct_triage_salvages_valid_rows_from_malformed_text_envelope():
    class _MalformedTextLlm:
        def with_structured_output(self, _schema, method=None):  # noqa: ARG002
            return _Structured(None)

        def invoke(self, _messages):
            return SimpleNamespace(
                content=(
                    '{"issues":['
                    '{"claim":"保留的候选","mechanism":"状态改变",'
                    '"proof_scope":"local","location_line":2},'
                    '{"claim":"无法解析的候选","proof_scope":"invalid"}'
                    ']}'
                )
            )

    result, diagnostic = _invoke_once(
        llm=_MalformedTextLlm(),
        system_prompt="system",
        user_prompt="user",
        max_retries=1,
        structured_method="function_calling",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        location_file="A.java",
    )

    assert result is not None
    assert [item.claim for item in result.issues] == ["保留的候选"]
    assert diagnostic == "triage_text_fallback_used"


def test_direct_triage_salvages_structured_rows_with_missing_proof_scope():
    raw = LlmDirectTriageResult.model_validate(
        {
            "issues": [
                {
                    "reviewer": "behavior",
                    "change_unit_id": "",
                    "claim": "局部候选",
                    "mechanism": "条件变化",
                    "location_file": "",
                    "location_line": 2,
                    "proof_scope": None,
                }
            ]
        }
    )
    result, diagnostic = _invoke_once(
        llm=_TriageLLM(raw),
        system_prompt="system",
        user_prompt="user",
        max_retries=1,
        structured_method="function_calling",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        location_file="A.java",
    )

    assert result is not None
    assert result.issues[0].proof_scope is ProofScope.LOCAL
    assert diagnostic == "triage_provider_rows_salvaged"


def test_direct_triage_normalizes_blank_provider_path_kind():
    valid = CandidateSeed(
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号候选",
        mechanism="调用顺序发生变化",
        location_file="A.java",
        location_line=2,
        proof_scope=ProofScope.CROSS_FILE,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查下游路径",
        ),
    )
    payload = valid.model_dump()
    payload["graph_question"]["path_kind"] = ""
    raw = LlmDirectTriageResult.model_validate({"issues": [payload]})
    result, diagnostic = _invoke_once(
        llm=_TriageLLM(raw),
        system_prompt="system",
        user_prompt="user",
        max_retries=1,
        structured_method="function_calling",
    )
    assert diagnostic == ""
    assert result is not None
    assert result.issues[0].graph_question is not None
    assert result.issues[0].graph_question.path_kind is None


def test_graph_plan_adds_subject_source_for_graph_question():
    seed = CandidateSeed(
        seed_id="seed-registration",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号行为变化需要验证",
        mechanism="调用路径可能改变",
        location_file="A.java",
        location_line=4,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查路径",
        ),
    )
    context = TaskSymbolContext(
        task_id="A.java#h0",
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="A.java",
                symbol_id="s1",
                kind="METHOD",
                start_line=1,
                end_line=10,
                source_set="MAIN",
            ),
            ResolvedSymbol(
                file="A.java",
                symbol_id="java:pkg.RetryTemplate#doOpenInternal()",
                kind="METHOD",
                start_line=11,
                end_line=20,
                source_set="MAIN",
            ),
            ResolvedSymbol(
                file="A.java",
                symbol_id="java:pkg.RetryListener#open()",
                kind="METHOD",
                start_line=21,
                end_line=25,
                source_set="MAIN",
            ),
        ),
    )
    plan = ReviewerGraphPlan(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        work_items=(
            WorkItem(
                seed_id=seed.seed_id,
                reviewer=ReviewerKind.BEHAVIOR,
                hypothesis=seed.claim,
                expected_mechanism=seed.mechanism,
                evidence_steps=(
                    EvidenceStep(
                        tool="inspect_path",
                        subject_ref="s1",
                        path_kind="behavior",
                        max_depth=3,
                        purpose="路径",
                        expected_fact="listener",
                    ),
                ),
            ),
        ),
    )
    normalized, diagnostics = validate_graph_plan(
        plan,
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        seeds=(seed,),
        symbol_context=context,
        max_path_depth=3,
        enabled_tools={"inspect_path", "get_file_content"},
    )
    steps = normalized.work_items[0].evidence_steps
    assert [step.tool for step in steps] == ["inspect_path", "get_file_content"]
    assert steps[1].subject_ref == "s1"
    assert any(
        "subject_source_step_added" in item
        for item in diagnostics
    )


def test_graph_plan_uses_changed_member_when_question_subject_is_type():
    """A type-level graph subject must still receive a bounded local proof."""

    seed = CandidateSeed(
        seed_id="seed-modifier",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-RetryTemplate.java#file",
        claim="共享字段的并发保护修饰被移除",
        mechanism="volatile 修饰从变更字段声明中删除",
        location_file="RetryTemplate.java",
        location_line=89,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_CHANGE_IMPACT,
        graph_question=GraphQuestion(
            subject_ref="java:pkg.RetryTemplate",
            direction="upstream",
            required_relationships=("CALLS",),
            question="确认该实例的并发使用方式",
        ),
    )
    context = TaskSymbolContext(
        task_id="RetryTemplate.java#file",
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="RetryTemplate.java",
                symbol_id="java:pkg.RetryTemplate",
                kind="TYPE",
                start_line=1,
                end_line=200,
                source_set="MAIN",
            ),
            ResolvedSymbol(
                file="RetryTemplate.java",
                symbol_id="java:pkg.RetryTemplate#backOffPolicy",
                kind="FIELD",
                start_line=89,
                end_line=89,
                source_set="MAIN",
            ),
        ),
    )
    plan = ReviewerGraphPlan(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="RetryTemplate.java#file",
        work_items=(
            WorkItem(
                seed_id=seed.seed_id,
                reviewer=ReviewerKind.BEHAVIOR,
                hypothesis=seed.claim,
                expected_mechanism=seed.mechanism,
                evidence_steps=(
                    EvidenceStep(
                        tool="inspect_change_impact",
                        subject_ref="java:pkg.RetryTemplate",
                        purpose="查找并发使用方",
                        expected_fact="调用方",
                    ),
                ),
            ),
        ),
    )

    normalized, diagnostics = validate_graph_plan(
        plan,
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="RetryTemplate.java#file",
        seeds=(seed,),
        symbol_context=context,
        max_path_depth=3,
        enabled_tools={"inspect_change_impact", "get_file_content"},
    )

    steps = normalized.work_items[0].evidence_steps
    assert [step.tool for step in steps] == [
        "inspect_change_impact",
        "get_file_content",
    ]
    assert steps[1].subject_ref == "java:pkg.RetryTemplate#backOffPolicy"
    assert any("subject_source_step_added" in item for item in diagnostics)


def test_graph_plan_replaces_an_independent_extra_graph_query_with_subject_source():
    seed = CandidateSeed(
        seed_id="seed-extra-graph",
        reviewer=ReviewerKind.BEHAVIOR,
        change_unit_id="CU-A.java#h0",
        claim="跨符号行为变化需要验证",
        mechanism="调用路径可能改变",
        location_file="A.java",
        location_line=4,
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            required_relationships=("CALLS",),
            question="检查路径",
        ),
    )
    context = TaskSymbolContext(
        task_id="A.java#h0",
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="A.java",
                symbol_id="s1",
                kind="METHOD",
                start_line=1,
                end_line=10,
                source_set="MAIN",
            ),
            ResolvedSymbol(
                file="A.java",
                symbol_id="s2",
                kind="METHOD",
                start_line=11,
                end_line=20,
                source_set="MAIN",
            ),
        ),
    )
    plan = ReviewerGraphPlan(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        work_items=(
            WorkItem(
                seed_id=seed.seed_id,
                reviewer=ReviewerKind.BEHAVIOR,
                hypothesis=seed.claim,
                expected_mechanism=seed.mechanism,
                evidence_steps=(
                    EvidenceStep(
                        tool="inspect_path",
                        subject_ref="s1",
                        path_kind="behavior",
                        max_depth=3,
                        purpose="主路径",
                        expected_fact="目标一",
                    ),
                    EvidenceStep(
                        tool="inspect_path",
                        subject_ref="s2",
                        path_kind="behavior",
                        max_depth=3,
                        purpose="额外路径",
                        expected_fact="目标二",
                    ),
                ),
            ),
        ),
    )
    normalized, diagnostics = validate_graph_plan(
        plan,
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        seeds=(seed,),
        symbol_context=context,
        max_path_depth=3,
        enabled_tools={"inspect_path", "get_file_content"},
    )
    steps = normalized.work_items[0].evidence_steps
    assert [step.tool for step in steps] == ["inspect_path", "get_file_content"]
    assert steps[0].subject_ref == "s1"
    assert steps[1].subject_ref == "s1"
    assert any("subject_source_step_added" in item for item in diagnostics)


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


def test_direct_triage_repairs_omitted_local_evidence_basis():
    """A local provider row without routing labels remains directly provable."""

    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        patch="-volatile int value;\n+int value;",
        changed_lines=[2],
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-A.java#h0",
                decision="local_only",
                reason="变更行足以说明局部机制",
            ),
        ),
        issues=(
            CandidateSeed(
                reviewer=ReviewerKind.BEHAVIOR,
                change_unit_id="CU-A.java#h0",
                claim="并发保护修饰被移除",
                mechanism="volatile 从字段声明中删除",
                location_file="A.java",
                location_line=2,
                proof_scope=ProofScope.LOCAL,
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
    assert triage.issues[0].evidence_basis == ("changed_lines",)
    assert any("local_evidence_basis_repaired" in item for item in diagnostics)


def test_deleted_file_patch_can_route_local_candidate_without_new_side_line():
    """A deleted-file fallback keeps the old-side patch visible to DirectTriage."""

    task = ReviewTask(
        id="Removed.java#file",
        file="Removed.java",
        patch=(
            "diff --git a/Removed.java b/Removed.java\n"
            "deleted file mode 100644\n"
            "--- a/Removed.java\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-void run() {\n"
            "-    dangerous();\n"
        ),
        changed_lines=[],
    )
    result = DirectTriageResult(
        coverage=(
            CoverageDeclaration(
                change_unit_id="CU-Removed.java#file",
                decision="local_only",
                reason="删除的局部代码直接可见",
            ),
        ),
        issues=(
            CandidateSeed(
                reviewer=ReviewerKind.BEHAVIOR,
                change_unit_id="CU-Removed.java#file",
                claim="删除的 run 方法不再执行原有调用",
                mechanism="run 方法及其调用被删除",
                location_file="Removed.java",
                location_line=0,
                proof_scope=ProofScope.LOCAL,
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
    assert triage.issues[0].evidence_basis == ("deletion_patch",)
    assert any("local_evidence_basis_repaired" in item for item in diagnostics)
