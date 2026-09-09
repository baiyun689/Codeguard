"""审查员执行引擎（可插拔接缝）。

同一个领域审查员可以用不同方式执行：
- DirectEngine：单次结构化 LLM 调用，无工具——"无工具"对照基准。
有工具审查由 pipeline.controlled.subtask_react 负责。

调用方按 tool_client 是否存在选择引擎。
"""

from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from time import sleep
from typing import Any
from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.schemas import ReviewResult

logger = logging.getLogger("codeguard")


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


@dataclass
class ReviewOutcome:
    """单个领域审查员的产出信封:结构化结果 + 本次工具调用记录与证据目录。"""

    result: Any | None
    status: ReviewExecutionStatus = ReviewExecutionStatus.COMPLETE
    failure_reason: str = ""
    tool_trace_records: list[Any] = field(default_factory=list)
    execution_events: list[str] = field(default_factory=list)
    evidence_catalog: Any = None

    def __post_init__(self) -> None:
        if self.status is ReviewExecutionStatus.COMPLETE and self.result is None:
            raise ValueError("complete review outcome requires a structured result")
        if (
            self.status is not ReviewExecutionStatus.COMPLETE
            and self.result is not None
        ):
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
        structured_llm = llm.with_structured_output(
            result_schema, method=structured_method
        )
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
                    "[%s] 审查员未返回结构化结果(第 %d 次),1s 后重试",
                    reviewer_name,
                    attempt + 1,
                )
                sleep(1)
        if result is None:
            logger.warning(
                "[%s] 审查员未返回结构化结果(重试 3 次后仍空)", reviewer_name
            )
            return ReviewOutcome(
                result=None,
                status=ReviewExecutionStatus.PROTOCOL_FAILED,
                failure_reason="structured_output_missing",
                execution_events=["structured_output_missing"],
                evidence_catalog=evidence_catalog,
            )
        return ReviewOutcome(result, evidence_catalog=evidence_catalog)
