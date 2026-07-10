"""ADR-032 ReviewCouncil 编排图。

默认拓扑:

    START → [summary] → diff_task_builder → risk_triage → task_rank
              → context_provider → discover_* → council_coordinator(fan-in)
              → evidence_agent → council_judge
                    ↑                   │
                    └──(needs_more)──────┤
                                         │
                                        END

旧 LLM Supervisor 图已迁移到 `services/agent/legacy/supervisor_graph/graph.py`,
仅作历史参考,不再作为主编排运行回退。
challenge_agent 和 self_checker 节点已合并为 council_judge(规则+LLM 混合裁决)。
"""

from __future__ import annotations

import logging
import operator
from typing import Annotated, Any, TypedDict

from codeguard_agent.llm.client import mock_review_result
from codeguard_agent.models.council import (
    CandidateIssue,
    ContextBundle,
    CouncilRunStats,
    CouncilTrace,
    DEFAULT_MAX_EVIDENCE_ROUNDS as COUNCIL_DEFAULT_MAX_EVIDENCE_ROUNDS,
    EvidenceJudgment,
    EvidenceNote,
    EvidenceNoteStatus,
    EvidenceRequest,
    JudgeDecision,  # noqa: F401  # 供测试通过 G.JudgeDecision 访问
    JudgeDecisions,
    MAX_CANDIDATES_PER_AGENT,
    MAX_EVIDENCE_REQUESTS_PER_CANDIDATE,
    MAX_TOTAL_EVIDENCE_REQUESTS,
    Verdict,
    build_evidence_requests,
)
from codeguard_agent.models.schemas import ReviewResult, Severity
from codeguard_agent.models.tasks import (
    ReviewBudget,
    ReviewTask,
    RiskProfile,
    TaskContextBundle,
    TaskSelection,
)
from codeguard_agent.pipeline import task_prep
from codeguard_agent.pipeline.engines import (
    DirectEngine,
    GatheredContext,
    ReviewEngine,
    ReviewOutcome,
    ToolAgentEngine,
)
from codeguard_agent.pipeline.stages.base import PipelineContext
from codeguard_agent.pipeline.stages.context_provider import ContextProviderStage
from codeguard_agent.pipeline.stages.reviewer_stage import (
    DEFAULT_REVIEWERS,
    Reviewer,
    _build_user_prompt,
    _load_prompt,
)
from codeguard_agent.pipeline.stages.aggregation import (
    _MergePlan,
    _apply_merge_plan,
    _format_issues,
    deduplicate,
)
from codeguard_agent.pipeline.stages.summary import SummaryStage

logger = logging.getLogger("codeguard")

DEFAULT_MAX_ROUNDS = 1
DEFAULT_MAX_EVIDENCE_ROUNDS = COUNCIL_DEFAULT_MAX_EVIDENCE_ROUNDS
DEFAULT_RECURSION_LIMIT = 50

_ALL_REVIEWER_NAMES = [r.source_agent for r in DEFAULT_REVIEWERS]


def dedup_gathered_reducer(existing: list | None, new: list | None) -> list:
    """`gathered_context` reducer:按 `(tool, args)` 去重,保留首次出现顺序。"""
    merged = list(existing or []) + list(new or [])
    seen: set[tuple[str, str]] = set()
    out: list = []
    for it in merged:
        key = (getattr(it, "tool", ""), getattr(it, "args", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def capped_evidence_request_reducer(existing: list | None, new: list | None) -> list:
    """`evidence_requests` reducer:按稳定 ID 去重后再按全局上限截断。"""
    merged = list(existing or []) + list(new or [])
    seen: set[str] = set()
    unique: list[EvidenceRequest] = []
    for request in merged:
        if request.id in seen:
            continue
        seen.add(request.id)
        unique.append(request)
    return unique[:MAX_TOTAL_EVIDENCE_REQUESTS]


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

    # 层 2: 同文件+同根因合并（方法名匹配 + 邻行容差）
    if len(surviving) >= 2:
        final: list[CandidateIssue] = []
        seen: list[tuple[str, int, CandidateIssue, set[str]]] = []
        for c in surviving:
            c_file = (c.file or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
            # 从 claim 中提取方法名/变量名作为根因锚点
            c_tokens = _extract_identifier_tokens(c.claim)
            merged_into = None
            for s_file, s_line, survivor, s_tokens in seen:
                if s_file != c_file:
                    continue
                # 同方法名/同变量名 → 同一根因（不管行号差多少）
                if c_tokens and s_tokens and _share_key_identifier(c_tokens, s_tokens):
                    merged_into = survivor
                    break
                # 邻行容差(±3)兜底：同文件+行号接近
                if c.line > 0 and abs(c.line - s_line) <= 3:
                    merged_into = survivor
                    break
            if merged_into is None:
                seen.append((c_file, c.line, c, c_tokens))
                final.append(c)
        return final

    return surviving


# ── 辅助：从 claim 文本中提取 Java 标识符（方法名/变量名）──

def _extract_identifier_tokens(claim: str) -> set[str]:
    """提取 claim 中疑似 Java 标识符的 token（camelCase 或 lower_case 模式）。"""
    import re
    if not claim:
        return set()
    # 匹配 camelCase（如 getUserDisplayName、findById）和 snake_case 标识符
    idents = set(re.findall(r'\b[a-z_][a-zA-Z0-9_]*(?:[A-Z][a-z0-9_]+)+\b', claim))
    # 也匹配被反引号/代码字体包裹的标识符
    idents |= set(re.findall(r'`([a-zA-Z_][a-zA-Z0-9_]*)`', claim))
    # 过滤掉过短/过泛的词
    stop = {'the', 'and', 'for', 'get', 'set', 'has', 'is', 'not', 'new', 'try', 'all', 'any', 'the', 'this', 'that', 'with', 'from', 'into', 'null', 'true', 'false', 'when', 'case', 'line', 'file', 'code', 'diff', '非本次变更'}
    return {t for t in idents if len(t) >= 4 and t.lower() not in stop}


def _share_key_identifier(tokens_a: set[str], tokens_b: set[str]) -> bool:
    """两组 token 是否共享至少一个「关键」标识符（长度 ≥ 5 且非泛词）。"""
    shared = tokens_a & tokens_b
    return any(len(t) >= 5 for t in shared)


def _discover_node_name(reviewer: Reviewer) -> str:
    return f"discover_{reviewer.source_agent}"


class ReviewState(TypedDict, total=False):
    """ADR-032 图共享状态。"""

    diff_text: str
    enabled_tools: Any
    max_retries: int
    structured_method: str
    react_recursion_limit: int
    max_evidence_rounds: int

    diff_summary: str

    review_budget: ReviewBudget
    review_tasks: list[ReviewTask]
    risk_profiles: dict[str, RiskProfile]
    task_selection: TaskSelection
    task_context_bundles: dict[str, TaskContextBundle]

    context_bundle: ContextBundle
    candidate_issues: Annotated[list[CandidateIssue], _candidate_dedup_reducer]
    evidence_requests: Annotated[list[EvidenceRequest], capped_evidence_request_reducer]
    evidence_notes: Annotated[list[EvidenceNote], operator.add]
    council_verdicts: list  # council_judge 产出，_route_after_council_judge 读取（非 Annotated，每轮覆盖）
    council_trace: Annotated[list[CouncilTrace], operator.add]
    evidence_round: int
    truncated_candidates: Annotated[int, operator.add]
    truncated_evidence_requests: Annotated[int, operator.add]

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


def _summary_node(llm, tool_client):
    def _node(state: ReviewState) -> dict:
        ctx = _state_to_context(state, llm=llm, tool_client=tool_client)
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
    """RiskTriage：为每个任务产出 RiskProfile（Phase 1 为空画像）。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        profiles = task_prep.triage_tasks(tasks)
        return {
            "risk_profiles": profiles,
            "council_trace": [
                CouncilTrace(
                    node="risk_triage",
                    event="profiled",
                    detail=f"profiles={len(profiles)}",
                )
            ],
        }

    return _node


def _task_rank_node():
    """TaskRank：根据画像与预算选择进入深审的任务（Phase 1 全选）。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        profiles = state.get("risk_profiles") or {}
        budget = state.get("review_budget") or ReviewBudget()
        selection = task_prep.rank_tasks(tasks, profiles, budget)
        return {
            "task_selection": selection,
            "council_trace": [
                CouncilTrace(
                    node="task_rank",
                    event="selected",
                    detail=f"selected={len(selection.selected_task_ids)} skipped={len(selection.skipped_tasks)}",
                )
            ],
        }

    return _node


def _context_provider_node(tool_client):
    def _node(state: ReviewState) -> dict:
        ctx = _state_to_context(state, tool_client=tool_client)
        ContextProviderStage().execute(ctx)
        selection = state.get("task_selection")
        selected_ids = selection.selected_task_ids if selection is not None else []
        # Phase 1：为每个选中任务建立空 TaskContextBundle，确立所有权；
        # 按 RiskTag 定向填充留到 Phase 3。
        task_bundles = {tid: TaskContextBundle(task_id=tid) for tid in selected_ids}
        return {
            "context_bundle": ctx.context_bundle,
            "gathered_context": list(ctx.gathered_context),
            "task_context_bundles": task_bundles,
            "council_trace": [
                CouncilTrace(
                    node="context_provider",
                    event="bundle_created",
                    detail=f"facts={len(ctx.context_bundle.facts)} task_bundles={len(task_bundles)}",
                )
            ],
        }

    return _node


def build_reviewer_subgraph(reviewer: Reviewer, checkpointer=None, llm=None, tool_client=None):
    """把发现者 Agent 构造成 prepare → review → collect 子图。"""
    from langgraph.graph import END, START, StateGraph

    def _direct_fallback(state: ReviewerState) -> ReviewOutcome:
        return DirectEngine().review(
            llm,
            system_prompt=_load_prompt(reviewer.prompt_file),
            user_prompt=state.get("user_prompt", ""),
            reviewer_name=reviewer.name,
            max_retries=state.get("max_retries", 3),
            structured_method=state.get("structured_method", "function_calling"),
        )

    def _prepare(state: ReviewerState) -> dict:
        if llm is None:
            return {}
        user = _build_user_prompt(
            state["diff_text"], summary=state.get("diff_summary", "")
        )
        bundle = state.get("context_bundle")
        if bundle is not None:
            user += "\n\n<shared_context>\n" + bundle.render() + "\n</shared_context>"
        return {"user_prompt": user}

    def _review(state: ReviewerState) -> dict:
        if llm is None:
            if reviewer.source_agent == "threat_model":
                return {"outcome": ReviewOutcome(mock_review_result())}
            return {"outcome": ReviewOutcome(ReviewResult(summary=""))}
        engine = _make_engine(state, tool_client=tool_client)
        try:
            outcome = engine.review(
                llm,
                system_prompt=_load_prompt(reviewer.prompt_file),
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
        if not outcome.result.issues:
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
    subgraph = build_reviewer_subgraph(reviewer, checkpointer=checkpointer, llm=llm, tool_client=tool_client)

    def _node(state: ReviewState) -> dict:
        effective_tools = (
            state.get("enabled_tools")
            if state.get("enabled_tools") is not None
            else reviewer.tool_allowlist
        )
        result = subgraph.invoke(
            {
                "diff_text": state.get("diff_text", ""),
                "enabled_tools": effective_tools,
                "max_retries": state.get("max_retries", 3),
                "structured_method": state.get("structured_method", "function_calling"),
                "diff_summary": state.get("diff_summary", ""),
                "react_recursion_limit": state.get("react_recursion_limit", 24),
                "context_bundle": state.get("context_bundle"),
            }
        )
        issues = list(result.get("issues") or [])
        kept_issues = issues[:MAX_CANDIDATES_PER_AGENT]
        truncated_candidates = max(0, len(issues) - len(kept_issues))
        tasks = state.get("review_tasks") or []
        # 只接纳 TaskRank 选中的任务；selection 缺席（直连节点测试等）时不设门槛。
        selection = state.get("task_selection")
        selected_ids = set(selection.selected_task_ids) if selection is not None else None
        candidates: list[CandidateIssue] = []
        rejected_unmapped: list[str] = []
        rejected_unselected: list[str] = []
        accepted_count = 0
        for issue in kept_issues:
            task_id = task_prep.map_candidate_to_task(issue.file, issue.line, tasks)
            if task_id is None:
                rejected_unmapped.append(f"{issue.file}:{issue.line}")
                continue
            if selected_ids is not None and task_id not in selected_ids:
                # 任务被 TaskRank 跳过（如 Phase 2 Top-K）→ 不进黑板，让预算真正生效。
                rejected_unselected.append(f"{issue.file}:{issue.line} -> {task_id}")
                continue
            accepted_count += 1
            candidates.append(
                CandidateIssue.from_issue(
                    issue,
                    source_agent=reviewer.source_agent,
                    index=accepted_count,
                    task_id=task_id,
                )
            )
        truncated_evidence_requests = 0
        requests: list[EvidenceRequest] = []
        for candidate in candidates:
            candidate_requests = build_evidence_requests(candidate)
            requests.extend(candidate_requests[:MAX_EVIDENCE_REQUESTS_PER_CANDIDATE])
            truncated_evidence_requests += max(
                0,
                len(candidate_requests) - MAX_EVIDENCE_REQUESTS_PER_CANDIDATE,
            )
        trace: list[CouncilTrace] = list(result.get("council_trace") or [])
        trace.append(
            CouncilTrace(
                node=reviewer.source_agent,
                event="candidates_created",
                detail=(
                    f"count={len(candidates)} truncated={truncated_candidates} "
                    f"rejected_unmapped={len(rejected_unmapped)} "
                    f"rejected_unselected={len(rejected_unselected)}"
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
        if rejected_unselected:
            trace.append(
                CouncilTrace(
                    node=reviewer.source_agent,
                    event="candidate_rejected_unselected",
                    detail="; ".join(rejected_unselected),
                )
            )
        out: dict = {
            "candidate_issues": candidates,
            "evidence_requests": requests,
            "truncated_candidates": truncated_candidates,
            "truncated_evidence_requests": truncated_evidence_requests,
            "council_trace": trace,
        }
        for key in ("gathered_context", "review_summaries"):
            if result.get(key):
                out[key] = result[key]
        return out

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


def _route_after_council_judge(state: ReviewState) -> str:
    """council_judge 之后：有 needs_more_evidence 且轮次未超 → 补证；否则 END。"""
    verdicts = state.get("council_verdicts") or []
    evidence_round = state.get("evidence_round", 0)
    max_rounds = state.get("max_evidence_rounds", DEFAULT_MAX_EVIDENCE_ROUNDS)

    has_needs_more = any(
        getattr(v, "action", "") == "needs_more_evidence" for v in verdicts
    )
    if has_needs_more and evidence_round < max_rounds:
        return "evidence_agent"
    return "END"


def _evidence_agent_node(tool_client=None, judge_llm=None):
    """EvidenceAgent:调 Java 工具补证，再用 LLM 分析证据含义。

    两阶段:
      1. 调 Java 工具获取原始事实（去重调用）
      2. 对每条工具输出调 LLM 分析：SUPPORTS / CONTRADICTS / INSUFFICIENT
    judge_llm 不可用时回退 raw output[:200] 模式。
    """
    from codeguard_agent.tools.tool_client import ToolResponse as _ToolResponse

    def _tool_routes():
        return {
            "get_file_content": (
                lambda target=None, **_kw: tool_client.get_file_content(target) if target else _ToolResponse(False, error="缺少文件路径"),
                lambda c, r: {"target": r.target or c.file},
            ),
            "find_sensitive_apis": (
                lambda **_kw: tool_client.find_sensitive_apis(),
                lambda c, r: {},
            ),
            "find_callers": (
                lambda query=None, **_kw: tool_client.find_callers(query) if query else _ToolResponse(False, error="缺少查询参数"),
                lambda c, r: {"query": f"{c.file}#{r.target or c.line}"},
            ),
            "get_code_metrics": (
                lambda file_path=None, **_kw: tool_client.get_code_metrics(file_path) if file_path else _ToolResponse(False, error="缺少文件路径"),
                lambda c, r: {"file_path": r.target or c.file},
            ),
        }

    def _analyse_evidence(
        judge_llm,
        structured_method: str,
        candidate: CandidateIssue,
        tool_name: str,
        tool_output: str,
    ) -> EvidenceJudgment | None:
        """调 LLM 分析单条证据的含义。失败返回 None。"""
        from codeguard_agent.llm.client import invoke_with_retry

        system_prompt = _load_prompt("evidence-analysis.txt")
        user_prompt = (
            f"候选问题:\n"
            f"  文件: {candidate.file}:{candidate.line}\n"
            f"  类型: {candidate.type}\n"
            f"  主张: {candidate.claim}\n\n"
            f"工具: {tool_name}\n"
            f"工具返回:\n{tool_output[:3000]}\n"
        )
        try:
            structured = judge_llm.with_structured_output(
                EvidenceJudgment, method=structured_method
            )
            result = invoke_with_retry(
                structured,
                [("system", system_prompt), ("human", user_prompt)],
                max_retries=1,
            )
            if isinstance(result, EvidenceJudgment):
                return result
        except Exception:
            pass
        return None

    def _node(state: ReviewState) -> dict:
        processed_request_ids = {
            note.request_id for note in state.get("evidence_notes") or []
        }
        requests = [
            request
            for request in state.get("evidence_requests") or []
            if request.id not in processed_request_ids
        ]
        candidates = {c.id: c for c in state.get("candidate_issues") or []}
        notes: list[EvidenceNote] = []
        gathered: list[GatheredContext] = []
        called: set[tuple[str, str]] = set()
        structured_method = state.get("structured_method", "function_calling")

        routes = _tool_routes() if tool_client is not None else {}

        for req in requests:
            candidate = candidates.get(req.candidate_id)
            if candidate is None:
                continue
            supports: list[str] = []
            contradicts: list[str] = []
            unknowns: list[str] = []
            evidence_ids: list[str] = []

            if tool_client is not None and req.preferred_tools:
                for tool_name in req.preferred_tools:
                    if tool_name not in routes:
                        unknowns.append(f"EvidenceAgent 不支持工具:{tool_name}")
                        continue

                    call_fn, arg_builder = routes[tool_name]
                    kwargs = arg_builder(candidate, req)
                    dedup_key = (tool_name, str(kwargs))
                    if dedup_key in called:
                        continue
                    called.add(dedup_key)

                    try:
                        resp = call_fn(**kwargs)
                    except Exception as exc:  # noqa: BLE001
                        unknowns.append(f"工具 {tool_name} 调用异常: {exc}")
                        continue

                    content = resp.as_tool_output() if hasattr(resp, "as_tool_output") else str(resp)
                    gathered.append(GatheredContext(tool_name, str(kwargs), content))
                    success = getattr(resp, "success", True)
                    if not success or not content.strip():
                        unknowns.append(f"[{tool_name}] 无结果或失败")
                        continue

                    evidence_ids.append(f"tool:{tool_name}:{str(kwargs)}")

                    # ── LLM 证据分析 ──
                    if judge_llm is not None:
                        judgment = _analyse_evidence(
                            judge_llm, structured_method, candidate, tool_name, content
                        )
                        if judgment is not None:
                            entry = f"[{tool_name}] {judgment.reasoning}"
                            if judgment.judgment == "SUPPORTS":
                                supports.append(entry)
                            elif judgment.judgment == "CONTRADICTS":
                                contradicts.append(entry)
                            else:
                                unknowns.append(entry)
                            continue

                    # 回退：raw output 模式
                    supports.append(f"[{tool_name}] {content[:200]}")
            else:
                bundle = state.get("context_bundle")
                rendered = bundle.render(1200) if bundle is not None else ""
                target = req.target or candidate.file
                if target and target in rendered:
                    supports.append(f"ContextBundle 包含目标文件事实:{target}")
                    evidence_ids.append(f"context:{target}")
                else:
                    question = f" question={req.question}" if req.question else ""
                    unknowns.append(f"当前上下文不足以补证:{target or 'unknown'}{question}")

            # ── status 自动计算 ──
            status: EvidenceNoteStatus
            if supports and not contradicts:
                status = "supported"
            elif contradicts and not supports:
                status = "contradicted"
            elif supports and contradicts:
                status = "mixed"
            else:
                status = "insufficient"

            notes.append(
                EvidenceNote(
                    request_id=req.id,
                    candidate_id=req.candidate_id,
                    status=status,
                    supports=supports,
                    contradicts=contradicts,
                    unknowns=unknowns,
                    evidence_ids=evidence_ids,
                )
            )

        return {
            "evidence_notes": notes,
            "gathered_context": gathered,
            "evidence_round": state.get("evidence_round", 0) + 1,
            "council_trace": [
                CouncilTrace(
                    node="evidence_agent",
                    event="evidence_collected",
                    detail=f"requests={len(requests)} notes={len(notes)} tools_called={len(called)}",
                )
            ],
        }

    return _node


# ── CouncilJudge 规则层 ──

# 规则签名: (CandidateIssue, list[EvidenceNote], context_bundle) -> Verdict | None
# 返回 None 表示"不命中，交由下一条规则或 LLM"。


def _rule_invalid_file(candidate: CandidateIssue, _notes: list[EvidenceNote], _bundle) -> Verdict | None:
    """文件路径为空或明显无效 → drop。"""
    if not candidate.file or candidate.file.strip() == "":
        return Verdict(candidate_id=candidate.id, action="drop", reason_code="invalid_file", reason="候选指向的文件路径为空")
    return None


def _rule_contradicted(candidate: CandidateIssue, notes: list[EvidenceNote], _bundle) -> Verdict | None:
    """evidence 有 contradicts（证据反驳） + 低置信度 → drop。"""
    if not notes:
        return None
    has_contradiction = any(n.status in ("contradicted", "mixed") for n in notes)
    if has_contradiction and candidate.confidence < 0.5:
        return Verdict(candidate_id=candidate.id, action="drop", reason_code="contradicted", reason="证据包含反证且候选置信度低")
    return None


def _rule_no_evidence(candidate: CandidateIssue, notes: list[EvidenceNote], _bundle) -> Verdict | None:
    """全部 evidence insufficient + 低置信度 → drop。CRITICAL 额外 downgrade。"""
    if not notes:
        return None
    all_weak = all(n.status == "insufficient" for n in notes)
    if not all_weak:
        return None
    if candidate.confidence < 0.5:
        return Verdict(candidate_id=candidate.id, action="drop", reason_code="no_evidence", reason="全部证据不足以支持或反驳且置信度低")
    if candidate.severity_proposal == Severity.CRITICAL:
        return Verdict(
            candidate_id=candidate.id,
            action="downgrade",
            reason_code="critical_insufficient_evidence",
            reason="CRITICAL 判定但证据全部 insufficient，降级为 WARNING",
            severity_override=Severity.WARNING,
        )
    return None


def _rule_strong_support(candidate: CandidateIssue, notes: list[EvidenceNote], _bundle) -> Verdict | None:
    """高置信 + 全 supported + 零 contradicts → fast-track keep（跳过 LLM 终审）。"""
    if not notes:
        return None
    if candidate.confidence < 0.9:
        return None
    all_supported = all(n.status == "supported" for n in notes)
    if all_supported:
        return Verdict(
            candidate_id=candidate.id,
            action="keep",
            reason_code="strong_support",
            reason="高置信且证据完全支持，快速通道保留",
        )
    return None


# 规则列表（优先级从高到低）。
# 去重/合并复用 AggregationStage 的两段式去重（规则指纹 + LLM 语义综合）。
_COUNCIL_RULES = [
    _rule_invalid_file,
    _rule_strong_support,
    _rule_contradicted,
    _rule_no_evidence,
]


def _build_merge_prompt(issues: list) -> str:
    """构造 LLM 语义去重 user 消息（复用 AggregationStage prompt 文件）。"""
    user_tpl = _load_prompt("aggregation-user.txt")
    return (
        user_tpl
        .replace("{{summary}}", "(由 ReviewCouncil 裁决节点生成)")
        .replace("{{issues}}", _format_issues(issues))
    )


def _council_judge_node(llm, judge_llm=None):
    """CouncilJudge: 规则淘汰 → 两段式去重 → LLM 终审 → final_issues。

    三阶段:
      1. 规则列表逐条匹配 → 命中则产出 Verdict（确定性淘汰/降级）
      2. 复用 AggregationStage 两段式去重（规则指纹 + LLM 语义综合）— 用 judge_llm
      3. 去重后剩余候选 → LLM 终审（JudgeDecision）— 用 judge_llm
    judge_llm 默认回退到 llm；建议用异源+低温模型以提高稳定性。
    """
    _judge = judge_llm or llm

    def _build_llm_prompt(
        unhandled: list[CandidateIssue],
        handled: list[Verdict],
        bundle: ContextBundle | None,
        notes_by_candidate: dict[str, list[EvidenceNote]],
    ) -> tuple[str, dict[str, str]]:
        """构建 LLM 终审 prompt，同时返回短别名→真实 ID 的映射表。

        用短别名（C001/C002/...）替代冗长的真实 candidate_id，
        LLM 返回后再通过映射表校验并还原——不再无条件信任 LLM 返回的 ID。
        """
        template = _load_prompt("council-judge.txt")

        # 共享上下文
        context_block = ""
        if bundle is not None:
            context_block = "## 共享上下文\n" + bundle.render(2000)

        # 短别名映射: C001 → 真实 candidate_id
        alias_map: dict[str, str] = {}
        id_map: dict[str, str] = {}  # 反向: 真实 id → 短别名

        candidates_lines: list[str] = []
        if unhandled:
            for i, c in enumerate(unhandled):
                alias = f"C{i + 1:03d}"
                alias_map[alias] = c.id
                id_map[c.id] = alias

                evidence_lines: list[str] = []
                for note in notes_by_candidate.get(c.id, []):
                    for s in note.supports:
                        evidence_lines.append(f"    ✅ 支持: {s}")
                    for ct in note.contradicts:
                        evidence_lines.append(f"    ❌ 反驳: {ct}")
                    for u in note.unknowns:
                        evidence_lines.append(f"    ⚠️  不足: {u}")
                evidence_block = "\n".join(evidence_lines) if evidence_lines else "    (无证据)"

                candidates_lines.append(
                    f"--- 候选 {alias} ---\n"
                    f"file: {c.file}:{c.line}\n"
                    f"type: {c.type}\n"
                    f"severity: {c.severity_proposal}\n"
                    f"source: {c.source_agent}\n"
                    f"confidence: {c.confidence:.2f}\n"
                    f"claim: {c.claim}\n"
                    f"证据:\n{evidence_block}"
                )
        candidates_block = "\n\n".join(candidates_lines) if candidates_lines else "(无待裁决候选)"

        # 已被规则处理的候选
        handled_block = ""
        if handled:
            handled_lines = ["## 已被规则处理的候选（供参考，防止重复判断）"]
            for v in handled:
                handled_alias = id_map.get(v.candidate_id, v.candidate_id)
                handled_lines.append(f"- {handled_alias}: {v.action} ({v.reason_code}) {v.reason}")
            handled_block = "\n".join(handled_lines)

        return template.format(
            context_block=context_block,
            candidates_block=candidates_block,
            handled_block=handled_block,
        ), alias_map

    def _node(state: ReviewState) -> dict:
        candidates = list(state.get("candidate_issues") or [])
        notes_by_candidate: dict[str, list[EvidenceNote]] = {}
        for note in state.get("evidence_notes") or []:
            notes_by_candidate.setdefault(note.candidate_id, []).append(note)

        # ── 阶段 1: 规则列表（确定性淘汰/降级）──
        verdicts: list[Verdict] = []
        handled_ids: set[str] = set()
        remaining: list[CandidateIssue] = []

        for candidate in candidates:
            notes = notes_by_candidate.get(candidate.id, [])
            matched = False
            for rule in _COUNCIL_RULES:
                verdict = rule(candidate, notes, state.get("context_bundle"))
                if verdict is not None:
                    verdicts.append(verdict)
                    handled_ids.add(candidate.id)
                    matched = True
                    break
            if not matched:
                remaining.append(candidate)

        # ── 阶段 2: 两段式去重（复用 AggregationStage 的成熟逻辑）──
        # 规则指纹 + LLM 语义综合，解决"同源不同 type/行号漂移"的重复问题。
        if len(remaining) >= 2:
            # 2a. 规则指纹去重（确定性、零成本）
            issues = [c.to_issue() for c in remaining]
            deduped = deduplicate(issues)

            # 2b. LLM 语义综合（仅当去重后仍 ≥2 条且有 LLM）
            if len(deduped) >= 2 and _judge is not None:
                try:
                    from codeguard_agent.llm.client import invoke_with_retry

                    structured = _judge.with_structured_output(
                        _MergePlan, method=state.get("structured_method", "function_calling")
                    )
                    system_prompt = _load_prompt("aggregation-system.txt")
                    plan = invoke_with_retry(
                        structured,
                        [("system", system_prompt), ("human", _build_merge_prompt(deduped))],
                        max_retries=state.get("max_retries", 2),
                    )
                    if plan is not None and isinstance(plan, _MergePlan):
                        merged = _apply_merge_plan(deduped, plan)
                        if merged and len(merged) <= len(deduped):
                            deduped = merged
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[council_judge] LLM 语义综合失败,使用规则去重结果: %s", exc)

            # 2c. 映射回 CandidateIssue
            if len(deduped) < len(remaining):
                surviving: list[CandidateIssue] = []
                merged_ids: set[str] = set()

                for dedup_issue in deduped:
                    # 找原始候选中"最接近"这个去重结果的 candidate
                    best: CandidateIssue | None = None
                    best_score = -1
                    for c in remaining:
                        if c.id in merged_ids:
                            continue
                        score = 0
                        c_file = (c.file or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
                        if c_file == (dedup_issue.file or "").replace("\\", "/").rsplit("/", 1)[-1].lower():
                            score += 3
                        if c.type.lower() == dedup_issue.type.lower():
                            score += 2
                        if abs(c.line - (dedup_issue.line or 0)) <= 3:
                            score += 1
                        if score > best_score:
                            best_score = score
                            best = c
                    if best is not None:
                        surviving.append(best)
                        merged_ids.add(best.id)
                        for other in remaining:
                            if other.id != best.id and other.id not in merged_ids:
                                o_file = (other.file or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
                                if o_file == c_file and abs(other.line - best.line) <= 10:
                                    merged_ids.add(other.id)
                                    verdicts.append(Verdict(
                                        candidate_id=other.id,
                                        action="merge",
                                        reason_code="aggregation_merge",
                                        reason=f"聚合去重:与 {best.id} 指向同一底层问题",
                                        suggested_target_id=best.id,
                                    ))
                    else:
                        fallback = next(
                            (c for c in remaining if c.id not in merged_ids),
                            None,
                        )
                        if fallback is not None:
                            surviving.append(fallback)
                            merged_ids.add(fallback.id)
                remaining = surviving

        # ── 安全网：同文件+同根因合并（方法名匹配优先，邻行容差兜底）──
        if len(remaining) >= 2:
            deduped_remaining: list[CandidateIssue] = []
            seen: list[tuple[str, int, CandidateIssue, set[str]]] = []
            for c in remaining:
                c_file = (c.file or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
                c_tokens = _extract_identifier_tokens(c.claim)
                merged_into = None
                for s_file, s_line, survivor, s_tokens in seen:
                    if s_file != c_file:
                        continue
                    # 同方法名/同变量名 → 同一根因（不管行号差多少）
                    if c_tokens and s_tokens and _share_key_identifier(c_tokens, s_tokens):
                        merged_into = survivor
                        break
                    # 邻行容差(±3)兜底
                    if c.line > 0 and abs(c.line - s_line) <= 3:
                        merged_into = survivor
                        break
                if merged_into is not None:
                    verdicts.append(Verdict(
                        candidate_id=c.id,
                        action="merge",
                        reason_code="same_position_merge",
                        reason=f"同文件同根因:与 {merged_into.id} 指向同一底层问题，强制合并",
                        suggested_target_id=merged_into.id,
                    ))
                else:
                    seen.append((c_file, c.line, c, c_tokens))
                    deduped_remaining.append(c)
            remaining = deduped_remaining

        # ── 阶段 3: LLM 终审（仅处理去重后仍未裁决的候选）──
        unhandled = remaining
        if unhandled and _judge is not None:
            try:
                prompt, alias_map = _build_llm_prompt(
                    unhandled,
                    verdicts,
                    state.get("context_bundle"),
                    notes_by_candidate,
                )
                # 用 JudgeDecisions 包装而非 list[JudgeDecision]——
                # DeepSeek 等兼容端点不支持 list[T] 泛型作为 response_format。
                structured_llm = _judge.with_structured_output(
                    JudgeDecisions, method=state.get("structured_method", "function_calling")
                )
                llm_result = structured_llm.invoke([("human", prompt)])
                decisions = llm_result.decisions if isinstance(llm_result, JudgeDecisions) else []
                if isinstance(decisions, list):
                    seen_aliases: set[str] = set()
                    for decision in decisions:
                        raw_id = decision.candidate_id
                        # 校验: candidate_id 必须在 alias_map 中
                        real_id = alias_map.get(raw_id)
                        if real_id is None:
                            logger.warning(
                                "[council_judge] LLM 返回未知 candidate_id=%r，不在 alias_map 中，丢弃该裁决",
                                raw_id,
                            )
                            continue
                        # 校验: 同一候选不得重复裁决
                        if raw_id in seen_aliases:
                            logger.warning(
                                "[council_judge] LLM 对 %r 重复裁决，保留首次，丢弃后续", raw_id
                            )
                            continue
                        seen_aliases.add(raw_id)
                        # 转换 merge_target_id: 短别名 → 真实 ID
                        merge_target = decision.merge_target_id or ""
                        if merge_target and merge_target in alias_map:
                            merge_target = alias_map[merge_target]
                        verdicts.append(Verdict(
                            candidate_id=real_id,
                            action=decision.action,
                            reason_code="llm_judge",
                            reason=decision.reason,
                            suggested_target_id=merge_target,
                            severity_override=decision.adjusted_severity,
                            suggested_tools=decision.suggested_tools if decision.action == "needs_more_evidence" else [],
                        ))
                        handled_ids.add(real_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("CouncilJudge LLM 调用失败: %s，规则未命中的候选保守 keep", exc)

        # 未命中规则 + LLM 未返回（或返回无效 ID）→ 保守 keep
        for candidate in unhandled:
            if candidate.id not in handled_ids:
                verdicts.append(Verdict(
                    candidate_id=candidate.id,
                    action="keep",
                    reason_code="conservative_keep",
                    reason="规则与 LLM 均未判定，保守保留",
                ))

        # ── 阶段 3: 执行裁决 → final_issues ──
        merge_targets: dict[str, str] = {}  # candidate_id → merge into target_id
        drop_ids: set[str] = set()
        severity_overrides: dict[str, Severity] = {}
        new_evidence_requests: list[EvidenceRequest] = []
        existing_evidence_request_ids = {
            request.id for request in state.get("evidence_requests") or []
        }

        for v in verdicts:
            if v.action == "drop":
                drop_ids.add(v.candidate_id)
            elif v.action == "merge":
                drop_ids.add(v.candidate_id)
                merge_targets[v.candidate_id] = v.suggested_target_id
            elif v.action == "downgrade" and v.severity_override is not None:
                severity_overrides[v.candidate_id] = v.severity_override
            elif v.action == "needs_more_evidence":
                # 生成新 EvidenceRequest 供下轮 evidence_agent 补证
                matched_candidate = next((c for c in candidates if c.id == v.candidate_id), None)
                if matched_candidate is not None:
                    tools = v.suggested_tools or ["get_file_content"]
                    request = EvidenceRequest(
                        candidate_id=v.candidate_id,
                        target=matched_candidate.file,
                        question=f"[council_judge] {v.reason}",
                        preferred_tools=tools,
                    )
                    if request.id not in existing_evidence_request_ids:
                        existing_evidence_request_ids.add(request.id)
                        new_evidence_requests.append(request)

        final_candidates = [c for c in candidates if c.id not in drop_ids]
        for c in final_candidates:
            if c.id in severity_overrides:
                object.__setattr__(c, "severity_proposal", severity_overrides[c.id])

        final_issues = [c.to_issue() for c in final_candidates]

        # ── stats ──
        by_agent: dict[str, int] = {}
        for candidate in candidates:
            by_agent[candidate.source_agent] = by_agent.get(candidate.source_agent, 0) + 1
        stats = CouncilRunStats(
            candidate_count=len(candidates),
            candidate_count_by_agent=by_agent,
            evidence_request_count=len(state.get("evidence_requests") or []),
            truncated_candidates=state.get("truncated_candidates", 0),
            truncated_evidence_requests=state.get("truncated_evidence_requests", 0),
            evidence_rounds=state.get("evidence_round", 0),
            challenge_count=len(verdicts),
            removed_by_challenge=len(drop_ids),
        )

        summaries = state.get("review_summaries") or []
        summary = "  ".join(summaries)

        out = {
            "council_verdicts": verdicts,
            "final_issues": final_issues,
            "council_stats": stats,
            "summary": summary,
            "council_trace": [
                CouncilTrace(
                    node="council_judge",
                    event="judged",
                    detail=f"candidates={len(candidates)} rules={len(verdicts)-len([v for v in verdicts if v.reason_code=='llm_judge' or v.reason_code=='conservative_keep'])} llm={len([v for v in verdicts if v.reason_code=='llm_judge'])} final={len(final_issues)}",
                )
            ],
        }
        if new_evidence_requests:
            out["evidence_requests"] = new_evidence_requests
        return out

    return _node


def build_review_graph(*, enable_summary: bool = True, checkpointer=None, llm=None, fp_verify_llm=None, tool_client=None):
    """编译 ADR-032 审查状态图（风险路由 Phase 1）。

    目标拓扑:
        summary? → diff_task_builder → risk_triage → task_rank → context_provider
          → discover_*(×3) → council_coordinator(fan-in 一次)
          → evidence_agent(必经一次) → council_judge
          → [evidence_agent(needs_more 且轮次未超) | END]
    """
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(ReviewState)
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
    g.add_node("evidence_agent", _evidence_agent_node(tool_client, judge_llm=fp_verify_llm))
    g.add_node("council_judge", _council_judge_node(llm, judge_llm=fp_verify_llm))

    if enable_summary:
        g.add_node("summary", _summary_node(llm, tool_client))
        g.add_edge(START, "summary")
        g.add_edge("summary", "diff_task_builder")
    else:
        g.add_edge(START, "diff_task_builder")
    g.add_edge("diff_task_builder", "risk_triage")
    g.add_edge("risk_triage", "task_rank")
    g.add_edge("task_rank", "context_provider")

    for reviewer in DEFAULT_REVIEWERS:
        node_name = _discover_node_name(reviewer)
        g.add_edge("context_provider", node_name)
        g.add_edge(node_name, "council_coordinator")

    # 三路 fan-in 后固定进一次 EvidenceAgent，再进 CouncilJudge。
    g.add_edge("council_coordinator", "evidence_agent")
    g.add_edge("evidence_agent", "council_judge")
    # Judge 仅在 needs_more 且轮次未超时回环补证，否则 END。
    g.add_conditional_edges(
        "council_judge",
        _route_after_council_judge,
        {
            "evidence_agent": "evidence_agent",
            "END": END,
        },
    )
    return g.compile(checkpointer=checkpointer)
