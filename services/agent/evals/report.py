"""把聚合指标与历史归档渲染成 Markdown 评测报告。

两部分:
  - render_report:单次运行的详细报告(核心指标 + 逐用例明细 + 诊断)。
  - render_history_views:从历史归档渲染趋势 / profile 对照 / 能力切片三类视图,
    构成"系统怎么演进都能纵向比、横向比"的回归基建视图(纯函数,吃归档 dict)。
"""

from __future__ import annotations
from datetime import datetime
from codeguard_agent.config import Settings
from evals.schema import AggregateMetrics, MatchOutcome


def _fmt(x, nd: int = 3) -> str:
    """格式化指标数值;缺失时占位。"""
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def render_history_views(records: list[dict], trend_limit: int = 8) -> str:
    """从历史归档记录渲染三类视图:趋势 / profile 对照 / 能力切片。

    records:archive.load_archives() 读出的归档 dict 列表(已按时间升序)。
    这是统一标准下的回归视图——数据集与指标固定,任意 profile(工具/编排/未来规则)同框比较。
    """
    if not records:
        return "## 趋势 / 对照 / 能力切片\n\n_(暂无历史归档,跑一次评测后即可生成)_\n"
    lines: list[str] = []
    lines += [
        "## 历史趋势(最近 %d 次)" % trend_limit,
        "",
        "| 时间 | git | profile | 工具 | P | R | F1 | 误报率 |",
        "|---|---|---|---|---|---|",
    ]
    for r in records[-trend_limit:]:
        prof = r.get("profile", {})
        m = r.get("metrics", {})
        tools = "开" if prof.get("tools_enabled") else "关"
        lines.append(
            f"| {r.get('timestamp', '—')} | {r.get('git_sha', '—')} | {prof.get('name', '—')} | {tools} | {_fmt(m.get('precision'))} | {_fmt(m.get('recall'))} | {_fmt(m.get('f1'))} | {_fmt(m.get('false_positives_on_clean'))} |"
        )
    latest_by_profile: dict[str, dict] = {}
    for r in records:
        latest_by_profile[r.get("profile", {}).get("name", "?")] = r
    profiles_sorted = sorted(latest_by_profile)
    lines += [
        "",
        "## profile 横向对照(各 profile 最近一次)",
        "",
        "| profile | 工具 | P | R | F1 | 误报率 |",
        "|---|---|---|---|---|---|",
    ]
    for name in profiles_sorted:
        r = latest_by_profile[name]
        m = r.get("metrics", {})
        tools = "开" if r.get("profile", {}).get("tools_enabled") else "关"
        lines.append(
            f"| {name} | {tools} | {_fmt(m.get('precision'))} | {_fmt(m.get('recall'))} | {_fmt(m.get('f1'))} | {_fmt(m.get('false_positives_on_clean'))} |"
        )
    all_caps = sorted(
        {
            cap
            for r in latest_by_profile.values()
            for cap in r.get("by_capability") or {}
        }
    )
    if all_caps:
        header = "| 能力 \\ profile | " + " | ".join(profiles_sorted) + " |"
        sep = "|---" * (len(profiles_sorted) + 1) + "|"
        lines += [
            "",
            "## 按能力切片(各 profile 最近一次的 Recall)",
            "",
            "在'需要该能力'的用例子集上各 profile 的 Recall;同一能力行内比较即该能力的工具/编排增益。",
            "",
            header,
            sep,
        ]
        for cap in all_caps:
            row = [f"| {cap}"]
            for name in profiles_sorted:
                bycap = latest_by_profile[name].get("by_capability") or {}
                cell = bycap.get(cap)
                row.append(_fmt(cell.get("recall")) if cell else "—")
            lines.append(" | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def render_report(
    metrics: AggregateMetrics,
    settings: Settings,
    runs: list[list[MatchOutcome]],
    cases,
    *,
    model_label: str | None = None,
    quality_metrics_meaningful: bool = True,
) -> str:
    """生成 Markdown 报告文本。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    judge_line = ""
    if metrics.avg_judge_message_quality is not None:
        judge_line = f"| LLM-judge 描述质量 | {metrics.avg_judge_message_quality:.2f} / 5 |\n| LLM-judge 建议质量 | {metrics.avg_judge_suggestion_quality:.2f} / 5 |\n"
    effective_model_label = model_label or settings.model or "(provider-default)"
    lines = [
        "# Codeguard 审查质量评测报告",
        "",
        f"- 生成时间:{ts}",
        f"- Provider / Model:`{settings.provider}` / `{effective_model_label}`",
        f"- 数据集:{metrics.num_cases} 条(漏洞 {metrics.num_vuln_cases} / 干净 {metrics.num_clean_cases})",
        f"- 重复跑测:{metrics.runs} 次",
        "",
    ]
    if not quality_metrics_meaningful:
        lines += [
            "> **⚠️ Smoke only**：本次未调用审查 LLM；Precision / Recall / F1 不具有质量意义，",
            "> 仅用于验证数据集、编排、统计、归档和报告链路可执行。",
            "",
        ]
    lines += [
        "> 审查质量以固定的数据集 + 指标为统一标准;被测目标(mode / 工具 / 模型)由 profile 描述。",
        "> 跨 profile、跨版本的纵向趋势与横向对照见下方「历史趋势 / profile 对照 / 能力切片」。",
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
        f"| 诱饵命中率 | {_fmt(metrics.distractor_hit_rate)} | 过度上报里「被诱饵骗」的比例(越低=越克制) |",
        f"| vuln 噪音/条 | {_fmt(metrics.vuln_noise_per_case)} | 脏代码上平均每条 diff 误报几个(区别于 clean 误报率) |",
        f"| 报告膨胀比 | {_fmt(metrics.report_inflation)} | vuln 用例上 报告数/标答数 的均值(>1 偏过度上报) |",
        f"| 级别准确率·复杂用例 | {_fmt(metrics.severity_accuracy_complex)} | 多问题场景下的级别判准率 |",
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
            f"| {o.case_id} | {('clean' if o.is_clean else 'vuln')} | {o.expected_total} | {o.reported_total} | {o.true_positives} | {o.false_positives} | {o.false_negatives} |"
        )
    usage_rows = [o for o in runs[-1] if o.tool_usage is not None]
    if usage_rows:
        lines += [
            "",
            "## 工具使用(最后一次跑测)",
            "",
            "审查员实际发起的工具调用画像(去重后取得有效上下文的调用)。",
            "",
            "| 用例 | 工具调用 | 用到的工具 | 读取符号 |",
            "|---|---|---|---|",
        ]
        for o in usage_rows:
            u = o.tool_usage
            lines.append(
                f"| {o.case_id} | {u.tool_calls} | {', '.join(u.tools_used) or '—'} | {', '.join(u.symbols_read) or '—'} |"
            )
    council_rows = [o for o in runs[-1] if o.council_trace is not None]
    if council_rows:
        lines += [
            "",
            "## ReviewCouncil 过程统计(最后一次跑测)",
            "",
            "Evidence Ledger 中间态只用于 trace/eval,不进入最终 ReviewResult。",
            "",
            "| 用例 | 候选数 | 角色候选分布 | Artifact | Judge 裁决 | 移除 | 候选截断 |",
            "|---|---|---|---|---|---|---|",
        ]
        for o in council_rows:
            c = o.council_trace
            agent_order = ["threat_model", "behavior", "maintainability"]
            seen = set(agent_order)
            agent_parts = [
                f"{name}={c.candidate_count_by_agent.get(name, 0)}"
                for name in agent_order
            ]
            agent_parts.extend(
                (
                    f"{name}={count}"
                    for name, count in sorted(c.candidate_count_by_agent.items())
                    if name not in seen
                )
            )
            agent_detail = ", ".join(agent_parts) if agent_parts else "—"
            lines.append(
                f"| {o.case_id} | {c.candidate_count} | {agent_detail} | {c.artifact_count} | {c.verdict_count} | {c.removed_by_judge} | {c.truncated_candidates} |"
            )
        lines += [
            "",
            "### 裁决指标",
            "",
            "无支持 drop = Judge 因候选无支持事实而 drop;Judge 失败 = 合同违约 fail-closed。",
            "",
            "| 用例 | 无支持 drop | Judge 失败 | CRITICAL 候选 | 等级转移 |",
            "|---|---|---|---|---|",
        ]
        for o in council_rows:
            c = o.council_trace
            lines.append(
                f"| {o.case_id} | {c.judge_no_support_drop_count} | {c.judge_failed_candidate_count} | {c.critical_candidate_count} | {', '.join((f'{key}={value}' for key, value in sorted(c.severity_transitions.items()))) or '—'} |"
            )
        lines += [
            "",
            "### 证据覆盖与成本",
            "",
            "| 用例 | 最终 Issue 支持覆盖率 | 候选画像(patch/context/tool/ungrounded) |",
            "|---|---|---|",
        ]
        for o in council_rows:
            c = o.council_trace
            lines.append(
                f"| {o.case_id} | {c.final_issue_supported_count}/{c.final_issue_count} ({_fmt(c.final_issue_support_coverage)}) | {c.candidate_patch_only_count}/{c.candidate_context_backed_count}/{c.candidate_tool_backed_count}/{c.candidate_ungrounded_count} |"
            )
        lines += [
            "",
            "### 证据账本(Evidence Ledger)",
            "",
            "Artifact = 运行时捕获的证据(patch P01 / 预取上下文 Cxx / 工具 Txx,reused 为跨任务复用)；",
            "引用 = 候选引用经验证后的 valid/limited/invalid 数；重放 = 请求/valid/limited/失败数。",
            "",
            "| 用例 | Artifact(p/c/t/reused) | 引用(v/l/i) | 缺口(g/gi) | 重放(rq/v/l/f) | Judge(批/失败/无支持drop) |",
            "|---|---|---|---|---|---|",
        ]
        for o in council_rows:
            c = o.council_trace
            lines.append(
                f"| {o.case_id} | {c.patch_artifact_count}/{c.context_artifact_count}/{c.tool_artifact_count}/{c.reused_artifact_count} | {c.valid_reference_count}/{c.limited_reference_count}/{c.invalid_reference_count} | {c.evidence_gap_count}/{c.graph_indeterminate_count} | {c.replay_requested_count}/{c.replay_valid_count}/{c.replay_limited_count}/{c.replay_failed_count} | {c.judge_batch_call_count}/{c.judge_failed_candidate_count}/{c.judge_no_support_drop_count} |"
            )
        lines += [
            "",
            "### 降级摘要",
            "",
            "| 用例 | Direct 分派 | 发现者失败 | Task 失败 | Judge 失败 | 调查未完成 |",
            "|---|---|---|---|---|---|",
        ]
        for o in council_rows:
            c = o.council_trace
            lines.append(
                f"| {o.case_id} | {c.direct_tier_task_count} | {c.discoverer_failed_count} | {c.task_review_failed_count} | {c.judge_synthesis_failed_count} | {c.investigation_incomplete_count} |"
            )
    last = runs[-1]
    if any((o.primary_judge == "llm" for o in last)):
        diverged = [
            o
            for o in last
            if (o.true_positives, o.false_positives, o.false_negatives)
            != (o.rule_true_positives, o.rule_false_positives, o.rule_false_negatives)
        ]
        agreement = (
            f"{metrics.judge_rule_agreement:.1%}"
            if metrics.judge_rule_agreement is not None
            else "—"
        )
        lines += [
            "",
            "## 规则尺 vs 裁判尺(最后一次跑测)",
            "",
            f"**裁判↔规则一致率:{agreement}**(全部跑测累计)。这是评测尺自身的健康度——一致率低说明规则尺关键词匹配偏差大、需靠裁判纠偏,此时复杂用例指标只有开 `--judge` 才可信。",
            "",
            f"主判为 LLM 裁判(语义配对),规则尺并行作确定性交叉校验。下表只列两尺判定不一致的用例;共 {len(diverged)} 条分歧(本次跑测)。分歧为 0 则两尺一致,可放心用规则尺做廉价回归。",
            "",
            "| 用例 | 裁判 TP/FP/FN | 规则 TP/FP/FN |",
            "|---|---|---|",
        ]
        for o in diverged:
            lines.append(
                f"| {o.case_id} | {o.true_positives}/{o.false_positives}/{o.false_negatives} | {o.rule_true_positives}/{o.rule_false_positives}/{o.rule_false_negatives} |"
            )
    severity_rows = [(o.case_id, d) for o in runs[-1] for d in o.severity_detail]
    if severity_rows:
        miss = sum((1 for _, d in severity_rows if d.get("match") == "✗"))
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
                f"| {case_id} | {d.get('type', '')} | {d.get('expected', '')} | {d.get('reported', '')} | {d.get('match', '')} |"
            )
    bait_rows = [o for o in runs[-1] if o.distractor_total > 0]
    if bait_rows:
        lines += [
            "",
            "## 过度上报诊断(最后一次跑测)",
            "",
            "对埋了诱饵的用例,把误报拆成「中诱饵(被似是而非的点骗了)」与「凭空乱报(既非真问题也非诱饵)」。中诱饵高=克制力差、易被表象误导;凭空乱报高=无中生有。",
            "",
            "| 用例 | 诱饵数 | 中诱饵 | 凭空乱报 | FP 合计 |",
            "|---|---|---|---|---|",
        ]
        for o in bait_rows:
            spurious = o.false_positives - o.distractor_hits
            lines.append(
                f"| {o.case_id} | {o.distractor_total} | {o.distractor_hits} | {spurious} | {o.false_positives} |"
            )
    if metrics.recall_primary is not None or metrics.recall_secondary is not None:
        lines += [
            "",
            "## 主/次项 recall 对照",
            "",
            "按严重级别分层的检出率:主项=CRITICAL(必须修),次项=WARNING/INFO(建议/可选)。主低次高=漏掉要紧问题(危险);主高次低=只盯大的、忽略次要(可接受)。",
            "",
            "| 档位 | Recall |",
            "|---|---|",
            f"| 主项(CRITICAL) | {_fmt(metrics.recall_primary)} |",
            f"| 次项(WARNING/INFO) | {_fmt(metrics.recall_secondary)} |",
        ]
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
