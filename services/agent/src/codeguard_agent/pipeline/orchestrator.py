"""管线编排器(阶段 1 版本:单阶段直线)。

把若干 PipelineStage 串成一条管线,依次在共享 PipelineContext 上执行,最后产出 ReviewResult。

⚠️ 这是 reviewer.review() 之外**并存**的新实现,不替换它(见 ADR-002)。
阶段 1 的默认管线只有 SecurityReviewerStage,跑出来应与 baseline 逐条一致——
这一步的目的就是验证"引入管线骨架没有改变审查结果"。

后续阶段在 build_default_pipeline() 里往里加:
    阶段 2:摘要 → 并行审查(security/logic/quality)→ 聚合去重 → 误报过滤
"""

from __future__ import annotations

import logging

from codeguard_agent.models.schemas import ReviewResult
from codeguard_agent.pipeline.stages.base import PipelineContext, PipelineStage
from codeguard_agent.pipeline.stages.security_reviewer import SecurityReviewerStage

logger = logging.getLogger("codeguard")


def build_default_pipeline() -> list[PipelineStage]:
    """构造默认管线。阶段 1:只有安全审查一个 stage。"""
    return [SecurityReviewerStage()]


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
