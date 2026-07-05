"""ADR-032 ReviewCouncil 编排图。

默认拓扑:

    START → [summary] → context_provider → discover_* → council_coordinator
                                                ↑             │
                                                └ evidence/challenge loop
                                                              │
                                                        self_checker → END

旧 LLM Supervisor 图已迁移到 `services/agent/legacy/supervisor_graph/graph.py`,
仅作历史参考,不再作为主编排运行回退。
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
    MAX_CANDIDATES_PER_AGENT,
    MAX_EVIDENCE_REQUESTS_PER_CANDIDATE,
    MAX_TOTAL_EVIDENCE_REQUESTS,
)
from codeguard_agent.models.schemas import ReviewResult
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
from codeguard_agent.pipeline.stages.self_checker import SelfCheckerStage
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
    candidate_issues: Annotated[list[CandidateIssue], operator.add]
    evidence_requests: Annotated[list[EvidenceRequest], capped_evidence_request_reducer]
    evidence_notes: Annotated[list[EvidenceNote], operator.add]
    challenges: Annotated[list[Challenge], operator.add]
    council_trace: Annotated[list[CouncilTrace], operator.add]
    evidence_round: int
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
                "file_groups": state.get("file_groups") or {},
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


def _needs_more_evidence(challenges: list[Challenge]) -> bool:
    return any(c.verdict == "needs_more_evidence" for c in challenges)


def _route_after_coordinator(state: ReviewState) -> str:
    candidates = state.get("candidate_issues") or []
    if not candidates:
        return "self_checker"

    evidence_round = state.get("evidence_round", 0)
    max_rounds = state.get("max_evidence_rounds", DEFAULT_MAX_EVIDENCE_ROUNDS)
    evidence_notes = state.get("evidence_notes") or []
    challenges = state.get("challenges") or []

    if _needs_more_evidence(challenges) and evidence_round < max_rounds:
        return "evidence_agent"
    if any(c.needs_evidence for c in candidates) and not evidence_notes and evidence_round < max_rounds:
        return "evidence_agent"
    if not challenges:
        return "challenge_agent"
    return "self_checker"


def _conditional_route(state: ReviewState) -> str:
    return state.get("council_route") or _route_after_coordinator(state)


def _evidence_agent_node(tool_client=None):
    def _node(state: ReviewState) -> dict:
        requests = state.get("evidence_requests") or []
        candidates = {c.id: c for c in state.get("candidate_issues") or []}
        notes: list[EvidenceNote] = []
        gathered: list[GatheredContext] = []
        for req in requests:
            candidate = candidates.get(req.candidate_id)
            if candidate is None:
                continue
            supports: list[str] = []
            unknowns: list[str] = []
            evidence_ids: list[str] = []
            status = "mixed"
            if tool_client is not None and req.kind == "related_snippet" and req.target:
                resp = tool_client.get_file_content(req.target)
                content = resp.as_tool_output()
                gathered.append(GatheredContext("get_file_content", req.target, content))
                if resp.success and content.strip():
                    supports.append(f"读取到相关文件片段:{req.target}")
                    evidence_ids.append(f"tool:get_file_content:{req.target}")
                    status = "supported"
                else:
                    unknowns.append(f"无法读取相关文件片段:{req.target}")
                    status = "not_found"
            elif req.kind not in {
                "related_snippet",
                "caller_path",
                "sensitive_sink",
                "metric_context",
                "open_question",
            }:
                unknowns.append(f"当前 EvidenceAgent 不支持该证据请求:{req.kind}")
                status = "unsupported"
            else:
                bundle = state.get("context_bundle")
                rendered = bundle.render(1200) if bundle is not None else ""
                if req.target and req.target in rendered:
                    supports.append(f"ContextBundle 包含目标文件事实:{req.target}")
                    evidence_ids.append(f"context:{req.target}")
                    status = "supported"
                else:
                    question = f" question={req.question}" if req.question else ""
                    unknowns.append(f"当前上下文不足以补证:{req.target or req.kind}{question}")
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
                    detail=f"requests={len(requests)} notes={len(notes)}",
                )
            ],
        }

    return _node


def _challenge_agent_node():
    def _node(state: ReviewState) -> dict:
        notes_by_candidate: dict[str, list[EvidenceNote]] = {}
        for note in state.get("evidence_notes") or []:
            notes_by_candidate.setdefault(note.candidate_id, []).append(note)

        challenges: list[Challenge] = []
        for candidate in state.get("candidate_issues") or []:
            notes = notes_by_candidate.get(candidate.id, [])
            has_support = any(n.supports for n in notes)
            has_unknown = any(n.unknowns for n in notes)
            if candidate.confidence < 0.35:
                verdict = "drop"
                reason = "候选置信度过低"
            elif candidate.category == "quality" and candidate.evidence_status == "missing":
                verdict = "drop"
                reason = "维护性候选缺少明确维护成本证据"
            elif candidate.needs_evidence and not has_support and has_unknown:
                verdict = "needs_more_evidence"
                reason = "候选要求补证,但现有证据仍不足"
            else:
                verdict = "keep"
                reason = "未发现足以否定候选的问题"
            challenges.append(
                Challenge(candidate_id=candidate.id, verdict=verdict, reason=reason)
            )

        return {
            "challenges": challenges,
            "council_trace": [
                CouncilTrace(
                    node="challenge_agent",
                    event="challenged",
                    detail=f"count={len(challenges)}",
                )
            ],
        }

    return _node


def _self_checker_node(llm, fp_verify_llm):
    def _node(state: ReviewState) -> dict:
        ctx = _state_to_context(state, llm=llm, fp_verify_llm=fp_verify_llm)
        checker = SelfCheckerStage(
            enable_fp_llm_verification=state.get("fp_llm_verify", False)
        )
        outcome = checker.decide(
            ctx,
            candidates=list(state.get("candidate_issues") or []),
            challenges=list(state.get("challenges") or []),
            evidence_rounds=state.get("evidence_round", 0),
            evidence_request_count=len(state.get("evidence_requests") or []),
            truncated_candidates=state.get("truncated_candidates", 0),
            truncated_evidence_requests=state.get("truncated_evidence_requests", 0),
        )
        summaries = state.get("review_summaries") or []
        summary = "  ".join(summaries)
        return {
            "final_issues": outcome.issues,
            "summary": summary,
            "filter_stats": outcome.filter_stats,
            "council_stats": outcome.stats,
            "council_trace": [
                CouncilTrace(
                    node="self_checker",
                    event="finalized",
                    detail=f"final_issues={len(outcome.issues)}",
                )
            ],
        }

    return _node


def build_review_graph(*, enable_summary: bool = True, checkpointer=None, llm=None, fp_verify_llm=None, tool_client=None):
    """编译 ADR-032 审查状态图。"""
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
    g.add_node("challenge_agent", _challenge_agent_node())
    g.add_node("self_checker", _self_checker_node(llm, fp_verify_llm))

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
            "challenge_agent": "challenge_agent",
            "self_checker": "self_checker",
        },
    )
    g.add_edge("evidence_agent", "council_coordinator")
    g.add_edge("challenge_agent", "council_coordinator")
    g.add_edge("self_checker", END)
    return g.compile(checkpointer=checkpointer)
