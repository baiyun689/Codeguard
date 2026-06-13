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
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.schemas import ReviewResult

logger = logging.getLogger("codeguard")


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
    ) -> ReviewResult:
        """执行一次领域审查,返回结构化结果。假定 llm 非 None、diff 非空(由 stage 统一处理边界)。"""


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
    ) -> ReviewResult:
        structured_llm = llm.with_structured_output(ReviewResult, method=structured_method)
        result = invoke_with_retry(
            structured_llm,
            [("system", system_prompt), ("human", user_prompt)],
            max_retries=max_retries,
        )
        # 结构化输出可能返回 None(模型没正确发起工具调用),兜底为空(沿用既有 None 防御)。
        if result is None:
            logger.warning("[%s] 审查员未返回结构化结果,本次按空处理", reviewer_name)
            return ReviewResult(summary="")
        return result


class ToolAgentEngine(ReviewEngine):
    """ReAct Agent 引擎:可调 Java 工具服务获取上下文,再产出结构化结果。

    基于 langchain v1 的 ``create_agent``(langgraph 预构建图):
    - 工具循环 + 停止条件由图托管,无需手写 AgentExecutor;
    - ``response_format=ReviewResult`` 让图内置结构化收口,免去"逼 prompt 吐 JSON 再正则解析";
    - 这条路与 ROADMAP 阶段4「用 LangGraph 重构编排」同源,是提前铺路(见 design.md D5)。

    与 DirectEngine 同构地返回 ReviewResult;拿不到结构化结果时一律兜底为空并告警,绝不抛断
    (见 spec「ReAct 审查结果的结构化与健壮性」)。
    """

    def __init__(self, tool_client: Any, recursion_limit: int = 12) -> None:
        self._tool_client = tool_client
        # langgraph 用 recursion_limit 约束图的总步数,间接限制工具调用轮数,防止失控。
        self._recursion_limit = recursion_limit

    def review(
        self,
        llm: Any,
        *,
        system_prompt: str,
        user_prompt: str,
        reviewer_name: str,
        max_retries: int,
        structured_method: str,
    ) -> ReviewResult:
        # LangChain 相关导入延迟到此:mock 模式 / 无工具路径不需要它们。
        from langchain.agents import create_agent

        from codeguard_agent.tools.definitions import make_file_content_tool

        tools = [make_file_content_tool(self._tool_client)]
        agent = create_agent(
            llm,
            tools,
            system_prompt=system_prompt,
            response_format=ReviewResult,
        )
        raw = agent.invoke(
            {"messages": [("human", user_prompt)]},
            config={"recursion_limit": self._recursion_limit},
        )
        return self._extract_result(raw, reviewer_name)

    def _extract_result(self, raw: Any, reviewer_name: str) -> ReviewResult:
        """从 create_agent 的返回状态里取结构化结果,层层兜底。"""
        # 1) 首选:图内置的结构化收口结果。
        structured = raw.get("structured_response") if isinstance(raw, dict) else None
        if isinstance(structured, ReviewResult):
            return structured

        # 2) 兜底:从最后一条消息的文本里抠 JSON(防止个别模型没走结构化通道)。
        text = _last_message_text(raw)
        snippet = _extract_json_object(text)
        if snippet:
            try:
                return ReviewResult.model_validate_json(snippet)
            except Exception:  # noqa: BLE001
                pass

        # 3) 最终兜底:空结果 + 告警,不抛断。
        logger.warning("[%s] ReAct 未产出可用结构化结果,本次按空处理", reviewer_name)
        return ReviewResult(summary="")


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
