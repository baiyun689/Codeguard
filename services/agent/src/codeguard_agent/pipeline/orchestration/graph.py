
"""ReviewCouncil 编排图。

默认先按 PR 体量路由：small 构建 whole-diff task；medium 构建 file task；
large 构建 hunk task。所有 task 经过 DirectGate，Full task 进入 Plan、发现、举证与裁决链。
"""

from __future__ import annotations

import logging
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
from codeguard_agent.models.schemas import DiscoveryReviewResult, Issue, ReviewResult
from codeguard_agent.models.tasks import (
    ReviewBudget,
    ReviewMode,
    ReviewRoute,
    ReviewRouteThresholds,
    ReviewerKind,
    ReviewTask,
    SkippedTask,
    TaskSelection,
    TaskRoute,
)
from codeguard_agent.pipeline.tasks import task_builder as task_prep
from codeguard_agent.pipeline.execution.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.execution.discovery import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
)
from codeguard_agent.pipeline.knowledge.catalog import KnowledgeCatalog
from codeguard_agent.pipeline.knowledge.selector import select_knowledge
from codeguard_agent.pipeline.location import locate_issues
from codeguard_agent.models.knowledge import KnowledgeBudget
from codeguard_agent.pipeline.tasks.scope import LargeDiffPlan, plan_large_diff
from codeguard_agent.pipeline.planning import (
    build_plan_units,
    plan_coverage,
    run_plan_units,
)
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


def _direct_review_node(llm):
    """小型 PR：单次 LLM 直接审查完整 diff，不走管线。"""
    _prompt_dir = Path(__file__).resolve().parents[1] / "prompts"

    def _node(state: ReviewState) -> dict:
        route_value = state.get("review_route")
        route = (
            route_value
            if isinstance(route_value, ReviewRoute)
            else ReviewRoute.model_validate(route_value)
        )
        if llm is None:
            result = mock_review_result()
            route = route.model_copy(update={"outcome": "completed"})
            return {
                "final_issues": result.issues,
                "summary": result.summary,
                "direct_review_status": "completed",
                "review_route": route,
                "council_trace": [
                    CouncilTrace(
                        node="direct_review",
                        event="completed",
                        detail=f"mode=small mock=true issues={len(result.issues)}",
                    )
                ],
            }
        system = (_prompt_dir / "eval-direct-reviewer.txt").read_text(encoding="utf-8")
        user = (
            "请审查以下 unified diff，报告所有由变更引入或暴露的、"
            "具有具体运行时影响的问题。\n\n"
            f"```diff\n{state['diff_text']}\n```"
        )
        try:
            outcome = DirectEngine().review(
                llm,
                system_prompt=system,
                user_prompt=user,
                reviewer_name="direct_review",
                max_retries=state.get("max_retries", 3),
                structured_method=state.get("structured_method", "function_calling"),
            )
        except Exception as exc:
            logger.warning("direct_review 失败，回退文件级完整管线", exc_info=True)
            route = route.model_copy(update={
                "effective_mode": ReviewMode.MEDIUM,
                "selected_node": "file_task_builder",
                "fallback": True,
                "fallback_reason": "direct_review_exception",
                "fallback_exception_type": type(exc).__name__,
            })
            return {
                "direct_review_status": "fallback",
                "review_mode": "medium",
                "review_route": route,
                "council_trace": [
                    CouncilTrace(
                        node="direct_review",
                        event="fallback",
                        detail="direct review exception; route=file_task_builder",
                    )
                ],
            }
        if outcome.status is not ReviewExecutionStatus.COMPLETE:
            route = route.model_copy(update={
                "effective_mode": ReviewMode.MEDIUM,
                "selected_node": "file_task_builder",
                "fallback": True,
                "fallback_reason": outcome.failure_reason or "review_execution_failed",
            })
            return {
                "direct_review_status": "fallback",
                "review_mode": "medium",
                "review_route": route,
                "council_trace": [
                    CouncilTrace(
                        node="direct_review",
                        event="fallback",
                        detail=(
                            f"{outcome.failure_reason or 'review execution failed'}; "
                            "route=file_task_builder"
                        ),
                    )
                ],
            }
        structured_result = outcome.result
        assert structured_result is not None
        route = route.model_copy(update={"outcome": "completed"})
        return {
            "final_issues": structured_result.issues,
            "summary": structured_result.summary,
            "direct_review_status": "completed",
            "review_route": route,
            "council_trace": [
                CouncilTrace(
                    node="direct_review",
                    event="completed",
                    detail=(
                        f"mode=small issues={len(structured_result.issues)} "
                        f"diff_chars={len(state['diff_text'])}"
                    ),
                )
            ],
        }

    return _node


def _file_task_builder_node():
    """SMALL 保持单 task；MEDIUM 构建文件级 task。"""

    def _node(state: ReviewState) -> dict:
        diff_text = state.get("diff_text", "")
        mode = state.get("review_mode", "medium")
        tasks = (
            task_prep.build_whole_diff_task(diff_text)
            if mode == "small"
            else task_prep.build_file_tasks(diff_text)
        )
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
        trace: list[CouncilTrace] = [
            CouncilTrace(
                node="symbol_resolution",
                event="resolution_completed",
                detail=(
                    f"tasks={len(tasks)} "
                    f"resolved={sum(bool(item.symbols) for item in resolution.contexts.values())} "
                    f"symbols={sum(len(item.symbols) for item in resolution.contexts.values())}"
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
            complete_patch_files = (
                {task.file}
                if task is not None
                and task.patch_complete
                and task.hunk_header.strip().startswith("@@ -0,0 +")
                else set()
            )
            return CoordinatedDiscoveryToolClient(
                tool_client,
                _coordinator,
                complete_patch_files=complete_patch_files,
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
    return assemble_dossiers(
        state.get("candidate_issues") or [],
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
):
    """编译审查状态图。

    按 PR 体量自动路由：
      - small：直接审查完整 diff，不走管线
      - medium：文件级 task 拆分 + 完整管线
      - large：hunk 级 task 拆分 + 预算控制（现状）

    默认拓扑:
        START → classify_mode
          ├─ small  → direct_review → END
          ├─ medium → file_task_builder → task_route → task_selection → plan → review_plan
          └─ large  → diff_task_builder → task_route → task_selection → plan → review_plan → summary?
                       → symbol_resolution → discover_*(×3)
                       → council_coordinator(fan-in)
                         ├─ evidence_mode=full → evidence_verifier
                         │    → council_judge → causal_merge → END
                         └─ evidence_mode=off  → direct_judge → END
                           (无证据链消融基线:跳过取证/门控,DirectJudge 直接终审)

    discovery_only 拓扑:
        START → classify_mode
          ├─ small  → direct_review → END
          ├─ medium → file_task_builder → task_selection → plan → review_plan → discover_*(×3)
          │           → discovery_collector → END
          └─ large  → diff_task_builder → task_selection → plan → review_plan → discover_*(×3)
                       → discovery_collector → END
    """
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(ReviewState)
    effective_judge_llm = fp_verify_llm or llm

    # ── 全模式共用节点 ──
    g.add_node("diff_task_builder", _diff_task_builder_node())
    g.add_node("classify_mode", _classify_mode_node())
    g.add_node("task_route", _task_route_node())
    g.add_node("direct_task_review", _direct_task_review_node(llm))
    g.add_node("task_selection", _task_selection_node())
    g.add_node("plan", _plan_node(llm))
    g.add_node("review_plan", _review_plan_node(tool_client))
    g.add_node("symbol_resolution", _symbol_resolution_node(tool_client))
    for reviewer in DEFAULT_REVIEWERS:
        g.add_node(
            _discover_node_name(reviewer),
            make_reviewer_node(reviewer, checkpointer=checkpointer, llm=llm, tool_client=tool_client),
        )

    # ── 模式特定节点 ──
    g.add_node("direct_review", _direct_review_node(llm))
    g.add_node("file_task_builder", _file_task_builder_node())

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
                tool_client,
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

    # ── small 路径 ──
    g.add_conditional_edges(
        "direct_review",
        lambda state: state.get("direct_review_status", "fallback"),
        {
            "completed": END,
            "fallback": "file_task_builder",
        },
    )

    # ── task 构建后统一执行 DirectGate 和 Direct task ──
    g.add_edge("file_task_builder", "task_route")
    g.add_edge("diff_task_builder", "task_route")
    g.add_edge("task_route", "direct_task_review")
    g.add_edge("direct_task_review", "task_selection")

    # ── 所有非 Direct task 共用管线 ──
    g.add_edge("task_selection", "plan")
    g.add_edge("plan", "review_plan")
    if enable_summary:
        g.add_node("summary", _summary_node(llm))
        g.add_edge("review_plan", "summary")
        g.add_edge("summary", "symbol_resolution")
    else:
        g.add_edge("review_plan", "symbol_resolution")

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
