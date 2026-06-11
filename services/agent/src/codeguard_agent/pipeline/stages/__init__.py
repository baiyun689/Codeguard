"""管线阶段(stages)。

每个 stage 是管线里的一环,读取并写回共享的 PipelineContext。
阶段 2 起会有:摘要 → 并行审查(security/logic/quality)→ 聚合去重 → 误报过滤。
阶段 1 只有一个 SecurityReviewerStage,用来把现有单次安全审查纳入管线骨架。
"""
