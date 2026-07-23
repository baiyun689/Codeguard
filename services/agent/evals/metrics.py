"""由判定结果(MatchOutcome)聚合统计指标。

核心指标定义:
    precision = TP / (TP + FP)        报出的问题里有多少是真的(越高=越少噪音)
    recall    = TP / (TP + FN)        该审出的问题里审出了多少(越高=越少漏报)
    f1        = 2PR / (P + R)         两者的调和平均
    误报率     = clean 样本上的 FP 总数 / clean 样本数(平均每条干净 diff 误报几个)
    定位准确率 = 命中项里行号也对上的比例
    级别准确率 = 命中项里 severity 也对上的比例

multi-run:同一数据集跑 N 次,先对每次算 precision/recall,再求均值与标准差。
LLM 输出有随机性,只报单次数字没意义,必须带方差。
"""

from __future__ import annotations

from statistics import mean, pstdev

from evals.schema import AggregateMetrics, MatchOutcome


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _f1(precision: float, recall: float) -> float:
    return _safe_div(2 * precision * recall, precision + recall)


def aggregate_run(outcomes: list[MatchOutcome]) -> tuple[float, float]:
    """对单次跑测的所有用例,算 (precision, recall)。"""
    tp = sum(o.true_positives for o in outcomes)
    fp = sum(o.false_positives for o in outcomes)
    fn = sum(o.false_negatives for o in outcomes)
    return _safe_div(tp, tp + fp), _safe_div(tp, tp + fn)


def aggregate_by_capability(
    runs: list[list[MatchOutcome]],
    case_capabilities: dict[str, list[str]],
) -> dict[str, AggregateMetrics]:
    """按能力标签切片聚合:对"需要某能力"的用例子集各算一组指标。

    这是回归基建的归因维度——在标注为需要 `file` 的用例上比较开/关工具的 profile,
    指标差即该能力的工具增益,比笼统的"工具开 vs 关"精确(见 design.md D2)。

    参数:
        runs: 多次跑测,每次是一组 MatchOutcome。
        case_capabilities: case_id → 能力标签列表(来自 EvalCase.capability)。
    返回:
        能力标签 → 该子集的 AggregateMetrics;无对应用例的标签不出现。
    """
    tags = sorted({t for caps in case_capabilities.values() for t in caps})
    sliced: dict[str, AggregateMetrics] = {}
    for tag in tags:
        ids = {cid for cid, caps in case_capabilities.items() if tag in caps}
        filtered = [[o for o in run if o.case_id in ids] for run in runs]
        if not any(filtered):  # 该能力下没有用例,跳过
            continue
        sliced[tag] = aggregate(filtered)
    return sliced


def aggregate(runs: list[list[MatchOutcome]]) -> AggregateMetrics:
    """把 N 次跑测(每次是一组 MatchOutcome)聚合成最终指标。

    参数:
        runs: 长度为 N 的列表,每个元素是"该次跑测下所有用例的判定结果"。
    """
    if not runs:
        raise ValueError("没有任何跑测结果可聚合")

    # 每次跑测的 precision/recall,用于求均值与方差
    per_run_pr = [aggregate_run(run) for run in runs]
    precisions = [p for p, _ in per_run_pr]
    recalls = [r for _, r in per_run_pr]

    # 用全部跑测的累计计数算"总体"P/R/F1(比对均值更稳)
    tp = sum(o.true_positives for run in runs for o in run)
    fp = sum(o.false_positives for run in runs for o in run)
    fn = sum(o.false_negatives for run in runs for o in run)
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)

    # 误报率:只看 clean 样本
    clean_fp = sum(o.false_positives for run in runs for o in run if o.is_clean)
    clean_count = sum(1 for o in runs[0] if o.is_clean) * len(runs)
    fp_on_clean = _safe_div(clean_fp, clean_count)

    # 定位 / 级别准确率(分母是命中项)
    loc_hits = sum(o.localization_hits for run in runs for o in run)
    sev_hits = sum(o.severity_hits for run in runs for o in run)
    sev_checked = sum(o.severity_checked for run in runs for o in run)
    localization_acc = _safe_div(loc_hits, tp)
    severity_acc = _safe_div(sev_hits, sev_checked)

    # LLM-as-judge 质量分(若有)
    all_scores = [s for run in runs for o in run for s in o.judge_scores]
    avg_msg = mean(s.message_quality for s in all_scores) if all_scores else None
    avg_sug = mean(s.suggestion_quality for s in all_scores) if all_scores else None

    # ---- 行为诊断指标族(eval-complex-behavior)----
    all_outcomes = [o for run in runs for o in run]
    vuln_outcomes = [o for o in all_outcomes if not o.is_clean]

    # 诱饵命中率:Σ中诱饵 / Σ诱饵总数;无诱饵用例 → None
    distractor_total = sum(o.distractor_total for o in all_outcomes)
    distractor_hits = sum(o.distractor_hits for o in all_outcomes)
    distractor_hit_rate = _safe_div(distractor_hits, distractor_total) if distractor_total else None

    # vuln 噪音/条 + 报告膨胀比(只看 vuln 用例)
    vuln_noise_per_case = _safe_div(sum(o.false_positives for o in vuln_outcomes), len(vuln_outcomes))
    inflations = [_safe_div(o.reported_total, o.expected_total) for o in vuln_outcomes if o.expected_total]
    report_inflation = mean(inflations) if inflations else 0.0

    # 候选归并诊断。重复率是无需语义二次裁判即可稳定计算的上界；疑似误归并
    # 只定位待人工复核用例，不把“归并后仍漏报”直接宣称为因果误归并。
    council_outcomes = [o for o in all_outcomes if o.council_trace is not None]
    raw_candidates = sum(
        o.council_trace.raw_candidate_count
        for o in council_outcomes
        if o.council_trace is not None
    )
    removed_candidates = sum(
        o.council_trace.candidate_dedup_removed_count
        for o in council_outcomes
        if o.council_trace is not None
    )
    candidate_compression_rate = (
        _safe_div(removed_candidates, raw_candidates)
        if raw_candidates
        else None
    )
    dedup_vuln_outcomes = [
        o for o in vuln_outcomes if o.council_trace is not None
    ]
    reported_with_dedup = sum(o.reported_total for o in dedup_vuln_outcomes)
    duplicate_excess = sum(
        max(0, o.reported_total - o.expected_total)
        for o in dedup_vuln_outcomes
    )
    duplicate_report_rate = (
        _safe_div(duplicate_excess, reported_with_dedup)
        if reported_with_dedup
        else None
    )
    merged_outcomes = [
        o
        for o in dedup_vuln_outcomes
        if o.council_trace is not None
        and o.council_trace.candidate_dedup_removed_count > 0
    ]
    suspected_false_merge_rate = (
        _safe_div(
            sum(o.false_negatives > 0 for o in merged_outcomes),
            len(merged_outcomes),
        )
        if merged_outcomes
        else None
    )

    # severity 分层 recall(主=CRITICAL,次=WARNING/INFO)
    tp_p = sum(o.tp_primary for o in all_outcomes)
    fn_p = sum(o.fn_primary for o in all_outcomes)
    tp_s = sum(o.tp_secondary for o in all_outcomes)
    fn_s = sum(o.fn_secondary for o in all_outcomes)
    recall_primary = _safe_div(tp_p, tp_p + fn_p) if (tp_p + fn_p) else None
    recall_secondary = _safe_div(tp_s, tp_s + fn_s) if (tp_s + fn_s) else None

    # 级别准确率·复杂用例切片(标答 > 1)
    cx = [o for o in all_outcomes if o.expected_total > 1]
    cx_sev_hits = sum(o.severity_hits for o in cx)
    cx_sev_checked = sum(o.severity_checked for o in cx)
    severity_accuracy_complex = _safe_div(cx_sev_hits, cx_sev_checked) if cx_sev_checked else None

    # 裁判↔规则一致率:两尺 TP/FP/FN 全等的 LLM 主判用例占 LLM 主判用例的比例
    llm_judged = [o for o in all_outcomes if o.primary_judge == "llm"]
    agreed = sum(
        1 for o in llm_judged
        if (o.true_positives, o.false_positives, o.false_negatives)
        == (o.rule_true_positives, o.rule_false_positives, o.rule_false_negatives)
    )
    judge_rule_agreement = _safe_div(agreed, len(llm_judged)) if llm_judged else None

    first_run = runs[0]
    return AggregateMetrics(
        runs=len(runs),
        num_cases=len(first_run),
        num_vuln_cases=sum(1 for o in first_run if not o.is_clean),
        num_clean_cases=sum(1 for o in first_run if o.is_clean),
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        false_positives_on_clean=fp_on_clean,
        localization_accuracy=localization_acc,
        severity_accuracy=severity_acc,
        recall_std=pstdev(recalls) if len(recalls) > 1 else 0.0,
        precision_std=pstdev(precisions) if len(precisions) > 1 else 0.0,
        avg_judge_message_quality=avg_msg,
        avg_judge_suggestion_quality=avg_sug,
        distractor_hit_rate=distractor_hit_rate,
        vuln_noise_per_case=vuln_noise_per_case,
        report_inflation=report_inflation,
        candidate_compression_rate=candidate_compression_rate,
        duplicate_report_rate=duplicate_report_rate,
        suspected_false_merge_rate=suspected_false_merge_rate,
        recall_primary=recall_primary,
        recall_secondary=recall_secondary,
        severity_accuracy_complex=severity_accuracy_complex,
        judge_rule_agreement=judge_rule_agreement,
    )
