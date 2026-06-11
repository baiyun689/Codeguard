"""把聚合指标渲染成 Markdown 评测报告。

报告就是要被固化下来的 baseline 凭证:阶段 3 加 Agent 后再跑一份,两份并排对比。
"""

from __future__ import annotations

from datetime import datetime

from codeguard_agent.config import Settings

from evals.schema import AggregateMetrics, MatchOutcome


def render_report(
    metrics: AggregateMetrics,
    settings: Settings,
    runs: list[list[MatchOutcome]],
    cases,
) -> str:
    """生成 Markdown 报告文本。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    judge_line = ""
    if metrics.avg_judge_message_quality is not None:
        judge_line = (
            f"| LLM-judge 描述质量 | {metrics.avg_judge_message_quality:.2f} / 5 |\n"
            f"| LLM-judge 建议质量 | {metrics.avg_judge_suggestion_quality:.2f} / 5 |\n"
        )

    lines = [
        "# Codeguard 审查质量评测报告",
        "",
        f"- 生成时间:{ts}",
        f"- Provider / Model:`{settings.provider}` / `{settings.model or '(mock)'}`",
        f"- 数据集:{metrics.num_cases} 条(漏洞 {metrics.num_vuln_cases} / 干净 {metrics.num_clean_cases})",
        f"- 重复跑测:{metrics.runs} 次",
        "",
        "> 这份报告是**阶段 1「无 Agent 基准版」的 baseline**。阶段 3 引入工具调用 Agent 后,",
        "> 用同一数据集、同一脚本再跑一份,两份对比即 Agent 的价值证明(见 DECISIONS.md ADR-002)。",
        "",
        "## 核心指标",
        "",
        "| 指标 | 数值 | 含义 |",
        "|---|---|---|",
        f"| **Precision** | {metrics.precision:.3f} (±{metrics.precision_std:.3f}) | 报出的问题里真问题占比(越高=噪音越少) |",
        f"| **Recall** | {metrics.recall:.3f} (±{metrics.recall_std:.3f}) | 该审出的问题被审出占比(越高=漏报越少) |",
        f"| **F1** | {metrics.f1:.3f} | Precision 与 Recall 的调和平均 |",
        f"| 误报率(每条干净 diff) | {metrics.false_positives_on_clean:.3f} | 干净代码上平均误报几个(越低越好) |",
        f"| 定位准确率 | {metrics.localization_accuracy:.3f} | 命中项里行号也对上的比例 |",
        f"| 级别准确率 | {metrics.severity_accuracy:.3f} | 命中项里 severity 也对上的比例 |",
    ]
    if judge_line:
        lines.append(judge_line.rstrip())
    lines += [
        "",
        "## 逐用例明细(最后一次跑测)",
        "",
        "| 用例 | 类别 | 标答 | 报告 | TP | FP | FN |",
        "|---|---|---|---|---|---|---|",
    ]
    for o in runs[-1]:
        lines.append(
            f"| {o.case_id} | {'clean' if o.is_clean else 'vuln'} | "
            f"{o.expected_total} | {o.reported_total} | "
            f"{o.true_positives} | {o.false_positives} | {o.false_negatives} |"
        )

    # 级别诊断:逐条列出"期望级别 vs 报告级别",定位是哪几条把级别判错了。
    # 只统计标了期望 severity 的命中项(漏报的 FN 不会出现在这里)。
    severity_rows = [
        (o.case_id, d)
        for o in runs[-1]
        for d in o.severity_detail
    ]
    if severity_rows:
        miss = sum(1 for _, d in severity_rows if d.get("match") == "✗")
        lines += [
            "",
            "## 级别诊断(最后一次跑测)",
            "",
            f"只统计标了期望级别的命中项(漏报项不计)。共 {len(severity_rows)} 项,其中 {miss} 项级别判错(✗)。",
            "",
            "| 用例 | 类型 | 期望级别 | 报告级别 | 判定 |",
            "|---|---|---|---|---|",
        ]
        for case_id, d in severity_rows:
            lines.append(
                f"| {case_id} | {d.get('type', '')} | "
                f"{d.get('expected', '')} | {d.get('reported', '')} | {d.get('match', '')} |"
            )

    lines += [
        "",
        "## 怎么读这份报告",
        "",
        "- **Recall 低**:漏报多,prompt 没覆盖到的漏洞类型,或模型没看懂上下文 —— 这正是阶段 3 工具调用要补的(让 Agent 自己去读相关文件)。",
        "- **误报率高 / Precision 低**:噪音大,代码审查工具最致命的体验问题,对应阶段 2 的「误报过滤」。",
        "- **定位准确率低**:`Issue.line` 不准,影响开发者定位,可考虑结合 diff 行号映射。",
        "- **方差(±)大**:输出不稳定,温度过高或 prompt 不够约束。",
        "",
        "_本报告由 `python -m evals.runner` 自动生成。_",
    ]
    return "\n".join(lines) + "\n"
