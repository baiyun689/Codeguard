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
from enum import Enum
from html import unescape
from time import sleep
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.evidence import ToolTraceRef
from codeguard_agent.models.schemas import ReviewResult
from codeguard_agent.pipeline.execution.discovery import (
    COMPLETE_PATCH_RESULT,
    DISCOVERY_GATEWAY_TOOLS,
    REPEATED_TOOL_RESULT,
    ToolKey,
    canonical_tool_key,
)
from codeguard_agent.pipeline.evidence.projection import GraphProjectionFocus

logger = logging.getLogger("codeguard")

REACT_INLINE_STRUCTURED_EVENT = "react_inline_structured"
REACT_SYNTHESIS_FALLBACK_INVALID_OUTPUT_EVENT = (
    "react_synthesis_fallback_invalid_output"
)
REACT_SYNTHESIS_FALLBACK_RECURSION_EVENT = "react_synthesis_fallback_recursion"
REACT_SYNTHESIS_FALLBACK_FAILED_EVENT = "react_synthesis_fallback_failed"
REACT_DEGRADED_RECURSION_EVENT = "react_degraded_recursion"
REACT_DIRECT_FALLBACK_FAILED_EVENT = "react_direct_fallback_failed"
REACT_SYNTHESIS_FALLBACK_EVENTS = frozenset({
    REACT_SYNTHESIS_FALLBACK_INVALID_OUTPUT_EVENT,
    REACT_SYNTHESIS_FALLBACK_RECURSION_EVENT,
})


class ReviewExecutionStatus(str, Enum):
    """审查执行的协议状态。

    ``COMPLETE`` 表示模型已返回符合结果 schema 的语义结果，其
    ``issues=[]`` 才能解释为“未发现候选”。其它状态表示执行链未能
    产生可信的结构化结果，不得当作 clean review 消费。
    """

    COMPLETE = "complete"
    PROTOCOL_FAILED = "protocol_failed"
    SYNTHESIS_FAILED = "synthesis_failed"
    RECURSION_FAILED = "recursion_failed"
    EXECUTION_FAILED = "execution_failed"


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
    """单个领域审查员的产出信封:结构化结果 + 本次工具调用记录与证据目录。"""

    result: Any | None
    status: ReviewExecutionStatus = ReviewExecutionStatus.COMPLETE
    failure_reason: str = ""
    tool_trace_records: list[Any] = field(default_factory=list)
    execution_events: list[str] = field(default_factory=list)
    evidence_catalog: Any = None  # 本次发现的证据目录(P01/Cxx/Txx);Direct 档仅 P/C

    def __post_init__(self) -> None:
        if self.status is ReviewExecutionStatus.COMPLETE and self.result is None:
            raise ValueError("complete review outcome requires a structured result")
        if self.status is not ReviewExecutionStatus.COMPLETE and self.result is not None:
            raise ValueError("failed review outcome must not carry a semantic result")


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
        result_schema: Any = ReviewResult,
    ) -> ReviewOutcome:
        """执行一次领域审查,返回产出信封(结构化结果 + 获取的上下文)。

        假定 llm 非 None、diff 非空(由 stage 统一处理边界)。
        evidence_catalog:初始证据目录(Direct 档透传;ReAct 档追加工具记录)。
        result_schema:结构化输出模型(发现者用 DiscoveryReviewResult,直审用 ReviewResult)。
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
        result_schema: Any = ReviewResult,
    ) -> ReviewOutcome:
        structured_llm = llm.with_structured_output(result_schema, method=structured_method)
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
            logger.warning("[%s] 审查员未返回结构化结果(重试 3 次后仍空)", reviewer_name)
            return ReviewOutcome(
                result=None,
                status=ReviewExecutionStatus.PROTOCOL_FAILED,
                failure_reason="structured_output_missing",
                execution_events=["structured_output_missing"],
                evidence_catalog=evidence_catalog,
            )
        # 直连无工具:目录透传(仅 P/C)。
        return ReviewOutcome(result, evidence_catalog=evidence_catalog)


class ToolAgentEngine(ReviewEngine):
    """ReAct Agent 引擎:探索工具并在同一轨迹终止消息中产出结构化结果。

    基于 langchain v1 的 ``create_agent``(langgraph 预构建图):
    - 工具循环 + 停止条件由图托管,无需手写 AgentExecutor;
    - ``ToolStrategy`` 将 ``result_schema`` 注册为 Agent 的最终结果工具，正常路径
      直接读取 ``structured_response``；
    - 只有 Agent 未返回结构化响应时，才解析终止文本并进行一次结构化 synthesis;
    - 与图编排同源，均基于 LangGraph 预构建图。

    全部工具调用先进入 Evidence Ledger/Trace，再解释终止消息；候选只引用其中最小子集。
    """

    def __init__(
        self,
        tool_client: Any,
        recursion_limit: int = 12,
        enabled_tools: list[str] | None = None,
        allow_direct_fallback: bool = True,
        projection_focus: GraphProjectionFocus | None = None,
    ) -> None:
        self._tool_client = tool_client
        # langgraph 用 recursion_limit 约束图的总步数,间接限制工具调用轮数,防止失控。
        self._recursion_limit = recursion_limit
        # 工具白名单:None=暴露所有已实现工具;否则只暴露列出的(profile 控制,对照可控)。
        self._enabled_tools = enabled_tools
        self._allow_direct_fallback = allow_direct_fallback
        self._projection_focus = projection_focus

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
        result_schema: Any = ReviewResult,
    ) -> ReviewOutcome:
        # GraphRecursionError 延迟导入(mock/无工具路径不需要 langgraph)。
        from langgraph.errors import GraphRecursionError

        try:
            raw = self._run_agent(
                llm, system_prompt, user_prompt, result_schema=result_schema
            )
        except GraphRecursionError:
            # HITL 开启时不吞异常，让它传播到上层 _review 节点的 interrupt handler，
            # 由人决定 continue/retry/skip。
            if enable_hitl:
                raise
            tool_records = list(getattr(self._tool_client, "trace_records", ()))
            gathered = _gathered_context_from_records(
                tool_records, focus=self._projection_focus
            )
            if not self._allow_direct_fallback and not gathered:
                raise
            if gathered:
                logger.warning(
                    "[%s] ReAct 达到 %d 步上限，使用已取得的 %d 条工具事实结构化收束",
                    reviewer_name,
                    self._recursion_limit,
                    len(gathered),
                )
                catalog, trace_refs = _capture_records(
                    evidence_catalog, tool_records
                )
                return _run_structured_fallback(
                    llm,
                    system_prompt=system_prompt,
                    user_prompt=_synthesis_prompt(
                        user_prompt,
                        catalog,
                        gathered,
                        focus=self._projection_focus,
                    ),
                    reviewer_name=reviewer_name,
                    max_retries=max_retries,
                    structured_method=structured_method,
                    result_schema=result_schema,
                    catalog=catalog,
                    trace_refs=trace_refs,
                    event=REACT_SYNTHESIS_FALLBACK_RECURSION_EVENT,
                )
            # ReAct 在 recursion_limit 步内没收敛(绕的难例 / 工具反复绕)。不让该域被静默丢弃
            # (那会直接丢失这一维度的发现、压低 recall),而是降级为无工具直连复审一次,至少
            # 据 diff 产出一份结论。直连无工具不会再循环。
            logger.warning(
                "[%s] ReAct 撞递归上限(%d 步未收敛),降级为无工具直连复审以保住该域产出",
                reviewer_name,
                self._recursion_limit,
            )
            catalog, trace_refs = _capture_records(evidence_catalog, tool_records)
            return _run_structured_fallback(
                llm,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                reviewer_name=reviewer_name,
                max_retries=max_retries,
                structured_method=structured_method,
                result_schema=result_schema,
                catalog=catalog,
                trace_refs=trace_refs,
                event=REACT_DEGRADED_RECURSION_EVENT,
                failure_event=REACT_DIRECT_FALLBACK_FAILED_EVENT,
            )
        tool_records = list(getattr(self._tool_client, "trace_records", ()))
        catalog, trace_refs = _capture_records(evidence_catalog, tool_records)
        structured_result = _extract_agent_structured_response(raw, result_schema)
        if structured_result is not None:
            return ReviewOutcome(
                structured_result,
                tool_trace_records=list(trace_refs),
                execution_events=["react_agent_structured"],
                evidence_catalog=catalog,
            )
        inline_result = _extract_inline_result(raw, result_schema)
        if inline_result is not None:
            return ReviewOutcome(
                inline_result,
                tool_trace_records=list(trace_refs),
                execution_events=[REACT_INLINE_STRUCTURED_EVENT],
                evidence_catalog=catalog,
            )

        gathered = _gathered_context_from_records(
            tool_records, focus=self._projection_focus
        )
        return _run_structured_fallback(
            llm,
            system_prompt=system_prompt,
            user_prompt=_synthesis_prompt(
                user_prompt,
                catalog,
                gathered,
                focus=self._projection_focus,
            ),
            reviewer_name=reviewer_name,
            max_retries=max_retries,
            structured_method=structured_method,
            result_schema=result_schema,
            catalog=catalog,
            trace_refs=trace_refs,
            event=REACT_SYNTHESIS_FALLBACK_INVALID_OUTPUT_EVENT,
        )

    def _run_agent(
        self,
        llm: Any,
        system_prompt: str,
        user_prompt: str,
        *,
        result_schema: Any,
    ) -> Any:
        """构建 ReAct agent 并执行,返回原始状态。

        抽成独立方法是为了让"撞递归上限降级"逻辑可被单测覆盖(测试覆写本方法抛
        ``GraphRecursionError``,无需构造真实 agent / 调真实 LLM)。
        """
        # LangChain 相关导入延迟到此:mock 模式 / 无工具路径不需要它们。
        from langchain.agents import create_agent
        from langchain.agents.structured_output import ToolStrategy

        from codeguard_agent.tools.definitions import (
            make_change_impact_tool,
            make_file_content_tool,
            make_path_tool,
            make_structure_tool,
        )

        # 已实现工具的工厂表。领域 Prompt 决定查询时机与 path_kind。
        available = {
            "get_file_content": lambda: make_file_content_tool(self._tool_client),
            "inspect_structure": lambda: make_structure_tool(self._tool_client),
            "inspect_change_impact": lambda: make_change_impact_tool(self._tool_client),
            "inspect_path": lambda: make_path_tool(self._tool_client),
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
            response_format=ToolStrategy(result_schema, handle_errors=True),
        )
        return agent.invoke(
            {"messages": [("human", user_prompt)]},
            config={"recursion_limit": self._recursion_limit},
        )


def _extract_agent_structured_response(raw: Any, result_schema: Any) -> Any | None:
    """读取 ToolStrategy 的结构化收口结果，并再做本地 schema 校验。"""
    if not isinstance(raw, dict) or "structured_response" not in raw:
        return None
    response = raw["structured_response"]
    if isinstance(response, result_schema):
        return response
    try:
        return result_schema.model_validate(response)
    except Exception:  # noqa: BLE001 不合规结果沿用既有 fallback
        return None


def _run_structured_fallback(
    llm: Any,
    *,
    system_prompt: str,
    user_prompt: str,
    reviewer_name: str,
    max_retries: int,
    structured_method: str,
    result_schema: Any,
    catalog: Any,
    trace_refs: list[Any],
    event: str,
    failure_event: str = REACT_SYNTHESIS_FALLBACK_FAILED_EVENT,
) -> ReviewOutcome:
    """执行唯一一次结构化收口；失败后返回显式事件，不再启动新阶段。"""
    try:
        synthesis = DirectEngine().review(
            llm,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            reviewer_name=reviewer_name,
            max_retries=max_retries,
            structured_method=structured_method,
            result_schema=result_schema,
        )
    except Exception as exc:  # noqa: BLE001 fallback 失败必须在本层终止
        logger.warning("[%s] ReAct 结构化收口失败: %s", reviewer_name, exc)
        return ReviewOutcome(
            result=None,
            status=ReviewExecutionStatus.SYNTHESIS_FAILED,
            failure_reason=type(exc).__name__,
            tool_trace_records=list(trace_refs),
            execution_events=[event, failure_event],
            evidence_catalog=catalog,
        )
    synthesis.tool_trace_records.extend(trace_refs)
    synthesis.execution_events.append(event)
    synthesis.evidence_catalog = catalog
    if synthesis.status is not ReviewExecutionStatus.COMPLETE:
        synthesis.status = ReviewExecutionStatus.SYNTHESIS_FAILED
        synthesis.execution_events.append(failure_event)
    return synthesis


def _extract_inline_result(raw: Any, result_schema: Any) -> Any | None:
    """解析 ReAct 的终止消息；结构不合法时返回 None 交给 synthesis 降级。"""
    if not isinstance(raw, dict):
        return None
    messages = raw.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return None
    final_message = messages[-1]
    if getattr(final_message, "type", "") != "ai":
        return None
    if getattr(final_message, "tool_calls", None):
        return None
    content = _message_text(getattr(final_message, "content", ""))
    if not content:
        return None
    payload = _unwrap_json_fence(content)
    variants = [payload]
    decoded = unescape(payload)
    if decoded != payload:
        variants.append(decoded)
    for variant in variants:
        try:
            return result_schema.model_validate_json(variant)
        except Exception:  # noqa: BLE001 先完成所有严格变体尝试
            continue
    # 末尾提取必须在 HTML 解码后的统一语义文本上判断唯一性，
    # 否则“实体编码对象 + 原始末尾对象”会被误当成唯一 JSON。
    suffix = _terminal_json_suffix(decoded)
    if suffix is not None:
        try:
            return result_schema.model_validate_json(suffix)
        except Exception:  # noqa: BLE001 schema 不合法时交给 synthesis
            pass
    return None


def _terminal_json_suffix(text: str) -> str | None:
    """只提取消息末尾的唯一 JSON 对象。

    DeepSeek 在工具返回后偶尔会先输出可见分析，再输出正式 JSON。
    只兼容这一种受限形态：JSON 必须位于消息末尾，且前缀不得再包含
    另一个可解析 JSON 对象。
    """
    stripped = text.strip()
    decoder = json.JSONDecoder()
    for start, char in enumerate(stripped):
        if char != "{":
            continue
        decoded = _decode_json_object_at(decoder, stripped, start)
        if decoded is None:
            continue
        _, end = decoded
        if end != len(stripped):
            continue
        prefix = stripped[:start]
        if _contains_json_object(prefix):
            return None
        return stripped[start:end]
    return None


def _contains_json_object(text: str) -> bool:
    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char != "{":
            continue
        if _decode_json_object_at(decoder, text, start) is not None:
            return True
    return False


def _decode_json_object_at(
    decoder: json.JSONDecoder, text: str, start: int
) -> tuple[dict[str, Any], int] | None:
    """在指定位置解码 JSON 对象；非对象或非法 JSON 返回 None。"""
    try:
        value, end = decoder.raw_decode(text, start)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        return value, end
    return None


def _message_text(content: Any) -> str:
    """归一化 LangChain AIMessage 的字符串或文本 content blocks。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if isinstance(block, dict) and block.get("type") in {"text", "plain_text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
        return ""
    return "".join(parts).strip()


def _unwrap_json_fence(text: str) -> str:
    """仅兼容包裹整个终止结果的单一 JSON 代码围栏。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"}:
        return stripped
    if lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()

def _synthesis_prompt(
    user_prompt: str,
    catalog: Any,
    gathered: list[GatheredContext],
    *,
    focus: GraphProjectionFocus | None = None,
) -> str:
    """合成期提示词:优先渲染证据目录(编号+摘要内容),无目录时回退有界事实。"""
    if catalog is not None:
        from codeguard_agent.pipeline.evidence.ledger import render_evidence_catalog

        rendered = render_evidence_catalog(catalog, focus=focus)
        if rendered:
            return (
                f"{user_prompt}\n\n"
                "以下是本次探索经运行时登记的 <evidence_catalog>。"
                "引用证据时只能使用其中存在的编号,不得猜测、构造或修改编号;"
                "目录外的内容不得作为证据引用。\n\n"
                f"{rendered}"
            )
    if gathered:
        return _bounded_synthesis_prompt(user_prompt, gathered)
    return user_prompt


def _capture_records(catalog: Any, tool_records: Any) -> tuple[Any, list[ToolTraceRef]]:
    """原文进入 Ledger；引擎产出只携带紧凑工具引用。

    延迟导入避免无工具路径携带 ledger 依赖。
    """
    if catalog is not None:
        from codeguard_agent.pipeline.evidence.ledger import capture_tool_records

        batch = capture_tool_records(catalog, tool_records)
        return batch.catalog, batch.trace_refs
    refs = [
        ToolTraceRef(
            call_id=str(getattr(record, "call_id", "")),
            tool=str(getattr(record, "tool", "")),
            arguments={
                key: value
                for key, value in dict(
                    getattr(record, "arguments", {}) or {}
                ).items()
                if isinstance(value, str)
            },
            status=str(getattr(record, "status", "complete")),
            duration_ms=float(getattr(record, "duration_ms", 0.0)),
            reuse_key=str(getattr(record, "reuse_key", "")),
            reused_from_call_id=str(
                getattr(record, "reused_from_call_id", "")
            ),
        )
        for record in tool_records or ()
    ]
    return None, refs


def _gathered_context_from_records(
    tool_records: Any,
    *,
    focus: GraphProjectionFocus | None = None,
) -> list[GatheredContext]:
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
        from codeguard_agent.pipeline.evidence.projection import (
            ProjectionAudience,
            project_tool_payload,
        )

        output = project_tool_payload(
            tool_name,
            output,
            ProjectionAudience.REVIEWER,
            arguments=arguments,
            focus=focus,
        ).content
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
