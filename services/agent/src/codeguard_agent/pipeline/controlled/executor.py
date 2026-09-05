"""受控 EvidenceExecutor：只执行已声明的有限工具步骤，不调用 LLM。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from codeguard_agent.models.evidence import (
    EvidenceArtifact,
    EvidenceCatalog,
    EvidenceSourceKind,
    ToolTraceRef,
)
from codeguard_agent.models.tasks import (
    CandidateSeed,
    EvidenceStep,
    ReviewerGraphPlan,
    ReviewTask,
    TaskSymbolContext,
)
from codeguard_agent.pipeline.controlled.contracts import get_tool_proof_contract
from codeguard_agent.pipeline.evidence.ledger import (
    EvidenceCatalogBuilder,
    capture_tool_records,
)
from codeguard_agent.pipeline.evidence.projection import (
    graph_projection_focus,
    project_tool_payload,
    ProjectionAudience,
)
from codeguard_agent.pipeline.execution.discovery import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
    canonical_tool_key,
)
from codeguard_agent.tools.tool_client import ToolResponse


@dataclass(frozen=True)
class StepExecution:
    work_item_id: str
    step: EvidenceStep
    status: str
    raw_payload: str = ""
    projected_payload: str = ""
    alias: str = ""
    error: str = ""


@dataclass(frozen=True)
class ExecutionBatch:
    catalog: EvidenceCatalog
    artifacts: dict[str, EvidenceArtifact]
    trace_refs: tuple[ToolTraceRef, ...]
    steps: tuple[StepExecution, ...]
    diagnostics: tuple[str, ...] = ()


class ControlledEvidenceExecutor:
    """任务级共享执行器。

    计划在进入本类前已经通过 GraphPlanValidator；这里再次执行最小参数护栏，
    确保任何意外状态合并都不会变成自由探索。每个 canonical 工具键只消耗一次
    初始预算；失败调用同样消耗预算，重复命中本地缓存不消耗预算。
    """

    def __init__(
        self,
        *,
        tool_client: Any,
        task: ReviewTask,
        symbol_context: TaskSymbolContext | None,
        revision: str,
        enabled_tools: list[str] | None = None,
        initial_budget: int = 6,
        max_path_depth: int = 3,
        extra_symbol_ids: set[str] | frozenset[str] = frozenset(),
        seed_by_id: dict[str, CandidateSeed] | None = None,
    ) -> None:
        self._tool_client = tool_client
        self._task = task
        self._symbol_context = symbol_context
        self._revision = revision
        self._enabled_tools = set(enabled_tools) if enabled_tools is not None else None
        self._initial_budget = max(0, initial_budget)
        self._max_path_depth = max(1, min(3, max_path_depth))
        self._extra_symbol_ids = set(extra_symbol_ids)
        # Internal scheduling metadata; it never changes the Gateway request.
        self._seed_by_id = dict(seed_by_id or {})

    def execute(
        self,
        plans: tuple[ReviewerGraphPlan, ...],
        *,
        catalog: EvidenceCatalog | None = None,
    ) -> ExecutionBatch:
        if catalog is None:
            catalog = EvidenceCatalogBuilder().build_initial(
                task=self._task,
                symbol_context=self._symbol_context,
                reviewer="controlled",
                revision=self._revision,
            )
        if self._tool_client is None:
            return ExecutionBatch(
                catalog=catalog,
                artifacts=dict(catalog.artifacts),
                trace_refs=(),
                steps=tuple(
                    StepExecution(
                        work_item_id=item.work_item_id,
                        step=step,
                        status="failed",
                        error="tool_client_unavailable",
                    )
                    for plan in plans
                    for item in plan.work_items
                    for step in item.evidence_steps
                ),
                diagnostics=("tool_client_unavailable",),
            )

        coordinator = DiscoveryToolCoordinator()
        complete_patch_symbols = (
            {
                symbol.symbol_id
                for symbol in (
                    self._symbol_context.symbols if self._symbol_context else ()
                )
            }
            if self._task.patch_complete
            and self._task.hunk_header.strip().startswith("@@ -0,0 +")
            else set()
        )
        focus = graph_projection_focus(self._task, self._symbol_context)
        if self._extra_symbol_ids:
            focus = replace(
                focus,
                changed_symbol_ids=tuple(
                    dict.fromkeys((*focus.changed_symbol_ids, *self._extra_symbol_ids))
                ),
            )
        client = CoordinatedDiscoveryToolClient(
            self._tool_client,
            coordinator,
            complete_patch_symbol_ids=complete_patch_symbols,
            projection_focus=focus,
        )
        executions: list[StepExecution] = []
        diagnostics: list[str] = []
        seen_keys: set[tuple[str, str]] = set()
        cached_payloads: dict[tuple[str, str], tuple[str, str, str]] = {}
        for alias, artifact_id in catalog.alias_to_artifact_id.items():
            artifact = catalog.artifacts.get(artifact_id)
            if (
                artifact is None
                or artifact.source_kind is not EvidenceSourceKind.TOOL_CALL
                or not artifact.tool
            ):
                continue
            key = canonical_tool_key(artifact.tool, artifact.arguments)
            seen_keys.add(key)
            projected = project_tool_payload(
                artifact.tool,
                artifact.payload,
                ProjectionAudience.REVIEWER,
                arguments=artifact.arguments,
                focus=focus,
            ).content
            cached_payloads[key] = (artifact.payload, projected, alias)
        budget_used = 0
        # Schedule the first declared step for every WorkItem before moving to
        # second/third steps.  A task-level budget is intentionally shared by
        # all reviewers, but consuming it in plan order would let an early
        # reviewer starve later candidates of their primary graph fact.  The
        # round-robin schedule preserves the declared order within each item,
        # while maximizing proof coverage when the bounded budget is smaller
        # than the total number of planned steps.
        for item, step in _fair_plan_steps(
            plans,
            task=self._task,
            seed_by_id=self._seed_by_id,
        ):
            contract = get_tool_proof_contract(step.tool)
            if contract is None:
                executions.append(
                    StepExecution(
                        item.work_item_id, step, "rejected", error="unknown_tool"
                    )
                )
                diagnostics.append(f"unknown_tool:{step.tool}")
                continue
            if self._enabled_tools is not None and step.tool not in self._enabled_tools:
                executions.append(
                    StepExecution(
                        item.work_item_id, step, "rejected", error="tool_disabled"
                    )
                )
                diagnostics.append(f"tool_disabled:{step.tool}")
                continue
            arguments: dict[str, Any] = {"symbol_id": step.subject_ref}
            if step.tool == "inspect_path":
                arguments.update(
                    {
                        "path_kind": step.path_kind,
                        "max_depth": step.max_depth or self._max_path_depth,
                    }
                )
            key = canonical_tool_key(step.tool, arguments)
            if key in seen_keys:
                # The shared cache has already captured this exact fact. Do not
                # create another Gateway call or consume budget. Preserve the
                # original payload on the step so ProofMatcher and the
                # EvidencePack can reuse the fact, not just its alias.
                payload, projected, _alias = cached_payloads.get(key, ("", "", ""))
                executions.append(
                    StepExecution(
                        item.work_item_id,
                        step,
                        "reused",
                        raw_payload=payload,
                        projected_payload=projected,
                        alias=_alias,
                    )
                )
                continue
            if budget_used >= self._initial_budget:
                executions.append(
                    StepExecution(
                        item.work_item_id,
                        step,
                        "budget_exhausted",
                        error="initial_budget_exhausted",
                    )
                )
                diagnostics.append("initial_budget_exhausted")
                continue
            seen_keys.add(key)
            budget_used += 1
            response = self._call(client, step, arguments)
            raw = ""
            if response.success:
                # CoordinatedDiscoveryToolClient returns the projected
                # reviewer message (including the Txx echo). The ledger
                # record is the authoritative raw Gateway payload; do
                # not feed the echo back into Projection/ProofMatcher.
                latest_record = (
                    client.trace_records[-1] if client.trace_records else None
                )
                raw = (
                    latest_record.resolved_output
                    if latest_record is not None and latest_record.resolved_output
                    else response.result or ""
                )
            projected = ""
            if raw:
                projected = project_tool_payload(
                    step.tool,
                    raw,
                    ProjectionAudience.REVIEWER,
                    arguments=arguments,
                    focus=focus,
                ).content
            status = "complete" if response.success else "failed"
            executions.append(
                StepExecution(
                    work_item_id=item.work_item_id,
                    step=step,
                    status=status,
                    raw_payload=raw,
                    projected_payload=projected,
                    error=response.error or "",
                )
            )
            if raw:
                cached_payloads[key] = (raw, projected, "")

        capture = capture_tool_records(catalog, client.trace_records)
        alias_by_key: dict[tuple[str, str], str] = {}
        for alias, artifact_id in capture.catalog.alias_to_artifact_id.items():
            artifact = capture.catalog.artifacts.get(artifact_id)
            if artifact is None or not alias.startswith("T"):
                continue
            alias_by_key[canonical_tool_key(artifact.tool, artifact.arguments)] = alias
        with_aliases = tuple(
            StepExecution(
                work_item_id=item.work_item_id,
                step=item.step,
                status=item.status,
                raw_payload=item.raw_payload,
                projected_payload=item.projected_payload,
                alias=alias_by_key.get(
                    canonical_tool_key(
                        item.step.tool,
                        {
                            "symbol_id": item.step.subject_ref,
                            **(
                                {
                                    "path_kind": item.step.path_kind,
                                    "max_depth": item.step.max_depth
                                    or self._max_path_depth,
                                }
                                if item.step.tool == "inspect_path"
                                else {}
                            ),
                        },
                    ),
                    "",
                ),
                error=item.error,
            )
            for item in executions
        )
        return ExecutionBatch(
            catalog=capture.catalog,
            artifacts=dict(capture.catalog.artifacts),
            trace_refs=tuple(capture.trace_refs),
            steps=with_aliases,
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _call(
        client: CoordinatedDiscoveryToolClient,
        step: EvidenceStep,
        arguments: dict[str, Any],
    ) -> ToolResponse:
        if step.tool == "get_file_content":
            return client.get_file_content(step.subject_ref)
        if step.tool == "inspect_structure":
            return client.inspect_structure(step.subject_ref)
        if step.tool == "inspect_change_impact":
            return client.inspect_change_impact(step.subject_ref)
        if step.tool == "inspect_path":
            return client.inspect_path(
                step.subject_ref,
                str(arguments["path_kind"]),
                int(arguments["max_depth"]),
            )
        return ToolResponse(success=False, error="unknown_tool")


def _topological_steps(steps: tuple[EvidenceStep, ...]) -> tuple[EvidenceStep, ...]:
    """按 declared depends_on 排序；输入无环时保持稳定的原始顺序。"""

    pending = list(steps)
    ordered: list[EvidenceStep] = []
    completed: set[str] = set()
    while pending:
        ready = [step for step in pending if set(step.depends_on).issubset(completed)]
        if not ready:
            # Validator normally prevents this; fail closed without spinning.
            return tuple(ordered + pending)
        for step in ready:
            pending.remove(step)
            ordered.append(step)
            completed.add(step.step_id)
    return tuple(ordered)


def _fair_plan_steps(
    plans: tuple[ReviewerGraphPlan, ...],
    *,
    task: ReviewTask | None = None,
    seed_by_id: dict[str, CandidateSeed] | None = None,
) -> tuple[tuple[Any, EvidenceStep], ...]:
    """Flatten planned steps with bounded proof coverage first.

    Each WorkItem keeps its validated topological order.  When seed locations
    are available, reserve the first two validated steps (the graph fact and
    complementary source fact) for one WorkItem per changed-line region.
    This prevents several hypotheses about one hunk from consuming the task
    budget before another independent changed region gets any evidence.  With
    no seed metadata the historical round-robin policy is retained.  This is
    only a scheduling policy: it does not alter the plan, infer a symbol, or
    create an additional tool call.
    """

    queues = [
        (item, _topological_steps(item.evidence_steps))
        for plan in plans
        for item in plan.work_items
    ]
    if not queues:
        return ()
    scheduled: list[tuple[Any, EvidenceStep]] = []
    first_steps: list[tuple[int, Any, EvidenceStep]] = [
        (index, item, steps[0])
        for index, (item, steps) in enumerate(queues)
        if steps
    ]
    priority_indices: set[int] = set()
    representatives_by_cluster: dict[int, tuple[int, int]] = {}
    if task is not None and seed_by_id:
        clusters = _changed_line_clusters_for_schedule(task)
        for index, item, _steps in [
            (index, item, steps) for index, (item, steps) in enumerate(queues)
        ]:
            seed = seed_by_id.get(item.seed_id)
            if seed is None or seed.location_line <= 0 or not clusters:
                continue
            distances = [
                min(abs(seed.location_line - line) for line in cluster)
                for cluster in clusters
            ]
            nearest = min(distances)
            if nearest > 6:
                continue
            cluster_index = distances.index(nearest)
            current = representatives_by_cluster.get(cluster_index)
            rank = (nearest, index)
            if current is None or rank < current:
                representatives_by_cluster[cluster_index] = rank
        for _nearest, index in representatives_by_cluster.values():
            priority_indices.add(index)
    # Keep one representative of each graph subject/path in the first round;
    # prefer the largest validated depth because it subsumes the shorter
    # bounded query for proof purposes.  Non-graph first steps remain one per
    # WorkItem (their arguments may differ in ways that are meaningful to the
    # tool contract).
    representatives: dict[tuple[str, str, str], tuple[int, Any, EvidenceStep]] = {}
    selected_first_indices: set[int] = set(priority_indices)
    for index, item, step in first_steps:
        if index in priority_indices:
            continue
        if step.tool != "inspect_path":
            selected_first_indices.add(index)
            continue
        key = (step.tool, step.subject_ref, step.path_kind or "")
        current = representatives.get(key)
        depth = step.max_depth or 0
        if current is None or depth > (current[2].max_depth or 0):
            representatives[key] = (index, item, step)
    selected_first_indices.update(index for index, _, _ in representatives.values())
    # Emit the changed-region representatives first, then the remaining
    # distinct graph subjects.  The first round therefore contains graph
    # facts for as many independent hypotheses as the budget permits; source
    # complements are emitted in the second round below.  This matters when
    # two reviewers ask different graph questions for the same hunk: an eager
    # graph+source pair for one reviewer must not hide the other subject's
    # primary fact.
    priority_order = [
        index
        for _nearest, index in sorted(
            representatives_by_cluster.values(), key=lambda value: (value[0], value[1])
        )
        if index in selected_first_indices
    ]
    remaining_order = [
        index for index, _item, _step in first_steps
        if index in selected_first_indices and index not in priority_indices
    ]
    for index in (*priority_order, *remaining_order):
        item, steps = queues[index]
        if steps:
            scheduled.append((item, steps[0]))

    # Advance every WorkItem once after the primary graph round.  Priority
    # representatives participate in this same round instead of having both
    # steps inserted eagerly.  Eager graph+source pairs let one representative
    # consume the whole budget and starve a different subject in the same
    # changed region; keeping a global first-step round gives every distinct
    # graph question a chance before complementary source reads.
    for step_index in range(1, max(len(steps) for _, steps in queues)):
        for index, (item, steps) in enumerate(queues):
            if step_index < len(steps):
                scheduled.append((item, steps[step_index]))

    # Any first graph steps that were suppressed as lower-depth duplicates are
    # retained at the end. They can still reuse a higher-depth cache hit or run
    # when the configured budget permits, so the plan is never silently
    # rewritten.
    for index, item, step in first_steps:
        if index not in selected_first_indices and index not in priority_indices:
            scheduled.append((item, step))
    return tuple(scheduled)


def _changed_line_clusters_for_schedule(
    task: ReviewTask,
) -> tuple[tuple[int, ...], ...]:
    """Group nearby changed/deleted lines for evidence scheduling only."""

    lines = sorted(
        {
            *task.changed_lines,
            *(anchor.anchor_line for anchor in task.deletion_anchors),
        }
    )
    if not lines:
        return ()
    clusters: list[list[int]] = [[lines[0]]]
    for line in lines[1:]:
        if line - clusters[-1][-1] <= 8:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    return tuple(tuple(cluster) for cluster in clusters)


__all__ = ["ControlledEvidenceExecutor", "ExecutionBatch", "StepExecution"]
