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

    # ① 历史趋势(最近 trend_limit 次,跨 profile 合并按时间排)
    lines += [
        "## 历史趋势(最近 %d 次)" % trend_limit,
        "",
        "| 时间 | git | profile | 工具 | P | R | F1 | 误报率 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in records[-trend_limit:]:
        prof = r.get("profile", {})
        m = r.get("metrics", {})
        tools = "开" if prof.get("tools_enabled") else "关"
        lines.append(
            f"| {r.get('timestamp', '—')} | {r.get('git_sha', '—')} | "
            f"{prof.get('name', '—')} | {tools} | "
            f"{_fmt(m.get('precision'))} | {_fmt(m.get('recall'))} | "
            f"{_fmt(m.get('f1'))} | {_fmt(m.get('false_positives_on_clean'))} |"
        )

    # 各 profile 取最近一次(对照与能力切片都基于"最新快照")
    latest_by_profile: dict[str, dict] = {}
    for r in records:
        latest_by_profile[r.get("profile", {}).get("name", "?")] = r
    profiles_sorted = sorted(latest_by_profile)

    # ② profile 横向对照(各 profile 最近一次)
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
            f"| {name} | {tools} | {_fmt(m.get('precision'))} | {_fmt(m.get('recall'))} | "
            f"{_fmt(m.get('f1'))} | {_fmt(m.get('false_positives_on_clean'))} |"
        )

    # ③ 按能力切片(行=能力,列=profile,值=该子集 recall;recall 最能体现工具增益)
    all_caps = sorted({
        cap for r in latest_by_profile.values() for cap in (r.get("by_capability") or {})
    })
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
            f"| {o.case_id} | {'clean' if o.is_clean else 'vuln'} | "
            f"{o.expected_total} | {o.reported_total} | "
            f"{o.true_positives} | {o.false_positives} | {o.false_negatives} |"
        )

    # 工具使用画像:回答"工具到底有没有被用上"(ADR-022 未答的问题)。仅工具档的用例有 tool_usage。
    # 用来分辨"真调工具导航"与"纯靠 diff 推理蒙对",尤其 callers 段有没有被实际读到。
    usage_rows = [o for o in runs[-1] if o.tool_usage is not None]
    if usage_rows:
        lines += [
            "",
            "## 工具使用(最后一次跑测)",
            "",
            "审查员实际发起的工具调用画像(去重后取得有效上下文的调用)。"
            "若 repo_map / 读到 callers 段 全为 — 但该用例仍判 TP,即工具没用上、"
            "纯靠 diff 推理蒙对(见 ADR-022)。",
            "",
            "| 用例 | 工具调用 | 用到的工具 | repo_map | 读到 callers 段 | 读取文件 |",
            "|---|---|---|---|---|---|",
        ]
        for o in usage_rows:
            u = o.tool_usage
            lines.append(
                f"| {o.case_id} | {u.tool_calls} | {', '.join(u.tools_used) or '—'} | "
                f"{'✓' if u.repomap_called else '—'} | "
                f"{'✓' if u.repomap_caller_section_read else '—'} | "
                f"{', '.join(u.files_read) or '—'} |"
            )

    council_rows = [o for o in runs[-1] if o.council_trace is not None]
    if council_rows:
        lines += [
            "",
            "## ReviewCouncil 过程统计(最后一次跑测)",
            "",
            "ADR-032 中间态只用于 trace/eval,不进入最终 ReviewResult。",
            "",
            "| 用例 | 候选 | 角色候选分布 | 证据请求 | 证据轮次 | Challenge | SelfChecker 移除 | 截断 | Trace 事件 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for o in council_rows:
            c = o.council_trace
            agent_order = ["threat_model", "behavior", "maintainability"]
            seen = set(agent_order)
            agent_parts = [
                f"{name}={c.candidate_count_by_agent.get(name, 0)}" for name in agent_order
            ]
            agent_parts.extend(
                f"{name}={count}"
                for name, count in sorted(c.candidate_count_by_agent.items())
                if name not in seen
            )
            agent_detail = ", ".join(agent_parts) if agent_parts else "—"
            removed = (
                c.removed_by_challenge
                + c.removed_by_aggregation
                + c.removed_by_fp_rules
                + c.removed_by_fp_llm
            )
            detail = (
                f"challenge={c.removed_by_challenge}, "
                f"aggregation={c.removed_by_aggregation}, "
                f"fp_rules={c.removed_by_fp_rules}, fp_llm={c.removed_by_fp_llm}"
            )
            truncated = (
                f"candidates={c.truncated_candidates}, "
                f"evidence_requests={c.truncated_evidence_requests}"
            )
            lines.append(
                f"| {o.case_id} | {c.candidate_count} | {agent_detail} | "
                f"{c.evidence_request_count} | {c.evidence_rounds} | {c.challenge_count} | "
                f"{removed} ({detail}) | {truncated} | {c.trace_events} |"
            )

    # 规则尺 vs 裁判尺交叉校验:仅当本次确有用例走 LLM 主判时才有意义。
    # 主判(LLM)与规则尺判出的 TP/FP/FN 不一致的用例,正是"关键词撞词/漏配"被裁判纠正之处,
    # 也是核对裁判是否离谱、留存可复现凭证的地方。
    last = runs[-1]
    if any(o.primary_judge == "llm" for o in last):
        diverged = [
            o for o in last
            if (o.true_positives, o.false_positives, o.false_negatives)
            != (o.rule_true_positives, o.rule_false_positives, o.rule_false_negatives)
        ]
        agreement = (
            f"{metrics.judge_rule_agreement:.1%}"
            if metrics.judge_rule_agreement is not None else "—"
        )
        lines += [
            "",
            "## 规则尺 vs 裁判尺(最后一次跑测)",
            "",
            f"**裁判↔规则一致率:{agreement}**(全部跑测累计)。这是评测尺自身的健康度——"
            "一致率低说明规则尺关键词匹配偏差大、需靠裁判纠偏,此时复杂用例指标只有开 `--judge` 才可信。",
            "",
            "主判为 LLM 裁判(语义配对),规则尺并行作确定性交叉校验。下表只列两尺判定不一致的用例;"
            f"共 {len(diverged)} 条分歧(本次跑测)。分歧为 0 则两尺一致,可放心用规则尺做廉价回归。",
            "",
            "| 用例 | 裁判 TP/FP/FN | 规则 TP/FP/FN |",
            "|---|---|---|",
        ]
        for o in diverged:
            lines.append(
                f"| {o.case_id} | "
                f"{o.true_positives}/{o.false_positives}/{o.false_negatives} | "
                f"{o.rule_true_positives}/{o.rule_false_positives}/{o.rule_false_negatives} |"
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

    # 过度上报诊断:逐复杂/带诱饵用例拆"被骗"和"凭空乱报",直接点名哪条用例骗到了 agent。
    bait_rows = [o for o in runs[-1] if o.distractor_total > 0]
    if bait_rows:
        lines += [
            "",
            "## 过度上报诊断(最后一次跑测)",
            "",
            "对埋了诱饵的用例,把误报拆成「中诱饵(被似是而非的点骗了)」与「凭空乱报(既非真问题也非诱饵)」。"
            "中诱饵高=克制力差、易被表象误导;凭空乱报高=无中生有。",
            "",
            "| 用例 | 诱饵数 | 中诱饵 | 凭空乱报 | FP 合计 |",
            "|---|---|---|---|---|",
        ]
        for o in bait_rows:
            spurious = o.false_positives - o.distractor_hits
            lines.append(
                f"| {o.case_id} | {o.distractor_total} | {o.distractor_hits} | "
                f"{spurious} | {o.false_positives} |"
            )

    # 主/次项 recall 对照:一眼看出"抓大漏小"还是反过来。
    if metrics.recall_primary is not None or metrics.recall_secondary is not None:
        lines += [
            "",
            "## 主/次项 recall 对照",
            "",
            "按严重级别分层的检出率:主项=CRITICAL(必须修),次项=WARNING/INFO(建议/可选)。"
            "主低次高=漏掉要紧问题(危险);主高次低=只盯大的、忽略次要(可接受)。",
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
