"""Codeguard 评测(eval)框架。

阶段 1 的核心配套设施:用"带标注的数据集 + 统计指标"量化审查质量,
而不是用 assert 死磕不确定的 LLM 输出。

为什么要它(呼应 DECISIONS.md ADR-002):
    阶段 1 是"无 Agent 基准版"。本框架现在跑出的一组指标(precision/recall/F1/
    误报率)就是 baseline。阶段 3 加入工具调用 Agent 后,用同一个数据集、同一套
    指标再跑一次,差值即 Agent 的价值证明——这是整个项目最有说服力的一段。

模块划分:
    schema.py   —— 数据结构:用例(EvalCase)、标准答案(ExpectedIssue)、指标(Metrics)
    matcher.py  —— 把"报出的 Issue"对到"标准答案":规则匹配 + 可选 LLM-as-judge
    metrics.py  —— 由 TP/FP/FN 聚合 precision/recall/F1/误报率/定位准确率
    runner.py   —— CLI:加载数据集 → 跑审查(可多次跑测方差)→ 算指标 → 出报告
    report.py   —— 把聚合指标渲染成 Markdown 评测报告
"""
