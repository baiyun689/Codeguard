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
from codeguard_agent.models.tasks import EvidenceStep, ReviewerGraphPlan, ReviewTask, TaskSymbolContext
from codeguard_agent.pipeline.controlled.contracts import get_tool_proof_contract
from codeguard_agent.pipeline.evidence.ledger import EvidenceCatalogBuilder, capture_tool_records
from codeguard_agent.pipeline.evidence.projection import graph_projection_focus, project_tool_payload, ProjectionAudience
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
    ) -> None:
        self._tool_client = tool_client
        self._task = task
        self._symbol_context = symbol_context
        self._revision = revision
        self._enabled_tools = set(enabled_tools) if enabled_tools is not None else None
        self._initial_budget = max(0, initial_budget)
        self._max_path_depth = max(1, min(3, max_path_depth))
        self._extra_symbol_ids = set(extra_symbol_ids)

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
        complete_patch_symbols = {
            symbol.symbol_id
            for symbol in (self._symbol_context.symbols if self._symbol_context else ())
        } if self._task.patch_complete and self._task.hunk_header.strip().startswith("@@ -0,0 +") else set()
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
        for plan in plans:
            for item in plan.work_items:
                for step in _topological_steps(item.evidence_steps):
                    contract = get_tool_proof_contract(step.tool)
                    if contract is None:
                        executions.append(StepExecution(item.work_item_id, step, "rejected", error="unknown_tool"))
                        diagnostics.append(f"unknown_tool:{step.tool}")
                        continue
                    if self._enabled_tools is not None and step.tool not in self._enabled_tools:
                        executions.append(StepExecution(item.work_item_id, step, "rejected", error="tool_disabled"))
                        diagnostics.append(f"tool_disabled:{step.tool}")
                        continue
                    arguments: dict[str, Any] = {"symbol_id": step.subject_ref}
                    if step.tool == "inspect_path":
                        arguments.update({"path_kind": step.path_kind, "max_depth": step.max_depth or self._max_path_depth})
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
                        executions.append(StepExecution(item.work_item_id, step, "budget_exhausted", error="initial_budget_exhausted"))
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
                        latest_record = client.trace_records[-1] if client.trace_records else None
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
                                    "max_depth": item.step.max_depth or self._max_path_depth,
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
    def _call(client: CoordinatedDiscoveryToolClient, step: EvidenceStep, arguments: dict[str, Any]) -> ToolResponse:
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


__all__ = ["ControlledEvidenceExecutor", "ExecutionBatch", "StepExecution"]
