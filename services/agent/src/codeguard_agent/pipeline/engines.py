"""审查员执行引擎(可插拔接缝)。

同一个领域审查员(security/logic/quality)可以用不同方式执行:
- DirectEngine:单次结构化 LLM 调用,无工具——这是阶段 1/2 的方式,留作"无工具"对照基准。
- ToolAgentEngine:ReAct Agent,可经 Java 工具服务自主获取 diff 之外的上下文(阶段 3)。

ReviewerStage 按 `context.tool_client` 是否存在选择引擎(见 design.md D1)。
把"执行方式"抽成引擎,是为了阶段 4 用 LangGraph 重构编排时只需新增一个引擎实现,
不动 ReviewerStage(扩展接缝①)。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.schemas import ReviewResult

logger = logging.getLogger("codeguard")


@dataclass(frozen=True)
class GatheredContext:
    """审查员经工具获取的一段 diff 之外上下文(供下游误报复核实证判定)。

    tool:工具名(如 get_file_content);args:入参摘要(用于去重与展示);content:工具返回内容。
    只在管线上下文流转,绝不进 Issue(守 ADR-001)。
    """

    tool: str
    args: str
    content: str


@dataclass
class ReviewOutcome:
    """单个领域审查员的产出信封:结构化结果 + 本次经工具获取的上下文。

    gathered_context 仅 ToolAgentEngine 可能非空;DirectEngine(无工具)恒为空。
    """

    result: ReviewResult
    gathered_context: list[GatheredContext] = field(default_factory=list)


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
    ) -> ReviewOutcome:
        """执行一次领域审查,返回产出信封(结构化结果 + 获取的上下文)。

        假定 llm 非 None、diff 非空(由 stage 统一处理边界)。
        """


class DirectEngine(ReviewEngine):
    """单次直接结构化调用——无工具的对照基准(行为与阶段 2 一致)。"""

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
    ) -> ReviewOutcome:
        structured_llm = llm.with_structured_output(ReviewResult, method=structured_method)
        result = invoke_with_retry(
            structured_llm,
            [("system", system_prompt), ("human", user_prompt)],
            max_retries=max_retries,
        )
        # 结构化输出可能返回 None(模型没正确发起工具调用),兜底为空(沿用既有 None 防御)。
        if result is None:
            logger.warning("[%s] 审查员未返回结构化结果,本次按空处理", reviewer_name)
            return ReviewOutcome(ReviewResult(summary=""))
        # 直连无工具:gathered_context 恒空。
        return ReviewOutcome(result)


class ToolAgentEngine(ReviewEngine):
    """ReAct Agent 引擎:可调 Java 工具服务获取上下文,再产出结构化结果。

    基于 langchain v1 的 ``create_agent``(langgraph 预构建图):
    - 工具循环 + 停止条件由图托管,无需手写 AgentExecutor;
    - ``response_format=ReviewResult`` 让图内置结构化收口,免去"逼 prompt 吐 JSON 再正则解析";
    - 这条路与 ROADMAP 阶段4「用 LangGraph 重构编排」同源,是提前铺路(见 design.md D5)。

    与 DirectEngine 同构地返回 ReviewResult;拿不到结构化结果时一律兜底为空并告警,绝不抛断
    (见 spec「ReAct 审查结果的结构化与健壮性」)。
    """

    def __init__(
        self,
        tool_client: Any,
        recursion_limit: int = 12,
        enabled_tools: list[str] | None = None,
    ) -> None:
        self._tool_client = tool_client
        # langgraph 用 recursion_limit 约束图的总步数,间接限制工具调用轮数,防止失控。
        self._recursion_limit = recursion_limit
        # 工具白名单:None=暴露所有已实现工具;否则只暴露列出的(profile 控制,对照可控)。
        self._enabled_tools = enabled_tools

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
    ) -> ReviewOutcome:
        # GraphRecursionError 延迟导入(mock/无工具路径不需要 langgraph)。
        from langgraph.errors import GraphRecursionError

        try:
            raw = self._run_agent(llm, system_prompt, user_prompt)
        except GraphRecursionError:
            # HITL 开启时不吞异常,让它传播到上层 _review 节点的 interrupt handler,
            # 由人决定 continue/retry/skip(修 ADR-018 死代码问题:此前 ToolAgentEngine
            # 内部捕获了异常,上层 HITL handler 永远收不到)。
            if enable_hitl:
                raise
            # ReAct 在 recursion_limit 步内没收敛(绕的难例 / 工具反复绕)。不让该域被静默丢弃
            # (那会直接丢失这一维度的发现、压低 recall),而是降级为无工具直连复审一次,至少
            # 据 diff 产出一份结论(见 ADR-017"审查员无工具调用预算"残留)。直连无工具不会再循环。
            logger.warning(
                "[%s] ReAct 撞递归上限(%d 步未收敛),降级为无工具直连复审以保住该域产出",
                reviewer_name,
                self._recursion_limit,
            )
            return DirectEngine().review(
                llm,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                reviewer_name=reviewer_name,
                max_retries=max_retries,
                structured_method=structured_method,
            )
        result = self._extract_result(raw, reviewer_name)
        gathered = _extract_gathered_context(raw)
        return ReviewOutcome(result, gathered)

    def _run_agent(self, llm: Any, system_prompt: str, user_prompt: str) -> Any:
        """构建 ReAct agent 并执行,返回原始状态。

        抽成独立方法是为了让"撞递归上限降级"逻辑可被单测覆盖(测试覆写本方法抛
        ``GraphRecursionError``,无需构造真实 agent / 调真实 LLM)。
        """
        # LangChain 相关导入延迟到此:mock 模式 / 无工具路径不需要它们。
        from langchain.agents import create_agent

        from codeguard_agent.tools.definitions import (
            make_file_content_tool,
            make_repo_map_tool,
        )

        # 已实现工具的工厂表。顺序即推荐用法:先 get_repo_map 导航(该读哪),再 get_file_content 细读(读得到)。
        available = {
            "get_repo_map": lambda: make_repo_map_tool(self._tool_client),
            "get_file_content": lambda: make_file_content_tool(self._tool_client),
        }
        # 按白名单挑工具:None=全开(CLI 默认);否则只开 profile 列出的(保持其声明顺序)。
        names = list(available) if self._enabled_tools is None else self._enabled_tools
        tools = [available[n]() for n in names if n in available]
        if not tools:  # 防御:白名单解析为空时回退全开,避免构造无工具的 Agent。
            tools = [factory() for factory in available.values()]
        agent = create_agent(
            llm,
            tools,
            system_prompt=system_prompt,
            response_format=ReviewResult,
        )
        return agent.invoke(
            {"messages": [("human", user_prompt)]},
            config={"recursion_limit": self._recursion_limit},
        )

    def _extract_result(self, raw: Any, reviewer_name: str) -> ReviewResult:
        """从 create_agent 的返回状态里取结构化结果,层层兜底。"""
        # 1) 首选:图内置的结构化收口结果。
        structured = raw.get("structured_response") if isinstance(raw, dict) else None
        if isinstance(structured, ReviewResult):
            return structured
        # 部分模型返回 dict 而非 Pydantic 实例(Pydantic v1/v2 兼容差异),尝试转换。
        if isinstance(structured, dict):
            try:
                return ReviewResult.model_validate(structured)
            except Exception:  # noqa: BLE001
                pass

        # 2) 兜底:扫描全部消息的文本抠 JSON(不止最后一条,防止模型在中间消息吐了结果)。
        msgs = raw.get("messages") if isinstance(raw, dict) else []
        for msg in reversed(msgs):  # 从后往前扫,优先用最后一条
            text = getattr(msg, "content", "") if hasattr(msg, "content") else str(msg)
            snippet = _extract_json_object(text)
            if snippet:
                try:
                    return ReviewResult.model_validate_json(snippet)
                except Exception:  # noqa: BLE001
                    continue

        # 3) 最终兜底:空结果 + 告警(含诊断信息,便于定位模型产出格式)。
        last_text = (_last_message_text(raw) or "")[:300]
        sr_type = type(structured).__name__ if structured is not None else "None"
        logger.warning(
            "[%s] ReAct 未产出可用结构化结果,本次按空处理 "
            "(structured_response type=%s, last_msg[:300]=%s)",
            reviewer_name, sr_type, last_text,
        )
        return ReviewResult(summary="")


def _extract_gathered_context(raw: Any) -> list[GatheredContext]:
    """从 create_agent 返回状态的消息流里抽取工具返回的上下文(ToolMessage)。

    工具入参在调用它的 AIMessage.tool_calls 里,故先建 tool_call_id → (name, args) 映射,
    再把每条 ToolMessage 配回去。对任何缺失/异常健壮:取不到一律返回已收集的部分(或空),
    绝不抛断(工具上下文是"锦上添花",不该让审查失败)。
    """
    try:
        if not isinstance(raw, dict):
            return []
        messages = raw.get("messages") or []
        # tool_call_id → (工具名, 入参摘要)
        call_meta: dict[str, tuple[str, str]] = {}
        for msg in messages:
            for call in getattr(msg, "tool_calls", None) or []:
                cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
                if cid:
                    call_meta[cid] = (name or "", _summarize_args(args))
        gathered: list[GatheredContext] = []
        for msg in messages:
            if getattr(msg, "type", "") != "tool":
                continue
            cid = getattr(msg, "tool_call_id", None)
            name, args = call_meta.get(cid or "", (getattr(msg, "name", "") or "", ""))
            content = getattr(msg, "content", "")
            content = content if isinstance(content, str) else str(content)
            if content.strip():
                gathered.append(GatheredContext(tool=name, args=args, content=content))
        return gathered
    except Exception as exc:  # noqa: BLE001 上下文捕获失败不应影响审查
        logger.warning("[engines] 抽取工具上下文失败,本次按空处理: %s", exc)
        return []


def _summarize_args(args: Any) -> str:
    """把工具入参压成简短字符串(用于去重键与展示),失败回退 str()。"""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001
        return str(args)


def _last_message_text(raw: Any) -> str:
    """取 create_agent 返回状态里最后一条消息的文本内容。"""
    if not isinstance(raw, dict):
        return str(raw)
    messages = raw.get("messages") or []
    if not messages:
        return ""
    last = messages[-1]
    content = getattr(last, "content", last)
    return content if isinstance(content, str) else str(content)


def _extract_json_object(text: str) -> str | None:
    """从可能混了 markdown/文字的文本里抽出第一个花括号配平的 JSON 对象。"""
    if not text:
        return None
    # 优先 ```json ... ``` 代码块
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1)
        if _is_json(candidate):
            return candidate
    # 否则做花括号配平扫描
    depth = 0
    start = -1
    for i, c in enumerate(text):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start : i + 1]
                if _is_json(candidate):
                    return candidate
    return None


def _is_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except json.JSONDecodeError:
        return False
