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
    )
