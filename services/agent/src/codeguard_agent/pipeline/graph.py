"""ADR-032 ReviewCouncil 编排图。

默认拓扑:

    START → [summary] → context_provider → discover_* → council_coordinator
                                                ↑             │
                                                └ evidence/council_judge loop
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

from codeguard_agent.git.diff_collector import split_diff_by_file
from codeguard_agent.llm.client import mock_review_result
from codeguard_agent.models.council import (
    CandidateIssue,
    Challenge,
    ContextBundle,
    CouncilRunStats,
    CouncilTrace,
    DEFAULT_MAX_EVIDENCE_ROUNDS as COUNCIL_DEFAULT_MAX_EVIDENCE_ROUNDS,
    EvidenceNote,
    EvidenceRequest,
    JudgeDecision,  # noqa: F401  # 供测试通过 G.JudgeDecision 访问
    JudgeDecisions,
    MAX_CANDIDATES_PER_AGENT,
    MAX_EVIDENCE_REQUESTS_PER_CANDIDATE,
    MAX_TOTAL_EVIDENCE_REQUESTS,
    Verdict,
)
from codeguard_agent.models.schemas import ReviewResult, Severity
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
    _effective_diff,
    _file_group_for_reviewer,
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
    """`evidence_requests` reducer:合并后按全局上限截断。"""
    merged = list(existing or []) + list(new or [])
    return merged[:MAX_TOTAL_EVIDENCE_REQUESTS]


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
            if merged_into is not None:
                merged_into.evidence_notes = list(merged_into.evidence_notes) + list(c.evidence_notes)
                merged_into.evidence_ids = list(set(merged_into.evidence_ids + c.evidence_ids))
            else:
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
    enable_hitl: bool
    react_recursion_limit: int
    max_review_rounds: int
    max_evidence_rounds: int
    fp_llm_verify: bool

    diff_summary: str
    file_groups: dict
    change_types: list
    risk_level: int

    context_bundle: ContextBundle
    candidate_issues: Annotated[list[CandidateIssue], _candidate_dedup_reducer]
    evidence_requests: Annotated[list[EvidenceRequest], capped_evidence_request_reducer]
    evidence_notes: Annotated[list[EvidenceNote], operator.add]
    challenges: Annotated[list[Challenge], operator.add]
    council_verdicts: list  # council_judge 产出，供 coordinator 路由判断（非 Annotated，每轮覆盖）
    council_trace: Annotated[list[CouncilTrace], operator.add]
    evidence_round: int
    judge_pass: int  # council_judge 执行次数计数器
    council_route: str
    truncated_candidates: Annotated[int, operator.add]
    truncated_evidence_requests: Annotated[int, operator.add]

    issues: Annotated[list, operator.add]
    gathered_context: Annotated[list, dedup_gathered_reducer]
    review_summaries: Annotated[list, operator.add]
    dispatched: Annotated[set, operator.or_]

    final_issues: list
    summary: str
    filter_stats: Any
    council_stats: CouncilRunStats


class ReviewerState(TypedDict, total=False):
    """单个发现者 Agent 子图状态。"""

    diff_text: str
    enabled_tools: Any
    max_retries: int
    structured_method: str
    diff_summary: str
    file_groups: dict
    focus_notes: dict
    enable_hitl: bool
    react_recursion_limit: int
    context_bundle: ContextBundle

    issues: list
    gathered_context: list
    review_summaries: list
    dispatched: set
    council_trace: Annotated[list[CouncilTrace], operator.add]

    eff_diff: str
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
        file_groups=state.get("file_groups") or {},
        gathered_context=list(state.get("gathered_context") or []),
    )
    ctx.context_bundle = state.get("context_bundle")
    return ctx


def _summary_node(llm, tool_client):
    def _node(state: ReviewState) -> dict:
        ctx = _state_to_context(state, llm=llm, tool_client=tool_client)
        SummaryStage().execute(ctx)
        return {
            "diff_summary": ctx.diff_summary,
            "file_groups": ctx.file_groups,
            "change_types": ctx.change_types,
            "risk_level": ctx.risk_level,
        }

    return _node


def _context_provider_node(tool_client):
    def _node(state: ReviewState) -> dict:
        ctx = _state_to_context(state, tool_client=tool_client)
        ContextProviderStage().execute(ctx)
        return {
            "context_bundle": ctx.context_bundle,
            "gathered_context": list(ctx.gathered_context),
            "council_trace": [
                CouncilTrace(
                    node="context_provider",
                    event="bundle_created",
                    detail=f"facts={len(ctx.context_bundle.facts)}",
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
        file_groups = state.get("file_groups") or {}
        file_diffs = split_diff_by_file(state["diff_text"]) if file_groups else {}
        eff_diff = _effective_diff(
            state["diff_text"], file_diffs, _file_group_for_reviewer(file_groups, reviewer)
        )
        user = _build_user_prompt(eff_diff, summary=state.get("diff_summary", ""))
        bundle = state.get("context_bundle")
        if bundle is not None:
            user += "\n\n<shared_context>\n" + bundle.render() + "\n</shared_context>"
        return {"eff_diff": eff_diff, "user_prompt": user}

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
        return {"outcome": outcome}

    def _collect(state: ReviewerState) -> dict:
        outcome = state.get("outcome")
        out: dict = {
            "dispatched": {reviewer.source_agent},
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
                "file_groups": {},  # 三个审查员始终吃整份 diff，不做文件分派
                "focus_notes": {},
                "enable_hitl": False,
                "react_recursion_limit": state.get("react_recursion_limit", 24),
                "context_bundle": state.get("context_bundle"),
            }
        )
        issues = list(result.get("issues") or [])
        kept_issues = issues[:MAX_CANDIDATES_PER_AGENT]
        truncated_candidates = max(0, len(issues) - len(kept_issues))
        candidates = [
            CandidateIssue.from_issue(
                issue,
                source_agent=reviewer.source_agent,
                category=reviewer.category,
                index=i + 1,
            )
            for i, issue in enumerate(kept_issues)
        ]
        truncated_evidence_requests = 0
        for candidate in candidates:
            original_count = len(candidate.evidence_requests)
            candidate.evidence_requests = candidate.evidence_requests[
                :MAX_EVIDENCE_REQUESTS_PER_CANDIDATE
            ]
            truncated_evidence_requests += max(0, original_count - len(candidate.evidence_requests))
        requests = [req for c in candidates for req in c.evidence_requests]
        out: dict = {
            "dispatched": result.get("dispatched") or {reviewer.source_agent},
            "candidate_issues": candidates,
            "evidence_requests": requests,
            "truncated_candidates": truncated_candidates,
            "truncated_evidence_requests": truncated_evidence_requests,
            "council_trace": list(result.get("council_trace") or [])
            + [
                CouncilTrace(
                    node=reviewer.source_agent,
                    event="candidates_created",
                    detail=f"count={len(candidates)} truncated={truncated_candidates}",
                )
            ],
        }
        for key in ("gathered_context", "review_summaries"):
            if result.get(key):
                out[key] = result[key]
        return out

    return _node


def _coordinator_node():
    def _node(state: ReviewState) -> dict:
        candidates = state.get("candidate_issues") or []
        route = _route_after_coordinator(state)
        return {
            "council_route": route,
            "council_trace": [
                CouncilTrace(
                    node="council_coordinator",
                    event="route",
                    detail=f"{route}; candidates={len(candidates)}; evidence_round={state.get('evidence_round', 0)}",
                )
            ],
        }

    return _node


def _route_after_coordinator(state: ReviewState) -> str:
    """从 discover 节点或 evidence_agent 返回后，决定下一步。

    仅 evidence_round==0 时自动进 evidence_agent——后续补证
    只能由 _route_after_council_judge 触发，避免 evidence_requests
    因 reducer 累积导致反复路由。
    """
    candidates = state.get("candidate_issues") or []
    if not candidates:
        return "council_judge"

    evidence_round = state.get("evidence_round", 0)
    if evidence_round == 0:
        pending = state.get("evidence_requests") or []
        if pending:
            return "evidence_agent"

    return "council_judge"


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


def _conditional_route(state: ReviewState) -> str:
    return state.get("council_route") or _route_after_coordinator(state)


def _evidence_agent_node(tool_client=None):
    """EvidenceAgent:按 preferred_tools 确定性地调用 Java 工具补证。

    不再依赖 EvidenceKind——每种 preferred_tool 直接映射到一个 tool_client 方法。
    同一 (文件, 工具) 去重，避免三个候选对同一文件重复扫描。
    tool_client 不可用或 preferred_tools 为空时，回退 ContextBundle 字符串搜索。
    """
    from codeguard_agent.tools.tool_client import ToolResponse as _ToolResponse

    # 工具名 → (调用函数, 参数构造器)
    # 调用函数签名为 (**kwargs) -> ToolResponse
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

    def _node(state: ReviewState) -> dict:
        requests = state.get("evidence_requests") or []
        candidates = {c.id: c for c in state.get("candidate_issues") or []}
        notes: list[EvidenceNote] = []
        gathered: list[GatheredContext] = []
        # 去重：同一 (工具, 参数) 不重复调用
        called: set[tuple[str, str]] = set()

        routes = _tool_routes() if tool_client is not None else {}

        for req in requests:
            candidate = candidates.get(req.candidate_id)
            if candidate is None:
                continue
            supports: list[str] = []
            unknowns: list[str] = []
            evidence_ids: list[str] = []
            status = "mixed"

            if tool_client is not None and req.preferred_tools:
                for tool_name in req.preferred_tools:
                    if tool_name not in routes:
                        unknowns.append(f"EvidenceAgent 不支持工具:{tool_name}")
                        if status != "supported":
                            status = "unsupported"
                        continue

                    call_fn, arg_builder = routes[tool_name]
                    kwargs = arg_builder(candidate, req)
                    # 用去重键
                    dedup_key = (tool_name, str(kwargs))
                    if dedup_key in called:
                        continue
                    called.add(dedup_key)

                    try:
                        resp = call_fn(**kwargs)
                    except Exception as exc:  # noqa: BLE001 单工具失败不中断其他工具
                        unknowns.append(f"工具 {tool_name} 调用异常: {exc}")
                        if status != "supported":
                            status = "not_found"
                        continue

                    content = resp.as_tool_output() if hasattr(resp, "as_tool_output") else str(resp)
                    gathered.append(GatheredContext(tool_name, str(kwargs), content))
                    if getattr(resp, "success", True) and content.strip():
                        supports.append(f"[{tool_name}] {content[:200]}")
                        evidence_ids.append(f"tool:{tool_name}:{str(kwargs)}")
                        status = "supported"
                    else:
                        unknowns.append(f"[{tool_name}] 无结果或失败")
                        if status != "supported":
                            status = "not_found"
            else:
                # 兜底：tool_client 不可用或 preferred_tools 为空 → ContextBundle 搜索
                bundle = state.get("context_bundle")
                rendered = bundle.render(1200) if bundle is not None else ""
                target = req.target or candidate.file
                if target and target in rendered:
                    supports.append(f"ContextBundle 包含目标文件事实:{target}")
                    evidence_ids.append(f"context:{target}")
                    status = "supported"
                else:
                    question = f" question={req.question}" if req.question else ""
                    unknowns.append(f"当前上下文不足以补证:{target or 'unknown'}{question}")
                    status = "not_found"

            notes.append(
                EvidenceNote(
                    candidate_id=req.candidate_id,
                    status=status,
                    supports=supports,
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
    """evidence 有 contradicts + 低置信度 → drop。"""
    has_contradicts = any(n.contradicts for n in notes)
    if has_contradicts and candidate.confidence < 0.5:
        return Verdict(candidate_id=candidate.id, action="drop", reason_code="contradicted", reason="证据包含反证且候选置信度低")
    return None


def _rule_no_evidence(candidate: CandidateIssue, notes: list[EvidenceNote], _bundle) -> Verdict | None:
    """全部 evidence not_found + 低置信度 → drop。"""
    if not notes:
        return None
    all_not_found = all(n.status == "not_found" for n in notes)
    if all_not_found and candidate.confidence < 0.5:
        return Verdict(candidate_id=candidate.id, action="drop", reason_code="no_evidence", reason="无法获取任何支持证据且置信度低")
    return None


def _rule_quality_no_metrics(candidate: CandidateIssue, notes: list[EvidenceNote], _bundle) -> Verdict | None:
    """quality 类候选缺少维护性量化证据 → drop。"""
    if candidate.category != "quality":
        return None
    has_metrics = any("get_code_metrics" in eid for n in notes for eid in n.evidence_ids)
    if not has_metrics and candidate.evidence_status == "missing":
        return Verdict(candidate_id=candidate.id, action="drop", reason_code="quality_no_metrics", reason="维护性候选缺少量化度量证据")
    return None


def _rule_guard_detected(candidate: CandidateIssue, notes: list[EvidenceNote], _bundle) -> Verdict | None:
    """evidence 中检测到保护逻辑（sanitize/try-catch/校验）→ downgrade 或 drop。"""
    guard_keywords = ["sanitize", "try-catch", "try {", "validate", "校验", "escap", "filter"]
    for note in notes:
        for s in note.supports:
            if any(kw.lower() in s.lower() for kw in guard_keywords):
                return Verdict(
                    candidate_id=candidate.id,
                    action="downgrade",
                    reason_code="guard_detected",
                    reason="代码中已存在保护逻辑",
                    severity_override=Severity.INFO if candidate.severity_proposal == Severity.WARNING else Severity.WARNING,
                )
    return None


def _rule_critical_partial(candidate: CandidateIssue, notes: list[EvidenceNote], _bundle) -> Verdict | None:
    """CRITICAL 但 evidence 不足（无任何 supported）→ downgrade 到 WARNING。"""
    if candidate.severity_proposal != Severity.CRITICAL:
        return None
    has_no_support = notes and all(n.status in ("not_found", "unsupported", "mixed") for n in notes)
    if has_no_support:
        return Verdict(
            candidate_id=candidate.id,
            action="downgrade",
            reason_code="critical_partial_evidence",
            reason="CRITICAL 判定但证据不充分，降级为 WARNING",
            severity_override=Severity.WARNING,
        )
    return None


# 规则列表（优先级从高到低）。
# 去重/合并不再由单独规则处理，而是复用 AggregationStage 的两段式去重
# （规则指纹 + LLM 语义综合），在 council_judge 内部调用。
_COUNCIL_RULES = [
    _rule_invalid_file,
    _rule_contradicted,
    _rule_no_evidence,
    _rule_quality_no_metrics,
    _rule_guard_detected,
    _rule_critical_partial,
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

    def _build_llm_prompt(unhandled: list[CandidateIssue], handled: list[Verdict], bundle: ContextBundle | None) -> str:
        parts: list[str] = []
        parts.append("你是代码审查裁决者。对以下候选问题逐一判定：keep（保留）/ drop（淘汰）/ downgrade（降级）/ merge（合并）。")

        if bundle is not None:
            parts.append("\n## 共享上下文\n" + bundle.render(3000))

        if unhandled:
            parts.append("\n## 待裁决候选")
            for i, c in enumerate(unhandled):
                evidence_summary = ""
                for note in c.evidence_notes:
                    if note.supports:
                        evidence_summary += f"  证据支持: {'; '.join(note.supports[:3])}\n"
                    if note.unknowns:
                        evidence_summary += f"  证据不足: {'; '.join(note.unknowns[:3])}\n"
                parts.append(
                    f"[{i}] {c.id}\n"
                    f"  file={c.file}:{c.line} type={c.type} severity={c.severity_proposal}\n"
                    f"  source={c.source_agent} confidence={c.confidence:.2f}\n"
                    f"  claim={c.claim}\n"
                    f"{evidence_summary}"
                )

        if handled:
            parts.append("\n## 已被规则处理的候选（供参考，防止重复判断）")
            for v in handled:
                parts.append(f"- {v.candidate_id}: {v.action} ({v.reason_code}) {v.reason}")

        parts.append("\n对每条待裁决候选，输出一个 JSON 对象，包含 candidate_id / action / reason。")
        parts.append("action 取值: keep, drop, downgrade, merge。不确定时保守 keep。")
        return "\n".join(parts)

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

            # 2c. 映射回 CandidateIssue（保留被合并方的 evidence）
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
                        # 把其他没被选中的同源 candidate 的 evidence 合进来
                        for other in remaining:
                            if other.id != best.id and other.id not in merged_ids:
                                o_file = (other.file or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
                                if o_file == c_file and abs(other.line - best.line) <= 10:
                                    best.evidence_notes = list(best.evidence_notes) + list(other.evidence_notes)
                                    best.evidence_ids = list(set(best.evidence_ids + other.evidence_ids))
                                    merged_ids.add(other.id)
                                    verdicts.append(Verdict(
                                        candidate_id=other.id,
                                        action="merge",
                                        reason_code="aggregation_merge",
                                        reason=f"聚合去重:与 {best.id} 指向同一底层问题",
                                        suggested_target_id=best.id,
                                    ))
                    else:
                        surviving.append(dedup_issue)  # fallback
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
                    merged_into.evidence_notes = list(merged_into.evidence_notes) + list(c.evidence_notes)
                    merged_into.evidence_ids = list(set(merged_into.evidence_ids + c.evidence_ids))
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
                prompt = _build_llm_prompt(unhandled, verdicts, state.get("context_bundle"))
                # 用 JudgeDecisions 包装而非 list[JudgeDecision]——
                # DeepSeek 等兼容端点不支持 list[T] 泛型作为 response_format。
                structured_llm = _judge.with_structured_output(
                    JudgeDecisions, method=state.get("structured_method", "function_calling")
                )
                llm_result = structured_llm.invoke([("human", prompt)])
                decisions = llm_result.decisions if isinstance(llm_result, JudgeDecisions) else []
                if isinstance(decisions, list):
                    for decision in decisions:
                        verdicts.append(Verdict(
                            candidate_id=decision.candidate_id,
                            action=decision.action,
                            reason_code="llm_judge",
                            reason=decision.reason,
                            suggested_target_id=decision.merge_target_id,
                            severity_override=decision.adjusted_severity,
                            suggested_tools=decision.suggested_tools if decision.action == "needs_more_evidence" else [],
                        ))
                        handled_ids.add(decision.candidate_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("CouncilJudge LLM 调用失败: %s，规则未命中的候选保守 keep", exc)

        # 未命中规则 + LLM 未返回 → 保守 keep
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
                candidate = next((c for c in candidates if c.id == v.candidate_id), None)
                if candidate is not None:
                    tools = v.suggested_tools or ["get_file_content"]
                    new_evidence_requests.append(EvidenceRequest(
                        candidate_id=v.candidate_id,
                        target=candidate.file,
                        question=f"[council_judge] {v.reason}",
                        reason=v.reason,
                        preferred_tools=tools,
                        reason_code=v.reason_code,
                    ))

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

        # 合并新产生的 evidence_requests（needs_more_evidence 路径）
        all_evidence_requests = list(state.get("evidence_requests") or []) + new_evidence_requests
        return {
            "council_verdicts": verdicts,
            "final_issues": final_issues,
            "council_stats": stats,
            "summary": summary,
            "judge_pass": state.get("judge_pass", 0) + 1,
            "evidence_requests": all_evidence_requests,
            "council_trace": [
                CouncilTrace(
                    node="council_judge",
                    event="judged",
                    detail=f"candidates={len(candidates)} rules={len(verdicts)-len([v for v in verdicts if v.reason_code=='llm_judge' or v.reason_code=='conservative_keep'])} llm={len([v for v in verdicts if v.reason_code=='llm_judge'])} final={len(final_issues)}",
                )
            ],
        }

    return _node


def build_review_graph(*, enable_summary: bool = True, checkpointer=None, llm=None, fp_verify_llm=None, tool_client=None):
    """编译 ADR-032 审查状态图。

    目标拓扑:
        discover_* ─→ coordinator ─→ evidence_agent ─┐
                        ↑       ↑                     │
                        │   council_judge ←───────────┘
                        │       │
                        └── END (otherwise)
    """
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(ReviewState)
    g.add_node("context_provider", _context_provider_node(tool_client))
    for reviewer in DEFAULT_REVIEWERS:
        g.add_node(
            _discover_node_name(reviewer),
            make_reviewer_node(reviewer, checkpointer=checkpointer, llm=llm, tool_client=tool_client),
        )
    g.add_node("council_coordinator", _coordinator_node())
    g.add_node("evidence_agent", _evidence_agent_node(tool_client))
    g.add_node("council_judge", _council_judge_node(llm, judge_llm=fp_verify_llm))

    if enable_summary:
        g.add_node("summary", _summary_node(llm, tool_client))
        g.add_edge(START, "summary")
        g.add_edge("summary", "context_provider")
    else:
        g.add_edge(START, "context_provider")

    for reviewer in DEFAULT_REVIEWERS:
        node_name = _discover_node_name(reviewer)
        g.add_edge("context_provider", node_name)
        g.add_edge(node_name, "council_coordinator")

    g.add_conditional_edges(
        "council_coordinator",
        _conditional_route,
        {
            "evidence_agent": "evidence_agent",
            "council_judge": "council_judge",
        },
    )
    g.add_edge("evidence_agent", "council_coordinator")
    g.add_conditional_edges(
        "council_judge",
        _route_after_council_judge,
        {
            "evidence_agent": "evidence_agent",
            "END": END,
        },
    )
    return g.compile(checkpointer=checkpointer)
