
"""ReviewCouncil 编排图。

默认先按 PR 体量路由：small / medium 构建 file task；large 构建 hunk task。
所有 task 经过 DirectGate，Full task 进入 Plan、发现、举证与裁决链。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from codeguard_agent.llm.client import mock_review_result
from codeguard_agent.models.council import (
    CandidateIssue,
    CouncilTrace,
    MAX_CANDIDATES_PER_AGENT,
)
from codeguard_agent.models.state import (
    ReviewState,
    ReviewerState,
    collect_candidate_reducer,
)
from codeguard_agent.models.schemas import (
    DiscoveredIssue,
    DiscoveryReviewResult,
    EvidenceRefSelection,
    EvidenceRole,
    Issue,
    ReviewResult,
)
from codeguard_agent.models.tasks import (
    AssessmentStatus,
    CandidateSeed,
    EvidenceAssessment,
    EvidenceNeed,
    EvidenceStep,
    KnowledgeRoutePlan,
    ReviewerGraphPlan,
    ReviewBudget,
    ReviewMode,
    ReviewRoute,
    ReviewRouteThresholds,
    ReviewerKind,
    ReviewTask,
    SkippedTask,
    TaskSelection,
    TaskRoute,
    InvestigationSeed,
    SubtaskInstruction,
)
from codeguard_agent.pipeline.tasks import task_builder as task_prep
from codeguard_agent.pipeline.execution.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.execution.discovery import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
)
from codeguard_agent.pipeline.knowledge.catalog import KnowledgeCatalog
from codeguard_agent.pipeline.knowledge.selector import select_knowledge, select_shared_knowledge
from codeguard_agent.pipeline.location import locate_issues
from codeguard_agent.models.knowledge import KnowledgeBudget
from codeguard_agent.pipeline.tasks.scope import LargeDiffPlan, plan_large_diff
from codeguard_agent.pipeline.planning import (
    build_plan_units,
    plan_coverage,
    run_plan_units,
)
from codeguard_agent.pipeline.controlled.assessment import (
    candidate_from_seed,
    collapse_candidate_duplicates,
    finalize_evidence_assessment,
    match_execution_proof,
    run_evidence_assessment,
    visible_source_symbol_ids,
    visible_symbol_ids,
)
from codeguard_agent.pipeline.controlled.executor import ControlledEvidenceExecutor
from codeguard_agent.pipeline.controlled.graph_plan import (
    graph_replan_reason,
    run_graph_replan,
    run_graph_plan,
)
from codeguard_agent.pipeline.controlled.planning import run_knowledge_route
from codeguard_agent.pipeline.controlled.subtask_plan import run_subtask_plan
from codeguard_agent.pipeline.controlled.subtask_react import SubtaskReactEngine
from codeguard_agent.pipeline.controlled.subtask_grouping import (
    group_investigation_seeds,
)
from codeguard_agent.pipeline.controlled.routing import route_seed
from codeguard_agent.pipeline.controlled.triage import run_direct_triage
from codeguard_agent.pipeline.execution.engines import (
    DirectEngine,
    REACT_DEGRADED_RECURSION_EVENT,
    REACT_DIRECT_FALLBACK_FAILED_EVENT,
    REACT_INLINE_STRUCTURED_EVENT,
    REACT_SYNTHESIS_FALLBACK_FAILED_EVENT,
    REACT_SYNTHESIS_FALLBACK_INVALID_OUTPUT_EVENT,
    REACT_SYNTHESIS_FALLBACK_RECURSION_EVENT,
    ReviewExecutionStatus,
    ReviewEngine,
    ReviewOutcome,
    ToolAgentEngine,
)
from codeguard_agent.models.evidence import (
    EvidenceArtifact,
)
from codeguard_agent.pipeline.evidence.ledger import (
    EvidenceCatalogBuilder,
    bind_discovered_issue,
    capture_tool_records,
)
from codeguard_agent.pipeline.evidence.planner import assemble_dossiers
from codeguard_agent.pipeline.evidence.projection import graph_projection_focus
from codeguard_agent.pipeline.symbols import resolve_task_symbols
from codeguard_agent.pipeline.reviewers.reviewers import (
    DEFAULT_REVIEWERS,
    Reviewer,
    build_reviewer_system_prompt,
    build_reviewer_user_prompt,
)
from codeguard_agent.pipeline.summary.summary import build_diff_summary

logger = logging.getLogger("codeguard")

DEFAULT_RECURSION_LIMIT = 50

_ALL_REVIEWER_NAMES = [r.source_agent for r in DEFAULT_REVIEWERS]


def _discover_node_name(reviewer: Reviewer) -> str:
    return f"discover_{reviewer.source_agent}"


def _make_engine(state: ReviewState | ReviewerState, tool_client=None) -> ReviewEngine:
    if tool_client is not None:
        return ToolAgentEngine(
            tool_client,
            recursion_limit=state.get("react_recursion_limit", 24),
            enabled_tools=state.get("enabled_tools"),
            allow_direct_fallback=state.get("allow_direct_fallback", True),
            projection_focus=getattr(tool_client, "projection_focus", None),
        )
    return DirectEngine()


def _capture_catalog_from_client(
    catalog: Any,
    coordinated_client: Any,
) -> tuple[Any, list[Any]]:
    """ReAct 降级时保留原始 Artifact，只向 State 返回紧凑引用。"""
    records = list(getattr(coordinated_client, "trace_records", ()))
    if catalog is None or not records:
        return catalog, []
    from codeguard_agent.pipeline.evidence.ledger import capture_tool_records

    batch = capture_tool_records(catalog, records)
    return batch.catalog, list(batch.trace_refs)


def _scope_plan(state: ReviewState) -> LargeDiffPlan:
    return plan_large_diff(
        state.get("diff_text", ""),
        list(state.get("review_tasks") or []),
        state.get("review_budget") or ReviewBudget(),
    )


def _selected_diff(state: ReviewState, scope: LargeDiffPlan) -> str:
    selection = state.get("task_selection")
    if selection is None:
        raise ValueError("task_selection is required before scoped context stages")
    return scope.selected_diff(list(state.get("review_tasks") or []), selection)


def _summary_node(llm):
    def _node(state: ReviewState) -> dict:
        scope = _scope_plan(state)
        summary = build_diff_summary(
            _selected_diff(state, scope),
            llm=llm,
            max_retries=state.get("max_retries", 3),
            structured_method=state.get("structured_method", "function_calling"),
        )
        return {"diff_summary": summary}

    return _node


def _classify_mode_node():
    """根据 PR 体量决定审查模式（纯确定性，不调 LLM，不建 task）。

    只做轻量 diff 统计（文件数/hunk 数/字符数），不构建 ReviewTask 对象。
    """

    def _node(state: ReviewState) -> dict:
        diff_text = state.get("diff_text", "")
        budget = state.get("review_budget") or ReviewBudget()
        mode = task_prep.classify_diff(diff_text, budget)
        metrics = task_prep.diff_metrics(diff_text)
        selected_node: Literal["file_task_builder", "diff_task_builder"]
        if mode is ReviewMode.SMALL:
            selected_node = "file_task_builder"
        elif mode is ReviewMode.MEDIUM:
            selected_node = "file_task_builder"
        else:
            selected_node = "diff_task_builder"
        review_route = ReviewRoute(
            initial_mode=mode,
            effective_mode=mode,
            selected_node=selected_node,
            metrics=metrics,
            thresholds=ReviewRouteThresholds(
                small_max_files=budget.small_max_files,
                small_max_hunks=budget.small_max_hunks,
                small_max_diff_chars=budget.small_max_diff_chars,
                medium_max_files=budget.medium_max_files,
                medium_max_diff_chars=budget.medium_max_diff_chars,
            ),
        )
        return {
            "review_mode": mode.value,
            "review_route": review_route,
            "council_trace": [
                CouncilTrace(
                    node="classify_mode",
                    event="mode_selected",
                    detail=(
                        f"mode={mode.value} files={metrics.file_count} "
                        f"hunks={metrics.hunk_count} "
                        f"diff_chars={metrics.diff_chars} "
                        f"small_max={budget.small_max_diff_chars} "
                        f"medium_max={budget.medium_max_diff_chars}"
                    ),
                )
            ],
        }

    return _node


def _file_task_builder_node():
    """SMALL / MEDIUM 均构建文件级 task。

    SMALL 与 MEDIUM 的下游管线完全一致，仅预算不同；两者统一按文件拆分，
    使每个 task 携带真实文件路径与该文件的变更行，让 symbol_resolution
    能逐文件解析出稳定符号——发现者据此获得图工具入口。
    """

    def _node(state: ReviewState) -> dict:
        diff_text = state.get("diff_text", "")
        mode = state.get("review_mode", "medium")
        tasks = task_prep.build_file_tasks(diff_text)
        file_count = len({t.file for t in tasks})
        hunk_fallback_count = len([t for t in tasks if t.hunk_header])
        return {
            "review_tasks": tasks,
            "council_trace": [
                CouncilTrace(
                    node="file_task_builder",
                    event="tasks_built",
                    detail=(
                        f"mode={mode} tasks={len(tasks)} "
                        f"files={file_count} "
                        f"hunk_fallback={hunk_fallback_count}"
                    ),
                )
            ],
        }

    return _node


def _diff_task_builder_node():
    """DiffTaskBuilder：解析 diff → ReviewTask（large 模式的 hunk 级拆分）。"""

    def _node(state: ReviewState) -> dict:
        tasks = task_prep.build_tasks(state.get("diff_text", ""))
        file_count = len({t.file for t in tasks})
        return {
            "review_tasks": tasks,
            "council_trace": [
                CouncilTrace(
                    node="diff_task_builder",
                    event="tasks_built",
                    detail=(
                        f"mode=large tasks={len(tasks)} "
                        f"files={file_count}"
                    ),
                )
            ],
        }

    return _node


def _task_selection_node():
    """按 DirectGate 和大 diff 确定性限制构造 Full task 工作集。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        routes = state.get("task_routes") or {}
        scope = _scope_plan(state)
        budget = _scope_plan(state).effective_budget
        full_tasks = [task for task in tasks if routes.get(task.id, TaskRoute(task_id=task.id, route="full")).route == "full"]
        selected: list[str] = []
        skipped = []
        per_file: dict[str, int] = {}
        for task in full_tasks:
            file_key = task.file.replace("\\", "/").lower()
            if budget.max_tasks_to_review is not None and len(selected) >= budget.max_tasks_to_review:
                skipped.append(SkippedTask(task_id=task.id, reason="total_limit"))
                continue
            if budget.max_tasks_per_file is not None and per_file.get(file_key, 0) >= budget.max_tasks_per_file:
                skipped.append(SkippedTask(task_id=task.id, reason="per_file_limit"))
                continue
            selected.append(task.id)
            per_file[file_key] = per_file.get(file_key, 0) + 1
        skipped.extend(
            SkippedTask(task_id=task.id, reason="direct_gate")
            for task in tasks
            if routes.get(task.id, TaskRoute(task_id=task.id, route="full")).route == "direct"
        )
        selection = TaskSelection(
            selected_task_ids=selected,
            skipped_tasks=skipped,
        )
        trace = [
            CouncilTrace(
                node="task_selection",
                event="selected",
                detail=f"selected={len(selection.selected_task_ids)} skipped={len(selection.skipped_tasks)}",
            )
        ]
        if scope.active:
            trace.append(
                CouncilTrace(
                    node="task_selection",
                    event="large_diff_limited",
                    detail=(
                        f"lines={scope.total_lines} tasks={scope.total_tasks} "
                        f"selected={len(selection.selected_task_ids)} "
                        f"skipped={len(selection.skipped_tasks)} "
                        f"max_tasks={budget.max_tasks_to_review} "
                        f"max_per_file={budget.max_tasks_per_file} "
                        f"context_chars={budget.max_context_chars_per_task}"
                    ),
                )
            )
        return {
            "task_selection": selection,
            "council_trace": trace,
        }

    return _node


def _review_plan_node(tool_client=None):
    """把 Plan 的 Reviewer 选择转换为执行计划。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        selection = state.get("task_selection")
        if selection is None:
            raise ValueError("task_selection is required before review plan")
        task_plans = state.get("task_plans") or {}
        plan_units = state.get("plan_units") or []
        routes = state.get("task_routes") or {}
        plan = plan_coverage(
            tasks=tasks,
            selection_ids=set(selection.selected_task_ids),
            routes=routes,
            plan_units=plan_units,
            plans=task_plans,
            tools_available=tool_client is not None,
        )
        assignment_count = sum(len(item.assignments) for item in plan.tasks)
        trace = [
            CouncilTrace(
                node="review_plan",
                event="planned",
                detail=f"tasks={len(plan.tasks)} assignments={assignment_count}",
            )
        ]
        trace.extend(
            CouncilTrace(
                node="review_plan",
                event="assignment",
                detail=(
                    f"task={item.task_id} reviewer={assignment.reviewer.value} "
                    f"tier={assignment.tier.value} "
                    f"reasons={','.join(reason.value for reason in assignment.reasons)}"
                ),
            )
            for item in plan.tasks
            for assignment in item.assignments
        )
        return {"review_assignments": plan, "council_trace": trace}

    return _node


def _symbol_resolution_node(tool_client):
    """把选中 Full task 的变更行批量解析为稳定项目符号。"""

    def _node(state: ReviewState) -> dict:
        scope = _scope_plan(state)
        selection = state.get("task_selection")
        selected_ids = set(selection.selected_task_ids) if selection is not None else set()
        all_tasks: list[ReviewTask] = state.get("review_tasks") or []
        tasks = [task for task in all_tasks if task.id in selected_ids]
        resolution = resolve_task_symbols(
            tasks,
            tool_client=tool_client,
            max_chars_per_task=scope.effective_budget.max_context_chars_per_task,
        )
        deletion_anchor_count = sum(
            len(task.deletion_anchors) for task in tasks
        )
        resolved_deletion_anchor_count = sum(
            1
            for task in tasks
            for anchor in task.deletion_anchors
            if any(
                symbol.start_line <= anchor.anchor_line <= symbol.end_line
                for symbol in resolution.contexts[task.id].symbols
            )
        )
        trace: list[CouncilTrace] = [
            CouncilTrace(
                node="symbol_resolution",
                event="resolution_completed",
                detail=(
                    f"tasks={len(tasks)} "
                    f"resolved={sum(bool(item.symbols) for item in resolution.contexts.values())} "
                    f"symbols={sum(len(item.symbols) for item in resolution.contexts.values())} "
                    f"deletion_anchors={deletion_anchor_count} "
                    f"deletion_anchors_resolved={resolved_deletion_anchor_count}"
                ),
            )
        ]
        for task in tasks:
            context = resolution.contexts[task.id]
            trace.append(
                CouncilTrace(
                    node="symbol_resolution",
                    event="task_symbols_resolved",
                    detail=(
                        f"task={task.id} status={context.status.value} "
                        f"symbols={len(context.symbols)} "
                        f"deletion_anchors={len(task.deletion_anchors)} "
                        f"limitations={','.join(context.limitations)} "
                        f"truncated={context.truncated}"
                    ),
                )
            )

        return {
            "task_symbol_contexts": resolution.contexts,
            "symbol_resolution_diagnostics": dict(resolution.diagnostics),
            "council_trace": trace,
        }

    return _node


def build_reviewer_subgraph(reviewer: Reviewer, checkpointer=None, llm=None, tool_client=None):
    """把发现者 Agent 构造成 prepare → review → collect 子图。"""
    from langgraph.graph import END, START, StateGraph

    def _system_prompt(state: ReviewerState) -> str:
        return build_reviewer_system_prompt(reviewer)

    def _direct_fallback(state: ReviewerState) -> ReviewOutcome:
        return DirectEngine().review(
            llm,
            system_prompt=_system_prompt(state),
            user_prompt=state.get("user_prompt", ""),
            reviewer_name=reviewer.name,
            max_retries=state.get("max_retries", 3),
            structured_method=state.get("structured_method", "function_calling"),
            evidence_catalog=state.get("evidence_catalog"),
            result_schema=DiscoveryReviewResult,
        )

    def _prepare(state: ReviewerState) -> dict:
        if llm is None:
            # mock 模式:无需 task 也可运行(原有语义);真实管线中候选绑定
            # 由 make_reviewer_node 侧构建目录兜底。
            return {}
        review_task = state.get("review_task")
        if review_task is None:
            raise ValueError("review_task is required for task-scoped discovery")
        # 注册 P01/Cxx：patch 与稳定符号事实成为一等证据(Evidence Ledger)。
        catalog = EvidenceCatalogBuilder().build_initial(
            task=review_task,
            symbol_context=state.get("task_symbol_context"),
            reviewer=reviewer.source_agent,
            revision=state.get("evidence_revision", ""),
        )
        return {
            "user_prompt": build_reviewer_user_prompt(
                task=review_task,
                summary=state.get("diff_summary", ""),
                symbol_context=state.get("task_symbol_context"),
                task_knowledge=state.get("task_knowledge", ""),
                plan_objectives=state.get("plan_objectives", ()),
                task_scope=state.get("task_scope", "current_hunk"),
                catalog=catalog,
                user_prompt_file=reviewer.prompt_file.replace("-base.txt", "-user.txt"),
            ),
            "evidence_catalog": catalog,
        }

    def _review(state: ReviewerState) -> dict:
        if llm is None:
            if reviewer.source_agent == "threat_model":
                return {"outcome": ReviewOutcome(mock_review_result())}
            return {"outcome": ReviewOutcome(ReviewResult(summary=""))}
        tier = state.get("tier")
        effective_tool_client = state.get("review_tool_client") or tool_client
        engine = (
            _make_engine(state, tool_client=None)
            if tier == "direct"
            else _make_engine(state, tool_client=effective_tool_client)
        )
        review_traces: list[CouncilTrace] = []
        if tier == "direct":
            task = state.get("review_task")
            task_id = task.id if task is not None else ""
            review_traces.append(
                CouncilTrace(node=reviewer.source_agent, event="tier_direct", detail=task_id)
            )

        try:
            outcome = engine.review(
                llm,
                system_prompt=_system_prompt(state),
                user_prompt=state.get("user_prompt", ""),
                reviewer_name=reviewer.name,
                max_retries=state.get("max_retries", 3),
                structured_method=state.get("structured_method", "function_calling"),
                enable_hitl=False,
                evidence_catalog=state.get("evidence_catalog"),
                result_schema=DiscoveryReviewResult,
            )
        except Exception as exc:  # noqa: BLE001 单发现者失败不拖垮 council
            from langgraph.errors import GraphRecursionError

            if isinstance(exc, GraphRecursionError):
                logger.warning("[%s] 发现者撞递归上限,降级直连: %s", reviewer.name, exc)
                review_traces.append(
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event=REACT_DEGRADED_RECURSION_EVENT,
                        detail=str(exc)[:200],
                    )
                )
                if not state.get("allow_direct_fallback", True):
                    catalog, trace_refs = _capture_catalog_from_client(
                        state.get("evidence_catalog"), effective_tool_client
                    )
                    return {
                        "outcome": ReviewOutcome(
                            result=None,
                            status=ReviewExecutionStatus.RECURSION_FAILED,
                            failure_reason=type(exc).__name__,
                            tool_trace_records=trace_refs,
                            execution_events=[REACT_DEGRADED_RECURSION_EVENT],
                            evidence_catalog=catalog,
                        ),
                        "council_trace": review_traces,
                    }
                outcome = _direct_fallback(state)
                catalog, trace_refs = _capture_catalog_from_client(
                    outcome.evidence_catalog, effective_tool_client
                )
                outcome.evidence_catalog = catalog
                outcome.tool_trace_records.extend(trace_refs)
            else:
                if (
                    tier != "direct"
                    and state.get("allow_direct_fallback", True)
                ):
                    logger.warning(
                        "[%s] ReAct 发现者失败,降级直连: %s",
                        reviewer.name,
                        exc,
                    )
                    review_traces.append(
                        CouncilTrace(
                            node=reviewer.source_agent,
                            event="react_degraded_error",
                            detail=str(exc)[:200],
                        )
                    )
                    outcome = _direct_fallback(state)
                    catalog, trace_refs = _capture_catalog_from_client(
                        outcome.evidence_catalog, effective_tool_client
                    )
                    outcome.evidence_catalog = catalog
                    outcome.tool_trace_records.extend(trace_refs)
                else:
                    logger.warning("[%s] 发现者失败,跳过: %s", reviewer.name, exc)
                    catalog, trace_refs = _capture_catalog_from_client(
                        state.get("evidence_catalog"), effective_tool_client
                    )
                    return {
                        "outcome": ReviewOutcome(
                            result=None,
                            status=ReviewExecutionStatus.EXECUTION_FAILED,
                            failure_reason=type(exc).__name__,
                            tool_trace_records=trace_refs,
                            evidence_catalog=catalog,
                        ),
                        "council_trace": [
                            CouncilTrace(
                                node=reviewer.source_agent,
                                event="discover_failed",
                                detail=str(exc),
                            )
                        ],
                    }

        event_details = {
            REACT_INLINE_STRUCTURED_EVENT: "used the ReAct terminal structured result",
            REACT_SYNTHESIS_FALLBACK_INVALID_OUTPUT_EVENT: (
                "synthesized from captured tool facts after invalid terminal output"
            ),
            REACT_SYNTHESIS_FALLBACK_RECURSION_EVENT: (
                "synthesized from captured tool facts after the recursion limit"
            ),
            REACT_SYNTHESIS_FALLBACK_FAILED_EVENT: (
                "structured fallback failed; no further review stage was started"
            ),
            REACT_DEGRADED_RECURSION_EVENT: (
                "used the configured direct fallback after recursion without facts"
            ),
            REACT_DIRECT_FALLBACK_FAILED_EVENT: (
                "direct fallback failed; no further review stage was started"
            ),
        }
        for event in outcome.execution_events:
            review_traces.append(
                CouncilTrace(
                    node=reviewer.source_agent,
                    event=event,
                    detail=event_details.get(event, event),
                )
            )

        if review_traces:
            return {"outcome": outcome, "council_trace": review_traces}
        return {"outcome": outcome}

    def _collect(state: ReviewerState) -> dict:
        outcome = state.get("outcome")
        out: dict = {
            "council_trace": [
                CouncilTrace(node=reviewer.source_agent, event="discover_done")
            ],
        }
        if outcome is None:

            return out
        if outcome.tool_trace_records:
            out["tool_trace_records"] = list(outcome.tool_trace_records)
        catalog = outcome.evidence_catalog or state.get("evidence_catalog")
        if catalog is not None:
            out["evidence_catalog"] = catalog
        if outcome.status is not ReviewExecutionStatus.COMPLETE or outcome.result is None:
            out["council_trace"].append(
                CouncilTrace(
                    node=reviewer.source_agent,
                    event="task_review_failed",
                    detail=(
                        outcome.failure_reason
                        or str(getattr(outcome.status, "value", outcome.status))
                    ),
                )
            )
            return out
        out["issues"] = list(outcome.result.issues)
        if outcome.result.summary:
            out["review_summaries"] = (
                [outcome.result.summary]
                if llm is None
                else [f"【{reviewer.name}】{outcome.result.summary}"]
            )
        return out

    sg = StateGraph(ReviewerState)
    sg.add_node("prepare", _prepare)
    sg.add_node("review", _review)
    sg.add_node("collect", _collect)
    sg.add_edge(START, "prepare")
    sg.add_edge("prepare", "review")
    sg.add_edge("review", "collect")
    sg.add_edge("collect", END)
    return sg.compile(checkpointer=checkpointer)


def make_reviewer_node(reviewer: Reviewer, checkpointer=None, llm=None, tool_client=None):
    """发现者节点：按覆盖计划运行 reviewer 并转换为 CandidateIssue。"""
    # task 级 fan-out 会在线程池中并发 invoke；任务子图不持久化，避免复用外层
    # SQLite saver 的线程绑定连接。外层 ReviewState 仍由 build_review_graph 的
    # checkpointer 持久化，足以恢复整次审查。
    subgraph = build_reviewer_subgraph(reviewer, checkpointer=None, llm=llm, tool_client=tool_client)

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        assignments = state.get("review_assignments")
        selection = state.get("task_selection")
        if selection is None:
            raise ValueError("task_selection is required before discovery")

        _coordinator = DiscoveryToolCoordinator() if tool_client is not None else None

        def _task_tool_client(
            task: ReviewTask | None = None,
            symbol_context: Any = None,
        ):
            if tool_client is None or _coordinator is None:
                return None
            complete_patch_symbol_ids = (
                {
                    symbol.symbol_id
                    for symbol in (symbol_context.symbols if symbol_context is not None else ())
                }
                if task is not None
                and task.patch_complete
                and task.hunk_header.strip().startswith("@@ -0,0 +")
                else set()
            )
            return CoordinatedDiscoveryToolClient(
                tool_client,
                _coordinator,
                complete_patch_symbol_ids=complete_patch_symbol_ids,
                projection_focus=(
                    graph_projection_focus(task, symbol_context)
                    if task is not None
                    else None
                ),
            )

        assignment_by_task = {
            item.task_id: item
            for item in (assignments.tasks if assignments is not None else ())
        }
        ordered_ids = [
            task_id for task_id in selection.selected_task_ids
            if any(
                assignment.reviewer.value == reviewer.source_agent
                for assignment in (
                    assignment_by_task[task_id].assignments
                    if task_id in assignment_by_task else ()
                )
            )
        ]
        routed_ids = set(ordered_ids)
        if not routed_ids:
            return {
                "raw_candidate_issues": [],
                "truncated_candidates": 0,
                "council_trace": [
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event="no_tasks_routed",
                        detail="selected tasks have no assignment for this reviewer",
                    )
                ],
            }

        effective_tools = (
            state.get("enabled_tools")
            if state.get("enabled_tools") is not None
            else reviewer.tool_allowlist
        )

        # 每个路由到的 task 独立调用，task 间并发派发。
        task_by_id = {t.id: t for t in tasks}
        task_symbol_contexts = state.get("task_symbol_contexts") or {}
        task_plans = state.get("task_plans") or {}
        plan_units = state.get("plan_units") or []
        plan_unit_by_task = {
            task_id: unit
            for unit in plan_units
            for task_id in unit.task_ids
        }

        def _invoke_one(task_id: str) -> dict:
            task = task_by_id[task_id]
            scope = _scope_plan(state)
            scoped_patch = scope.scoped_patch(task.patch)
            scoped_task = task.model_copy(
                update={
                    "patch": scoped_patch,
                    "patch_complete": task.patch_complete
                    and scoped_patch == task.patch,
                }
            )
            tier = "react" if tool_client is not None else "direct"
            symbol_context = task_symbol_contexts.get(task_id)

            catalog = KnowledgeCatalog()
            budget = KnowledgeBudget()
            reviewer_kind_val = reviewer.source_agent  # "threat_model", "behavior", "maintainability"
            reviewer_kind = ReviewerKind(reviewer_kind_val)
            unit = plan_unit_by_task.get(task_id)
            agent_plan = task_plans.get(unit.id) if unit is not None else None
            reviewer_plan = next(
                (
                    item for item in (agent_plan.reviewer_plans if agent_plan else ())
                    if item.reviewer is reviewer_kind
                ),
                None,
            )
            knowledge_bundle = select_knowledge(
                reviewer=reviewer_kind,
                requested_topics=(reviewer_plan.knowledge_topics if reviewer_plan else ()),
                catalog=catalog,
                budget=budget,
            )
            task_knowledge = knowledge_bundle.rendered_text
            # 根据审查模式推导 task_scope：文件级 → current_file，其余 → current_hunk
            mode = state.get("review_mode", "large")
            task_scope = "current_file" if mode in ("small", "medium") else "current_hunk"
            # 子图未挂 checkpointer（见 make_reviewer_node），因此线程池中的每次 task
            # invoke 都不需要也不应创建独立 thread_id；审查级恢复仍由外层图承担。
            result = subgraph.invoke(
                {
                    "diff_text": scoped_task.patch,
                    "enabled_tools": effective_tools,
                    "max_retries": state.get("max_retries", 3),
                    "structured_method": state.get("structured_method", "function_calling"),
                    "diff_summary": state.get("diff_summary", ""),
                    "react_recursion_limit": state.get("react_recursion_limit", 24),
                    "allow_direct_fallback": False,
                    "review_task": scoped_task,
                    "task_symbol_context": symbol_context,
                    "task_knowledge": task_knowledge,
                    "plan_objectives": reviewer_plan.objectives if reviewer_plan else (),
                    "knowledge_topics": reviewer_plan.knowledge_topics if reviewer_plan else (),
                    "tier": tier,
                    "task_scope": task_scope,
                    "review_tool_client": _task_tool_client(
                        scoped_task, symbol_context
                    ),
                    "evidence_revision": state.get("evidence_revision", ""),
                },
            )
            return result

        task_results = run_bounded_parallel(ordered_ids, _invoke_one, max_workers=8)

        per_task_issues: list[tuple[str, Any]] = []
        trace = [
            CouncilTrace(
                node=reviewer.source_agent,
                event="task_tier_planned",
                detail=f"task={task_id} tier={'react' if tool_client is not None else 'direct'}",
            )
            for task_id in ordered_ids
        ]
        tool_trace_records: list = []
        review_summaries: list = []
        evidence_artifacts: dict[str, EvidenceArtifact] = {}
        catalog_by_task: dict[str, Any] = {}
        for task_id, result in zip(ordered_ids, task_results):
            if result is None:
                trace.append(
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event="task_review_failed",
                        detail=task_id,
                    )
                )
                continue
            for issue in result.get("issues") or []:
                per_task_issues.append((task_id, issue))
            trace.extend(result.get("council_trace") or [])
            if result.get("tool_trace_records"):
                tool_trace_records.extend(result["tool_trace_records"])
            if result.get("review_summaries"):
                review_summaries.extend(result["review_summaries"])
            catalog = result.get("evidence_catalog")
            if catalog is not None:
                catalog_by_task[task_id] = catalog
                evidence_artifacts.update(dict(catalog.artifacts))

        kept_pairs = per_task_issues[:MAX_CANDIDATES_PER_AGENT]
        truncated_candidates = max(0, len(per_task_issues) - len(kept_pairs))

        candidates = []
        rejected_mismatched: list[str] = []
        rejected_noise = 0
        accepted_pairs: list[tuple[str, Any]] = []
        for task_id, issue in kept_pairs:
            task = task_by_id[task_id]
            if not task_prep.file_matches_task(issue.file, task):
                rejected_mismatched.append(f"{issue.file}:{issue.line} -> {task_id}")
                continue
            if task_prep.is_noise_issue(issue.file, issue.type, issue.message):
                rejected_noise += 1
                continue
            accepted_pairs.append((task_id, issue))

        located_by_index: dict[int, Any] = {}
        for task_id in dict.fromkeys(item[0] for item in accepted_pairs):
            pair_indexes = [
                index
                for index, (candidate_task_id, _issue) in enumerate(accepted_pairs)
                if candidate_task_id == task_id
            ]
            location_batch = locate_issues(
                [accepted_pairs[index][1] for index in pair_indexes],
                task_by_id[task_id],
                llm=llm,
                structured_method=state.get("structured_method", "function_calling"),
                max_retries=state.get("max_retries", 3),
            )
            for pair_index, located_issue in zip(
                pair_indexes, location_batch.issues, strict=True
            ):
                located_by_index[pair_index] = located_issue
            trace.extend(
                CouncilTrace(node=reviewer.source_agent, event=event, detail=detail)
                for event, detail in location_batch.trace
            )

        for pair_index, (task_id, _issue) in enumerate(accepted_pairs):
            accepted_count = pair_index + 1
            task = task_by_id[task_id]
            issue = located_by_index[pair_index]
            # 短别名引用绑定为内部 artifact ID;无效引用留痕并退化 patch-only。
            catalog = catalog_by_task.get(task_id)
            if catalog is None:
                catalog = EvidenceCatalogBuilder().build_initial(
                    task=task,
                    symbol_context=task_symbol_contexts.get(task_id),
                    reviewer=reviewer.source_agent,
                    revision=state.get("evidence_revision", ""),
                )
                catalog_by_task[task_id] = catalog
                # 兜底目录同样汇入 Artifact 归并,保证 patch 证据对下游可见。
                evidence_artifacts.update(dict(catalog.artifacts))
            candidates.append(
                bind_discovered_issue(
                    issue,
                    task=task,
                    reviewer=reviewer.source_agent,
                    catalog=catalog,
                    candidate_index=accepted_count,
                )
            )

        trace.append(
            CouncilTrace(
                node=reviewer.source_agent,
                event="candidates_created",
                detail=(
                    f"count={len(candidates)} truncated={truncated_candidates} "
                    f"rejected_task_mismatch={len(rejected_mismatched)} "
                    f"rejected_noise={rejected_noise}"
                ),
            )
        )
        if rejected_mismatched:
            trace.append(
                CouncilTrace(
                    node=reviewer.source_agent,
                    event="candidate_rejected_task_mismatch",
                    detail="; ".join(rejected_mismatched),
                )
            )

        routed_out: dict = {
            "raw_candidate_issues": candidates,
            "truncated_candidates": truncated_candidates,
            "council_trace": trace,
        }
        if tool_trace_records:
            routed_out["tool_trace_records"] = tool_trace_records
        if evidence_artifacts:
            routed_out["evidence_artifacts"] = evidence_artifacts
        if review_summaries:
            routed_out["review_summaries"] = review_summaries
        return routed_out

    return _node


def _discovery_collector_node():
    """历史诊断拓扑:只记录发现者候选,不伪造未经过 Judge 的产品 Issue。"""

    def _node(state: ReviewState) -> dict:
        raw = list(state.get("raw_candidate_issues") or [])
        issues: list[Issue] = []

        trace = list(state.get("council_trace") or [])
        trace.append(
            CouncilTrace(
                node="discovery_collector",
                event="discovery_direct_output",
                detail=f"raw_candidates={len(raw)} final_issues={len(issues)}",
            )
        )

        return {
            "final_issues": [*(state.get("direct_final_issues") or []), *issues],
            "council_trace": trace,
        }

    return _node


def _coordinator_node(effective_judge_llm):
    """三路发现者的显式 fan-in barrier：只做候选规范化，不做语义合并。

    1. 读 raw_candidate_issues
    2. 按 candidate.id 做确定性去重并保留稳定顺序
    3. 产出 candidate_issues（唯一写入者）和 council_trace
    """

    def _node(state: ReviewState) -> dict:
        raw = list(state.get("raw_candidate_issues") or [])
        candidates = collect_candidate_reducer([], raw)
        trace = [
            CouncilTrace(
                node="council_coordinator",
                event="candidate_fan_in",
                detail=f"raw={len(raw)} unique={len(candidates)} semantic_merge=deferred",
            )
        ]

        return {
            "candidate_issues": list(candidates),
            "council_trace": trace,
        }

    return _node


def _assemble_state_dossiers(state: ReviewState):
    candidates = list(state.get("candidate_issues") or [])
    contexts = state.get("controlled_candidate_contexts") or {}
    if contexts:
        rehydrated: list[CandidateIssue] = []
        for candidate in candidates:
            context = contexts.get(candidate.id)
            if not isinstance(context, Mapping):
                rehydrated.append(candidate)
                continue
            updates = {
                key: str(context[key])
                for key in (
                    "mechanism",
                    "impact",
                    "impact_locale",
                    "claim_type",
                    "evidence_observation",
                )
                if context.get(key)
            }
            rehydrated.append(
                candidate.model_copy(update=updates) if updates else candidate
            )
        candidates = rehydrated
    return assemble_dossiers(
        candidates,
        state.get("review_tasks") or [],
        state.get("task_symbol_contexts") or {},
    )


def _evidence_verifier_node(tool_client=None, judge_llm=None):
    """证据验证节点:Artifact 健康检查 + 图护栏 + 异常重放(零 LLM)。"""

    def _node(state: ReviewState) -> dict:
        from codeguard_agent.pipeline.evidence.verifier import verify_evidence

        assembly = _assemble_state_dossiers(state)
        batch = verify_evidence(
            assembly.dossiers,
            artifacts=state.get("evidence_artifacts") or {},
            tool_client=tool_client,
            revision=state.get("evidence_revision", ""),
            enabled_replay_tools=state.get(
                "enabled_evidence_tools", state.get("enabled_tools")
            ),
        )
        result: dict[str, Any] = {
            "candidate_verifications": batch.candidates,
            "council_trace": [
                CouncilTrace(node="evidence_verifier", event=event, detail=detail)
                for event, detail in batch.trace
            ],
        }
        if batch.replayed_artifacts:
            result["evidence_artifacts"] = batch.replayed_artifacts
        return result

    return _node


def _council_judge_node(judge_llm=None):
    """裁决节点:验证淘汰 → 批量 EvidenceJudge，暂不做语义合并。"""

    def _node(state: ReviewState) -> dict:
        from codeguard_agent.pipeline.council.metrics import compute_council_run_stats
        from codeguard_agent.pipeline.council.verdict import judge_with_evidence

        assembly = _assemble_state_dossiers(state)
        batch = judge_with_evidence(
            assembly,
            state.get("candidate_verifications") or {},
            state.get("evidence_artifacts") or {},
            judge_llm=judge_llm,
            structured_method=state.get("structured_method", "function_calling"),
            max_retries=state.get("max_retries", 2),
        )
        judge_trace = [
            CouncilTrace(node="council_judge", event=event, detail=detail)
            for event, detail in (*assembly.trace, *batch.trace)
        ]
        stats = compute_council_run_stats(
            candidates=state.get("candidate_issues") or [],
            assembly=assembly,
            verdicts=batch.verdicts,
            final_candidate_ids=batch.final_candidate_ids,
            truncated_candidates=state.get("truncated_candidates", 0),
            council_trace=[*(state.get("council_trace") or []), *judge_trace],
            artifacts=state.get("evidence_artifacts") or {},
            verifications=state.get("candidate_verifications") or {},
        )
        summaries = list(state.get("review_summaries") or [])
        selection = state.get("task_selection")
        if selection is not None:
            notice = _scope_plan(state).limit_notice(selection)
            if notice:
                summaries.insert(0, notice)
        return {
            "final_issues": [
                *batch.final_issues,
                *(state.get("direct_final_issues") or []),
            ],
            "judge_survivor_ids": list(batch.final_candidate_ids),
            "council_stats": stats,
            "summary": "  ".join(summaries),
            "council_trace": judge_trace,
        }

    return _node


def _task_route_node():
    """Task 构建后的确定性 Direct/Full 路由。"""

    def _node(state: ReviewState) -> dict:
        routes = task_prep.classify_task_routes(list(state.get("review_tasks") or []))
        direct = sum(route.route == "direct" for route in routes.values())
        full = sum(route.route == "full" for route in routes.values())
        return {
            "task_routes": routes,
            "council_trace": [
                CouncilTrace(
                    node="task_route",
                    event="task_routes_decided",
                    detail=f"tasks={len(routes)} direct={direct} full={full}",
                )
            ],
        }

    return _node


def _plan_node(llm):
    """Full task 的 Plan：按 PlanUnit 并发一次规划。"""

    def _node(state: ReviewState) -> dict:
        tasks = list(state.get("review_tasks") or [])
        routes = state.get("task_routes") or {}
        selection = state.get("task_selection")
        selected_ids = set(selection.selected_task_ids) if selection is not None else {
            task.id for task in tasks
        }
        selected_tasks = [task for task in tasks if task.id in selected_ids]
        units = build_plan_units(
            selected_tasks,
            routes,
            review_mode=state.get("review_mode", "large"),
        )
        plans, diagnostics = run_plan_units(
            plan_units=units,
            tasks=selected_tasks,
            llm=llm,
            max_retries=state.get("max_retries", 3),
            structured_method=state.get("structured_method", "function_calling"),
        )
        trace = [
            CouncilTrace(
                node="plan",
                event="completed",
                detail=f"plan_units={len(units)} plans={len(plans)}",
            )
        ]
        trace.extend(
            CouncilTrace(
                node="plan",
                event="fallback" if plan.fallback else "selected",
                detail=(
                    f"plan_unit={unit_id} reviewers="
                    f"{','.join(reviewer.value for reviewer in plan.reviewers)} "
                    f"reason={plan.fallback_reason}"
                ),
            )
            for unit_id, plan in plans.items()
        )
        trace.extend(
            CouncilTrace(node="plan", event="validation", detail=diagnostic)
            for diagnostic in diagnostics
        )
        return {"plan_units": units, "task_plans": plans, "council_trace": trace}

    return _node


def _controlled_plan_node(llm, *, knowledge_topics: int = 4):
    """Controlled 模式的 ReviewPlan：只做 task 级知识路由。"""

    def _node(state: ReviewState) -> dict:
        tasks = list(state.get("review_tasks") or [])
        routes = state.get("task_routes") or {}
        selection = state.get("task_selection")
        selected_ids = set(selection.selected_task_ids) if selection is not None else {
            task.id for task in tasks
        }
        selected_tasks = [task for task in tasks if task.id in selected_ids]
        units = build_plan_units(
            selected_tasks,
            routes,
            review_mode=state.get("review_mode", "large"),
        )
        catalog = KnowledgeCatalog()

        def plan_one(unit):
            return run_knowledge_route(
                plan_unit=unit,
                tasks=selected_tasks,
                llm=llm,
                catalog=catalog,
                max_retries=state.get("max_retries", 3),
                structured_method=state.get("structured_method", "function_calling"),
                max_topics=min(
                    knowledge_topics,
                    state.get("controlled_max_knowledge_topics", knowledge_topics),
                ),
            )

        planned = run_bounded_parallel(units, plan_one, max_workers=8)
        routes_by_unit: dict[str, KnowledgeRoutePlan] = {}
        trace = [
            CouncilTrace(
                node="review_plan",
                event="controlled_knowledge_plan",
                detail=f"plan_units={len(units)}",
            )
        ]
        for unit, outcome in zip(units, planned):
            if outcome is None:
                routes_by_unit[unit.id] = KnowledgeRoutePlan(plan_unit_id=unit.id)
                trace.append(
                    CouncilTrace(node="review_plan", event="knowledge_plan_failed", detail=unit.id)
                )
                continue
            route, diagnostics = outcome
            routes_by_unit[unit.id] = route
            for diagnostic in diagnostics:
                trace.append(
                    CouncilTrace(
                        node="review_plan",
                        event="knowledge_plan_diagnostic",
                        detail=f"plan_unit={unit.id} {diagnostic}",
                    )
                )
        return {
            "plan_units": units,
            "task_plans": {},
            "knowledge_route_plan": routes_by_unit,
            "council_trace": trace,
        }

    return _node


def _locate_controlled_seed(seed: CandidateSeed, task: ReviewTask) -> CandidateSeed:
    """通过统一 CandidateLocator 校验受控候选的位置。

    DirectTriage 已经把明显越界的行归一为 0；这里仍走同一个定位护栏，
    这样 controlled 和 ReAct 候选对 changed-line/deletion-anchor 的合同完全
    一致。受控路径不为定位再发起 LLM 调用。
    """

    issue = DiscoveredIssue(
        file=seed.location_file,
        line=seed.location_line,
        location_snippet="",
        type=seed.issue_type,
        message=seed.claim,
        suggestion=seed.suggestion,
        confidence=seed.confidence,
    )
    located = locate_issues(
        [issue],
        task,
        llm=None,
        structured_method="function_calling",
        max_retries=0,
    )
    if not located.issues:
        return seed.model_copy(update={"location_line": 0})
    resolved_line = located.issues[0].line
    if resolved_line == 0 and seed.location_line in {
        *task.changed_lines,
        *(anchor.anchor_line for anchor in task.deletion_anchors),
    }:
        # Some unit/eval fixtures carry changed_lines but only a patch fragment
        # without a @@ header.  The task contract is still authoritative for
        # that already-validated line; do not erase it merely because the
        # locator has no hunk metadata to parse.
        resolved_line = seed.location_line
    return seed.model_copy(update={"location_line": resolved_line})


def _auto_context_delta_step(
    *,
    seed: CandidateSeed,
    assessment: EvidenceAssessment,
    execution: Any,
    task: ReviewTask,
    allow_partial_proof_enrichment: bool = False,
) -> EvidenceStep | None:
    """Choose one bounded source lookup from symbols visible in graph facts.

    DirectTriage and GraphPlan remain LLM-owned.  This deterministic fallback
    is deliberately domain-neutral: when an assessment says that the bounded
    proof is incomplete, it may read one exact endpoint already returned by a
    successful graph query.  The fallback never guesses a symbol from a name,
    never reads the raw Gateway payload, and never infers a defect.
    """

    if assessment.status not in {
        AssessmentStatus.PARTIAL,
        AssessmentStatus.INDETERMINATE,
        AssessmentStatus.UNRESOLVED,
        AssessmentStatus.NEEDS_EVIDENCE,
    } and not (
        assessment.status is AssessmentStatus.REJECTED
        and bool(assessment.additional_evidence_question.strip())
    ) and not (
        allow_partial_proof_enrichment
        and assessment.status
        in {
            AssessmentStatus.PROVED,
            AssessmentStatus.CANDIDATE,
            AssessmentStatus.REJECTED,
        }
    ):
        return None
    question = seed.graph_question
    if question is None:
        return None
    queried = {
        step.step.subject_ref
        for step in execution.steps
        if step.step.tool == "get_file_content"
    }
    visible: set[str] = set()
    source_available: set[str] = set()
    adjacent: set[str] = set()
    adjacent_lines: dict[str, list[int]] = {}
    expected = set(question.expected_targets)
    hypothesis_text = " ".join(
        value
        for value in (
            seed.claim,
            seed.mechanism,
            seed.impact,
            question.question,
        )
        if value
    ).lower()
    mentioned_targets: set[str] = set()
    for item in execution.steps:
        if item.step.tool not in {"inspect_path", "inspect_structure", "inspect_change_impact"}:
            continue
        payload_text = item.projected_payload
        if not payload_text:
            continue
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for symbol in payload.get("symbols") or ():
            if isinstance(symbol, dict) and str(symbol.get("id", "")).strip():
                symbol_id = str(symbol["id"])
                visible.add(symbol_id)
                # A relationship endpoint may be a library/external symbol
                # without source in the current snapshot.  Only symbols with
                # an explicit source file are eligible for a source lookup;
                # this keeps Delta inside the Gateway's resolvable domain.
                if str(symbol.get("file", "")).strip():
                    source_available.add(symbol_id)
        for relation in payload.get("relationships") or ():
            if not isinstance(relation, dict):
                continue
            source = str(relation.get("sourceId", "")).strip()
            target = str(relation.get("targetId", "")).strip()
            if source:
                visible.add(source)
            if target:
                visible.add(target)
            if question.direction == "downstream" and source == question.subject_ref:
                adjacent.add(target)
                try:
                    adjacent_lines.setdefault(target, []).append(int(relation.get("line", 0)))
                except (TypeError, ValueError):
                    pass
            elif question.direction == "upstream" and target == question.subject_ref:
                adjacent.add(source)
                try:
                    adjacent_lines.setdefault(source, []).append(int(relation.get("line", 0)))
                except (TypeError, ValueError):
                    pass
    # A graph response can contain several valid nearby methods and fields.
    # If the candidate itself names one of those exact endpoints, prefer it
    # for the single bounded source lookup. This uses only provider-declared
    # hypothesis text and already-visible symbol IDs; it does not invent a
    # symbol or decide that the hypothesis is true.
    for symbol_id in visible & source_available:
        simple_name = symbol_id.rsplit("#", 1)[-1].split("(", 1)[0]
        if simple_name and re.search(
            rf"(?<![a-z0-9_]){re.escape(simple_name.lower())}(?![a-z0-9_])",
            hypothesis_text,
        ):
            mentioned_targets.add(symbol_id)
    candidates = [
        symbol_id
        for symbol_id in visible & source_available
        if symbol_id not in queried and symbol_id != question.subject_ref
    ]
    if not candidates:
        return None

    def score(symbol_id: str) -> tuple[int, int, int, str]:
        # A Delta slot is most valuable when it reads the first observable
        # endpoint of the declared path (listener/callback/state consumer),
        # not merely whichever changed-line helper happens to be closest. The
        # classifier is intentionally lexical and domain-neutral; it only
        # orders symbols that the Gateway already returned and never invents a
        # target or treats the name as proof.
        lowered = symbol_id.lower()
        semantic_rank = next(
            (
                index
                for index, tokens in enumerate(
                    (
                        ("listener", "callback", "consumer", "sink"),
                        ("state", "context", "synchronization", "cache"),
                        ("interceptor", "event", "route"),
                    )
                )
                if any(token in lowered for token in tokens)
            ),
            3,
        )
        if symbol_id in expected:
            rank = 0
        elif symbol_id in mentioned_targets:
            rank = 1
        elif semantic_rank < 3:
            rank = 2 + semantic_rank
        elif symbol_id in adjacent:
            rank = 5
        else:
            rank = 6
        # Prefer an endpoint whose call site is closest to the changed line.
        # This is a generic locality tie-breaker: it does not inspect names or
        # infer a defect, but avoids spending the only Delta read on an
        # unrelated helper when several endpoints share the same first hop.
        lines = [line for line in adjacent_lines.get(symbol_id, ()) if line > 0]
        reference_lines = {
            line for line in (*task.changed_lines, seed.location_line) if line > 0
        }
        if reference_lines and lines:
            distance = min(
                abs(line - reference)
                for line in lines
                for reference in reference_lines
            )
        else:
            distance = 10**9
        return rank, distance, len(symbol_id), symbol_id

    subject_ref = min(candidates, key=score)
    return EvidenceStep(
        tool="get_file_content",
        subject_ref=subject_ref,
        purpose="补充读取有界图谱事实中已出现的直接端点源码",
        expected_fact=(
            assessment.additional_evidence_question.strip()
            or seed.mechanism
            or seed.claim
        ),
        required=True,
    )


def _select_delta_work_items(
    *,
    graph_plans: list[ReviewerGraphPlan],
    seeds: dict[str, CandidateSeed],
    execution: Any,
    budget: int,
) -> set[str]:
    """Reserve bounded Delta slots fairly across candidate change locations.

    Delta is a task-level budget, while the fixed reviewers may emit the same
    changed mechanism several times.  Consuming the slots in reviewer/plan
    order lets one reviewer starve a different hunk before its source can be
    read.  Reserve at most one slot per concrete candidate location first,
    then fill remaining slots by stable relevance order.  Relevance uses only
    the seed's declared target/words and symbols already returned by the
    initial graph facts; it is not a semantic verdict and never creates a
    symbol or a new candidate.
    """

    if budget <= 0:
        return set()
    visible: set[str] = set()
    for item in getattr(execution, "steps", ()):
        if item.step.tool not in {
            "inspect_path",
            "inspect_structure",
            "inspect_change_impact",
        }:
            continue
        try:
            payload = json.loads(item.projected_payload or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for symbol in payload.get("symbols") or ():
            if isinstance(symbol, dict) and str(symbol.get("id", "")).strip():
                visible.add(str(symbol["id"]))

    def relevance(item: tuple[str, Any, CandidateSeed]) -> tuple[int, int, int, str]:
        work_item_id, _work_item, seed = item
        question = seed.graph_question
        hypothesis = " ".join(
            value
            for value in (
                seed.claim,
                seed.mechanism,
                seed.impact,
                question.question if question is not None else "",
            )
            if value
        ).lower()
        declared = bool(question is not None and question.expected_targets)
        mentioned = any(
            name
            and re.search(
                rf"(?<![a-z0-9_]){re.escape(name.lower())}(?![a-z0-9_])",
                hypothesis,
            )
            for symbol_id in visible
            for name in (symbol_id.rsplit("#", 1)[-1].split("(", 1)[0],)
        )
        # Prefer explicit/mentioned endpoints, then stable location, then the
        # fixed reviewer order.  A location of zero remains deterministic.
        relevance_rank = 0 if declared else 1 if mentioned else 2
        location = seed.location_line if seed.location_line > 0 else 10**9
        reviewer_rank = {ReviewerKind.BEHAVIOR: 0, ReviewerKind.THREAT_MODEL: 1, ReviewerKind.MAINTAINABILITY: 2}.get(seed.reviewer, 9)
        return relevance_rank, location, reviewer_rank, work_item_id

    entries: list[tuple[str, Any, CandidateSeed]] = []
    for plan in graph_plans:
        for work_item in plan.work_items:
            seed = seeds.get(work_item.seed_id)
            if seed is not None:
                entries.append((work_item.work_item_id, work_item, seed))
    entries.sort(key=relevance)
    selected: list[tuple[str, Any, CandidateSeed]] = []
    location_keys: set[tuple[str, int]] = set()
    for entry in entries:
        if len(selected) >= budget:
            break
        seed = entry[2]
        location_key = (seed.change_unit_id, seed.location_line)
        if location_key in location_keys:
            continue
        selected.append(entry)
        location_keys.add(location_key)
    if len(selected) < budget:
        selected_ids = {entry[0] for entry in selected}
        selected.extend(
            entry for entry in entries
            if entry[0] not in selected_ids
        )
    return {entry[0] for entry in selected[:budget]}


def _approved_replan_symbols(
    *,
    work_item: Any,
    seed: CandidateSeed,
    symbol_context: Any,
) -> tuple[set[str], set[str]]:
    """Return only task-resolved IDs that Delta may use after projection loss.

    GraphPlan has already validated every initial step against this context.
    Reusing those IDs is therefore a bounded continuation of the original
    plan, not a way to expose omitted/raw Gateway symbols to the model.
    """

    context_symbols = {
        symbol.symbol_id: symbol
        for symbol in (symbol_context.symbols if symbol_context is not None else ())
        if symbol.symbol_id
    }
    question = seed.graph_question
    candidate_ids = tuple(
        (
            (question.subject_ref,)
            if question is not None and question.subject_ref
            else ()
        )
        + tuple(step.subject_ref for step in work_item.evidence_steps)
    )
    approved = {
        symbol_id for symbol_id in candidate_ids if symbol_id in context_symbols
    }
    source_approved = {
        symbol_id
        for symbol_id in approved
        if str(context_symbols[symbol_id].kind).upper()
        in {"METHOD", "CONSTRUCTOR", "FIELD", "FRAMEWORK_ENTRYPOINT"}
    }
    return approved, source_approved


def _neutral_seed_from_candidate(seed: CandidateSeed) -> InvestigationSeed:
    """兼容旧 DirectTriage 输出：剥离 claim 后形成中性调查种子。"""

    question = seed.graph_question
    allowed = {
        EvidenceNeed.INSPECT_PATH: ("inspect_path", "get_file_content"),
        EvidenceNeed.INSPECT_CHANGE_IMPACT: (
            "inspect_change_impact",
            "get_file_content",
        ),
        EvidenceNeed.INSPECT_STRUCTURE: ("inspect_structure", "get_file_content"),
    }.get(seed.evidence_need, ("inspect_path", "get_file_content"))
    if question is not None and question.direction == "upstream":
        allowed = ("inspect_change_impact", "get_file_content")
    return InvestigationSeed(
        seed_id=f"investigation-{seed.seed_id}",
        reviewer=seed.reviewer,
        change_unit_id=seed.change_unit_id,
        observed_change=(
            seed.mechanism.strip()
            or "当前变更区间存在需要跨 symbol 核对的行为变化"
        ),
        investigation_question=(
            question.question.strip()
            if question is not None and question.question.strip()
            else "核对当前变更与相关 symbol 之间是否存在直接可观察的行为影响"
        ),
        location_file=seed.location_file,
        location_line=seed.location_line,
        initial_symbol_ids=(
            (question.subject_ref,)
            if question is not None and question.subject_ref
            else ()
        ),
        evidence_need=seed.evidence_need,
        allowed_tools=allowed,
        path_kind=question.path_kind if question is not None else None,
        direction=question.direction if question is not None else None,
        risk_dimension=seed.claim_type,
        confidence=seed.confidence,
    )


def _investigation_candidate(
    finding: Any,
    *,
    task: ReviewTask,
    reviewer: str,
    catalog: Any,
    alias_by_call_id: dict[str, str],
    candidate_index: int,
) -> CandidateIssue | None:
    """将子任务 finding 绑定到真实工具 Artifact；没有证据则不造候选。"""

    selections: list[EvidenceRefSelection] = []
    for observation in finding.observations:
        observation_id = str(observation.observation_id).strip()
        alias = alias_by_call_id.get(observation_id, "")
        if not alias and observation_id in catalog.alias_to_artifact_id:
            alias = observation_id
        if not alias or alias not in catalog.alias_to_artifact_id:
            continue
        role = observation.role
        role_map = {
            "relation": EvidenceRole.REACHABILITY,
            "mechanism": EvidenceRole.MECHANISM,
            "impact": EvidenceRole.IMPACT,
            "counter": EvidenceRole.COUNTER,
            "location": EvidenceRole.LOCATION,
        }
        selections.append(
            EvidenceRefSelection(alias=alias, role=role_map.get(role, EvidenceRole.MECHANISM))
        )
    if not selections:
        return None
    message = finding.claim.strip()
    if finding.impact.strip() and finding.impact.strip() not in message:
        message = f"{message}；{finding.impact.strip()}"
    discovered = DiscoveredIssue(
        file=finding.location_file or task.file,
        line=finding.location_line,
        type=finding.type_hint.strip() or reviewer,
        message=message,
        suggestion=finding.suggestion,
        confidence=1.0,
        evidence_refs=selections,
    )
    candidate = bind_discovered_issue(
        discovered,
        task=task,
        reviewer=reviewer,
        catalog=catalog,
        candidate_index=candidate_index,
    )
    return candidate.model_copy(
        update={
            "mechanism": finding.mechanism,
            "impact": finding.impact,
            "claim_type": finding.type_hint,
        }
    )


def _allocate_subtask_budgets(
    count: int,
    *,
    total_budget: int,
    per_subtask_limit: int,
) -> tuple[int, ...]:
    """Split a task budget fairly.

    The caller caps the number of runnable subtasks to the available budget;
    direct callers that request more slots than calls receive trailing zeroes
    and must apply the same cap before starting a React.
    """

    if count <= 0 or total_budget <= 0 or per_subtask_limit <= 0:
        return tuple(0 for _ in range(max(0, count)))
    base = min(per_subtask_limit, total_budget // count)
    remainder = max(0, total_budget - base * count)
    budgets = [base] * count
    for index in range(count):
        if remainder <= 0 or budgets[index] >= per_subtask_limit:
            continue
        budgets[index] += 1
        remainder -= 1
    return tuple(budgets)


def _controlled_review_node(
    llm,
    tool_client=None,
    *,
    execute_concurrency: int = 3,
    controlled_execution_mode: str = "planned_steps",
    subtask_max_tool_calls: int = 6,
    subtask_max_rounds: int = 4,
    subtask_timeout_seconds: int = 120,
    task_max_tool_calls: int = 24,
    max_subtasks_per_reviewer: int = 4,
    max_subtasks_per_task: int = 12,
):
    """执行受控 DirectTriage → GraphPlan → EvidenceExecutor 链。"""

    def _node(state: ReviewState) -> dict:
        tasks = {task.id: task for task in state.get("review_tasks") or []}
        selection = state.get("task_selection")
        selected_ids = set(selection.selected_task_ids) if selection is not None else set(tasks)
        selected_tasks = [task for task_id, task in tasks.items() if task_id in selected_ids]
        symbol_contexts = state.get("task_symbol_contexts") or {}
        route_plans = state.get("knowledge_route_plan") or {}
        plan_units = state.get("plan_units") or []
        unit_by_task = {
            task_id: unit
            for unit in plan_units
            for task_id in unit.task_ids
        }
        catalog = KnowledgeCatalog()
        knowledge_budget = KnowledgeBudget(
            # The controlled-mode budget is user-configurable.  Do not apply
            # the legacy three-fragment cap here: ReviewPlan already limits
            # the routed topic count, and this value is the final per-task
            # injection budget shared by all three reviewers.
            max_specialized_fragments=max(
                0,
                state.get("controlled_max_knowledge_topics", 4),
            )
        )
        all_candidates: list[CandidateIssue] = []
        all_artifacts: dict[str, EvidenceArtifact] = {}
        all_trace_refs: list[Any] = []
        traces: list[CouncilTrace] = []
        triage_state: dict[str, Any] = {}
        graph_plan_state: dict[str, Any] = {}
        assessment_state: dict[str, Any] = {}
        proof_state: dict[str, Any] = {}

        def shared_knowledge(task: ReviewTask) -> str:
            unit = unit_by_task.get(task.id)
            route = route_plans.get(unit.id) if unit is not None else None
            task_route = next(
                (item for item in (route.task_routes if route else ()) if item.task_id == task.id),
                None,
            )
            bundle = select_shared_knowledge(
                requested_topics=task_route.knowledge_topics if task_route else (),
                catalog=catalog,
                budget=knowledge_budget,
                task_id=task.id,
            )
            return bundle.rendered_text

        scope = _scope_plan(state)
        for task in selected_tasks:
            task_candidate_start = len(all_candidates)
            context = symbol_contexts.get(task.id)
            scoped_patch = scope.scoped_patch(task.patch)
            scoped_task = task.model_copy(
                update={
                    "patch": scoped_patch,
                    "patch_complete": task.patch_complete
                    and scoped_patch == task.patch,
                }
            )
            triage_jobs = [
                (reviewer.source_agent, reviewer)
                for reviewer in DEFAULT_REVIEWERS
            ]

            def triage_one(item):
                reviewer_name, _reviewer = item
                return run_direct_triage(
                    reviewer=ReviewerKind(reviewer_name),
                    task=scoped_task,
                    symbol_context=context,
                    llm=llm,
                    diff_summary=state.get("diff_summary", ""),
                    task_knowledge=shared_knowledge(task),
                    max_retries=state.get("max_retries", 3),
                    structured_method=state.get("structured_method", "function_calling"),
                    max_seeds_per_change_unit=state.get("controlled_max_seeds_per_change_unit", 4),
                    max_seeds_per_reviewer=state.get("controlled_max_seeds_per_reviewer", 4),
                )

            triage_results = run_bounded_parallel(triage_jobs, triage_one, max_workers=3)
            seeds_by_reviewer: dict[str, list[CandidateSeed]] = {}
            investigation_seeds_by_reviewer: dict[str, list[InvestigationSeed]] = {}
            for reviewer_config, outcome in zip((item[1] for item in triage_jobs), triage_results):
                if outcome is None:
                    traces.append(CouncilTrace(node="direct_triage", event="reviewer_failed", detail=f"task={task.id} reviewer={reviewer_config.source_agent}"))
                    continue
                result, diagnostics = outcome
                if result is None:
                    traces.extend(CouncilTrace(node="direct_triage", event="diagnostic", detail=f"task={task.id} reviewer={reviewer_config.source_agent} {diagnostic}") for diagnostic in diagnostics)
                    continue
                seeds = [_locate_controlled_seed(seed, task) for seed in result.issues]
                result = result.model_copy(update={"issues": tuple(seeds)})
                triage_state[f"{task.id}:{reviewer_config.source_agent}"] = result
                seeds_by_reviewer[reviewer_config.source_agent] = seeds
                investigation_seeds_by_reviewer[reviewer_config.source_agent] = list(
                    result.investigation_seeds
                )
                traces.extend(CouncilTrace(node="direct_triage", event="diagnostic", detail=f"task={task.id} reviewer={reviewer_config.source_agent} {diagnostic}") for diagnostic in diagnostics)
                traces.append(CouncilTrace(node="direct_triage", event="completed", detail=f"task={task.id} reviewer={reviewer_config.source_agent} seeds={len(seeds)}"))

            task_catalog = EvidenceCatalogBuilder().build_initial(
                task=task,
                symbol_context=context,
                reviewer="controlled",
                revision=state.get("evidence_revision", ""),
            )
            graph_plans_for_task: list[Any] = []
            graph_seeds_by_id: dict[str, CandidateSeed] = {}
            graph_plan_jobs: list[tuple[Any, ReviewerKind, tuple[CandidateSeed, ...]]] = []
            for reviewer_config in DEFAULT_REVIEWERS:
                reviewer_kind = ReviewerKind(reviewer_config.source_agent)
                reviewer_seeds = tuple(seeds_by_reviewer.get(reviewer_config.source_agent, ()))
                direct_seeds = tuple(seed for seed in reviewer_seeds if route_seed(seed) == "direct_proven")
                graph_seeds = tuple(seed for seed in reviewer_seeds if route_seed(seed) == "graph_required")
                for index, seed in enumerate(direct_seeds, start=1):
                    all_candidates.append(
                        candidate_from_seed(
                            seed=seed,
                            task=task,
                            catalog=task_catalog,
                            reviewer=reviewer_config.source_agent,
                            candidate_index=index,
                        )
                    )
                if not graph_seeds:
                    continue
                graph_seeds_by_id.update({seed.seed_id: seed for seed in graph_seeds})

                # GraphPlan calls are independent: they only consume the
                # immutable triage output and do not mutate the evidence
                # catalog or the shared execution budget.  Run the three
                # reviewer plans concurrently, but collect them in the
                # stable DEFAULT_REVIEWERS order below.
                graph_plan_jobs.append((reviewer_config, reviewer_kind, graph_seeds))

            if controlled_execution_mode == "subtask_react":
                # New path: GraphPlan creates neutral bounded subtasks and each
                # subtask runs one local React.  The legacy fixed-step executor
                # below remains available under planned_steps until replay
                # validation authorizes its removal.
                # The three fixed reviewers triage independently, so equivalent
                # concerns can arrive with different wording and seed IDs.  Do
                # the semantic-independent grouping before GraphPlan.  A group
                # keeps one representative seed for execution and records all
                # reviewer/seed provenance in the trace; it therefore cannot
                # multiply the React/tool budget while still preserving which
                # reviewers observed the concern.
                neutral_by_reviewer: dict[ReviewerKind, list[InvestigationSeed]] = {}
                for reviewer_config in DEFAULT_REVIEWERS:
                    reviewer_name = reviewer_config.source_agent
                    reviewer_kind = ReviewerKind(reviewer_name)
                    neutral = list(investigation_seeds_by_reviewer.get(reviewer_name, ()))
                    existing_ids = {seed.seed_id for seed in neutral}
                    for seed in seeds_by_reviewer.get(reviewer_name, ()):
                        if route_seed(seed) != "graph_required":
                            continue
                        converted = _neutral_seed_from_candidate(seed)
                        if converted.seed_id not in existing_ids:
                            neutral.append(converted)
                            existing_ids.add(converted.seed_id)
                    neutral_by_reviewer[reviewer_kind] = neutral

                grouped_seeds = group_investigation_seeds(neutral_by_reviewer)
                for group in grouped_seeds:
                    if len(group.seed_ids) > 1:
                        traces.append(CouncilTrace(
                            node="graph_plan",
                            event="subtask_seed_merged",
                            detail=(
                                f"task={task.id} representative={group.seed.seed_id} "
                                f"merged={','.join(group.seed_ids)} "
                                f"reviewers={','.join(item.value for item in group.reviewers)}"
                            ),
                        ))

                grouped_by_reviewer: dict[ReviewerKind, list[InvestigationSeed]] = {}
                for group in grouped_seeds:
                    grouped_by_reviewer.setdefault(group.seed.reviewer, []).append(group.seed)

                subtask_plan_jobs: list[tuple[Any, ReviewerKind, tuple[InvestigationSeed, ...]]] = []
                reviewer_by_kind = {
                    ReviewerKind(config.source_agent): config
                    for config in DEFAULT_REVIEWERS
                }
                for reviewer_kind, neutral_values in grouped_by_reviewer.items():
                    reviewer_config = reviewer_by_kind[reviewer_kind]
                    reviewer_limit = max(0, max_subtasks_per_reviewer)
                    neutral = neutral_values[:reviewer_limit]
                    if len(neutral_values) > reviewer_limit:
                        traces.append(CouncilTrace(
                            node="graph_plan",
                            event="subtask_seed_limit",
                            detail=(
                                f"task={task.id} reviewer={reviewer_config.source_agent} "
                                f"omitted={len(neutral_values) - len(neutral)}"
                            ),
                        ))
                    if neutral:
                        subtask_plan_jobs.append((reviewer_config, reviewer_kind, tuple(neutral)))

                def subtask_plan_one(job):
                    reviewer_config, reviewer_kind, neutral = job
                    plan, diagnostics = run_subtask_plan(
                        reviewer=reviewer_kind,
                        task=scoped_task,
                        seeds=neutral,
                        symbol_context=context,
                        llm=llm,
                        max_retries=state.get("max_retries", 3),
                        structured_method=state.get("structured_method", "function_calling"),
                        max_tool_calls=subtask_max_tool_calls,
                        max_rounds=subtask_max_rounds,
                        max_subtasks=max_subtasks_per_reviewer,
                        max_path_depth=state.get("controlled_max_path_depth", 3),
                        enabled_tools=state.get("enabled_tools"),
                    )
                    return reviewer_config, plan, diagnostics

                subtask_plan_results = run_bounded_parallel(
                    subtask_plan_jobs,
                    subtask_plan_one,
                    max_workers=min(
                        max(1, execute_concurrency), len(subtask_plan_jobs)
                    ) if subtask_plan_jobs else 1,
                )
                planned_subtasks: list[tuple[Any, SubtaskInstruction]] = []
                for job, result in zip(subtask_plan_jobs, subtask_plan_results):
                    reviewer_config, _reviewer_kind, _neutral = job
                    if result is None:
                        traces.append(CouncilTrace(
                            node="graph_plan",
                            event="failed",
                            detail=f"task={task.id} reviewer={reviewer_config.source_agent} reason=worker_failed",
                        ))
                        continue
                    _config, plan, diagnostics = result
                    graph_plan_state[f"{task.id}:{reviewer_config.source_agent}"] = plan
                    traces.extend(
                        CouncilTrace(
                            node="graph_plan",
                            event="diagnostic",
                            detail=f"task={task.id} reviewer={reviewer_config.source_agent} {diagnostic}",
                        )
                        for diagnostic in diagnostics
                    )
                    if any(
                        # A provider may omit one seed while the deterministic
                        # validator installs its bounded fallback instruction.
                        # That is a repaired protocol variation, not a task
                        # failure.  Only the colon form means seeds remain
                        # unplanned after fallback/capacity checks.
                        diagnostic.startswith("subtask_plan_missing_seeds:")
                        or diagnostic.startswith("subtask_unknown_symbol")
                        or diagnostic.startswith("subtask_no_allowed_tool")
                        for diagnostic in diagnostics
                    ):
                        traces.append(CouncilTrace(
                            node="graph_plan",
                            event="task_review_failed",
                            detail=(
                                f"task={task.id} reviewer={reviewer_config.source_agent} "
                                "reason=seed_not_planned"
                            ),
                        ))
                    planned_subtasks.extend(
                        (reviewer_config, item)
                        for item in plan.subtasks
                    )

                task_budget = max(0, task_max_tool_calls)
                # Never start a React that cannot make even one evidence call.
                # The task-level budget is a hard upper bound, so excess plans
                # are recorded as bounded omissions instead of failing one by
                # one with a zero-call budget.
                max_runnable_subtasks = (
                    min(
                        max(0, max_subtasks_per_task),
                        task_budget,
                    )
                    if subtask_max_tool_calls > 0
                    else 0
                )
                if len(planned_subtasks) > max_runnable_subtasks:
                    traces.append(CouncilTrace(
                        node="graph_plan",
                        event="subtask_task_limit",
                        detail=(
                            f"task={task.id} omitted="
                            f"{len(planned_subtasks) - max_runnable_subtasks} "
                            f"limit={max_runnable_subtasks}"
                        ),
                    ))
                planned_subtasks = planned_subtasks[:max_runnable_subtasks]
                if tool_client is None:
                    traces.append(CouncilTrace(
                        node="controlled_review",
                        event="task_review_failed",
                        detail=f"task={task.id} reason=tool_client_unavailable",
                    ))
                    all_artifacts.update(task_catalog.artifacts)
                    continue

                coordinator = DiscoveryToolCoordinator()
                focus = graph_projection_focus(task, context)
                # Divide the task budget before parallel execution so the
                # aggregate upper bound is deterministic rather than relying
                # on a post-hoc truncation of evidence.  Distribute a
                # remainder to the first stable subtasks instead of silently
                # leaving usable calls unused.
                subtask_budgets = _allocate_subtask_budgets(
                    len(planned_subtasks),
                    total_budget=task_budget,
                    per_subtask_limit=subtask_max_tool_calls,
                )

                def run_subtask(indexed_item):
                    subtask_index, item = indexed_item
                    reviewer_config, instruction = item
                    per_subtask_budget = subtask_budgets[subtask_index]
                    # Each React owns its local trace/allowed-symbol set while
                    # the coordinator still shares successful HTTP results.
                    # This prevents parallel subtasks from attributing one
                    # another's observations to the wrong finding.
                    coordinated_client = CoordinatedDiscoveryToolClient(
                        tool_client,
                        coordinator,
                        projection_focus=focus,
                        lossless_payload=True,
                        max_tool_calls=per_subtask_budget,
                        max_path_depth=state.get("controlled_max_path_depth", 3),
                        allowed_path_kind=instruction.path_kind,
                        initial_symbol_ids=set(instruction.initial_symbol_ids),
                        symbol_catalog_ids=tuple(
                            symbol.symbol_id
                            for symbol in (context.symbols if context is not None else ())
                        ),
                    )
                    engine = SubtaskReactEngine(
                        coordinated_client,
                        max_tool_calls=per_subtask_budget,
                        max_rounds=min(subtask_max_rounds, instruction.max_rounds),
                        timeout_seconds=subtask_timeout_seconds,
                    )
                    outcome = engine.run(
                        llm,
                        task=scoped_task,
                        symbol_context=context,
                        instruction=instruction.model_copy(
                            update={"max_tool_calls": per_subtask_budget}
                        ),
                        structured_method=state.get("structured_method", "function_calling"),
                        max_retries=state.get("max_retries", 3),
                    )
                    return subtask_index, reviewer_config, instruction, outcome, coordinated_client

                subtask_results = run_bounded_parallel(
                    list(enumerate(planned_subtasks)),
                    run_subtask,
                    max_workers=min(
                        max(1, execute_concurrency), len(planned_subtasks)
                    ) if planned_subtasks else 1,
                )
                valid_subtask_results = [
                    result for result in subtask_results if result is not None
                ]
                ordered_records = tuple(
                    record
                    for _subtask_index, _reviewer_config, _instruction, _outcome, client
                    in valid_subtask_results
                    for record in client.trace_records
                )
                capture = capture_tool_records(task_catalog, ordered_records)
                all_trace_refs.extend(capture.trace_refs)
                all_artifacts.update(capture.catalog.artifacts)
                alias_by_call_id = {
                    artifact.call_id: alias
                    for alias, artifact_id in capture.catalog.alias_to_artifact_id.items()
                    if (artifact := capture.catalog.artifacts.get(artifact_id)) is not None
                    and artifact.call_id
                }
                for _subtask_index, reviewer_config, instruction, outcome, _client in valid_subtask_results:
                    traces.extend(
                        CouncilTrace(
                            node="execute",
                            event=event,
                            detail=(
                                f"task={task.id} subtask={instruction.subtask_id} "
                                f"reviewer={reviewer_config.source_agent}"
                            ),
                        )
                        for event in outcome.events
                    )
                    if outcome.result is None or outcome.result.outcome != "findings":
                        if outcome.status == "failed":
                            traces.append(CouncilTrace(
                                node="execute",
                                event="task_review_failed",
                                detail=(
                                    f"task={task.id} subtask={instruction.subtask_id} "
                                    f"reason={outcome.reason or outcome.status}"
                                ),
                            ))
                        elif outcome.status == "inconclusive":
                            # A bounded investigation can legitimately end
                            # without enough facts (for example a partial
                            # page or an exhausted local budget).  This is an
                            # evidence gap, not a failed task; keep it out of
                            # the task-failure metric so one inconclusive
                            # subtask cannot make the whole review look broken.
                            traces.append(CouncilTrace(
                                node="execute",
                                event="subtask_inconclusive",
                                detail=(
                                    f"task={task.id} subtask={instruction.subtask_id} "
                                    f"reason={outcome.reason or outcome.status}"
                                ),
                            ))
                        if outcome.reason:
                            traces.append(CouncilTrace(
                                node="execute",
                                event="subtask_limited",
                                detail=f"task={task.id} subtask={instruction.subtask_id} reason={outcome.reason}",
                            ))
                        continue
                    local_alias_by_call_id = {
                        local_alias: alias_by_call_id.get(call_id, "")
                        for local_alias, call_id in _client.observation_aliases.items()
                        if alias_by_call_id.get(call_id, "")
                    }
                    finding_aliases = dict(alias_by_call_id)
                    finding_aliases.update(local_alias_by_call_id)
                    for finding in outcome.result.findings:
                        candidate = _investigation_candidate(
                            finding,
                            task=task,
                            reviewer=reviewer_config.source_agent,
                            catalog=capture.catalog,
                            alias_by_call_id=finding_aliases,
                            candidate_index=len(all_candidates) + 1,
                        )
                        if candidate is None:
                            traces.append(CouncilTrace(
                                node="execute",
                                event="finding_dropped_no_bound_evidence",
                                detail=f"task={task.id} subtask={instruction.subtask_id}",
                            ))
                            traces.append(CouncilTrace(
                                node="execute",
                                event="task_review_failed",
                                detail=(
                                    f"task={task.id} subtask={instruction.subtask_id} "
                                    "reason=finding_without_bound_evidence"
                                ),
                            ))
                            continue
                        all_candidates.append(candidate)
                all_artifacts.update(task_catalog.artifacts)
                traces.append(CouncilTrace(
                    node="controlled_review",
                    event="subtask_react_completed",
                    detail=f"task={task.id} subtasks={len(planned_subtasks)} candidates={len(all_candidates) - task_candidate_start}",
                ))
                task_candidate_limit = state.get("controlled_max_seeds_per_task", 12)
                if len(all_candidates) - task_candidate_start > task_candidate_limit:
                    del all_candidates[task_candidate_start + task_candidate_limit :]
                    traces.append(CouncilTrace(
                        node="controlled_review",
                        event="seed_task_limit",
                        detail=f"task={task.id} limit={task_candidate_limit}",
                    ))
                continue

            def graph_plan_one(job):
                reviewer_config, reviewer_kind, graph_seeds = job
                graph_plan, diagnostics = run_graph_plan(
                    reviewer=reviewer_kind,
                    task_id=task.id,
                    seeds=graph_seeds,
                    symbol_context=context,
                    llm=llm,
                    max_retries=state.get("max_retries", 3),
                    structured_method=state.get("structured_method", "function_calling"),
                    max_path_depth=state.get("controlled_max_path_depth", 3),
                    enabled_tools=state.get("enabled_tools"),
                )
                return reviewer_config, graph_plan, diagnostics

            graph_plan_results = run_bounded_parallel(
                graph_plan_jobs,
                graph_plan_one,
                max_workers=min(3, len(graph_plan_jobs)),
            )
            for job, result in zip(graph_plan_jobs, graph_plan_results):
                reviewer_config, reviewer_kind, _graph_seeds = job
                if result is None:
                    # Preserve the pre-parallel failure semantics: an
                    # unexpected worker exception gets one ordered retry
                    # instead of silently dropping this reviewer's graph
                    # candidates and reducing recall.
                    try:
                        result = graph_plan_one(job)
                        traces.append(
                            CouncilTrace(
                                node="graph_plan",
                                event="retry_after_parallel_failure",
                                detail=(
                                    f"task={task.id} reviewer={reviewer_config.source_agent}"
                                ),
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        traces.append(
                            CouncilTrace(
                                node="graph_plan",
                                event="failed",
                                detail=(
                                    f"task={task.id} reviewer={reviewer_config.source_agent} "
                                    f"reason=parallel_worker_failed:{type(exc).__name__}"
                                ),
                            )
                        )
                        continue
                _reviewer_config, graph_plan, diagnostics = result
                graph_plan_state[f"{task.id}:{reviewer_config.source_agent}"] = graph_plan
                traces.extend(
                    CouncilTrace(
                        node="graph_plan",
                        event="diagnostic",
                        detail=f"task={task.id} reviewer={reviewer_config.source_agent} {diagnostic}",
                    )
                    for diagnostic in diagnostics
                )
                if graph_plan.work_items:
                    graph_plans_for_task.append(graph_plan)
            if graph_plans_for_task:
                execution = ControlledEvidenceExecutor(
                    tool_client=tool_client,
                    task=task,
                    symbol_context=context,
                    revision=state.get("evidence_revision", ""),
                    enabled_tools=state.get("enabled_tools"),
                    initial_budget=state.get("controlled_initial_tool_budget", 6),
                    max_path_depth=state.get("controlled_max_path_depth", 3),
                    execute_concurrency=state.get(
                        "controlled_execute_concurrency", execute_concurrency
                    ),
                    seed_by_id=graph_seeds_by_id,
                ).execute(tuple(graph_plans_for_task))
                all_trace_refs.extend(execution.trace_refs)
                all_artifacts.update(execution.artifacts)
                delta_budget = max(0, int(state.get("controlled_delta_tool_budget", 2)))
                delta_used_count = 0
                replanned_work_items: set[str] = set()

                # Initial assessment calls are also independent.  Compute all
                # deterministic proof matches first, then run the LLM
                # assessments concurrently.  Replan/delta execution remains
                # in the stable reviewer order because it consumes the shared
                # per-task delta budget and extends the evidence catalog.
                assessment_jobs: list[tuple[Any, ReviewerKind, dict[str, CandidateSeed], dict[str, Any]]] = []
                for graph_plan in graph_plans_for_task:
                    reviewer_kind = ReviewerKind(graph_plan.reviewer)
                    plan_seeds = {
                        seed.seed_id: seed
                        for seed in graph_seeds_by_id.values()
                        if seed.reviewer is reviewer_kind
                    }
                    plan_proofs: dict[str, Any] = {}
                    for work_item in graph_plan.work_items:
                        seed_for_work = plan_seeds.get(work_item.seed_id)
                        if seed_for_work is None:
                            continue
                        proof = match_execution_proof(
                            work_item=work_item,
                            seed=seed_for_work,
                            steps=tuple(
                                step
                                for step in execution.steps
                                if step.work_item_id == work_item.work_item_id
                            ),
                            subject_symbol_id=(
                                seed_for_work.graph_question.subject_ref
                                if seed_for_work.graph_question
                                else ""
                            ),
                        )
                        plan_proofs[work_item.work_item_id] = proof
                        proof_state[work_item.work_item_id] = proof
                    assessment_jobs.append(
                        (graph_plan, reviewer_kind, plan_seeds, plan_proofs)
                    )

                def assessment_one(job):
                    graph_plan, reviewer_kind, plan_seeds, plan_proofs = job
                    assessments, assessment_diagnostics = run_evidence_assessment(
                        reviewer=reviewer_kind,
                        task_id=task.id,
                        work_items=tuple(graph_plan.work_items),
                        seeds=plan_seeds,
                        execution=execution,
                        proof_matches=plan_proofs,
                        llm=llm,
                        max_retries=state.get("max_retries", 3),
                        structured_method=state.get("structured_method", "function_calling"),
                    )
                    return assessments, assessment_diagnostics

                assessment_results = run_bounded_parallel(
                    assessment_jobs,
                    assessment_one,
                    max_workers=min(3, len(assessment_jobs)),
                )
                for job, result in zip(assessment_jobs, assessment_results):
                    graph_plan, reviewer_kind, plan_seeds, plan_proofs = job
                    if result is None:
                        # The normal assessment function already contains
                        # provider/protocol fallbacks.  This extra ordered
                        # retry only covers an exception escaping the worker
                        # wrapper, keeping parallelism from changing the
                        # candidate recall contract.
                        try:
                            result = assessment_one(job)
                            traces.append(
                                CouncilTrace(
                                    node="evidence_assessment",
                                    event="retry_after_parallel_failure",
                                    detail=f"task={task.id} reviewer={reviewer_kind.value}",
                                )
                            )
                        except Exception as exc:  # noqa: BLE001
                            assessments = {}
                            assessment_diagnostics = (
                                f"assessment_parallel_worker_failed:{type(exc).__name__}",
                            )
                        else:
                            assessments, assessment_diagnostics = result
                    else:
                        assessments, assessment_diagnostics = result
                    # A bounded Delta step is permitted per task. It may
                    # reference only a symbol returned by the initial graph facts;
                    # no recursive discovery or fuzzy symbol resolution is allowed.
                    for work_item in graph_plan.work_items:
                        initial_assessment = assessments.get(work_item.work_item_id)
                        if (
                            delta_used_count >= delta_budget
                            or initial_assessment is None
                        ):
                            continue
                        initial_proof = plan_proofs.get(work_item.work_item_id)
                        if graph_replan_reason(initial_assessment, initial_proof) is None:
                            # Definitive negative/accepted states stay
                            # terminal. Evidence gaps (including
                            # indeterminate/unresolved proof) are eligible
                            # for exactly one bounded Delta query; the helper
                            # keeps this policy identical to run_graph_replan.
                            continue
                        # The first Delta decision belongs to GraphPlan, not
                        # to the assessment model.  Assessment only describes
                        # the missing fact; Graph Replan chooses one
                        # executable step from this WorkItem's visible facts.
                        work_item_execution = replace(
                            execution,
                            steps=tuple(
                                step_execution
                                for step_execution in execution.steps
                                if step_execution.work_item_id == work_item.work_item_id
                            ),
                        )
                        visible_for_item = visible_symbol_ids(work_item_execution)
                        source_for_item = visible_source_symbol_ids(work_item_execution)
                        # A projection may legitimately omit every endpoint
                        # (for example after path truncation), but the
                        # WorkItem's subject was already validated against the
                        # task's resolved symbol context by GraphPlan.  Keep
                        # those originally approved IDs available for one
                        # bounded Delta query; never unlock raw Gateway IDs.
                        approved_replan_symbols, approved_source_symbols = (
                            _approved_replan_symbols(
                                work_item=work_item,
                                seed=plan_seeds[work_item.seed_id],
                                symbol_context=context,
                            )
                        )
                        if approved_replan_symbols - visible_for_item:
                            visible_for_item.update(approved_replan_symbols)
                            traces.append(
                                CouncilTrace(
                                    node="graph_replan",
                                    event="diagnostic",
                                    detail=(
                                        f"task={task.id} work_item={work_item.work_item_id} "
                                        "graph_replan_subject_seeded"
                                    ),
                                )
                            )
                        source_for_item.update(approved_source_symbols)
                        executed_for_item = {
                            (
                                step_execution.step.tool,
                                step_execution.step.subject_ref,
                                step_execution.step.path_kind or "",
                                step_execution.step.max_depth,
                            )
                            for step_execution in work_item_execution.steps
                        }
                        replan_step, replan_diagnostics = run_graph_replan(
                            reviewer=reviewer_kind,
                            task_id=task.id,
                            seed=plan_seeds[work_item.seed_id],
                            work_item=work_item,
                            assessment=initial_assessment,
                            proof=initial_proof,
                            visible_symbols=visible_for_item,
                            visible_source_symbols=source_for_item,
                            executed_queries=executed_for_item,
                            llm=llm,
                            max_retries=state.get("max_retries", 3),
                            structured_method=state.get("structured_method", "function_calling"),
                            max_path_depth=state.get("controlled_max_path_depth", 3),
                            enabled_tools=(
                                set(state["enabled_tools"])
                                if state.get("enabled_tools") is not None
                                else None
                            ),
                        )
                        traces.extend(
                            CouncilTrace(
                                node="graph_replan",
                                event="diagnostic",
                                detail=(
                                    f"task={task.id} work_item={work_item.work_item_id} "
                                    f"{diagnostic}"
                                ),
                            )
                            for diagnostic in replan_diagnostics
                        )
                        if replan_step is not None:
                            traces.append(
                                CouncilTrace(
                                    node="graph_replan",
                                    event="completed",
                                    detail=(
                                        f"task={task.id} work_item={work_item.work_item_id} "
                                        f"tool={replan_step.tool} subject={replan_step.subject_ref}"
                                    ),
                                )
                            )
                            replanned_work_items.add(work_item.work_item_id)
                        if replan_step is None:
                            traces.append(
                                CouncilTrace(
                                    node="graph_replan",
                                    event="rejected",
                                    detail=(
                                        f"task={task.id} work_item={work_item.work_item_id} "
                                        "reason=no_valid_plan"
                                    ),
                                )
                            )
                            continue
                        delta_step = replan_step
                        delta_step = delta_step.model_copy(update={"depends_on": ()})
                        allowed_delta_symbols = visible_for_item
                        if delta_step.tool == "get_file_content":
                            allowed_delta_symbols = source_for_item
                        delta_item = work_item.model_copy(
                            update={"evidence_steps": (delta_step,)}
                        )
                        delta_execution = ControlledEvidenceExecutor(
                            tool_client=tool_client,
                            task=task,
                            symbol_context=context,
                            revision=state.get("evidence_revision", ""),
                            enabled_tools=state.get("enabled_tools"),
                            initial_budget=state.get("controlled_delta_tool_budget", 2),
                            max_path_depth=state.get("controlled_max_path_depth", 3),
                            execute_concurrency=state.get(
                                "controlled_execute_concurrency", execute_concurrency
                            ),
                            extra_symbol_ids=allowed_delta_symbols,
                        ).execute((
                            ReviewerGraphPlan(
                                reviewer=reviewer_kind,
                                task_id=task.id,
                                work_items=(delta_item,),
                            ),
                        ), catalog=execution.catalog)
                        execution = replace(
                            execution,
                            catalog=delta_execution.catalog,
                            artifacts={**execution.artifacts, **delta_execution.artifacts},
                            trace_refs=execution.trace_refs + delta_execution.trace_refs,
                            steps=execution.steps + delta_execution.steps,
                            diagnostics=execution.diagnostics + delta_execution.diagnostics,
                        )
                        all_trace_refs.extend(delta_execution.trace_refs)
                        all_artifacts.update(delta_execution.artifacts)
                        delta_seed = plan_seeds[work_item.seed_id]
                        if delta_seed.graph_question is None:
                            continue
                        delta_proof = match_execution_proof(
                            work_item=work_item,
                            seed=delta_seed,
                            steps=tuple(
                                step
                                for step in execution.steps
                                if step.work_item_id == work_item.work_item_id
                            ),
                            subject_symbol_id=delta_seed.graph_question.subject_ref,
                        )
                        plan_proofs[work_item.work_item_id] = delta_proof
                        proof_state[work_item.work_item_id] = delta_proof
                        delta_assessments, delta_diagnostics = run_evidence_assessment(
                            reviewer=reviewer_kind,
                            task_id=task.id,
                            work_items=(work_item,),
                            seeds={work_item.seed_id: delta_seed},
                            execution=execution,
                            proof_matches={work_item.work_item_id: delta_proof},
                            llm=llm,
                            max_retries=state.get("max_retries", 3),
                            structured_method=state.get("structured_method", "function_calling"),
                        )
                        assessments.update(delta_assessments)
                        assessment_diagnostics = (*assessment_diagnostics, *delta_diagnostics)
                        delta_used_count += 1
                        traces.append(
                            CouncilTrace(
                                node="delta_execute",
                                event="completed",
                                detail=f"task={task.id} work_item={work_item.work_item_id}",
                            )
                        )
                    traces.extend(
                        CouncilTrace(
                            node="evidence_assessment",
                            event="diagnostic",
                            detail=f"task={task.id} reviewer={reviewer_kind.value} {diagnostic}",
                        )
                        for diagnostic in assessment_diagnostics
                    )
                    assessment_state.update(assessments)
                    for work_item in graph_plan.work_items:
                        seed_for_work = plan_seeds.get(work_item.seed_id)
                        proof_for_work = plan_proofs.get(work_item.work_item_id)
                        if seed_for_work is None:
                            traces.append(
                                CouncilTrace(
                                    node="controlled_review",
                                    event="candidate_gate_skip",
                                    detail=(
                                        f"task={task.id} work_item={work_item.work_item_id} "
                                        "reason=seed_not_bound"
                                    ),
                                )
                            )
                            continue
                        if proof_for_work is None:
                            traces.append(
                                CouncilTrace(
                                    node="controlled_review",
                                    event="candidate_gate_skip",
                                    detail=(
                                        f"task={task.id} work_item={work_item.work_item_id} "
                                        "reason=proof_not_bound"
                                    ),
                                )
                            )
                            continue
                        assessment = assessments.get(work_item.work_item_id)
                        final_status, finalize_reason = finalize_evidence_assessment(
                            assessment,
                            proof_for_work,
                        )
                        if final_status is not AssessmentStatus.CANDIDATE:
                            reason = (
                                "replan_required_or_budget_exhausted"
                                if finalize_reason in {
                                    "assessment_missing",
                                    "evidence_incomplete",
                                }
                                else finalize_reason
                            )
                            traces.append(
                                CouncilTrace(
                                    node="controlled_review",
                                    event="candidate_dropped_after_replan",
                                    detail=(
                                        f"task={task.id} work_item={work_item.work_item_id} "
                                        f"reason={reason} "
                                        f"replanned={work_item.work_item_id in replanned_work_items} "
                                        f"assessment={getattr(assessment, 'status', None)} "
                                        f"proof={proof_for_work.status}"
                                    ),
                                )
                            )
                            continue
                        # CandidateFinalize deliberately changes only the
                        # internal status; the original claim and evidence
                        # references remain owned by EvidenceAssessment.
                        assessment = assessment.model_copy(
                            update={"status": AssessmentStatus.CANDIDATE}
                        )
                        all_candidates.append(
                            candidate_from_seed(
                                seed=seed_for_work,
                                task=task,
                                catalog=execution.catalog,
                                reviewer=reviewer_kind.value,
                                candidate_index=len(all_candidates) + 1,
                                assessment=assessment,
                            )
                        )
            task_candidate_limit = state.get("controlled_max_seeds_per_task", 12)
            if len(all_candidates) - task_candidate_start > task_candidate_limit:
                del all_candidates[task_candidate_start + task_candidate_limit :]
                traces.append(
                    CouncilTrace(
                        node="controlled_review",
                        event="seed_task_limit",
                        detail=f"task={task.id} limit={task_candidate_limit}",
                    )
                )
            all_artifacts.update(task_catalog.artifacts)

        deduped_candidates, collapsed_count = collapse_candidate_duplicates(all_candidates)
        if collapsed_count:
            traces.append(
                CouncilTrace(
                    node="controlled_review",
                    event="candidate_duplicates_collapsed",
                    detail=f"collapsed={collapsed_count} remaining={len(deduped_candidates)}",
                )
            )
        traces.append(CouncilTrace(node="controlled_review", event="completed", detail=f"candidates={len(deduped_candidates)} tools={len(all_trace_refs)}"))
        # CandidateIssue's explanatory fields are deliberately excluded from
        # its generic ``model_dump`` (they are not product Issue fields). A
        # LangGraph checkpoint can consequently carry the candidate shell but
        # lose the mechanism/impact context before EvidenceJudge runs. Keep a
        # separate context map in graph state and rehydrate it at dossier
        # assembly; this is transport preservation, not a semantic decision
        # or an issue-specific rule.
        candidate_contexts = {
            candidate.id: {
                key: str(value)
                for key in (
                    "mechanism",
                    "impact",
                    "impact_locale",
                    "claim_type",
                    "evidence_observation",
                )
                if (value := getattr(candidate, key, ""))
            }
            for candidate in deduped_candidates
            if any(
                getattr(candidate, key, "")
                for key in (
                    "mechanism",
                    "impact",
                    "impact_locale",
                    "claim_type",
                    "evidence_observation",
                )
            )
        }
        return {
            "raw_candidate_issues": deduped_candidates,
            "candidate_issues": collect_candidate_reducer([], deduped_candidates),
            "evidence_artifacts": all_artifacts,
            "tool_trace_records": all_trace_refs,
            "controlled_triage": triage_state,
            "controlled_graph_plans": graph_plan_state,
            "controlled_assessments": assessment_state,
            "controlled_proof_matches": proof_state,
            "controlled_candidate_contexts": candidate_contexts,
            "council_trace": traces,
        }

    return _node


def _direct_task_review_node(llm):
    """执行 DirectGate 判定的 task，跳过取证但仍交给 DirectJudge 定级。"""
    prompt_dir = Path(__file__).resolve().parents[1] / "prompts"

    def _node(state: ReviewState) -> dict:
        routes = state.get("task_routes") or {}
        tasks = [
            task for task in state.get("review_tasks") or []
            if routes.get(task.id) is not None and routes[task.id].route == "direct"
        ]
        if not tasks or llm is None:
            return {"direct_final_issues": []}

        system = (prompt_dir / "eval-direct-reviewer.txt").read_text(encoding="utf-8")

        def review_one(task: ReviewTask):
            return DirectEngine().review(
                llm,
                system_prompt=system,
                user_prompt=(
                    "请只审查以下由确定性规则判定为低风险的 task。"
                    "如果没有具体问题，返回空 issues。\n\n"
                    f"<task id=\"{task.id}\" file=\"{task.file}\">\n"
                    f"{task.patch}\n</task>"
                ),
                reviewer_name="direct_task",
                max_retries=state.get("max_retries", 3),
                structured_method=state.get("structured_method", "function_calling"),
                result_schema=DiscoveryReviewResult,
            )

        results = run_bounded_parallel(tasks, review_one, max_workers=8)
        candidates: list[CandidateIssue] = []
        dossiers: list[Any] = []
        rejected = 0
        failed_tasks = 0
        location_trace: list[CouncilTrace] = []
        for task, outcome in zip(tasks, results):
            if (
                outcome is None
                or outcome.status is not ReviewExecutionStatus.COMPLETE
                or outcome.result is None
            ):
                failed_tasks += 1
                failure_reason = (
                    "parallel_review_missing"
                    if outcome is None
                    else outcome.failure_reason or outcome.status.value
                )
                location_trace.append(
                    CouncilTrace(
                        node="direct_task_review",
                        event="task_review_failed",
                        detail=(
                            f"task={task.id} reason="
                            f"{failure_reason}"
                        ),
                    )
                )
                continue
            accepted = []
            for issue in outcome.result.issues:
                if not task_prep.file_matches_task(issue.file, task):
                    rejected += 1
                    continue
                if task_prep.is_noise_issue(issue.file, issue.type, issue.message):
                    rejected += 1
                    continue
                accepted.append(issue)
            location_batch = locate_issues(
                accepted,
                task,
                llm=llm,
                structured_method=state.get("structured_method", "function_calling"),
                max_retries=state.get("max_retries", 3),
            )
            from codeguard_agent.pipeline.evidence.planner import CandidateDossier

            for index, issue in enumerate(location_batch.issues, start=1):
                candidate = CandidateIssue(
                    id=f"direct-{task.id}-{index}",
                    task_id=task.id,
                    source_agent="direct_task",
                    file=issue.file,
                    line=issue.line,
                    type=issue.type,
                    claim=issue.message,
                    suggestion=issue.suggestion,
                    confidence=issue.confidence,
                )
                candidates.append(candidate)
                dossiers.append(
                    CandidateDossier(
                        candidate=candidate,
                        task=task,
                        symbol_context=None,
                    )
                )
            location_trace.extend(
                CouncilTrace(node="direct_task_review", event=event, detail=detail)
                for event, detail in location_batch.trace
            )
        from codeguard_agent.pipeline.council.verdict import judge_direct
        from codeguard_agent.pipeline.evidence.planner import DossierAssembly

        verdict_batch = judge_direct(
            DossierAssembly(tuple(dossiers), (), ()),
            judge_llm=llm,
            structured_method=state.get("structured_method", "function_calling"),
            max_retries=state.get("max_retries", 3),
        )
        judge_trace = [
            CouncilTrace(node="direct_judge", event=event, detail=detail)
            for event, detail in verdict_batch.trace
        ]
        return {
            "direct_final_issues": verdict_batch.final_issues,
            "council_trace": [
                CouncilTrace(
                    node="direct_task_review",
                    event="partial" if failed_tasks else "completed",
                    detail=(
                        f"tasks={len(tasks)} candidates={len(candidates)} "
                        f"issues={len(verdict_batch.final_issues)} "
                        f"rejected={rejected} failed={failed_tasks}"
                    ),
                ),
                *location_trace,
                *judge_trace,
            ],
        }

    return _node


def _causal_merge_node(judge_llm=None):
    """对 EvidenceJudge survivors 做 Cause/Effect 语义分析和保守合并。"""

    def _node(state: ReviewState) -> dict:
        from codeguard_agent.pipeline.council.causal_merge import merge_survivors

        result = merge_survivors(
            state.get("candidate_issues") or [],
            state.get("judge_survivor_ids") or [],
            state.get("final_issues") or [],
            state.get("candidate_verifications") or {},
            llm=judge_llm,
            structured_method=state.get("structured_method", "function_calling"),
        )
        trace = [
            CouncilTrace(node="causal_merge", event=event, detail=detail)
            for event, detail in result.trace
        ]
        trace.append(
            CouncilTrace(
                node="causal_merge",
                event="completed",
                detail=(
                    f"profiles={len(result.profiles)} comparisons={len(result.comparisons)} "
                    f"groups={len(result.groups)} final_issues={len(result.final_issues)}"
                ),
            )
        )
        return {
            "final_issues": [
                *result.final_issues,
                *(state.get("direct_final_issues") or []),
            ],
            "causal_profiles": result.profiles,
            "causal_comparisons": result.comparisons,
            "causal_merge_groups": result.groups,
            "causal_merge_stats": result.stats,
            "council_trace": trace,
        }

    return _node


def _direct_judge_node(judge_llm=None):
    """无证据链消融档的候选终审:跳过取证/门控,DirectJudge 直接裁决(ADR-046)。"""

    def _node(state: ReviewState) -> dict:
        from codeguard_agent.pipeline.council.metrics import compute_council_run_stats
        from codeguard_agent.pipeline.council.verdict import judge_direct

        assembly = _assemble_state_dossiers(state)
        batch = judge_direct(
            assembly,
            judge_llm=judge_llm,
            structured_method=state.get("structured_method", "function_calling"),
            max_retries=state.get("max_retries", 2),
        )
        judge_trace = [
            CouncilTrace(node="direct_judge", event=event, detail=detail)
            for event, detail in (*assembly.trace, *batch.trace)
        ]
        stats = compute_council_run_stats(
            candidates=state.get("candidate_issues") or [],
            assembly=assembly,
            verdicts=batch.verdicts,
            final_candidate_ids=batch.final_candidate_ids,
            truncated_candidates=state.get("truncated_candidates", 0),
            council_trace=[*(state.get("council_trace") or []), *judge_trace],
            artifacts=state.get("evidence_artifacts") or {},
            verifications=state.get("candidate_verifications") or {},
        )
        summaries = list(state.get("review_summaries") or [])
        selection = state.get("task_selection")
        if selection is not None:
            notice = _scope_plan(state).limit_notice(selection)
            if notice:
                summaries.insert(0, notice)
        return {
            "final_issues": [
                *batch.final_issues,
                *(state.get("direct_final_issues") or []),
            ],
            "council_stats": stats,
            "summary": "  ".join(summaries),
            "council_trace": judge_trace,
        }

    return _node


def build_review_graph(
    *,
    enable_summary: bool = True,
    checkpointer=None,
    llm=None,
    fp_verify_llm=None,
    tool_client=None,
    discovery_only: bool = False,
    evidence_mode: str = "full",
    discovery_mode: str = "controlled",
    controlled_initial_tool_budget: int = 6,
    controlled_delta_tool_budget: int = 2,
    controlled_max_path_depth: int = 3,
    controlled_max_seeds_per_change_unit: int = 4,
    controlled_max_seeds_per_reviewer: int = 4,
    controlled_max_seeds_per_task: int = 12,
    controlled_max_knowledge_topics: int = 4,
    controlled_execute_concurrency: int = 3,
    controlled_execution_mode: str = "planned_steps",
    controlled_subtask_max_tool_calls: int = 6,
    controlled_subtask_max_rounds: int = 4,
    controlled_subtask_timeout_seconds: int = 120,
    controlled_task_max_tool_calls: int = 24,
    controlled_max_subtasks_per_reviewer: int = 4,
    controlled_max_subtasks_per_task: int = 12,
):
    """编译审查状态图。

    按 PR 体量自动路由：
      - small：文件级 task 拆分 + 完整管线（与 medium 同拓扑，仅预算不同）
      - medium：文件级 task 拆分 + 完整管线
      - large：hunk 级 task 拆分 + 预算控制（现状）

    默认受控拓扑:
        START → classify_mode
          ├─ small / medium → file_task_builder → task_route → task_selection → plan → summary?
          └─ large          → diff_task_builder → task_route → task_selection → plan → summary?
                       → symbol_resolution → controlled_review
                       (DirectTriage → GraphPlan → Execute → EvidenceAssessment)
                       → council_coordinator(fan-in)
                         ├─ evidence_mode=full → evidence_verifier
                         │    → council_judge → causal_merge → END
                         └─ evidence_mode=off  → direct_judge → END
                           (无证据链消融基线:跳过取证/门控,DirectJudge 直接终审)

    显式 ``discovery_mode="react"`` 的兼容拓扑:
        ... → plan → review_plan → summary? → symbol_resolution → discover_*(×3)
                       → discovery_collector → END

    discovery_only 拓扑:
        START → classify_mode
          ├─ small / medium → file_task_builder → task_selection → plan → controlled_review
          │                    → discovery_collector → END
          └─ large           → diff_task_builder → task_selection → plan → controlled_review
                       → discovery_collector → END
    """
    from langgraph.graph import END, START, StateGraph

    # Keep the public builder safe for direct callers too.  The orchestrator
    # applies the same guard, but a direct build with a supplied client must
    # not silently instantiate ReAct discovery nodes.
    effective_tool_client = None if discovery_mode == "direct" else tool_client
    g = StateGraph(ReviewState)
    effective_judge_llm = fp_verify_llm or llm

    # ── 全模式共用节点 ──
    g.add_node("diff_task_builder", _diff_task_builder_node())
    g.add_node("classify_mode", _classify_mode_node())
    g.add_node("task_route", _task_route_node())
    g.add_node("direct_task_review", _direct_task_review_node(llm))
    g.add_node("task_selection", _task_selection_node())
    if discovery_mode == "controlled":
        g.add_node("plan", _controlled_plan_node(llm, knowledge_topics=controlled_max_knowledge_topics))
    else:
        g.add_node("plan", _plan_node(llm))
    if discovery_mode != "controlled":
        g.add_node("review_plan", _review_plan_node(effective_tool_client))
    g.add_node("symbol_resolution", _symbol_resolution_node(effective_tool_client))
    if discovery_mode != "controlled":
        for reviewer in DEFAULT_REVIEWERS:
            g.add_node(
                _discover_node_name(reviewer),
                make_reviewer_node(
                    reviewer,
                    checkpointer=checkpointer,
                    llm=llm,
                    tool_client=effective_tool_client,
                ),
            )

    # ── 模式特定节点 ──
    g.add_node("file_task_builder", _file_task_builder_node())
    if discovery_mode == "controlled":
        g.add_node(
            "controlled_review",
            _controlled_review_node(
                llm,
                tool_client=tool_client,
                execute_concurrency=controlled_execute_concurrency,
                controlled_execution_mode=controlled_execution_mode,
                subtask_max_tool_calls=controlled_subtask_max_tool_calls,
                subtask_max_rounds=controlled_subtask_max_rounds,
                subtask_timeout_seconds=controlled_subtask_timeout_seconds,
                task_max_tool_calls=controlled_task_max_tool_calls,
                max_subtasks_per_reviewer=controlled_max_subtasks_per_reviewer,
                max_subtasks_per_task=controlled_max_subtasks_per_task,
            ),
        )

    if discovery_only:
        g.add_node("discovery_collector", _discovery_collector_node())
    else:
        g.add_node("council_coordinator", _coordinator_node(effective_judge_llm))
        g.add_node(
            "direct_judge",
            _direct_judge_node(effective_judge_llm),
        )
        g.add_node(
            "evidence_verifier",
            _evidence_verifier_node(
                effective_tool_client,
                judge_llm=effective_judge_llm,
            ),
        )
        g.add_node(
            "council_judge",
            _council_judge_node(judge_llm=effective_judge_llm),
        )
        g.add_node(
            "causal_merge",
            _causal_merge_node(judge_llm=effective_judge_llm),
        )

    # ── 边：START → classify_mode（不预建 task）──
    g.add_edge(START, "classify_mode")

    # ── 条件路由：按 PR 体量分流 ──
    g.add_conditional_edges(
        "classify_mode",
        lambda state: state.get("review_mode", "large"),
        {
            "small": "file_task_builder",
            "medium": "file_task_builder",
            "large": "diff_task_builder",
        },
    )

    # ── task 构建后统一执行 DirectGate 和 Direct task ──
    g.add_edge("file_task_builder", "task_route")
    g.add_edge("diff_task_builder", "task_route")
    g.add_edge("task_route", "direct_task_review")
    g.add_edge("direct_task_review", "task_selection")

    # ── 所有非 Direct task 共用管线 ──
    g.add_edge("task_selection", "plan")
    if discovery_mode != "controlled":
        g.add_edge("plan", "review_plan")
    if enable_summary:
        g.add_node("summary", _summary_node(llm))
        g.add_edge("plan" if discovery_mode == "controlled" else "review_plan", "summary")
        g.add_edge("summary", "symbol_resolution")
    else:
        g.add_edge("plan" if discovery_mode == "controlled" else "review_plan", "symbol_resolution")

    if discovery_mode == "controlled":
        g.add_edge("symbol_resolution", "controlled_review")
        if discovery_only:
            g.add_edge("controlled_review", "discovery_collector")
        else:
            g.add_edge("controlled_review", "council_coordinator")
    else:
        for reviewer in DEFAULT_REVIEWERS:
            node_name = _discover_node_name(reviewer)
            g.add_edge("symbol_resolution", node_name)
            if discovery_only:
                g.add_edge(node_name, "discovery_collector")
            else:
                g.add_edge(node_name, "council_coordinator")

    if discovery_only:
        g.add_edge("discovery_collector", END)
    else:
        if evidence_mode == "off":
            g.add_edge("council_coordinator", "direct_judge")
            g.add_edge("direct_judge", END)
        else:
            g.add_edge("council_coordinator", "evidence_verifier")
            g.add_edge("evidence_verifier", "council_judge")
            g.add_edge("council_judge", "causal_merge")
            g.add_edge("causal_merge", END)

    return g.compile(checkpointer=checkpointer)
