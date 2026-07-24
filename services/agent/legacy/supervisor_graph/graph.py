"""LangGraph 状态图编排(supervisor 驱动)—— 阶段 4。

取代原线性 stage 循环(见 change langgraph-supervisor-orchestration)。顶层拓扑显式可见:

    START → [summary] → supervisor ──(条件/Send)──► [security|logic|quality]
                            ▲                                │
                            └──────────── fan-in ────────────┘
                        supervisor 判 finish
                            ▼
                      aggregation → fp_filter → END

设计要点:
- **supervisor 节点**:`enable_supervisor` 开且有真实 LLM 时,由一次结构化决策驱动动态派发 /
  补派 / 重派 / 终止;关时(或 mock)退化为"全派一轮即 finish"的**确定性调度**(保评测控变量)。
- **审查员节点**:每个领域审查员是一张**编译子图**(prepare → review → collect),作为节点挂到
  父图上(design D12 第二刀)。内部步骤在图层面显式可见、可组合;`create_agent` 的 ReAct 图仍
  封装在 `review` 节点内(经 `ToolAgentEngine`),把它也内联为子子图留作后续可选深化。
- **聚合 / 误报过滤**:原样包裹现有 stage 逻辑(design D7),不改其已验证行为。
- **State**:`issues` 加法 fan-in;`gathered_context` 自定义去重 reducer;`final_issues` 承接
  聚合/过滤后的结果(避免与加法 reducer 冲突)。
- **护栏**:`iteration` 迭代上限 + 图 `recursion_limit` 双重,确保任意 diff 有限步到 END(ADR-016/018)。
"""

from __future__ import annotations

import logging
import operator
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field

from codeguard_agent.git.diff_collector import split_diff_by_file
from codeguard_agent.llm.client import invoke_with_retry, mock_review_result
from codeguard_agent.models.schemas import ReviewResult
from codeguard_agent.pipeline.engines import (
    DirectEngine,
    ReviewEngine,
    ReviewOutcome,
    ToolAgentEngine,
)
from codeguard_agent.legacy.stages.aggregation import AggregationStage
from codeguard_agent.pipeline.context.base import PipelineContext
from codeguard_agent.legacy.stages.fp_filter import FalsePositiveFilterStage
from codeguard_agent.pipeline.reviewers.reviewers import (
    DEFAULT_REVIEWERS,
    Reviewer,
    _build_user_prompt,
    _effective_diff,
    _load_prompt,
)
from codeguard_agent.pipeline.summary.summary import SummaryStage

logger = logging.getLogger("codeguard")

# 迭代上限默认值(design D10):支持"首派 + 一次补派/重派 + 收尾"。
DEFAULT_MAX_ROUNDS = 3
# 图总步数硬上限(兜底护栏,叠加在 iteration 计数之上)。
DEFAULT_RECURSION_LIMIT = 50

_ALL_REVIEWER_NAMES = [r.name for r in DEFAULT_REVIEWERS]

# 摘要 → 领域关键词(用于兜底时缩小派发范围,避免无脑全派)。
_DOMAIN_KW: dict[str, list[str]] = {
    "security": ["安全", "security", "注入", "鉴权", "穿越", "密钥", "加密", "认证", "越权", "xss", "csrf"],
    "logic":    ["逻辑", "logic",    "空指针", "边界", "并发", "递归", "除零", "null", "npe", "死锁", "竞态", "比较"],
    "quality":  ["质量", "quality",  "可读", "命名", "重复", "魔法", "复杂度", "泄漏", "资源", "异常"],
}


def _guess_domains_from_summary(summary: str) -> list[str]:
    """从变更摘要文本中猜测涉及的审查领域。未提到任何领域时返回全部(保 recall 兜底)。"""
    if not summary:
        return list(_ALL_REVIEWER_NAMES)
    s = summary.lower()
    domains = [name for name in _ALL_REVIEWER_NAMES
               if any(kw.lower() in s for kw in _DOMAIN_KW.get(name, []))]
    return domains or list(_ALL_REVIEWER_NAMES)


# ---------------------------------------------------------------------------
# State 与 reducer(纯数据层)
# ---------------------------------------------------------------------------


def dedup_gathered_reducer(existing: list | None, new: list | None) -> list:
    """`gathered_context` 的自定义 reducer:加法累积后按 `(tool, args)` 去重,保留首次出现顺序。

    同一文件被多审查员(或多轮补派)读取只保留一份,避免污染复核上下文 / 重复计入工具画像。
    这是 LangGraph 学习点:reducer 不止是 `operator.add`,可承载"合并+去重"语义。
    """
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


class ReviewState(TypedDict, total=False):
    """审查图的共享状态。

    静态输入字段(初始化设一次、节点只读)无 reducer = 覆盖语义但节点从不回写,故恒为初值。
    fan-in 字段带 reducer,承接并行审查员的并发写入。
    """

    # --- 静态输入 ---
    diff_text: str
    # 注意:llm / fp_verify_llm / tool_client 不在 state 中(不可 msgpack 序列化),
    # 而是通过 build_review_graph 的闭包传入各节点。enabled_tools 是 list[str] 可序列化故保留。
    enabled_tools: Any
    max_retries: int
    structured_method: str
    enable_supervisor: bool
    enable_hitl: bool
    react_recursion_limit: int
    max_review_rounds: int
    fp_llm_verify: bool

    # --- 摘要阶段产出 ---
    diff_summary: str
    file_groups: dict
    change_types: list
    risk_level: int

    # --- 审查产出(fan-in,带 reducer)---
    issues: Annotated[list, operator.add]
    gathered_context: Annotated[list, dedup_gathered_reducer]
    review_summaries: Annotated[list, operator.add]

    # --- supervisor 控制 ---
    dispatch: list
    dispatched: Annotated[set, operator.or_]
    focus_notes: dict
    supervisor_log: Annotated[list, operator.add]
    iteration: int
    route: str

    # --- 产出 ---
    final_issues: list
    summary: str
    filter_stats: Any


class ReviewerState(TypedDict, total=False):
    """审查员子图的状态(design D12 第二刀:审查员从普通函数升级为编译子图)。

    与父图 `ReviewState` **同名的键**(下方"输入""产出"两组),在子图作为节点挂入父图时按名对接:
    输入键由父图经 `Send` 注入,产出键经父图 reducer 完成 fan-in。
    **私有工作键**(eff_diff/user_prompt/outcome)只存在于子图内部、不与父图同名,故既不外泄给父图,
    也不会在三审查员并行时彼此写冲突(并行分支只能同写带 reducer 的共享键)。
    """

    # --- 父图注入的输入(只读)---
    diff_text: str
    # llm / tool_client 不可 msgpack 序列化,由 build_reviewer_subgraph 闭包传入。
    enabled_tools: Any
    max_retries: int
    structured_method: str
    diff_summary: str
    file_groups: dict
    focus_notes: dict
    enable_hitl: bool
    react_recursion_limit: int
    # --- 写回父图的产出(键名与 ReviewState 一致 → 经父图 reducer fan-in)---
    issues: list
    gathered_context: list
    review_summaries: list
    dispatched: set
    supervisor_log: Annotated[list, operator.add]  # 子图内 review/collect 均可能追加,故带 reducer
    # --- 子图私有工作键(不外泄,并行无冲突)---
    eff_diff: str
    user_prompt: str
    outcome: Any


# ---------------------------------------------------------------------------
# supervisor 决策
# ---------------------------------------------------------------------------


class SupervisorDecision(BaseModel):
    """supervisor 一轮调度决策。"""

    action: str = Field(default="dispatch", description="dispatch(派发审查员)或 finish(进入聚合)")
    reviewers: list[str] = Field(
        default_factory=list,
        description="本轮要派发的审查员子集,取值限 security/logic/quality",
    )
    focus_notes: dict[str, str] = Field(
        default_factory=dict,
        description="审查员名 → 聚焦复审指令(重派某审查员时给,可选)",
    )
    reason: str = Field(default="", description="本轮调度理由,一句话")


_supervisor_prompt_cache: str | None = None


def _load_supervisor_prompt() -> str:
    """加载 supervisor system prompt(从 prompts/supervisor-system.txt,懒加载缓存一次)。"""
    global _supervisor_prompt_cache
    if _supervisor_prompt_cache is None:
        _supervisor_prompt_cache = _load_prompt("supervisor-system.txt")
    return _supervisor_prompt_cache


def _render_supervisor_user(state: ReviewState) -> str:
    """构造 supervisor 决策的 user 输入:摘要 + 已派发 + 当前发现概况。"""
    dispatched = sorted(state.get("dispatched") or [])
    issues = state.get("issues") or []
    by_reviewer: dict[str, int] = {}
    for name in dispatched:
        by_reviewer[name] = by_reviewer.get(name, 0)
    file_groups = state.get("file_groups") or {}
    fg_summary = {k: len(v) for k, v in file_groups.items()} if file_groups else {}
    lines = [
        f"本次变更摘要:{state.get('diff_summary') or '(无摘要)'}",
        f"变更文件领域分布(各审查员相关文件数):{fg_summary or '(无)'}",
        f"已派发过的审查员:{dispatched or '(无,这是首轮)'}",
        f"当前已收集发现数:{len(issues)}",
    ]
    if issues:
        preview = "; ".join(
            f"{getattr(it, 'severity', '')}/{getattr(it, 'type', '')}@{getattr(it, 'file', '')}"
            for it in issues[:8]
        )
        lines.append(f"发现概览(前若干条):{preview}")
    lines.append("请给出本轮调度决策。")
    return "\n".join(lines)


def _decide_dispatch(state: ReviewState, llm) -> SupervisorDecision:
    """调结构化 LLM 产出调度决策;任何失败/无效一律回退摘要推断派发。"""
    structured = llm.with_structured_output(
        SupervisorDecision, method=state.get("structured_method", "function_calling")
    )
    try:
        decision = invoke_with_retry(
            structured,
            [("system", _load_supervisor_prompt()), ("human", _render_supervisor_user(state))],
            max_retries=state.get("max_retries", 3),
        )
    except Exception as exc:  # noqa: BLE001 决策失败不该中断审查
        domains = _guess_domains_from_summary(state.get("diff_summary") or "")
        logger.warning("[supervisor] 决策调用失败,回退摘要推断派发: %s -> %s", exc, domains)
        return SupervisorDecision(action="dispatch", reviewers=domains, reason=f"决策失败,摘要推断派发 {domains}")
    if decision is None or not isinstance(decision, SupervisorDecision):
        domains = _guess_domains_from_summary(state.get("diff_summary") or "")
        logger.warning("[supervisor] 决策无效,回退摘要推断派发 -> %s", domains)
        return SupervisorDecision(action="dispatch", reviewers=domains, reason=f"决策无效,摘要推断派发 {domains}")
    return decision


def _supervisor_node(llm):
    """返回 supervisor 节点函数(闭包捕获 llm,避免进 state 被 msgpack 序列化)。"""
    def _node(state: ReviewState) -> dict:
        iteration = state.get("iteration", 0) + 1
        dispatched = state.get("dispatched") or set()
        max_rounds = state.get("max_review_rounds", DEFAULT_MAX_ROUNDS)
        deterministic = (not state.get("enable_supervisor")) or llm is None

        # 护栏:迭代达上限强制收尾(直面 ADR-016/018)。
        if iteration > max_rounds:
            return {
                "iteration": iteration,
                "route": "finish",
                "supervisor_log": [f"[supervisor] 轮次达上限 {max_rounds},强制进入聚合"],
            }

        # 确定性调度:首轮派未派过的全部,之后 finish。
        if deterministic:
            pending = [n for n in _ALL_REVIEWER_NAMES if n not in dispatched]
            if pending:
                return {
                    "iteration": iteration,
                    "route": "dispatch",
                    "dispatch": pending,
                    "supervisor_log": [f"[supervisor] 确定性调度,派发 {pending}"],
                }
            return {
                "iteration": iteration,
                "route": "finish",
                "supervisor_log": ["[supervisor] 三审查员已完成,进入聚合"],
            }

        # 智能调度:LLM 决策。
        decision = _decide_dispatch(state, llm)
        log = [f"[supervisor] 第{iteration}轮:{decision.action} {decision.reviewers} — {decision.reason}"]
        valid = [n for n in decision.reviewers if n in _ALL_REVIEWER_NAMES]
        if decision.action == "finish" or not valid:
            if not dispatched:
                domains = _guess_domains_from_summary(state.get("diff_summary") or "")
                return {
                    "iteration": iteration,
                    "route": "dispatch",
                    "dispatch": domains,
                    "supervisor_log": log + [f"[supervisor] 兜底:尚无审查产出,摘要推断派发 {domains}"],
                }
            # HITL:判 finish 后暂停等待人工确认或补充派发(需 checkpoint)。
            hitl = state.get("enable_hitl")
            if hitl:
                from langgraph.types import interrupt

                current_issues = state.get("issues") or []
                resume = interrupt({
                    "type": "supervisor_finish",
                    "issues_count": len(current_issues),
                    "issues": [i.model_dump() for i in current_issues],
                    "dispatched": list(dispatched),
                    "reason": decision.reason,
                })
                # resume 是 Command.resume 的值:{"action": "continue"} 或 {"action": "retry", "reviewers": [...], ...}
                action = (resume or {}).get("action", "continue")
                if action == "retry" and (resume or {}).get("reviewers"):
                    retry_reviewers = [n for n in resume["reviewers"] if n in _ALL_REVIEWER_NAMES]
                    if retry_reviewers:
                        return {
                            "iteration": iteration,
                            "route": "dispatch",
                            "dispatch": retry_reviewers,
                            "focus_notes": (resume or {}).get("focus_notes") or {},
                            "supervisor_log": log + [
                                f"[supervisor] HITL 追加派发 {retry_reviewers}"
                            ],
                        }
                # continue 或其他 → fall through 到 finish
            return {"iteration": iteration, "route": "finish", "supervisor_log": log}
        return {
            "iteration": iteration,
            "route": "dispatch",
            "dispatch": valid,
            "focus_notes": decision.focus_notes or {},
            "supervisor_log": log,
        }
    return _node


def _route_after_supervisor(state: ReviewState):
    """条件边:finish → 聚合;否则对 dispatch 子集动态 Send 扇出。"""
    from langgraph.types import Send

    if state.get("route") == "finish":
        return "aggregation"
    return [Send(name, state) for name in (state.get("dispatch") or [])]


# ---------------------------------------------------------------------------
# 审查 / 摘要 / 聚合 / 误报过滤 节点
# ---------------------------------------------------------------------------


def _make_engine(state: ReviewState | ReviewerState, tool_client=None) -> ReviewEngine:
    """按是否配置工具客户端选引擎(沿用 design D1 分流)。"""
    if tool_client is not None:
        return ToolAgentEngine(
            tool_client,
            recursion_limit=state.get("react_recursion_limit", 24),
            enabled_tools=state.get("enabled_tools"),
        )
    return DirectEngine()


def _state_to_context(state: ReviewState, llm=None, fp_verify_llm=None, tool_client=None) -> PipelineContext:
    """从 State 构造一个临时 PipelineContext,供复用现有 stage 逻辑(不含 issues,调用方自行设)。"""
    return PipelineContext(
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


def _summary_node(llm, tool_client):
    """返回 summary 节点函数(闭包捕获 llm/tool_client,避免进 state 被 msgpack 序列化)。"""
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


def build_reviewer_subgraph(reviewer: Reviewer, checkpointer=None, llm=None, tool_client=None):
    """把单个领域审查员构造成一张编译子图(prepare → review → collect),供作节点挂入父图。

    取代原"普通函数节点"(design D12 第二刀):审查员的内部流水线由此在图层面显式可见、可组合——
    后续要给某审查员加步骤(独立的工具采集节点 / 自我复核节点等),在本子图加节点即可,父图无感。
    `create_agent` 的 ReAct 图仍封装在 `review` 节点内(经 `ToolAgentEngine`);把它也内联为真正的
    子子图涉及 `MessagesState` ↔ `ReviewerState` 映射,留作后续可选深化。

    三段职责单一:
    - prepare:组装本审查员的聚焦 diff 与 user prompt(mock/无 LLM 时跳过);
    - review :跑引擎得到一个 `ReviewOutcome`——mock 合成与异常隔离都在此收口为单一 outcome;
    - collect:把 outcome 归一化为写回父图的产出键(issues/gathered_context/summaries/dispatched)。
    """
    from langgraph.graph import END, START, StateGraph

    def _direct_fallback(state: ReviewerState) -> ReviewOutcome:
        """降级:以已收集上下文调 DirectEngine 收尾(不丢上下文,修 ADR-018)。"""
        from codeguard_agent.pipeline.engines import DirectEngine
        return DirectEngine().review(
            llm,
            system_prompt=_load_prompt(reviewer.prompt_file),
            user_prompt=state.get("user_prompt", ""),
            reviewer_name=reviewer.name,
            max_retries=state.get("max_retries", 3),
            structured_method=state.get("structured_method", "function_calling"),
        )

    def _prepare(state: ReviewerState) -> dict:
        if llm is None:  # mock/无 LLM:无需构造 prompt,留给 review 合成
            return {}
        file_groups = state.get("file_groups") or {}
        file_diffs = split_diff_by_file(state["diff_text"]) if file_groups else {}
        eff_diff = _effective_diff(state["diff_text"], file_diffs, file_groups.get(reviewer.name))
        user = _build_user_prompt(eff_diff, summary=state.get("diff_summary", ""))
        focus = (state.get("focus_notes") or {}).get(reviewer.name, "")
        if focus:
            user += f"\n\n<复审聚焦>\n{focus}\n</复审聚焦>"
        return {"eff_diff": eff_diff, "user_prompt": user}

    def _review(state: ReviewerState) -> dict:
        # mock 模式:仅让 security 合成一条 mock 结果,其余空——既保图端到端连通,
        # 又不把单条 mock 三倍化(确定性模式下三审查员都会被派发)。
        if llm is None:
            if reviewer.name == "security":
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
                enable_hitl=state.get("enable_hitl", False),
            )
        except Exception as exc:  # noqa: BLE001 单审查员失败不拖垮全图(节点级错误隔离)
            # 撞递归上限且 HITL 开启 → 暂停等人决策,不自动降级(修 ADR-018 丢上下文问题)。
            hitl = state.get("enable_hitl", False)
            from langgraph.errors import GraphRecursionError
            if isinstance(exc, GraphRecursionError) and hitl:
                from langgraph.types import interrupt
                resume = interrupt({
                    "type": "reviewer_hit_limit",
                    "reviewer": reviewer.name,
                    "gathered_count": len(state.get("gathered_context") or []),
                })
                action = (resume or {}).get("action", "continue")
                if action == "skip":
                    return {
                        "outcome": ReviewOutcome(ReviewResult(summary="")),
                        "supervisor_log": [f"[{reviewer.name}] HITL skip,跳过"],
                    }
                if action == "retry":
                    try:
                        outcome = engine.review(
                            llm,
                            system_prompt=_load_prompt(reviewer.prompt_file),
                            user_prompt=state.get("user_prompt", ""),
                            reviewer_name=reviewer.name,
                            max_retries=state.get("max_retries", 3),
                            structured_method=state.get("structured_method", "function_calling"),
                            enable_hitl=False,  # retry 不再次中断,失败直接降级
                        )
                    except Exception as retry_exc:  # noqa: BLE001
                        logger.warning("[%s] retry 也失败,降级: %s", reviewer.name, retry_exc)
                        outcome = _direct_fallback(state)
                    return {"outcome": outcome}
                # continue(默认):以已收集上下文调 DirectEngine 收尾
                outcome = _direct_fallback(state)
                return {"outcome": outcome}
            logger.warning("[%s] 审查员节点失败,跳过: %s", reviewer.name, exc)
            return {
                "outcome": ReviewOutcome(ReviewResult(summary="")),
                "supervisor_log": [f"[{reviewer.name}] 审查员失败,跳过: {exc}"],
            }
        return {"outcome": outcome}

    def _collect(state: ReviewerState) -> dict:
        outcome = state.get("outcome")
        out: dict = {"dispatched": {reviewer.name}}
        if outcome is None:
            return out
        out["issues"] = list(outcome.result.issues)
        if outcome.gathered_context:
            out["gathered_context"] = list(outcome.gathered_context)
        summary = outcome.result.summary
        if summary:
            # 真实审查加【域】前缀便于聚合溯源;mock 保持原样(与改造前行为一致)。
            out["review_summaries"] = (
                [summary] if llm is None else [f"【{reviewer.name}】{summary}"]
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
    """父图节点:调用审查员子图,并做父↔子 state 的**显式**映射(design D12 第二刀)。

    采用"在节点内 invoke 子图"这一 LangGraph 子图范式,而非把子图直接挂作父图节点。原因正是
    design 提示的"父子 state 映射"坑:三审查员并行 fan-out 时,若子图直接挂载,其 schema 里**只读**
    的共享键(如 diff_text)会在子图结束时被回写父图 → 对无 reducer 的键并发写 → InvalidUpdateError。
    显式投影输入、**只回传产出键**即可根除回写冲突,父↔子边界也一目了然。

    llm / tool_client 不可 msgpack 序列化,由闭包传入子图,不经过 state。
    """
    subgraph = build_reviewer_subgraph(reviewer, checkpointer=checkpointer, llm=llm, tool_client=tool_client)

    def _node(state: ReviewState) -> dict:
        # 工具清单:评测 profile 的全局 enabled_tools 优先(保对照可控);
        # CLI 默认(None)时回退到 reviewer 的专属 tool_allowlist(不对称分配)。
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
                "focus_notes": state.get("focus_notes") or {},
                "enable_hitl": state.get("enable_hitl", False),
                "react_recursion_limit": state.get("react_recursion_limit", 24),
            }
        )
        # 只回传产出键给父图(经父图 reducer fan-in);子图私有/只读键一律不外泄。
        out: dict = {"dispatched": result.get("dispatched") or {reviewer.name}}
        for key in ("issues", "gathered_context", "review_summaries", "supervisor_log"):
            if result.get(key):
                out[key] = result[key]
        return out

    return _node


def _aggregation_node(llm):
    """返回 aggregation 节点函数(闭包捕获 llm,避免进 state 被 msgpack 序列化)。"""
    def _node(state: ReviewState) -> dict:
        ctx = _state_to_context(state, llm=llm)
        ctx.issues = list(state.get("issues") or [])
        AggregationStage().execute(ctx)
        summaries = state.get("review_summaries") or []
        return {"final_issues": ctx.issues, "summary": "  ".join(summaries)}
    return _node


def _fp_filter_node(llm, fp_verify_llm):
    """返回 fp_filter 节点函数(闭包捕获 llm/fp_verify_llm,避免进 state 被 msgpack 序列化)。"""
    def _node(state: ReviewState) -> dict:
        ctx = _state_to_context(state, llm=llm, fp_verify_llm=fp_verify_llm)
        ctx.issues = list(state.get("final_issues") or [])
        FalsePositiveFilterStage(
            enable_llm_verification=state.get("fp_llm_verify", False)
        ).execute(ctx)
        return {"final_issues": ctx.issues, "filter_stats": ctx.filter_stats}
    return _node


# ---------------------------------------------------------------------------
# 建图
# ---------------------------------------------------------------------------


def build_review_graph(*, enable_summary: bool = True, checkpointer=None, llm=None, fp_verify_llm=None, tool_client=None):
    """编译审查状态图。

    enable_summary 决定拓扑入口(是否经摘要节点);supervisor 智能/迭代上限/fp 复核等
    运行期行为由初始 State 字段控制(见 PipelineOrchestrator.run),故不进 build 参数。

    checkpointer: 可选 LangGraph checkpointer(MemorySaver/SqliteSaver 等)。传入后
        图在每步执行后自动持久化 State,支持中断恢复;不传则无状态(当前行为,向后兼容)。
    llm / fp_verify_llm / tool_client: 不可 msgpack 序列化的对象,通过闭包传入各节点
        而非放入 state,避免 checkpoint 序列化报错。
    """
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(ReviewState)
    g.add_node("supervisor", _supervisor_node(llm))
    for r in DEFAULT_REVIEWERS:
        g.add_node(r.name, make_reviewer_node(r, checkpointer=checkpointer, llm=llm, tool_client=tool_client))
    g.add_node("aggregation", _aggregation_node(llm))
    g.add_node("fp_filter", _fp_filter_node(llm, fp_verify_llm))

    if enable_summary:
        g.add_node("summary", _summary_node(llm, tool_client))
        g.add_edge(START, "summary")
        g.add_edge("summary", "supervisor")
    else:
        g.add_edge(START, "supervisor")

    g.add_conditional_edges(
        "supervisor",
        _route_after_supervisor,
        _ALL_REVIEWER_NAMES + ["aggregation"],
    )
    for r in DEFAULT_REVIEWERS:
        g.add_edge(r.name, "supervisor")
    g.add_edge("aggregation", "fp_filter")
    g.add_edge("fp_filter", END)
    return g.compile(checkpointer=checkpointer)
