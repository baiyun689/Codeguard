"""安全审查阶段。

阶段 1 的唯一 stage:把现有"单次安全审查"(pipeline/reviewer.py 的 review())
原封不动地包进管线骨架里,做一个**薄适配层**。

为什么直接复用 review() 而不重写:
- baseline 的 review() 仍是安全审查逻辑的唯一来源,不产生第二份会漂移的实现。
- mock 模式、空 diff、结构化输出 None 兜底等处理全部白捡,与 baseline 行为逐字一致。
- 这正是 ADR-002 "保护 baseline" 的要求:管线版与基准版结果由构造保证相同。

阶段 2 会在这一层旁边新增 LogicReviewerStage / QualityReviewerStage 并行运行。
"""

from __future__ import annotations

import logging

from codeguard_agent.pipeline.reviewer import review
from codeguard_agent.pipeline.stages.base import PipelineContext, PipelineStage

logger = logging.getLogger("codeguard")


class SecurityReviewerStage(PipelineStage):
    """安全维度审查:委托给 reviewer.review(),把结果并入 context。"""

    @property
    def name(self) -> str:
        return "security_reviewer"

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("管线阶段 [%s]:执行安全审查", self.name)
        result = review(
            context.llm,
            context.diff_text,
            max_retries=context.max_retries,
            structured_method=context.structured_method,
        )
        context.issues.extend(result.issues)
        # 阶段 1 只有一个 stage,直接采用其摘要;阶段 3 聚合阶段会改写这里。
        if result.summary:
            context.summary = result.summary
        return context
