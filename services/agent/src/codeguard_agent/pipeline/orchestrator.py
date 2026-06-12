"""管线编排器。

把若干 PipelineStage 串成一条管线,依次在共享 PipelineContext 上执行,最后产出 ReviewResult。

⚠️ 这是 reviewer.review() 之外**并存**的新实现,不替换它(见 ADR-002)。
    --mode single 走 reviewer.review()(冻结基准);--mode pipeline 走这里。

进度:
    阶段 1:默认管线只有单个审查 stage,与 baseline 等价(已验证)。
    阶段 2:并行审查(security/logic/quality 三个领域审查员)。
    阶段 3:并行审查 → 聚合去重。← 当前
后续会继续往 build_default_pipeline() 里加:摘要 → [审查] → [聚合去重] → 误报过滤。
"""

from __future__ import annotations

import logging

from codeguard_agent.models.schemas import ReviewResult
from codeguard_agent.pipeline.stages.aggregation import AggregationStage
from codeguard_agent.pipeline.stages.base import PipelineContext, PipelineStage
from codeguard_agent.pipeline.stages.reviewer_stage import ReviewerStage

logger = logging.getLogger("codeguard")


def build_default_pipeline() -> list[PipelineStage]:
    """构造默认管线。

    阶段 3:并行审查(security/logic/quality)→ 聚合去重。
    后续会继续加:摘要 → [审查] → [聚合去重] → 误报过滤。
    """
    return [ReviewerStage(), AggregationStage()]


class PipelineOrchestrator:
    """串行执行各 stage 的编排器。

    参数:
        stages: 可选的自定义 stage 列表;不传则用 build_default_pipeline()。
    """

    def __init__(self, stages: list[PipelineStage] | None = None) -> None:
        self.stages = stages or build_default_pipeline()

    def run(
        self,
        llm,
        diff_text: str,
        max_retries: int = 3,
        structured_method: str = "function_calling",
    ) -> ReviewResult:
        """跑完整条管线,返回结构化的 ReviewResult。"""
        context = PipelineContext(
            diff_text=diff_text,
            llm=llm,
            max_retries=max_retries,
            structured_method=structured_method,
        )

        for stage in self.stages:
            context = stage.execute(context)

        return ReviewResult(summary=context.summary, issues=context.issues)
