"""审查员执行引擎（可插拔接缝）。

同一个领域审查员可以用不同方式执行：
- DirectEngine：单次结构化 LLM 调用，无工具——"无工具"对照基准。
- ToolAgentEngine：ReAct Agent，可经 Java 工具服务自主获取 diff 之外的上下文。

调用方按 tool_client 是否存在选择引擎。
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from time import sleep
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.schemas import ReviewResult
from codeguard_agent.pipeline.risk.discovery import (
    COMPLETE_PATCH_RESULT,
    DISCOVERY_GATEWAY_TOOLS,
    REPEATED_TOOL_RESULT,
    ToolKey,
    canonical_tool_key,
)

logger = logging.getLogger("codeguard")


@dataclass(frozen=True)
class GatheredContext:
    """审查员经工具获取的一段 diff 之外上下文(供下游误报复核实证判定)。

    tool:工具名(如 get_file_content);args:入参摘要(用于去重与展示);content:工具返回内容。
    只在管线上下文流转，不进入 Issue 结构体。
    """

    tool: str
    args: str
    content: str
    duration_ms: float = 0.0
    status: str = "complete"


@dataclass
class ReviewOutcome:
    """单个领域审查员的产出信封:结构化结果 + 本次经工具获取的上下文。

    gathered_context 仅 ToolAgentEngine 可能非空;DirectEngine(无工具)恒为空。
    """

    result: ReviewResult
    gathered_context: list[GatheredContext] = field(default_factory=list)
    tool_trace_records: list[Any] = field(default_factory=list)
    execution_events: list[str] = field(default_factory=list)
    evidence_catalog: Any = None  # 本次发现的证据目录(P01/Cxx/Txx);Direct 档仅 P/C


class ReviewEngine(ABC):
    """单个领域审查员的执行引擎契约。"""

    @abstractmethod
    def review(
        self,
        llm: Any,
        *,
        system_prompt: str,
        user_prompt: str,
        reviewer_name: str,
        max_retries: int,
        structured_method: str,
        enable_hitl: bool = False,
        evidence_catalog: Any = None,
    ) -> ReviewOutcome:
        """执行一次领域审查,返回产出信封(结构化结果 + 获取的上下文)。

        假定 llm 非 None、diff 非空(由 stage 统一处理边界)。
        evidence_catalog:初始证据目录(Direct 档透传;ReAct 档追加工具记录)。
        """


class DirectEngine(ReviewEngine):
    """单次直接结构化调用——无工具对照基准。"""

    def review(
        self,
        llm: Any,
        *,
        system_prompt: str,
        user_prompt: str,
        reviewer_name: str,
        max_retries: int,
        structured_method: str,
        enable_hitl: bool = False,
        evidence_catalog: Any = None,
    ) -> ReviewOutcome:
        structured_llm = llm.with_structured_output(ReviewResult, method=structured_method)
        # 结构化输出可能返回 None(模型没正确发起工具调用):invoke_with_retry 只重试抛异常路径,
        # None 需要单独重试(deepseek 对结构化收口偶发 None,重试有概率拿到合规输出),耗尽才兜底为空。
        result = None
        for attempt in range(3):
            result = invoke_with_retry(
                structured_llm,
                [("system", system_prompt), ("human", user_prompt)],
                max_retries=max_retries,
            )
            if result is not None:
                break
            if attempt < 2:
                logger.warning(
                    "[%s] 审查员未返回结构化结果(第 %d 次),1s 后重试", reviewer_name, attempt + 1
                )
                sleep(1)
        if result is None:
            logger.warning("[%s] 审查员未返回结构化结果(重试 3 次后仍空),本次按空处理", reviewer_name)
            return ReviewOutcome(
                ReviewResult(summary=""),
                execution_events=["structured_output_missing"],
                evidence_catalog=evidence_catalog,
            )
        # 直连无工具:gathered_context 恒空;目录透传(仅 P/C)。
        return ReviewOutcome(result, evidence_catalog=evidence_catalog)


class ToolAgentEngine(ReviewEngine):
    """ReAct Agent 引擎:可调 Java 工具服务获取上下文,再产出结构化结果。

    基于 langchain v1 的 ``create_agent``(langgraph 预构建图):
    - 工具循环 + 停止条件由图托管,无需手写 AgentExecutor;
    - ``response_format=ReviewResult`` 让图内置结构化收口,免去"逼 prompt 吐 JSON 再正则解析";
    - 与图编排同源，均基于 LangGraph 预构建图。

    与 DirectEngine 同构地返回 ReviewResult;拿不到结构化结果时一律兜底为空并告警,绝不抛断
    (见 spec「ReAct 审查结果的结构化与健壮性」)。
    """

    def __init__(
        self,
        tool_client: Any,
        recursion_limit: int = 12,
        enabled_tools: list[str] | None = None,
        allow_direct_fallback: bool = True,
    ) -> None:
        self._tool_client = tool_client
        # langgraph 用 recursion_limit 约束图的总步数,间接限制工具调用轮数,防止失控。
        self._recursion_limit = recursion_limit
        # 工具白名单:None=暴露所有已实现工具;否则只暴露列出的(profile 控制,对照可控)。
        self._enabled_tools = enabled_tools
        self._allow_direct_fallback = allow_direct_fallback

    def review(
        self,
        llm: Any,
        *,
        system_prompt: str,
        user_prompt: str,
        reviewer_name: str,
        max_retries: int,
        structured_method: str,
        enable_hitl: bool = False,
        evidence_catalog: Any = None,
    ) -> ReviewOutcome:
        # GraphRecursionError 延迟导入(mock/无工具路径不需要 langgraph)。
        from langgraph.errors import GraphRecursionError

        try:
            raw = self._run_agent(llm, system_prompt, user_prompt)
        except GraphRecursionError:
            # HITL 开启时不吞异常，让它传播到上层 _review 节点的 interrupt handler，
            # 由人决定 continue/retry/skip。
            if enable_hitl:
                raise
            tool_records = list(getattr(self._tool_client, "trace_records", ()))
            gathered = _gathered_context_from_records(tool_records)
            if not self._allow_direct_fallback and not gathered:
                raise
            if gathered:
                logger.warning(
                    "[%s] ReAct 达到 %d 步上限，使用已取得的 %d 条工具事实结构化收束",
                    reviewer_name,
                    self._recursion_limit,
                    len(gathered),
                )
                synthesis = DirectEngine().review(
                    llm,
                    system_prompt=system_prompt,
                    user_prompt=_bounded_synthesis_prompt(user_prompt, gathered),
                    reviewer_name=reviewer_name,
                    max_retries=max_retries,
                    structured_method=structured_method,
                )
                synthesis.tool_trace_records.extend(tool_records)
                synthesis.gathered_context.extend(gathered)
                synthesis.execution_events.append("react_bounded_synthesis")
                synthesis.evidence_catalog = _extend_catalog(evidence_catalog, tool_records)
                return synthesis
            # ReAct 在 recursion_limit 步内没收敛(绕的难例 / 工具反复绕)。不让该域被静默丢弃
            # (那会直接丢失这一维度的发现、压低 recall),而是降级为无工具直连复审一次,至少
            # 据 diff 产出一份结论。直连无工具不会再循环。
            logger.warning(
                "[%s] ReAct 撞递归上限(%d 步未收敛),降级为无工具直连复审以保住该域产出",
                reviewer_name,
                self._recursion_limit,
            )
            fallback = DirectEngine().review(
                llm,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                reviewer_name=reviewer_name,
                max_retries=max_retries,
                structured_method=structured_method,
            )
            tool_records = list(getattr(self._tool_client, "trace_records", ()))
            fallback.tool_trace_records.extend(tool_records)
            fallback.gathered_context.extend(
                _gathered_context_from_records(tool_records)
            )
            # ReAct 异常降级 Direct 时保留已捕获目录,不丢已取得的工具事实。
            fallback.evidence_catalog = _extend_catalog(evidence_catalog, tool_records)
            return fallback
        # 两阶段收束:ReAct 探索工具收集上下文 → DirectEngine 结构化合成。
        # create_agent 不再传 response_format(deepseek 不兼容 LangChain 隐式 Respond 工具),
        # 改为主动用 DirectEngine.with_structured_output 做最终收口。
        tool_records = list(getattr(self._tool_client, "trace_records", ()))
        gathered = _extract_gathered_context(raw, tool_records=tool_records)
        synthesis_prompt = (
            _bounded_synthesis_prompt(user_prompt, gathered)
            if gathered
            else user_prompt
        )
        synthesis = DirectEngine().review(
            llm,
            system_prompt=system_prompt,
            user_prompt=synthesis_prompt,
            reviewer_name=reviewer_name,
            max_retries=max_retries,
            structured_method=structured_method,
        )
        synthesis.tool_trace_records.extend(tool_records)
        synthesis.gathered_context.extend(gathered)
        synthesis.execution_events.append("react_two_phase_synthesis")
        synthesis.evidence_catalog = _extend_catalog(evidence_catalog, tool_records)
        return synthesis

    def _run_agent(self, llm: Any, system_prompt: str, user_prompt: str) -> Any:
        """构建 ReAct agent 并执行,返回原始状态。

        抽成独立方法是为了让"撞递归上限降级"逻辑可被单测覆盖(测试覆写本方法抛
        ``GraphRecursionError``,无需构造真实 agent / 调真实 LLM)。
        """
        # LangChain 相关导入延迟到此:mock 模式 / 无工具路径不需要它们。
        from langchain.agents import create_agent

        from codeguard_agent.tools.definitions import (
            make_change_impact_tool,
            make_file_content_tool,
            make_security_path_tool,
            make_structure_tool,
        )

        # 已实现工具的工厂表。顺序即推荐用法:先专属工具发现问题,再 get_file_content 细读确认。
        available = {
            "get_file_content": lambda: make_file_content_tool(self._tool_client),
            "inspect_security_path": lambda: make_security_path_tool(self._tool_client),
            "inspect_change_impact": lambda: make_change_impact_tool(self._tool_client),
            "inspect_structure": lambda: make_structure_tool(self._tool_client),
        }
        # 按白名单挑工具:None=全开(CLI 默认);否则只开 profile 列出的(保持其声明顺序)。
        names = list(available) if self._enabled_tools is None else self._enabled_tools
        tools = [available[n]() for n in names if n in available]
        if not tools:  # 防御:白名单解析为空时回退全开,避免构造无工具的 Agent。
            tools = [factory() for factory in available.values()]
        agent: Any = create_agent(
            llm,
            tools,
            system_prompt=system_prompt,
        )
        return agent.invoke(
            {"messages": [("human", user_prompt)]},
            config={"recursion_limit": self._recursion_limit},
        )

def _extract_gathered_context(
    raw: Any,
    *,
    tool_records: Any = (),
) -> list[GatheredContext]:
    """从 create_agent 返回状态的消息流里抽取工具返回的上下文(ToolMessage)。

    工具入参在调用它的 AIMessage.tool_calls 里,故先建 tool_call_id → (name, args) 映射,
    再把每条 ToolMessage 配回去。对任何缺失/异常健壮:取不到一律返回已收集的部分(或空),
    绝不抛断(工具上下文是"锦上添花",不该让审查失败)。
    """
    try:
        if not isinstance(raw, dict):
            return []
        messages = raw.get("messages") or []
        records_by_key: dict[ToolKey, Any] = {}
        for record in tool_records or ():
            arguments = getattr(record, "arguments", {})
            if not isinstance(arguments, dict):
                continue
            key = canonical_tool_key(str(getattr(record, "tool", "")), arguments)
            if getattr(record, "status", "") != "reused":
                records_by_key.setdefault(key, record)
        # tool_call_id → (工具名, 入参摘要)
        call_meta: dict[str, tuple[str, str, ToolKey]] = {}
        for msg in messages:
            for call in getattr(msg, "tool_calls", None) or []:
                cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
                if cid:
                    tool_name = name or ""
                    args_text = _summarize_args(args)
                    key = (
                        canonical_tool_key(tool_name, args)
                        if isinstance(args, dict)
                        else (tool_name, args_text)
                    )
                    call_meta[cid] = (tool_name, args_text, key)
        gathered: list[GatheredContext] = []
        seen: set[ToolKey] = set()
        for msg in messages:
            if getattr(msg, "type", "") != "tool":
                continue
            cid = getattr(msg, "tool_call_id", None)
            fallback_name = getattr(msg, "name", "") or ""
            name, args, key = call_meta.get(
                cid or "", (fallback_name, "", (fallback_name, ""))
            )
            if name not in DISCOVERY_GATEWAY_TOOLS:
                continue
            if key in seen:
                continue
            content = getattr(msg, "content", "")
            content = content if isinstance(content, str) else str(content)
            if not content.strip():
                continue
            if content in {COMPLETE_PATCH_RESULT, REPEATED_TOOL_RESULT}:
                continue
            seen.add(key)
            record = records_by_key.get(key)
            gathered.append(
                GatheredContext(
                    tool=name,
                    args=args,
                    content=content,
                    duration_ms=float(getattr(record, "duration_ms", 0.0)),
                    status=str(getattr(record, "status", "complete")),
                )
            )
        return gathered
    except Exception as exc:  # noqa: BLE001 上下文捕获失败不应影响审查
        logger.warning("[engines] 抽取工具上下文失败,本次按空处理: %s", exc)
        return []


def _extend_catalog(catalog: Any, tool_records: Any) -> Any:
    """把工具记录追加进证据目录;无初始目录时返回 None(目录非必需)。

    延迟导入避免无工具路径携带 ledger 依赖。
    """
    if catalog is None:
        return None
    from codeguard_agent.pipeline.evidence.ledger import EvidenceCatalogBuilder

    return EvidenceCatalogBuilder().append_tool_records(catalog, tool_records)


def _gathered_context_from_records(tool_records: Any) -> list[GatheredContext]:
    gathered: list[GatheredContext] = []
    seen: set[ToolKey] = set()
    for record in tool_records or ():
        arguments = getattr(record, "arguments", {})
        tool_name = str(getattr(record, "tool", ""))
        output = str(getattr(record, "output", ""))
        if not isinstance(arguments, dict) or tool_name not in DISCOVERY_GATEWAY_TOOLS:
            continue
        if output in {COMPLETE_PATCH_RESULT, REPEATED_TOOL_RESULT}:
            # 短标记记录:优先用运行时解析出的首次真实 payload(resolved_output),
            # 让复用方 conversation 也能拿到真实事实(源文档 §5.5)。
            resolved = str(getattr(record, "resolved_output", "") or "")
            if not resolved:
                continue
            output = resolved
        key = canonical_tool_key(tool_name, arguments)
        if key in seen:
            continue
        seen.add(key)
        gathered.append(
            GatheredContext(
                tool=tool_name,
                args=_summarize_args(arguments),
                content=output,
                duration_ms=float(getattr(record, "duration_ms", 0.0)),
                status=str(getattr(record, "status", "complete")),
            )
        )
    return gathered


def _bounded_synthesis_prompt(
    user_prompt: str,
    gathered: list[GatheredContext],
    *,
    max_chars: int = 12_000,
) -> str:
    """把有界探索已取得的事实交给一次结构化综合，不再开放工具循环。"""
    blocks: list[str] = []
    used = 0
    for item in gathered:
        block = f"[{item.tool} 入参={item.args}]\n{item.content}".strip()
        remaining = max_chars - used
        if remaining <= 0:
            break
        blocks.append(block[:remaining])
        used += min(len(block), remaining)
    facts = "\n\n".join(blocks)
    return (
        f"{user_prompt}\n\n"
        "以下是工具探索阶段收集到的项目上下文事实。请仅依据原始变更与以下事实完成结构化审查；"
        "未被事实覆盖的关系必须按未知处理，不得猜测。\n\n"
        f"{facts}"
    )


def _summarize_args(args: Any) -> str:
    """把工具入参压成简短字符串(用于去重键与展示),失败回退 str()。"""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001
        return str(args)
