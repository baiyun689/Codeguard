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
from codeguard_agent.pipeline.stages.fp_filter import FalsePositiveFilterStage
from codeguard_agent.pipeline.stages.reviewer_stage import ReviewerStage

logger = logging.getLogger("codeguard")


def build_default_pipeline(fp_llm_verify: bool = False) -> list[PipelineStage]:
    """构造默认管线。

    阶段 4:并行审查(security/logic/quality)→ 聚合去重 → 误报过滤。
    后续可在审查之前补"摘要"阶段。

    fp_llm_verify:误报过滤是否启用第二段 LLM 验证(默认关)。
    """
    return [
        ReviewerStage(),
        AggregationStage(),
        FalsePositiveFilterStage(enable_llm_verification=fp_llm_verify),
    ]


class PipelineOrchestrator:
    """串行执行各 stage 的编排器。

    参数:
        stages: 可选的自定义 stage 列表;不传则用 build_default_pipeline()。
    """

    def __init__(
        self,
        stages: list[PipelineStage] | None = None,
        fp_llm_verify: bool = False,
    ) -> None:
        self.stages = stages or build_default_pipeline(fp_llm_verify=fp_llm_verify)

    def run(
        self,
        llm,
        diff_text: str,
        max_retries: int = 3,
        structured_method: str = "function_calling",
        fp_verify_llm=None,
        repo_path: str | None = None,
        allowed_files: list[str] | None = None,
        tool_client=None,
    ) -> ReviewResult:
        """跑完整条管线,返回结构化的 ReviewResult。

        fp_verify_llm:误报过滤第二段的验证模型(建议异源);None 时回退到 llm。
        repo_path / allowed_files / tool_client:阶段 3 工具调用上下文;
            tool_client 非 None 时审查员走 ReAct(可调工具),否则走直连基准(见 design.md D1)。
        """
        context = PipelineContext(
            diff_text=diff_text,
            llm=llm,
            max_retries=max_retries,
            structured_method=structured_method,
            fp_verify_llm=fp_verify_llm,
            repo_path=repo_path,
            allowed_files=allowed_files or [],
            tool_client=tool_client,
        )

        for stage in self.stages:
            context = stage.execute(context)

        return ReviewResult(summary=context.summary, issues=context.issues)
