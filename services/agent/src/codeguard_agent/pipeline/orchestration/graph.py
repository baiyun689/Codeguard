"""变更驱动审查图：任务路由、有界调查、证据验证与裁决。"""

from __future__ import annotations
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from codeguard_agent.models.council import CandidateIssue, CouncilTrace
from codeguard_agent.models.state import ReviewState, collect_candidate_reducer
from codeguard_agent.models.schemas import (
    DiscoveredIssue,
    DiscoveryReviewResult,
    EvidenceRefSelection,
    EvidenceRole,
    Issue,
)
from codeguard_agent.models.tasks import (
    ReviewBudget,
    ReviewMode,
    ReviewRoute,
    ReviewRouteThresholds,
    ReviewTask,
    SkippedTask,
    TaskSelection,
    TaskRoute,
)
from codeguard_agent.pipeline.tasks import task_builder as task_prep
from codeguard_agent.pipeline.execution.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.location import locate_issues
from codeguard_agent.pipeline.tasks.scope import LargeDiffPlan, plan_large_diff
from codeguard_agent.pipeline.execution.engines import (
    DirectEngine,
    ReviewExecutionStatus,
)
from codeguard_agent.pipeline.evidence.ledger import bind_discovered_issue
from codeguard_agent.pipeline.evidence.planner import assemble_dossiers
from codeguard_agent.pipeline.symbols import resolve_task_symbols

logger = logging.getLogger("codeguard")
DEFAULT_RECURSION_LIMIT = 50


def _scope_plan(state: ReviewState) -> LargeDiffPlan:
    return plan_large_diff(
        state.get("diff_text", ""),
        list(state.get("review_tasks") or []),
        state.get("review_budget") or ReviewBudget(),
    )


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
        if mode is ReviewMode.NORMAL:
            selected_node = "file_task_builder"
        else:
            selected_node = "diff_task_builder"
        review_route = ReviewRoute(
            initial_mode=mode,
            effective_mode=mode,
            selected_node=selected_node,
            metrics=metrics,
            thresholds=ReviewRouteThresholds(
                normal_max_files=budget.normal_max_files,
                normal_max_diff_chars=budget.normal_max_diff_chars,
            ),
        )
        return {
            "review_mode": mode.value,
            "review_route": review_route,
            "council_trace": [
                CouncilTrace(
                    node="classify_mode",
                    event="mode_selected",
                    detail=f"mode={mode.value} files={metrics.file_count} hunks={metrics.hunk_count} diff_chars={metrics.diff_chars} normal_max_files={budget.normal_max_files} normal_max_chars={budget.normal_max_diff_chars}",
                )
            ],
        }

    return _node


def _file_task_builder_node():
    """NORMAL 按文件拆分，保留真实路径与变更行，供符号解析定位工具入口。"""

    def _node(state: ReviewState) -> dict:
        diff_text = state.get("diff_text", "")
        mode = state.get("review_mode", "normal")
        tasks = task_prep.build_file_tasks(diff_text)
        file_count = len({t.file for t in tasks})
        hunk_fallback_count = len([t for t in tasks if t.hunk_header])
        return {
            "review_tasks": tasks,
            "council_trace": [
                CouncilTrace(
                    node="file_task_builder",
                    event="tasks_built",
                    detail=f"mode={mode} tasks={len(tasks)} files={file_count} hunk_fallback={hunk_fallback_count}",
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
                    detail=f"mode=large tasks={len(tasks)} files={file_count}",
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
        full_tasks = [
            task
            for task in tasks
            if routes.get(task.id, TaskRoute(task_id=task.id, route="full")).route
            == "full"
        ]
        selected: list[str] = []
        skipped = []
        per_file: dict[str, int] = {}
        for task in full_tasks:
            file_key = task.file.replace("\\", "/").lower()
            if (
                budget.max_tasks_to_review is not None
                and len(selected) >= budget.max_tasks_to_review
            ):
                skipped.append(SkippedTask(task_id=task.id, reason="total_limit"))
                continue
            if (
                budget.max_tasks_per_file is not None
                and per_file.get(file_key, 0) >= budget.max_tasks_per_file
            ):
                skipped.append(SkippedTask(task_id=task.id, reason="per_file_limit"))
                continue
            selected.append(task.id)
            per_file[file_key] = per_file.get(file_key, 0) + 1
        skipped.extend(
            (
                SkippedTask(task_id=task.id, reason="direct_gate")
                for task in tasks
                if routes.get(task.id, TaskRoute(task_id=task.id, route="full")).route
                == "direct"
            )
        )
        selection = TaskSelection(selected_task_ids=selected, skipped_tasks=skipped)
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
                    detail=f"lines={scope.total_lines} tasks={scope.total_tasks} selected={len(selection.selected_task_ids)} skipped={len(selection.skipped_tasks)} max_tasks={budget.max_tasks_to_review} max_per_file={budget.max_tasks_per_file} context_chars={budget.max_context_chars_per_task}",
                )
            )
        return {"task_selection": selection, "council_trace": trace}

    return _node


def _symbol_resolution_node(tool_client):
    """把选中 Full task 的变更行批量解析为稳定项目符号。"""

    def _node(state: ReviewState) -> dict:
        scope = _scope_plan(state)
        selection = state.get("task_selection")
        selected_ids = (
            set(selection.selected_task_ids) if selection is not None else set()
        )
        all_tasks: list[ReviewTask] = state.get("review_tasks") or []
        tasks = [task for task in all_tasks if task.id in selected_ids]
        resolution = resolve_task_symbols(
            tasks,
            tool_client=tool_client,
            max_chars_per_task=scope.effective_budget.max_context_chars_per_task,
        )
        deletion_anchor_count = sum((len(task.deletion_anchors) for task in tasks))
        resolved_deletion_anchor_count = sum(
            (
                1
                for task in tasks
                for anchor in task.deletion_anchors
                if any(
                    (
                        symbol.start_line <= anchor.anchor_line <= symbol.end_line
                        for symbol in resolution.contexts[task.id].symbols
                    )
                )
            )
        )
        trace: list[CouncilTrace] = [
            CouncilTrace(
                node="symbol_resolution",
                event="resolution_completed",
                detail=f"tasks={len(tasks)} resolved={sum((bool(item.symbols) for item in resolution.contexts.values()))} symbols={sum((len(item.symbols) for item in resolution.contexts.values()))} deletion_anchors={deletion_anchor_count} deletion_anchors_resolved={resolved_deletion_anchor_count}",
            )
        ]
        for task in tasks:
            context = resolution.contexts[task.id]
            trace.append(
                CouncilTrace(
                    node="symbol_resolution",
                    event="task_symbols_resolved",
                    detail=f"task={task.id} status={context.status.value} symbols={len(context.symbols)} deletion_anchors={len(task.deletion_anchors)} limitations={','.join(context.limitations)} truncated={context.truncated}",
                )
            )
        return {
            "task_symbol_contexts": resolution.contexts,
            "symbol_resolution_diagnostics": dict(resolution.diagnostics),
            "council_trace": trace,
        }

    return _node


def _discovery_collector_node():
    """诊断节点只记录候选，不将未经裁决的问题转换为对外 Issue。"""

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
        return {"candidate_issues": list(candidates), "council_trace": trace}

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
        summaries: list[str] = []
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
        direct = sum((route.route == "direct" for route in routes.values()))
        full = sum((route.route == "full" for route in routes.values()))
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


def _investigation_candidate(
    finding: Any,
    *,
    task: ReviewTask,
    reviewer: str,
    catalog: Any,
    alias_by_call_id: dict[str, str],
    candidate_index: int,
    allow_patch_only: bool = True,
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
            "location": EvidenceRole.LOCATION,
        }
        selections.append(
            EvidenceRefSelection(
                alias=alias, role=role_map.get(role, EvidenceRole.MECHANISM)
            )
        )
    if not selections and (finding.observations or not allow_patch_only):
        return None
    message = finding.claim.strip()
    if finding.impact.strip() and finding.impact.strip() not in message:
        message = f"{message}；{finding.impact.strip()}"
    discovered = DiscoveredIssue(
        file=finding.location_file or task.file,
        line=finding.location_line,
        location_snippet=getattr(finding, "location_snippet", ""),
        type=finding.type_hint.strip() or reviewer,
        message=message,
        suggestion=finding.suggestion,
        confidence=1.0,
        evidence_refs=selections,
    )
    if allow_patch_only:
        if discovered.file.replace("\\", "/") != task.file.replace("\\", "/"):
            discovered = discovered.model_copy(
                update={"file": task.file, "line": 0, "location_snippet": ""}
            )
        discovered = locate_issues(
            [discovered],
            task,
            llm=None,
            structured_method="function_calling",
            max_retries=0,
        ).issues[0]
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
    count: int, *, total_budget: int, per_subtask_limit: int
) -> tuple[int, ...]:
    """均分任务的工具预算。

    当子任务数超过可分配调用次数时，末尾分组获得零预算；
    调用方须仅启动获得预算的调查组。
    """
    if count <= 0 or total_budget <= 0 or per_subtask_limit <= 0:
        return tuple((0 for _ in range(max(0, count))))
    base = min(per_subtask_limit, total_budget // count)
    remainder = max(0, total_budget - base * count)
    budgets = [base] * count
    for index in range(count):
        if remainder <= 0 or budgets[index] >= per_subtask_limit:
            continue
        budgets[index] += 1
        remainder -= 1
    return tuple(budgets)


def _direct_task_review_node(llm):
    """执行 DirectGate 判定的 task，跳过取证但仍交给 DirectJudge 定级。"""
    prompt_dir = Path(__file__).resolve().parents[1] / "prompts"

    def _node(state: ReviewState) -> dict:
        routes = state.get("task_routes") or {}
        tasks = [
            task
            for task in state.get("review_tasks") or []
            if routes.get(task.id) is not None and routes[task.id].route == "direct"
        ]
        if not tasks or llm is None:
            return {"direct_final_issues": []}
        system = (prompt_dir / "eval-direct-reviewer.txt").read_text(encoding="utf-8")

        def review_one(task: ReviewTask):
            return DirectEngine().review(
                llm,
                system_prompt=system,
                user_prompt=f'请只审查以下由确定性规则判定为低风险的 task。如果没有具体问题，返回空 issues。\n\n<task id="{task.id}" file="{task.file}">\n{task.patch}\n</task>',
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
                        detail=f"task={task.id} reason={failure_reason}",
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
                        candidate=candidate, task=task, symbol_context=None
                    )
                )
            location_trace.extend(
                (
                    CouncilTrace(node="direct_task_review", event=event, detail=detail)
                    for event, detail in location_batch.trace
                )
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
                    detail=f"tasks={len(tasks)} candidates={len(candidates)} issues={len(verdict_batch.final_issues)} rejected={rejected} failed={failed_tasks}",
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
                detail=f"profiles={len(result.profiles)} comparisons={len(result.comparisons)} groups={len(result.groups)} final_issues={len(result.final_issues)}",
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
    """无证据链评测模式的候选终审，由 DirectJudge 直接裁决。"""

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
        summaries: list[str] = []
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
    checkpointer=None,
    llm=None,
    fp_verify_llm=None,
    tool_client=None,
    evidence_mode="full",
    discovery_mode="controlled",
    discovery_only=False,
    controlled_max_path_depth=3,
    controlled_execute_concurrency=3,
    controlled_subtask_max_tool_calls=10,
    controlled_subtask_max_rounds=6,
    controlled_subtask_timeout_seconds=120,
    controlled_task_max_tool_calls=32,
    controlled_max_subtasks_per_task=8,
):
    """构建变更驱动的审查图，或显式选择无工具对照流程。"""
    from langgraph.graph import END, START, StateGraph
    from codeguard_agent.pipeline.controlled.change_review import (
        build_change_review_node,
    )

    if discovery_mode not in {"controlled", "direct"}:
        raise ValueError(
            "discovery_mode must be controlled or direct; historical engines were removed"
        )
    client = None if discovery_mode == "direct" else tool_client
    judge = fp_verify_llm or llm
    g = StateGraph(ReviewState)
    g.add_node("classify_mode", _classify_mode_node())
    g.add_node("file_task_builder", _file_task_builder_node())
    g.add_node("diff_task_builder", _diff_task_builder_node())

    def route(state):
        result = _task_route_node()(state)
        if discovery_mode == "direct":
            result["task_routes"] = {
                t.id: TaskRoute(
                    task_id=t.id, route="direct", reason="explicit_baseline"
                )
                for t in state.get("review_tasks", [])
            }
        return result

    g.add_node("task_route", route)
    g.add_node("direct_task_review", _direct_task_review_node(llm))
    g.add_node("task_selection", _task_selection_node())
    g.add_node("symbol_resolution", _symbol_resolution_node(client))
    g.add_node(
        "controlled_review",
        build_change_review_node(
            llm,
            client,
            candidate_factory=_investigation_candidate,
            scope_factory=_scope_plan,
            allocate_budgets=_allocate_subtask_budgets,
            execute_concurrency=controlled_execute_concurrency,
            max_tool_calls=controlled_subtask_max_tool_calls,
            max_rounds=controlled_subtask_max_rounds,
            timeout_seconds=controlled_subtask_timeout_seconds,
            task_tool_budget=controlled_task_max_tool_calls,
            max_subtasks=controlled_max_subtasks_per_task,
        ),
    )
    g.add_edge(START, "classify_mode")
    g.add_conditional_edges(
        "classify_mode",
        lambda s: s.get("review_mode", "large"),
        {
            "normal": "file_task_builder",
            "large": "diff_task_builder",
        },
    )
    g.add_edge("file_task_builder", "task_route")
    g.add_edge("diff_task_builder", "task_route")
    stages = [
        "task_route",
        "direct_task_review",
        "task_selection",
        "symbol_resolution",
        "controlled_review",
    ]
    for left, right in zip(stages, stages[1:]):
        g.add_edge(left, right)
    if discovery_only:
        g.add_node("discovery_collector", _discovery_collector_node())
        g.add_edge("controlled_review", "discovery_collector")
        g.add_edge("discovery_collector", END)
    else:
        g.add_node("council_coordinator", _coordinator_node(judge))
        g.add_edge("controlled_review", "council_coordinator")
        if evidence_mode == "off":
            g.add_node("direct_judge", _direct_judge_node(judge))
            g.add_edge("council_coordinator", "direct_judge")
            g.add_edge("direct_judge", END)
        else:
            g.add_node("evidence_verifier", _evidence_verifier_node(client))
            g.add_node("council_judge", _council_judge_node(judge))
            g.add_node("causal_merge", _causal_merge_node(judge))
            g.add_edge("council_coordinator", "evidence_verifier")
            g.add_edge("evidence_verifier", "council_judge")
            g.add_edge("council_judge", "causal_merge")
            g.add_edge("causal_merge", END)
    return g.compile(checkpointer=checkpointer)
