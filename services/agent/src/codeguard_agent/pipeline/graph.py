
"""ADR-032 ReviewCouncil 编排图。

默认拓扑:

    START → diff_task_builder → risk_triage → task_rank → [summary]
              → context_provider → discover_* → council_coordinator(fan-in)
              → evidence_planner → evidence_agent → council_judge → END

旧 LLM Supervisor 图已迁移到 `services/agent/legacy/supervisor_graph/graph.py`,
仅作历史参考,不再作为主编排运行回退。
challenge_agent 和 self_checker 节点已合并为 council_judge(规则+LLM 混合裁决)。
"""

from __future__ import annotations

import json
import logging
import operator
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
    ReviewTask,
    RiskProfile,
    TaskContextBundle,
    TaskSelection,
)
from codeguard_agent.pipeline import context_rules, task_prep
from codeguard_agent.pipeline.council_judge import judge_candidates
from codeguard_agent.pipeline.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.discovery_tools import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
    canonical_tool_key,
)
from codeguard_agent.pipeline.knowledge_rules import load_knowledge
from codeguard_agent.pipeline.large_diff_policy import LargeDiffPlan, plan_large_diff
from codeguard_agent.pipeline.risk_routing import (
    plan_task_tiers,
    routed_task_ids,
)
from codeguard_agent.pipeline.engines import (
    DirectEngine,
    GatheredContext,
    ReviewEngine,
    ReviewOutcome,
    ToolAgentEngine,
)
from codeguard_agent.pipeline.evidence_agent import collect_evidence
from codeguard_agent.pipeline.council_metrics import compute_council_run_stats
from codeguard_agent.pipeline.evidence_planner import assemble_dossiers, plan_evidence
from codeguard_agent.pipeline.stages.base import PipelineContext
from codeguard_agent.pipeline.stages.context_provider import ContextProviderStage
from codeguard_agent.pipeline.stages.reviewer_stage import (
    DEFAULT_REVIEWERS,
    Reviewer,
    _build_user_prompt,
    build_reviewer_system_prompt,
    build_reviewer_user_prompt,
)
from codeguard_agent.pipeline.stages.aggregation import deduplicate
from codeguard_agent.pipeline.stages.summary import SummaryStage

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


def _candidate_dedup_reducer(existing: list | None, new: list | None) -> list:
    """`candidate_issues` reducer: fan-in 时自动去重。

    两层：规则指纹 + 邻行容差(±3)强制合并。
    """
    merged = list(existing or []) + list(new or [])
    if len(merged) <= 1:
        return merged

    # 层 1: 规则指纹去重（同文件+同行号+同 type）
    issues = [c.to_issue() for c in merged]
    deduped_issues = deduplicate(issues)
    surviving: list[CandidateIssue] = []
    used_ids: set[str] = set()
    for di in deduped_issues:
        di_file = (di.file or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        for c in merged:
            if c.id in used_ids:
                continue
            c_file = (c.file or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
            if c_file == di_file and c.line == di.line and c.type == di.type:
                surviving.append(c)
                used_ids.add(c.id)
                break

    # 层 2: 同文件+同 type+邻行容差(±3)合并。
    # 三个条件必须同时满足，防止跨维度错误合并和远距离误合并。
    if len(surviving) >= 2:
        final: list[CandidateIssue] = []
        seen: list[tuple[str, int, CandidateIssue, str]] = []
        for c in surviving:
            c_file = (c.file or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
            c_norm_type = _normalize_type(c.type)
            merged_into = None
            for s_file, s_line, survivor, s_norm_type in seen:
                if s_file != c_file:
                    continue
                if c_norm_type != s_norm_type:
                    continue
                if c.line > 0 and abs(c.line - s_line) <= 3:
                    merged_into = survivor
                    break
            if merged_into is None:
                seen.append((c_file, c.line, c, c_norm_type))
                final.append(c)
        return final

    return surviving


# ── 跨维度去重守卫：标准化 type 用于语义等价判断 ──

_TYPE_ALIASES: dict[str, str] = {
    # 资源泄漏
    "资源泄漏": "resource_leak",
    "resource_lifecycle": "resource_leak",
    # 空指针
    "空指针": "null_safety",
    "空指针解引用": "null_safety",
    "null_state_safety": "null_safety",
    # 错误处理 / 异常吞没
    "error_handling": "error_handling",
    "异常信息静默吞没": "error_handling",
    # 硬编码凭据 / 配置安全
    "硬编码凭据": "hardcoded_credential",
    "硬编码密码": "hardcoded_credential",
    "config_security": "hardcoded_credential",
    # API 契约
    "api_contract": "api_contract",
    "不安全的随机令牌生成": "api_contract",
    # 敏感数据暴露
    "data_exposure": "data_exposure",
    "异常信息泄露（敏感数据暴露）": "data_exposure",
    # 路径穿越、SSRF、XXE、令牌标识符暴露等安全域专用 type
    # 无跨维度合并需求，统一留原值
}


def _normalize_type(raw_type: str) -> str:
    """将不同 Agent 产出的 type 字符串映射到统一的语义分类，用于跨维度等价判断。"""
    key = raw_type.strip().lower()
    return _TYPE_ALIASES.get(key, key)


def _discover_node_name(reviewer: Reviewer) -> str:
    return f"discover_{reviewer.source_agent}"


class ReviewState(TypedDict, total=False):
    """ADR-032 图共享状态。"""

    diff_text: str
    enabled_tools: Any

    max_retries: int
    structured_method: str
    react_recursion_limit: int
    diff_summary: str

    review_budget: ReviewBudget
    review_tasks: list[ReviewTask]
    risk_profiles: dict[str, RiskProfile]
    task_selection: TaskSelection
    task_context_bundles: dict[str, TaskContextBundle]

    context_bundle: ContextBundle
    candidate_issues: Annotated[list[CandidateIssue], _candidate_dedup_reducer]
    evidence_requests: Annotated[list[EvidenceRequest], dedup_evidence_request_reducer]
    evidence_notes: Annotated[list[EvidenceNote], operator.add]
    council_trace: Annotated[list[CouncilTrace], operator.add]
    truncated_candidates: Annotated[int, operator.add]

    gathered_context: Annotated[list, dedup_gathered_reducer]
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
    context_bundle: ContextBundle
    task_risk_context: str
    task_knowledge: str
    review_task: ReviewTask
    risk_profile: RiskProfile
    task_context_bundle: TaskContextBundle
    tier: str
    review_tool_client: Any

    issues: list
    gathered_context: list
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
        return state.get("diff_text", "")
    return scope.selected_diff(list(state.get("review_tasks") or []), selection)


def _summary_node(llm, tool_client):
    def _node(state: ReviewState) -> dict:
        scope = _scope_plan(state)
        ctx = _state_to_context(state, llm=llm, tool_client=tool_client)
        ctx.diff_text = _selected_diff(state, scope)
        SummaryStage().execute(ctx)
        return {"diff_summary": ctx.diff_summary}

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
            "council_trace": trace,
        }

    return _node


def _task_rank_node():
    """TaskRank：根据画像与预算选择进入深审的任务（Phase 1 全选）。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        profiles = state.get("risk_profiles") or {}
        scope = _scope_plan(state)
        budget = scope.effective_budget
        selection = task_prep.rank_tasks(tasks, profiles, budget)
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


def _execute_level1_call(
    call: context_rules.Level1Call, tool_client,
) -> tuple[context_rules.Level1Call, str | None, str]:
    """执行单个 Level1 调用，拒绝将失败信封作为事实返回。"""
    try:
        response = (
            tool_client.find_callers(call.key)
            if call.level is context_rules.ContextLevel.FIND_CALLERS
            else tool_client.get_code_metrics(call.key)
        )
    except Exception as exc:  # noqa: BLE001
        return call, None, f"{type(exc).__name__}: {exc}"
    if not getattr(response, "success", False):
        return call, None, str(getattr(response, "error", "tool_failed"))
    content = response.as_tool_output().strip()
    return call, (content or None), ""


def _context_provider_node(tool_client):
    """为选中任务装配 Level0 切片和按风险定向的 Level1 事实。"""

    def _node(state: ReviewState) -> dict:
        scope = _scope_plan(state)
        ctx = _state_to_context(state, tool_client=tool_client)
        ctx.diff_text = _selected_diff(state, scope)
        ContextProviderStage(include_broad_scan=not scope.active).execute(ctx)
        bundle = ctx.context_bundle
        broad_diagnostics = ctx.context_diagnostics

        selection = state.get("task_selection")
        selected_ids = set(selection.selected_task_ids) if selection is not None else set()
        all_tasks: list[ReviewTask] = state.get("review_tasks") or []
        tasks = [task for task in all_tasks if task.id in selected_ids]
        risk_profiles: dict[str, RiskProfile] = state.get("risk_profiles") or {}
        budget = scope.effective_budget

        ast_text = "\n".join(
            fact.content for fact in bundle.facts if fact.source == "tool:get_diff_ast"
        )
        sensitive_text = "\n".join(
            fact.content for fact in bundle.facts if fact.source == "tool:find_sensitive_apis"
        )
        ast_blocks: dict[str, str] = {}
        for task in tasks:
            key = context_rules.normalize_path(task.file)

            if key in ast_blocks:
                continue
            block = context_rules.ast_block_for_file(ast_text, task.file)
            if block is not None:
                ast_blocks[key] = block

        plan = context_rules.plan_context_calls(tasks, risk_profiles, ast_blocks)
        level1_content: dict[tuple[context_rules.ContextLevel, str], str] = {}
        failed_level1: dict[tuple[context_rules.ContextLevel, str], str] = {}
        gathered = list(ctx.gathered_context)
        if tool_client is not None and plan.level1_calls:
            outcomes = run_bounded_parallel(
                list(plan.level1_calls),
                lambda call: _execute_level1_call(call, tool_client),
                max_workers=8,
            )
            for outcome in outcomes:
                if outcome is None:
                    continue
                call, content, error = outcome
                if content is None:
                    failed_level1[(call.level, call.key)] = error or "tool_failed"
                    continue
                level1_content[(call.level, call.key)] = content
                gathered.append(GatheredContext(call.level.value, call.key, content))

        task_bundles: dict[str, TaskContextBundle] = {}
        trace: list[CouncilTrace] = [
            CouncilTrace(
                node="context_provider",
                event="bundle_created",
                detail=f"facts={len(bundle.facts)} tasks={len(tasks)}",
            )
        ]
        for task in tasks:
            facts: list[ContextFact] = []
            statuses: list[ContextStatus] = []
            ast_block = ast_blocks.get(context_rules.normalize_path(task.file))
            if ast_block:
                facts.append(
                    ContextFact(
                        source="tool:get_diff_ast",
                        kind="ast_structure",
                        content=ast_block,
                    )
                )
            else:
                ast_failure = broad_diagnostics.get("ast_structure")
                statuses.append(
                    ContextStatus(
                        kind="ast_structure",
                        status="failed" if ast_failure else "unavailable",
                        reason=(
                            ast_failure
                            or (
                                "tool_server_not_configured"
                                if tool_client is None
                                else "no_parseable_ast_for_current_file"
                            )
                        ),
                    )
                )
            sensitive_rows = context_rules.sensitive_api_rows_for_task(sensitive_text, task)
            if sensitive_rows:
                facts.append(
                    ContextFact(
                        source="tool:find_sensitive_apis",
                        kind="sensitive_api",
                        content="\n".join(sensitive_rows),
                    )
                )
            else:
                sensitive_failure = broad_diagnostics.get("sensitive_api")
                statuses.append(
                    ContextStatus(
                        kind="sensitive_api",
                        status=(
                            "skipped"
                            if scope.active
                            else ("failed" if sensitive_failure else "unavailable")
                        ),
                        reason=(
                            "large_diff_broad_scan_disabled"
                            if scope.active
                            else (
                                sensitive_failure
                                or (
                                    "tool_server_not_configured"
                                    if tool_client is None
                                    else "no_matching_sensitive_api_current_hunk"
                                )
                            )
                        ),
                    )
                )

            level1_labels: list[str] = []
            for call in plan.level1_calls:
                if task.id not in call.task_ids:
                    continue
                content = level1_content.get((call.level, call.key))
                if content is None:
                    failure = failed_level1.get((call.level, call.key))
                    if failure:
                        statuses.append(
                            ContextStatus(
                                kind=call.level.value,
                                status="failed",
                                reason=failure,
                            )
                        )
                    continue
                facts.append(
                    ContextFact(
                        source=f"tool:{call.level.value}",
                        kind=call.level.value,
                        content=content,
                    )
                )
                level1_labels.append(f"{call.level.value}({call.key})")

            for skip in plan.skips:
                if skip.task_id == task.id:
                    statuses.append(
                        ContextStatus(
                            kind=skip.level.value,
                            status="skipped",
                            reason=skip.reason,
                        )
                    )
            present_kinds = {fact.kind for fact in facts} | {
                status.kind for status in statuses
            }
            for level in context_rules.ContextLevel:
                if level.value not in present_kinds:
                    statuses.append(
                        ContextStatus(
                            kind=level.value,
                            status="skipped",
                            reason="risk_tag_not_required",
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
            skip_reasons = [skip.reason for skip in plan.skips if skip.task_id == task.id]
            failure_reasons = [
                failed_level1[(call.level, call.key)]
                for call in plan.level1_calls
                if task.id in call.task_ids and (call.level, call.key) in failed_level1
            ]
            trace.append(
                CouncilTrace(
                    node="context_provider",
                    event="task_bundle_filled",
                    detail=(
                        f"task={task.id} facts={len(facts)} level1={level1_labels} "
                        f"skips={skip_reasons} failed={failure_reasons} truncated={truncated}"
                    ),
                )
            )

        return {
            "context_bundle": bundle,
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
        if review_task is not None:
            return {
                "user_prompt": build_reviewer_user_prompt(
                    task=review_task,
                    summary=state.get("diff_summary", ""),
                    risk_profile=state.get("risk_profile"),
                    context_bundle=state.get("task_context_bundle"),
                    task_knowledge=state.get("task_knowledge", ""),
                )
            }
        user = _build_user_prompt(
            state["diff_text"], summary=state.get("diff_summary", "")
        )
        task_risk_context = state.get("task_risk_context")
        if task_risk_context:
            user += "\n\n" + task_risk_context
        else:
            bundle = state.get("context_bundle")
            if bundle is not None:
                user += "\n\n<shared_context>\n" + bundle.render() + "\n</shared_context>"
        return {"user_prompt": user}

    def _review(state: ReviewerState) -> dict:
        if llm is None:
            if reviewer.source_agent == "threat_model":
                return {"outcome": ReviewOutcome(mock_review_result())}
            return {"outcome": ReviewOutcome(ReviewResult(summary=""))}
        tier = state.get("tier")
        effective_tool_client = state.get("review_tool_client") or tool_client
        # tool_client=None 时 _make_engine 恒返回 DirectEngine，故意走同一工厂函数而不是
        # 直接 DirectEngine()，是为了保留 _make_engine 作为唯一的引擎选择入口
        # (可测试/可 monkeypatch 的 seam)，不是遗留笔误。
        engine = (
            _make_engine(state, tool_client=None)
            if tier == "direct"
            else _make_engine(state, tool_client=effective_tool_client)
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
        # tier=="direct" 时空结果是低风险任务的正确结论（不是故障），且本就已经是
        # DirectEngine 跑的，同引擎重跑一次不会改变结果，只会白白翻倍成本——跳过降级。
        # tier is None（selection is None 的旧兼容路径不设置 tier）保持历史行为不变：
        # 无条件降级复审，与 Phase4 之前完全一致。
        if tier != "direct" and not outcome.result.issues:
            logger.warning(
                "[%s] ReAct 未产出 issue,降级直连复审以保住该域覆盖", reviewer.name
            )
            outcome = _direct_fallback(state)
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
        selection = state.get("task_selection")

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

        routed_ids = (
            set(routed_task_ids(reviewer.source_agent, tasks, profiles, selection))
            if selection is not None
            else None
        )
        if routed_ids is not None and not routed_ids:
            return {
                "candidate_issues": [],
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

        if selection is None:
            # 兼容路径：无任务化 State（测试 / 非任务化调用场景）——整份 diff 一次调用，
            # 沿用 map_candidate_to_task 的按 file/line 猜测归属。
            result = subgraph.invoke(
                {
                    "diff_text": state.get("diff_text", ""),
                    "enabled_tools": effective_tools,
                    "max_retries": state.get("max_retries", 3),
                    "structured_method": state.get("structured_method", "function_calling"),
                    "diff_summary": state.get("diff_summary", ""),
                    "react_recursion_limit": state.get("react_recursion_limit", 24),
                    "context_bundle": state.get("context_bundle"),
                    "review_tool_client": _task_tool_client(),
                }
            )
            issues = list(result.get("issues") or [])
            kept_issues = issues[:MAX_CANDIDATES_PER_AGENT]
            truncated_candidates = max(0, len(issues) - len(kept_issues))
            candidates: list[CandidateIssue] = []
            rejected_unmapped: list[str] = []
            accepted_count = 0
            for issue in kept_issues:
                task_id = task_prep.map_candidate_to_task(issue.file, issue.line, tasks)
                if task_id is None:
                    rejected_unmapped.append(f"{issue.file}:{issue.line}")
                    continue
                accepted_count += 1
                candidates.append(
                    CandidateIssue.from_issue(
                        issue, source_agent=reviewer.source_agent,
                        index=accepted_count, task_id=task_id,
                    )
                )
            trace: list[CouncilTrace] = list(result.get("council_trace") or [])
            trace.append(
                CouncilTrace(
                    node=reviewer.source_agent,
                    event="candidates_created",
                    detail=(
                        f"count={len(candidates)} truncated={truncated_candidates} "
                        f"rejected_unmapped={len(rejected_unmapped)}"
                    ),
                )

            )
            if rejected_unmapped:
                trace.append(
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event="candidate_rejected_unmapped",
                        detail="; ".join(rejected_unmapped),
                    )
                )
            out: dict = {
                "candidate_issues": candidates,
                "truncated_candidates": truncated_candidates,
                "council_trace": trace,
            }
            for key in ("gathered_context", "review_summaries"):
                if result.get(key):
                    out[key] = result[key]
            return out

        # Phase4：每个路由到的 task 独立调用，task 间并发派发。
        task_by_id = {t.id: t for t in tasks}
        task_context_bundles = state.get("task_context_bundles") or {}
        ordered_ids = list(routed_task_ids(reviewer.source_agent, tasks, profiles, selection))
        budget = state.get("review_budget") or ReviewBudget()
        tier_by_task = plan_task_tiers(
            selection.selected_task_ids,
            profiles,
            budget.max_react_tasks,
            tools_available=tool_client is not None,
        )

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
            active_tags = (
                [tag for tag, score in profile.tag_scores.items() if score > 0]
                if profile is not None
                else []
            )
            task_knowledge = load_knowledge(reviewer.source_agent, active_tags)
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
                    "review_task": scoped_task,
                    "risk_profile": profile,
                    "task_context_bundle": bundle,
                    "task_knowledge": task_knowledge,
                    "tier": tier,
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
            "candidate_issues": candidates,
            "truncated_candidates": truncated_candidates,
            "council_trace": trace,
        }
        if gathered_context:
            routed_out["gathered_context"] = gathered_context
        if review_summaries:
            routed_out["review_summaries"] = review_summaries
        return routed_out

    return _node


def _coordinator_node():
    """三路发现者的显式 fan-in barrier：只在三路结束后运行一次。

    只记录本轮候选/证据请求批次统计，固定转入 EvidenceAgent；
    不承担"是否跳过首次补证"的路由决策，也不解析自然语言（spec §4.7）。
    """

    def _node(state: ReviewState) -> dict:
        candidates = state.get("candidate_issues") or []
        pending = state.get("evidence_requests") or []
        return {
            "council_trace": [
                CouncilTrace(
                    node="council_coordinator",
                    event="fan_in",
                    detail=f"candidates={len(candidates)} evidence_requests={len(pending)}",
                )
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
    )


def _evidence_planner_node(effective_judge_llm):
    """EvidencePlanner 是 graph 中 evidence_requests 的唯一写入者。"""

    def _node(state: ReviewState) -> dict:
        assembly = _assemble_state_dossiers(state)
        plan = plan_evidence(
            assembly.dossiers,
            classifier_llm=effective_judge_llm,
            structured_method=state.get("structured_method", "function_calling"),
        )
        trace = [
            CouncilTrace(node="evidence_planner", event=event, detail=detail)
            for event, detail in (*assembly.trace, *plan.trace)
        ]
        if not assembly.dossiers:
            trace.append(
                CouncilTrace(
                    node="evidence_planner",
                    event="no_op",
                    detail="no valid candidate dossiers",
                )
            )
        return {"evidence_requests": plan.requests, "council_trace": trace}

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
            enabled_tools=state.get("enabled_tools"),
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


def build_review_graph(*, enable_summary: bool = True, checkpointer=None, llm=None, fp_verify_llm=None, tool_client=None):
    """编译 ADR-032 审查状态图（一次性证据 + 固定策略定级）。

    拓扑:
        diff_task_builder → risk_triage → task_rank → summary? → context_provider
          → discover_*(×3) → council_coordinator(fan-in)
          → evidence_planner → evidence_agent → council_judge → END
    """
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(ReviewState)
    effective_judge_llm = fp_verify_llm or llm
    g.add_node("context_provider", _context_provider_node(tool_client))
    g.add_node("diff_task_builder", _diff_task_builder_node())
    g.add_node("risk_triage", _risk_triage_node())
    g.add_node("task_rank", _task_rank_node())
    for reviewer in DEFAULT_REVIEWERS:
        g.add_node(
            _discover_node_name(reviewer),
            make_reviewer_node(reviewer, checkpointer=checkpointer, llm=llm, tool_client=tool_client),
        )
    g.add_node("council_coordinator", _coordinator_node())
    g.add_node("evidence_planner", _evidence_planner_node(effective_judge_llm))
    g.add_node(
        "evidence_agent",
        _evidence_agent_node(tool_client, judge_llm=effective_judge_llm),
    )
    g.add_node(
        "council_judge",
        _council_judge_node(llm, judge_llm=effective_judge_llm),
    )

    g.add_edge(START, "diff_task_builder")
    g.add_edge("diff_task_builder", "risk_triage")
    g.add_edge("risk_triage", "task_rank")
    if enable_summary:
        g.add_node("summary", _summary_node(llm, tool_client))
        g.add_edge("task_rank", "summary")
        g.add_edge("summary", "context_provider")
    else:
        g.add_edge("task_rank", "context_provider")

    for reviewer in DEFAULT_REVIEWERS:
        node_name = _discover_node_name(reviewer)
        g.add_edge("context_provider", node_name)
        g.add_edge(node_name, "council_coordinator")

    # 三路 fan-in 后一次性规划、收集证据、综合定级。
    g.add_edge("council_coordinator", "evidence_planner")
    g.add_edge("evidence_planner", "evidence_agent")
    g.add_edge("evidence_agent", "council_judge")
    g.add_edge("council_judge", END)
    return g.compile(checkpointer=checkpointer)
