
"""ReviewCouncil 编排图。

默认拓扑:

    START → diff_task_builder → risk_triage → task_rank → review_coverage → [summary]
              → context_provider → discover_* → council_coordinator(fan-in)
              → concern_analyzer → evidence_planner → evidence_agent → council_judge → END

discovery_only 拓扑(跳过归并/举证/法官，直接输出发现者原始结果):

    START → diff_task_builder → risk_triage → task_rank → review_coverage → [summary]
              → context_provider → discover_* → discovery_collector → END
"""

from __future__ import annotations

import json
import logging
import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict

from codeguard_agent.llm.client import mock_review_result
from codeguard_agent.models.council import (
    CandidateIssue,
    ContextBundle,
    ContextFact,
    CouncilRunStats,
    CouncilTrace,
    EvidenceNote,
    EvidenceRequest,
    MAX_CANDIDATES_PER_AGENT,
)
from codeguard_agent.models.schemas import ReviewResult
from codeguard_agent.models.tasks import (
    ContextStatus,
    ReviewBudget,
    ReviewCoveragePlan,
    ReviewMode,
    ReviewerKind,
    ReviewTask,
    RiskCoverage,
    RiskProfile,
    RiskTag,
    TaskContextBundle,
    TaskRiskPrior,
    TaskSelection,
)
from codeguard_agent.pipeline.context import rules as context_rules
from codeguard_agent.pipeline.risk import task_prep
from codeguard_agent.pipeline.council.judge import judge_candidates
from codeguard_agent.pipeline.council.dedup import (
    CandidateGroup,
    CandidateDedupStats,
    deduplicate_candidates,
)
from codeguard_agent.pipeline.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.risk.discovery import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
    canonical_tool_key,
)
from codeguard_agent.pipeline.knowledge.catalog import KnowledgeCatalog
from codeguard_agent.pipeline.knowledge.selector import select_knowledge
from codeguard_agent.models.knowledge import KnowledgeBudget
from codeguard_agent.pipeline.risk.large_diff import LargeDiffPlan, plan_large_diff
from codeguard_agent.pipeline.risk.routing import (
    build_risk_priors,
    coverage_task_ids,
    coverage_tiers,
    ensure_review_coverage,
    plan_review_coverage,
)
from codeguard_agent.pipeline.engines import (
    DirectEngine,
    ReviewEngine,
    ReviewOutcome,
    ToolAgentEngine,
)
from codeguard_agent.pipeline.evidence.agent import collect_evidence
from codeguard_agent.pipeline.council.metrics import compute_council_run_stats
from codeguard_agent.pipeline.evidence.planner import assemble_dossiers, plan_evidence
from codeguard_agent.pipeline.evidence.rules.classify import (
    CandidateTagResolution,
    resolve_candidate_tags,
)
from codeguard_agent.models.council import ConcernAnalysis
from codeguard_agent.pipeline.council.concern import (
    analyze_candidate_groups,
)
from codeguard_agent.pipeline.evidence.planner import plan_claim_evidence
from codeguard_agent.pipeline.context.base import PipelineContext
from codeguard_agent.pipeline.context.provider import ContextProviderStage
from codeguard_agent.pipeline.reviewers.reviewers import (
    DEFAULT_REVIEWERS,
    Reviewer,
    build_reviewer_system_prompt,
    build_reviewer_user_prompt,
)
from codeguard_agent.pipeline.summary.summary import SummaryStage

logger = logging.getLogger("codeguard")

DEFAULT_RECURSION_LIMIT = 50

_ALL_REVIEWER_NAMES = [r.source_agent for r in DEFAULT_REVIEWERS]


def dedup_gathered_reducer(existing: list | None, new: list | None) -> list:
    """`gathered_context` reducer:按规范化工具参数去重,保留首次出现顺序。"""
    merged = list(existing or []) + list(new or [])
    seen: set[tuple[str, str]] = set()
    out: list = []
    for it in merged:
        tool = getattr(it, "tool", "")
        args = getattr(it, "args", "")
        try:
            structured_args = json.loads(args)
        except (TypeError, json.JSONDecodeError):
            structured_args = None
        key = (
            canonical_tool_key(tool, structured_args)
            if isinstance(structured_args, dict)
            else (tool, args)
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(it)

    return out


def dedup_evidence_request_reducer(existing: list | None, new: list | None) -> list:
    """`evidence_requests` reducer:仅按稳定 ID 去重，绝不截断。"""
    merged = list(existing or []) + list(new or [])
    seen: set[str] = set()
    unique: list[EvidenceRequest] = []
    for request in merged:
        if request.id in seen:
            continue
        seen.add(request.id)
        unique.append(request)
    return unique


def collect_candidate_reducer(existing: list | None, new: list | None) -> list:
    """`raw_candidate_issues` reducer: 仅按 candidate.id 去重，保留首次出现的 payload。

    语义去重在 CouncilCoordinator 中显式执行（见 candidate_dedup 模块）。
    """
    merged = list(existing or []) + list(new or [])
    seen: set[str] = set()
    out: list[CandidateIssue] = []
    for c in merged:
        if c.id in seen:
            continue
        seen.add(c.id)
        out.append(c)
    return out


def _discover_node_name(reviewer: Reviewer) -> str:
    return f"discover_{reviewer.source_agent}"


class ReviewState(TypedDict, total=False):
    """审查图共享状态。"""

    diff_text: str
    enabled_tools: Any
    enabled_evidence_tools: Any
    context_diagnostics: dict[str, str]

    max_retries: int
    structured_method: str
    react_recursion_limit: int
    allow_direct_fallback: bool
    diff_summary: str

    review_budget: ReviewBudget
    review_mode: str  # "small" | "medium" | "large"
    review_tasks: list[ReviewTask]
    risk_profiles: dict[str, RiskProfile]
    risk_priors: dict[str, TaskRiskPrior]
    task_selection: TaskSelection
    review_coverage_plan: ReviewCoveragePlan
    task_context_bundles: dict[str, TaskContextBundle]

    context_bundle: ContextBundle
    raw_candidate_issues: Annotated[list[CandidateIssue], collect_candidate_reducer]
    candidate_issues: list[CandidateIssue]
    candidate_groups: list[CandidateGroup]
    candidate_tag_resolutions: dict[str, CandidateTagResolution]
    candidate_dedup_stats: CandidateDedupStats
    concern_analysis: ConcernAnalysis
    evidence_requests: Annotated[list[EvidenceRequest], dedup_evidence_request_reducer]
    evidence_notes: Annotated[list[EvidenceNote], operator.add]
    council_trace: Annotated[list[CouncilTrace], operator.add]
    truncated_candidates: Annotated[int, operator.add]

    gathered_context: Annotated[list, dedup_gathered_reducer]
    tool_trace_records: Annotated[list, operator.add]
    review_summaries: Annotated[list, operator.add]

    final_issues: list
    summary: str
    council_stats: CouncilRunStats


class ReviewerState(TypedDict, total=False):
    """单个发现者 Agent 子图状态。"""

    diff_text: str
    enabled_tools: Any
    max_retries: int
    structured_method: str
    diff_summary: str
    react_recursion_limit: int
    allow_direct_fallback: bool
    task_knowledge: str
    review_task: ReviewTask
    risk_profile: RiskProfile
    task_context_bundle: TaskContextBundle
    tier: str
    task_scope: str  # "current_hunk" | "current_file"
    review_tool_client: Any

    issues: list
    gathered_context: list
    tool_trace_records: list
    review_summaries: list
    council_trace: Annotated[list[CouncilTrace], operator.add]

    user_prompt: str
    outcome: Any


def _make_engine(state: ReviewState | ReviewerState, tool_client=None) -> ReviewEngine:
    if tool_client is not None:
        return ToolAgentEngine(
            tool_client,
            recursion_limit=state.get("react_recursion_limit", 24),
            enabled_tools=state.get("enabled_tools"),
            allow_direct_fallback=state.get("allow_direct_fallback", True),
        )
    return DirectEngine()


def _state_to_context(state: ReviewState, llm=None, fp_verify_llm=None, tool_client=None) -> PipelineContext:
    ctx = PipelineContext(
        diff_text=state.get("diff_text", ""),
        llm=llm,
        max_retries=state.get("max_retries", 3),
        structured_method=state.get("structured_method", "function_calling"),
        fp_verify_llm=fp_verify_llm,
        tool_client=tool_client,
        enabled_tools=state.get("enabled_tools"),
        diff_summary=state.get("diff_summary", ""),
        gathered_context=list(state.get("gathered_context") or []),
    )
    ctx.context_bundle = state.get("context_bundle")
    return ctx


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


def _summary_node(llm, tool_client):
    def _node(state: ReviewState) -> dict:
        scope = _scope_plan(state)
        ctx = _state_to_context(state, llm=llm, tool_client=tool_client)
        ctx.diff_text = _selected_diff(state, scope)
        SummaryStage().execute(ctx)
        return {"diff_summary": ctx.diff_summary}

    return _node


def _classify_mode_node():
    """根据 PR 体量决定审查模式（纯确定性，不调 LLM）。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        budget = state.get("review_budget") or ReviewBudget()
        mode = task_prep.classify_pr_mode(tasks, budget)
        return {
            "review_mode": mode.value,
            "council_trace": [
                CouncilTrace(
                    node="classify_mode",
                    event="mode_selected",
                    detail=f"mode={mode.value} tasks={len(tasks)}",
                )
            ],
        }

    return _node


def _direct_review_node(llm):
    """小型 PR：单次 LLM 直接审查完整 diff，不走管线。"""
    _prompt_dir = Path(__file__).resolve().parents[1] / "prompts"

    def _node(state: ReviewState) -> dict:
        if llm is None:
            return {
                "final_issues": [],
                "summary": "",
                "council_trace": [
                    CouncilTrace(
                        node="direct_review",
                        event="skipped",
                        detail="no llm available",
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
        except Exception:
            logger.warning("direct_review 失败，返回空结果", exc_info=True)
            return {
                "final_issues": [],
                "summary": "",
                "council_trace": [
                    CouncilTrace(
                        node="direct_review",
                        event="failed",
                        detail="direct review exception",
                    )
                ],
            }
        return {
            "final_issues": outcome.result.issues,
            "summary": outcome.result.summary,
            "council_trace": [
                CouncilTrace(
                    node="direct_review",
                    event="completed",
                    detail=f"issues={len(outcome.result.issues)}",
                )
            ],
        }

    return _node


def _rebuild_file_tasks_node():
    """中型 PR：用文件级 task 替换 hunk 级 task。"""

    def _node(state: ReviewState) -> dict:
        diff_text = state.get("diff_text", "")
        budget = state.get("review_budget") or ReviewBudget()
        tasks = task_prep.build_file_tasks(diff_text, budget)
        return {
            "review_tasks": tasks,
            "council_trace": [
                CouncilTrace(
                    node="rebuild_file_tasks",
                    event="tasks_rebuilt",
                    detail=f"file_tasks={len(tasks)}",
                )
            ],
        }

    return _node


def _diff_task_builder_node():
    """DiffTaskBuilder：解析 diff → ReviewTask。不判断风险、不读仓库、不调 LLM。"""

    def _node(state: ReviewState) -> dict:
        tasks = task_prep.build_tasks(state.get("diff_text", ""))
        return {
            "review_tasks": tasks,
            "council_trace": [
                CouncilTrace(
                    node="diff_task_builder",
                    event="tasks_built",
                    detail=f"tasks={len(tasks)}",

                )
            ],
        }

    return _node


def _risk_triage_node():
    """RiskTriage：为每个任务产出 RiskProfile 和规则失败 trace。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        result = task_prep.triage_tasks(tasks)
        trace = [
            CouncilTrace(
                node="risk_triage",
                event="profiled",
                detail=f"profiles={len(result.profiles)}",
            )
        ]
        trace.extend(
            CouncilTrace(
                node="risk_triage",
                event="rule_failed",
                detail=diagnostic.detail,
            )
            for diagnostic in result.diagnostics
        )
        return {
            "risk_profiles": result.profiles,
            "risk_priors": build_risk_priors(tasks, result.profiles),
            "council_trace": trace,
        }

    return _node


def _task_rank_node():
    """TaskRank：根据画像与预算选择进入深审的任务。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        profiles = state.get("risk_profiles") or {}
        priors = state.get("risk_priors") or build_risk_priors(tasks, profiles)
        scope = _scope_plan(state)
        budget = scope.effective_budget
        selection = task_prep.rank_tasks(tasks, profiles, budget, priors)
        trace = [
            CouncilTrace(
                node="task_rank",
                event="selected",
                detail=f"selected={len(selection.selected_task_ids)} skipped={len(selection.skipped_tasks)}",
            )
        ]
        if scope.active:
            trace.append(
                CouncilTrace(
                    node="task_rank",
                    event="large_diff_degraded",
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


def _review_coverage_node(tool_client=None):
    """把 Risk prior 转成基础覆盖 + 风险增强的 Reviewer 计划。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        selection = state.get("task_selection")
        if selection is None:
            raise ValueError("task_selection is required before review coverage")
        priors = state.get("risk_priors") or build_risk_priors(
            tasks, state.get("risk_profiles") or {}
        )
        scope = _scope_plan(state)
        budget = scope.effective_budget
        plan = plan_review_coverage(
            tasks,
            priors,
            selection,
            react_budget=budget.max_react_tasks,
            tools_available=tool_client is not None,
        )
        assignment_count = sum(len(item.assignments) for item in plan.tasks)
        trace = [
            CouncilTrace(
                node="review_coverage",
                event="planned",
                detail=(
                    f"tasks={len(plan.tasks)} assignments={assignment_count} "
                    f"baseline={plan.baseline_assignments} "
                    f"risk_added={plan.risk_added_assignments} "
                    f"fallback={plan.ambiguity_fallback_assignments} "
                    f"unclassified={plan.unclassified_tasks} "
                    f"react_candidates={plan.react_candidate_tasks} "
                    f"react_tasks={plan.react_task_count} "
                    f"react_assignments={plan.react_assignment_count} "
                    f"risk_upgraded={plan.risk_upgraded_assignments} "
                    f"react_tasks_truncated={plan.truncated_react_task_count} "
                    f"react_assignments_truncated="
                    f"{plan.truncated_react_assignment_count} "
                    f"zero_assignments={plan.tasks_with_zero_assignments}"
                ),
            )
        ]
        trace.extend(
            CouncilTrace(
                node="review_coverage",
                event="assignment",
                detail=(
                    f"task={item.task_id} reviewer={assignment.reviewer.value} "
                    f"tier={assignment.tier.value} "
                    f"reasons={','.join(reason.value for reason in assignment.reasons)} "
                    f"tags={','.join(tag.value for tag in assignment.hypothesis_tags)}"
                ),
            )
            for item in plan.tasks
            for assignment in item.assignments
        )
        return {"risk_priors": priors, "review_coverage_plan": plan, "council_trace": trace}

    return _node


def _context_provider_node(tool_client):
    """为选中任务装配图谱解析后的稳定符号上下文。"""

    def _node(state: ReviewState) -> dict:
        scope = _scope_plan(state)
        selection = state.get("task_selection")
        selected_ids = set(selection.selected_task_ids) if selection is not None else set()
        all_tasks: list[ReviewTask] = state.get("review_tasks") or []
        tasks = [task for task in all_tasks if task.id in selected_ids]
        ctx = _state_to_context(state, tool_client=tool_client)
        ctx.diff_text = _selected_diff(state, scope)
        ctx.change_locations = [
            {"file": task.file, "lines": task.changed_lines}
            for task in tasks
        ]
        ContextProviderStage(include_broad_scan=not scope.active).execute(ctx)
        bundle = ctx.context_bundle

        budget = scope.effective_budget
        gathered = list(ctx.gathered_context)
        symbol_facts: list[tuple[ContextFact, dict[str, Any]]] = []
        for fact in bundle.facts:
            if fact.kind != "symbol_context":
                continue
            try:
                symbol_facts.append((fact, json.loads(fact.content)))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

        task_bundles: dict[str, TaskContextBundle] = {}
        trace: list[CouncilTrace] = [
            CouncilTrace(
                node="context_provider",
                event="bundle_created",
                detail=f"facts={len(bundle.facts)} tasks={len(tasks)}",
            )
        ]
        for task in tasks:
            task_start = min(task.changed_lines) if task.changed_lines else 0
            task_end = max(task.changed_lines) if task.changed_lines else 0
            facts = [
                fact
                for fact, item in symbol_facts
                if context_rules.normalize_path(str(item.get("file", "")))
                == context_rules.normalize_path(task.file)
                and (
                    not task.changed_lines
                    or (
                        int(item.get("start_line", 0)) <= task_end
                        and int(item.get("end_line", 0)) >= task_start
                    )
                )
            ]
            statuses: list[ContextStatus] = []
            if not facts:
                failure = ctx.context_diagnostics.get("symbol_context")
                statuses.append(
                    ContextStatus(
                        kind="symbol_context",
                        status="failed" if failure else "unavailable",
                        reason=(
                            failure
                            or (
                                "tool_server_not_configured"
                                if tool_client is None
                                else "no_resolved_symbol_for_current_hunk"
                            )
                        ),
                    )
                )

            facts, truncated = context_rules.truncate_task_facts(
                facts, budget.max_context_chars_per_task
            )
            task_bundles[task.id] = TaskContextBundle(
                task_id=task.id,
                facts=facts,
                statuses=statuses,
                truncated=truncated,
            )
            trace.append(
                CouncilTrace(
                    node="context_provider",
                    event="task_bundle_filled",
                    detail=(
                        f"task={task.id} facts={len(facts)} "
                        f"symbol_context={bool(facts)} "
                        f"diagnostic={ctx.context_diagnostics.get('symbol_context', '')} "
                        f"truncated={truncated}"
                    ),
                )
            )

        return {
            "context_bundle": bundle,
            "context_diagnostics": dict(ctx.context_diagnostics),
            "gathered_context": gathered,
            "task_context_bundles": task_bundles,

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
        )

    def _prepare(state: ReviewerState) -> dict:
        if llm is None:
            return {}
        review_task = state.get("review_task")
        if review_task is None:
            raise ValueError("review_task is required for task-scoped discovery")
        return {
            "user_prompt": build_reviewer_user_prompt(
                task=review_task,
                summary=state.get("diff_summary", ""),
                risk_profile=state.get("risk_profile"),
                context_bundle=state.get("task_context_bundle"),
                task_knowledge=state.get("task_knowledge", ""),
                task_scope=state.get("task_scope", "current_hunk"),
            )
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
            )
        except Exception as exc:  # noqa: BLE001 单发现者失败不拖垮 council
            from langgraph.errors import GraphRecursionError

            if isinstance(exc, GraphRecursionError):
                logger.warning("[%s] 发现者撞递归上限,降级直连: %s", reviewer.name, exc)
                review_traces.append(
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event="react_degraded_recursion",
                        detail=str(exc)[:200],
                    )
                )
                if not state.get("allow_direct_fallback", True):
                    return {
                        "outcome": ReviewOutcome(ReviewResult(summary="")),
                        "council_trace": review_traces,
                    }
                outcome = _direct_fallback(state)
            else:
                logger.warning("[%s] 发现者失败,跳过: %s", reviewer.name, exc)
                return {
                    "outcome": ReviewOutcome(ReviewResult(summary="")),
                    "council_trace": [
                        CouncilTrace(node=reviewer.source_agent, event="discover_failed", detail=str(exc))
                    ],
                }

        # ReAct 跑完但未产出任何 issue → LLM 偶发空响应（DeepSeek 已知问题），
        # 降级为 DirectEngine 直连复审以保住该域覆盖率。
        if (
            tier != "direct"
            and not outcome.result.issues
            and state.get("allow_direct_fallback", True)
        ):
            logger.warning(
                "[%s] ReAct 未产出 issue,降级直连复审以保住该域覆盖", reviewer.name
            )
            review_traces.append(
                CouncilTrace(
                    node=reviewer.source_agent,
                    event="react_degraded_empty",
                    detail="empty result",
                )
            )
            react_outcome = outcome
            outcome = _direct_fallback(state)
            outcome.gathered_context.extend(react_outcome.gathered_context)
            outcome.tool_trace_records.extend(react_outcome.tool_trace_records)

        for event in outcome.execution_events:
            review_traces.append(
                CouncilTrace(
                    node=reviewer.source_agent,
                    event=event,
                    detail="bounded ReAct exploration synthesized from gathered tool facts",
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
        out["issues"] = list(outcome.result.issues)
        if outcome.gathered_context:
            out["gathered_context"] = list(outcome.gathered_context)
        if outcome.tool_trace_records:
            out["tool_trace_records"] = list(outcome.tool_trace_records)
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
    """发现者节点:运行旧 reviewer 能力,再转换为 CandidateIssue。"""
    # task 级 fan-out 会在线程池中并发 invoke；任务子图不持久化，避免复用外层
    # SQLite saver 的线程绑定连接。外层 ReviewState 仍由 build_review_graph 的
    # checkpointer 持久化，足以恢复整次审查。
    subgraph = build_reviewer_subgraph(reviewer, checkpointer=None, llm=llm, tool_client=tool_client)

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        profiles = state.get("risk_profiles") or {}
        priors = state.get("risk_priors") or build_risk_priors(tasks, profiles)
        coverage = state.get("review_coverage_plan")
        selection = state.get("task_selection")
        if selection is None:
            raise ValueError("task_selection is required before discovery")

        _coordinator = DiscoveryToolCoordinator() if tool_client is not None else None

        def _task_tool_client(task: ReviewTask | None = None):
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
            )

        coverage = ensure_review_coverage(tasks, coverage, selection)
        ordered_ids = list(coverage_task_ids(reviewer.source_agent, coverage, selection))
        routed_ids = set(ordered_ids)
        if not routed_ids:
            return {
                "raw_candidate_issues": [],
                "truncated_candidates": 0,
                "council_trace": [
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event="no_tasks_routed",
                        detail="selected tasks do not match reviewer risk tags",
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
        task_context_bundles = state.get("task_context_bundles") or {}
        tier_by_task = coverage_tiers(reviewer.source_agent, coverage, selection)

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
            profile = profiles.get(task_id)
            tier = tier_by_task.get(task_id, "direct")
            bundle = task_context_bundles.get(task_id)
            prior = priors.get(task_id)
            if prior is None:
                prior = TaskRiskPrior(task_id=task_id, coverage=RiskCoverage.UNCLASSIFIED)

            catalog = KnowledgeCatalog()
            budget = KnowledgeBudget()
            reviewer_kind_val = reviewer.source_agent  # "threat_model", "behavior", "maintainability"
            reviewer_kind = ReviewerKind(reviewer_kind_val)

            knowledge_bundle = select_knowledge(
                reviewer=reviewer_kind,
                task=scoped_task,
                prior=prior,
                context=bundle,
                catalog=catalog,
                budget=budget,
            )
            task_knowledge = knowledge_bundle.rendered_text
            # 根据审查模式推导 task_scope：文件级 → current_file，其余 → current_hunk
            mode = state.get("review_mode", "large")
            task_scope = "current_file" if mode == "medium" else "current_hunk"
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
                    "allow_direct_fallback": state.get("allow_direct_fallback", True),
                    "review_task": scoped_task,
                    "risk_profile": profile,
                    "task_context_bundle": bundle,
                    "task_knowledge": task_knowledge,
                    "tier": tier,
                    "task_scope": task_scope,
                    "review_tool_client": _task_tool_client(scoped_task),
                },
            )
            if profile is None:
                traces = list(result.get("council_trace") or [])
                traces.append(
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event="missing_risk_profile",
                        detail=f"task={task_id} tier=direct",
                    )
                )
                result["council_trace"] = traces
            return result

        task_results = run_bounded_parallel(ordered_ids, _invoke_one, max_workers=8)

        per_task_issues: list[tuple[str, Any]] = []
        trace = [
            CouncilTrace(
                node=reviewer.source_agent,
                event="task_tier_planned",
                detail=f"task={task_id} tier={tier_by_task.get(task_id, 'direct')}",
            )
            for task_id in ordered_ids
        ]
        gathered_context: list = []
        tool_trace_records: list = []
        review_summaries: list = []
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
            if result.get("gathered_context"):
                gathered_context.extend(result["gathered_context"])
            if result.get("tool_trace_records"):
                tool_trace_records.extend(result["tool_trace_records"])
            if result.get("review_summaries"):
                review_summaries.extend(result["review_summaries"])

        kept_pairs = per_task_issues[:MAX_CANDIDATES_PER_AGENT]
        truncated_candidates = max(0, len(per_task_issues) - len(kept_pairs))

        candidates = []
        rejected_mismatched: list[str] = []
        accepted_count = 0
        for task_id, issue in kept_pairs:
            task = task_by_id[task_id]
            if not task_prep.file_matches_task(issue.file, task):
                rejected_mismatched.append(f"{issue.file}:{issue.line} -> {task_id}")
                continue

            accepted_count += 1
            candidates.append(
                CandidateIssue.from_issue(
                    issue, source_agent=reviewer.source_agent,
                    index=accepted_count, task_id=task_id,
                )
            )

        trace.append(
            CouncilTrace(
                node=reviewer.source_agent,
                event="candidates_created",
                detail=(
                    f"count={len(candidates)} truncated={truncated_candidates} "
                    f"rejected_task_mismatch={len(rejected_mismatched)}"
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
        if gathered_context:
            routed_out["gathered_context"] = gathered_context
        if tool_trace_records:
            routed_out["tool_trace_records"] = tool_trace_records
        if review_summaries:
            routed_out["review_summaries"] = review_summaries
        return routed_out

    return _node


def _discovery_collector_node():
    """发现者直出模式:将 raw_candidate_issues 直接转为 final_issues,不经归并/举证/法官。"""

    def _node(state: ReviewState) -> dict:
        raw = list(state.get("raw_candidate_issues") or [])
        from codeguard_agent.models.schemas import Issue

        issues = []
        seen: set[str] = set()
        for c in raw:
            if c.id in seen:
                continue
            seen.add(c.id)
            issues.append(
                Issue(
                    severity=c.severity_proposal,
                    file=c.file,
                    line=c.line,
                    type=f"[{c.source_agent}] {c.type}",
                    message=c.claim,
                    suggestion=c.suggestion or "",
                    confidence=c.confidence,
                )
            )

        trace = list(state.get("council_trace") or [])
        trace.append(
            CouncilTrace(
                node="discovery_collector",
                event="discovery_direct_output",
                detail=f"raw_candidates={len(raw)} final_issues={len(issues)}",
            )
        )

        return {
            "final_issues": issues,
            "council_trace": trace,
        }

    return _node


def _coordinator_node(effective_judge_llm):
    """三路发现者的显式 fan-in barrier：RiskTag 解析 + 候选语义归并。

    1. 读 raw_candidate_issues
    2. 组装轻量 dossier → 解析 RiskTag
    3. 调用 deduplicate_candidates 做语义归并
    4. 产出 candidate_issues（唯一写入者）、candidate_tag_resolutions、stats
    """

    def _node(state: ReviewState) -> dict:
        raw = list(state.get("raw_candidate_issues") or [])
        tasks = state.get("review_tasks") or []
        profiles = state.get("risk_profiles") or {}
        bundles = state.get("task_context_bundles") or {}
        structured_method = state.get("structured_method", "function_calling")

        trace: list[CouncilTrace] = []
        scope = _scope_plan(state)
        scoped_tasks = []
        for task in tasks:
            scoped_patch = scope.scoped_patch(task.patch)
            scoped_tasks.append(
                task.model_copy(
                    update={
                        "patch": scoped_patch,
                        "patch_complete": (
                            task.patch_complete and scoped_patch == task.patch
                        ),
                    }
                )
            )
        tasks_by_id = {task.id: task for task in scoped_tasks}

        # 1. 为 raw candidates 批量组装轻量 dossier 并解析 RiskTag
        assembly = assemble_dossiers(
            raw,
            scoped_tasks,
            profiles,
            bundles,
            (),
            (),
        )
        resolutions = resolve_candidate_tags(
            assembly.dossiers,
            classifier_llm=effective_judge_llm,
            structured_method=structured_method,
        )
        for failure in assembly.failures:
            resolutions[failure.candidate.id] = CandidateTagResolution(
                tag=RiskTag.GENERAL_REVIEW,
                confidence=0.5,
                source="general",
                reason=f"candidate_binding_{failure.reason}",
            )

        source_counts = {"rule": 0, "llm": 0, "general": 0}
        for resolution in resolutions.values():
            source_counts[resolution.source] = (
                source_counts.get(resolution.source, 0) + 1
            )

        trace.append(
            CouncilTrace(
                node="council_coordinator",
                event="candidate_tags_resolved",
                detail=(
                    f"resolved={len(resolutions)} "
                    f"rule={source_counts['rule']} llm={source_counts['llm']} "
                    f"general={source_counts['general']}"
                ),
            )
        )

        # 2. 语义归并
        result = deduplicate_candidates(
            raw,
            tasks_by_id=tasks_by_id,
            tag_resolutions=resolutions,
            llm=effective_judge_llm,
            structured_method=structured_method,
        )

        trace.append(
            CouncilTrace(
                node="council_coordinator",
                event="candidate_dedup_blocks_built",
                detail=(
                    f"raw={result.raw_candidate_count} "
                    f"singleton={result.block_count - result.multi_member_block_count} "
                    f"multi={result.multi_member_block_count}"
                ),
            )
        )
        trace.append(
            CouncilTrace(
                node="council_coordinator",
                event="candidate_dedup_completed",
                detail=(
                    f"raw={result.raw_candidate_count} "
                    f"logical={result.logical_candidate_count} "
                    f"grouped={result.grouped_member_count} "
                    f"blocks={result.block_count} multi={result.multi_member_block_count} "
                    f"llm_calls={result.llm_call_count} "
                    f"accepted_groups={len(result.accepted_groups)} "
                    f"rejected_groups={len(result.rejected_groups)} "
                    f"block_failures={len(result.block_failures)}"
                ),
            )
        )

        for group in result.accepted_groups:
            trace.append(
                CouncilTrace(
                    node="council_coordinator",
                    event="candidate_dedup_group_accepted",
                    detail=(
                        f"group={group.id} members={list(group.member_ids)} "
                        f"tag={group.primary_risk_tag.value} "
                        f"severity={group.severity_proposal.value} "
                        f"confidence={group.confidence:.2f} "
                        f"root_cause={group.shared_root_cause} "
                        f"behavior={group.shared_behavior} fix={group.shared_fix}"
                    ),
                )
            )
        for rejected in result.rejected_groups:
            trace.append(
                CouncilTrace(
                    node="council_coordinator",
                    event="candidate_dedup_group_rejected",
                    detail=f"members={list(rejected.member_ids)} reason={rejected.reason}",
                )
            )
        for block_failure in result.block_failures:
            trace.append(
                CouncilTrace(
                    node="council_coordinator",
                    event="candidate_dedup_block_failed",
                    detail=(
                        f"block={block_failure.block_id} "
                        f"reason={block_failure.reason}"
                    ),
                )
            )

        trace.append(
            CouncilTrace(
                node="council_coordinator",
                event="fan_in",
                detail=f"candidates={len(result.candidates)}",
            )
        )

        return {
            "candidate_issues": list(result.candidates),
            "candidate_groups": list(result.accepted_groups),
            "candidate_tag_resolutions": dict(resolutions),
            "candidate_dedup_stats": {
                "raw_candidate_count": result.raw_candidate_count,
                "logical_candidate_count": result.logical_candidate_count,
                "grouped_member_count": result.grouped_member_count,
                "removed_count": 0,
                "llm_call_count": result.llm_call_count,
                "block_failure_count": len(result.block_failures),
            },
            "council_trace": trace,
        }

    return _node


def _concern_analyzer_node(effective_judge_llm=None):
    """把 CandidateGroup 转换为结构化 CandidateConcern。"""

    def _node(state: ReviewState) -> dict:
        groups = state.get("candidate_groups") or []
        candidates = state.get("candidate_issues") or []
        priors = state.get("risk_priors") or {}
        tag_resolutions = state.get("candidate_tag_resolutions") or {}

        if not groups:
            # 兼容旧路径：无 CandidateGroup 时用 singleton fallback
            raw = candidates
            if raw:
                analysis = analyze_candidate_groups(
                    (),
                    candidates=raw,
                    candidate_tag_resolutions=tag_resolutions,
                    task_priors=priors,
                    llm=effective_judge_llm,
                    structured_method=state.get(
                        "structured_method", "function_calling",
                    ),
                )
                return {
                    "concern_analysis": analysis,
                    "council_trace": [
                        CouncilTrace(
                            node="concern_analyzer",
                            event="singleton_fallback",
                            detail=f"no groups, built {len(analysis.concerns)} singleton concerns",
                        )
                    ],
                }
            return {
                "concern_analysis": ConcernAnalysis(),
                "council_trace": [
                    CouncilTrace(
                        node="concern_analyzer",
                        event="no_op",
                        detail="no candidate groups or issues",
                    )
                ],
            }

        analysis = analyze_candidate_groups(
            groups,
            candidates=candidates,
            candidate_tag_resolutions=tag_resolutions,
            task_priors=priors,
            llm=effective_judge_llm,
            structured_method=state.get(
                "structured_method", "function_calling",
            ),
        )
        return {
            "concern_analysis": analysis,
            "council_trace": [
                CouncilTrace(
                    node="concern_analyzer",
                    event="concerns_built",
                    detail=(
                        f"concerns={len(analysis.concerns)} "
                        f"diagnostics={len(analysis.diagnostics)}"
                    ),
                ),
            ],
        }

    return _node


def _assemble_state_dossiers(state: ReviewState):
    return assemble_dossiers(
        state.get("candidate_issues") or [],
        state.get("review_tasks") or [],
        state.get("risk_profiles") or {},
        state.get("task_context_bundles") or {},
        state.get("evidence_requests") or [],
        state.get("evidence_notes") or [],
        state.get("candidate_groups") or [],
    )


def _evidence_planner_node(effective_judge_llm):
    """EvidencePlanner 是 graph 中 evidence_requests 的唯一写入者。

    当 concern_analysis 可用时，对每个 concern 使用 plan_claim_evidence()；
    否则走旧 plan_evidence() 兼容路径。
    """

    def _node(state: ReviewState) -> dict:
        concern_analysis = state.get("concern_analysis")
        trace: list[CouncilTrace] = []

        if concern_analysis is not None and concern_analysis.concerns:
            # 新路径：claim-driven planning
            all_requests: list = []
            for concern in concern_analysis.concerns:
                concern_plan = plan_claim_evidence(concern)
                all_requests.extend(concern_plan.requests)
                trace.append(
                    CouncilTrace(
                        node="evidence_planner",
                        event="concern_planned",
                        detail=(
                            f"concern={concern.concern_id} "
                            f"goals={len(concern_plan.goals)} "
                            f"requests={len(concern_plan.requests)} "
                            f"uncovered={len(concern_plan.uncovered_goals)}"
                        ),
                    )
                )
            if not all_requests:
                trace.append(
                    CouncilTrace(
                        node="evidence_planner",
                        event="no_op",
                        detail="no evidence requests from concerns",
                    )
                )
            return {"evidence_requests": all_requests, "council_trace": trace}

        # 旧路径：tag-driven planning (兼容)
        assembly = _assemble_state_dossiers(state)
        legacy_plan = plan_evidence(
            assembly.dossiers,
            classifier_llm=effective_judge_llm,
            structured_method=state.get("structured_method", "function_calling"),
            candidate_tag_resolutions=state.get("candidate_tag_resolutions"),
        )
        trace = [
            CouncilTrace(node="evidence_planner", event=event, detail=detail)
            for event, detail in (*assembly.trace, *legacy_plan.trace)
        ]
        if not assembly.dossiers:
            trace.append(
                CouncilTrace(
                    node="evidence_planner",
                    event="no_op",
                    detail="no valid candidate dossiers",
                )
            )
        return {
            "evidence_requests": legacy_plan.requests,
            "council_trace": trace,
        }

    return _node


def _evidence_agent_node(tool_client=None, judge_llm=None):
    """执行尚无 note 的 request。"""

    def _node(state: ReviewState) -> dict:
        requests = state.get("evidence_requests") or []
        completed = {note.request_id for note in state.get("evidence_notes") or []}
        pending = [request for request in requests if request.id not in completed]
        assembly = _assemble_state_dossiers(state)
        batch = collect_evidence(
            assembly.dossiers,
            pending,
            tool_client=tool_client,
            analyst_llm=judge_llm,
            structured_method=state.get("structured_method", "function_calling"),
            enabled_tools=state.get(
                "enabled_evidence_tools",
                state.get("enabled_tools"),
            ),
        )
        trace = [
            CouncilTrace(node="evidence_agent", event=event, detail=detail)
            for event, detail in batch.trace
        ]
        if not pending:
            trace.append(
                CouncilTrace(
                    node="evidence_agent",
                    event="no_op",
                    detail="no pending evidence requests",
                )
            )
        return {
            "evidence_notes": batch.notes,
            "gathered_context": batch.gathered_context,
            "council_trace": trace,
        }

    return _node


def _council_judge_node(llm, judge_llm=None):
    effective_judge_llm = judge_llm or llm

    def _node(state: ReviewState) -> dict:
        assembly = _assemble_state_dossiers(state)
        batch = judge_candidates(
            assembly,
            judge_llm=effective_judge_llm,
            structured_method=state.get("structured_method", "function_calling"),
            max_retries=state.get("max_retries", 2),
            candidate_groups=state.get("candidate_groups") or [],
            concern_analysis=state.get("concern_analysis"),
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
            evidence_request_count=len(state.get("evidence_requests") or []),
            truncated_candidates=state.get("truncated_candidates", 0),
            council_trace=[*(state.get("council_trace") or []), *judge_trace],
            candidate_dedup_stats=state.get("candidate_dedup_stats"),
        )
        summaries = list(state.get("review_summaries") or [])
        selection = state.get("task_selection")
        if selection is not None:
            notice = _scope_plan(state).coverage_notice(selection)
            if notice:
                summaries.insert(0, notice)
        return {
            "final_issues": batch.final_issues,
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
):
    """编译审查状态图。

    按 PR 体量自动路由：
      - small：直接审查完整 diff，不走管线
      - medium：文件级 task 拆分 + 完整管线
      - large：hunk 级 task 拆分 + 预算控制（现状）

    默认拓扑:
        START → diff_task_builder(hunk级) → classify_mode
          ├─ small  → direct_review → END
          ├─ medium → rebuild_file_tasks → risk_triage → ... (完整管线)
          └─ large  → risk_triage → task_rank → review_coverage → summary?
                       → context_provider → discover_*(×3)
                       → council_coordinator(fan-in)
                       → concern_analyzer → evidence_planner
                       → evidence_agent → council_judge → END

    discovery_only 拓扑:
        START → diff_task_builder → classify_mode
          ├─ small  → direct_review → END
          ├─ medium → rebuild_file_tasks → risk_triage → ... → discover_*(×3)
          │           → discovery_collector → END
          └─ large  → risk_triage → ... → discover_*(×3)
                       → discovery_collector → END
    """
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(ReviewState)
    effective_judge_llm = fp_verify_llm or llm

    # ── 全模式共用节点 ──
    g.add_node("diff_task_builder", _diff_task_builder_node())
    g.add_node("classify_mode", _classify_mode_node())
    g.add_node("risk_triage", _risk_triage_node())
    g.add_node("task_rank", _task_rank_node())
    g.add_node("review_coverage", _review_coverage_node(tool_client))
    g.add_node("context_provider", _context_provider_node(tool_client))
    for reviewer in DEFAULT_REVIEWERS:
        g.add_node(
            _discover_node_name(reviewer),
            make_reviewer_node(reviewer, checkpointer=checkpointer, llm=llm, tool_client=tool_client),
        )

    # ── 模式特定节点 ──
    g.add_node("direct_review", _direct_review_node(llm))
    g.add_node("rebuild_file_tasks", _rebuild_file_tasks_node())

    if discovery_only:
        g.add_node("discovery_collector", _discovery_collector_node())
    else:
        g.add_node("council_coordinator", _coordinator_node(effective_judge_llm))
        g.add_node(
            "concern_analyzer",
            _concern_analyzer_node(effective_judge_llm),
        )
        g.add_node("evidence_planner", _evidence_planner_node(effective_judge_llm))
        g.add_node(
            "evidence_agent",
            _evidence_agent_node(tool_client, judge_llm=effective_judge_llm),
        )
        g.add_node(
            "council_judge",
            _council_judge_node(llm, judge_llm=effective_judge_llm),
        )

    # ── 边：START → diff_task_builder → classify_mode ──
    g.add_edge(START, "diff_task_builder")
    g.add_edge("diff_task_builder", "classify_mode")

    # ── 条件路由：按 PR 体量分流 ──
    g.add_conditional_edges(
        "classify_mode",
        lambda state: state.get("review_mode", "large"),
        {
            "small": "direct_review",
            "medium": "rebuild_file_tasks",
            "large": "risk_triage",
        },
    )

    # ── small 路径 ──
    g.add_edge("direct_review", END)

    # ── medium 路径 ──
    g.add_edge("rebuild_file_tasks", "risk_triage")

    # ── medium + large 共用管线 ──
    g.add_edge("risk_triage", "task_rank")
    g.add_edge("task_rank", "review_coverage")
    if enable_summary:
        g.add_node("summary", _summary_node(llm, tool_client))
        g.add_edge("review_coverage", "summary")
        g.add_edge("summary", "context_provider")
    else:
        g.add_edge("review_coverage", "context_provider")

    for reviewer in DEFAULT_REVIEWERS:
        node_name = _discover_node_name(reviewer)
        g.add_edge("context_provider", node_name)
        if discovery_only:
            g.add_edge(node_name, "discovery_collector")
        else:
            g.add_edge(node_name, "council_coordinator")

    if discovery_only:
        g.add_edge("discovery_collector", END)
    else:
        g.add_edge("council_coordinator", "concern_analyzer")
        g.add_edge("concern_analyzer", "evidence_planner")
        g.add_edge("evidence_planner", "evidence_agent")
        g.add_edge("evidence_agent", "council_judge")
        g.add_edge("council_judge", END)

    return g.compile(checkpointer=checkpointer)
