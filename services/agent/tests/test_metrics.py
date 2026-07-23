"""评测指标聚合(evals.metrics.aggregate)的工程正确性测试。

重点:行为诊断指标族(eval-complex-behavior)的公式各一例,裁判↔规则一致率从 rule_* 算对,
以及既有六项指标口径在新增字段后保持不变(回归)。指标是确定性聚合,该死磕。
"""

from __future__ import annotations

from evals.metrics import aggregate
from evals.schema import CouncilTraceStats, MatchOutcome


def _vuln(**kw) -> MatchOutcome:
    base = dict(case_id="v", is_clean=False, expected_total=1, reported_total=1)
    base.update(kw)
    return MatchOutcome(**base)


def _clean(**kw) -> MatchOutcome:
    base = dict(case_id="c", is_clean=True, expected_total=0)
    base.update(kw)
    return MatchOutcome(**base)


# ---- 既有六项指标口径不变(回归) ----

def test_既有指标口径不变():
    # 2 TP, 1 FP, 1 FN → P=2/3, R=2/3;clean 1 条 2 误报 → 误报率 2.0
    run = [
        _vuln(expected_total=3, reported_total=3, true_positives=2, false_positives=1, false_negatives=1,
              localization_hits=2, severity_hits=1, severity_checked=2),
        _clean(reported_total=2, false_positives=2),
    ]
    m = aggregate([run])
    assert m.precision == 0.4                             # TP 2 / (TP 2 + FP 3:vuln 1 + clean 2)
    assert round(m.recall, 3) == 0.667
    assert m.false_positives_on_clean == 2.0
    assert round(m.localization_accuracy, 3) == 1.0      # 2 命中 / 2 TP
    assert m.severity_accuracy == 0.5                     # 1 / 2


# ---- 行为诊断指标族 ----

def test_诱饵命中率():
    run = [
        _vuln(distractor_total=2, distractor_hits=1, false_positives=1),
        _vuln(distractor_total=1, distractor_hits=1, false_positives=1),
    ]
    m = aggregate([run])
    assert m.distractor_hit_rate == 2 / 3   # (1+1) / (2+1)


def test_诱饵命中率_无诱饵为None():
    m = aggregate([[_vuln(true_positives=1)]])
    assert m.distractor_hit_rate is None


def test_vuln噪音每条与膨胀比():
    run = [
        _vuln(expected_total=2, reported_total=4, true_positives=2, false_positives=2),
        _vuln(expected_total=1, reported_total=1, true_positives=1, false_positives=0),
    ]
    m = aggregate([run])
    assert m.vuln_noise_per_case == 1.0          # (2+0)/2 条
    assert m.report_inflation == 1.5             # mean(4/2, 1/1) = mean(2,1)


def test_候选归并压缩重复与疑似误归并指标():
    run = [
        _vuln(
            case_id="duplicate",
            expected_total=1,
            reported_total=2,
            true_positives=1,
            false_positives=1,
            council_trace=CouncilTraceStats(
                raw_candidate_count=4,
                candidate_count=3,
                candidate_dedup_removed_count=1,
            ),
        ),
        _vuln(
            case_id="adjacent",
            expected_total=2,
            reported_total=2,
            true_positives=1,
            false_positives=1,
            false_negatives=1,
            council_trace=CouncilTraceStats(
                raw_candidate_count=3,
                candidate_count=2,
                candidate_dedup_removed_count=1,
            ),
        ),
    ]

    metrics = aggregate([run])

    assert metrics.candidate_compression_rate == 2 / 7
    assert metrics.duplicate_report_rate == 0.5
    assert metrics.suspected_false_merge_rate == 0.5


def test_主次项recall分层():
    run = [
        _vuln(expected_total=4, true_positives=2,
              tp_primary=1, fn_primary=1, tp_secondary=1, fn_secondary=1),
    ]
    m = aggregate([run])
    assert m.recall_primary == 0.5
    assert m.recall_secondary == 0.5


def test_分层无该档标答为None():
    run = [_vuln(tp_primary=1, fn_primary=0)]   # 只有主项
    m = aggregate([run])
    assert m.recall_primary == 1.0
    assert m.recall_secondary is None


def test_级别准确率复杂切片():
    run = [
        # 复杂用例(标答>1):级别 1 对 1 错
        _vuln(expected_total=3, true_positives=2, severity_hits=1, severity_checked=2),
        # 简单用例(标答=1):级别对,但不该计入复杂切片
        _vuln(expected_total=1, true_positives=1, severity_hits=1, severity_checked=1),
    ]
    m = aggregate([run])
    assert m.severity_accuracy == 2 / 3                  # 全局:(1+1)/(2+1)
    assert m.severity_accuracy_complex == 0.5            # 仅复杂:1/2


def test_裁判规则一致率():
    run = [
        # LLM 主判,两尺一致
        _vuln(primary_judge="llm", true_positives=1, false_positives=0, false_negatives=0,
              rule_true_positives=1, rule_false_positives=0, rule_false_negatives=0),
        # LLM 主判,两尺分歧
        _vuln(primary_judge="llm", true_positives=1, false_positives=0, false_negatives=0,
              rule_true_positives=0, rule_false_positives=1, rule_false_negatives=1),
        # 规则主判,不计入一致率分母
        _vuln(primary_judge="rule", true_positives=1),
    ]
    m = aggregate([run])
    assert m.judge_rule_agreement == 0.5   # 2 个 LLM 主判里 1 个一致


def test_无LLM主判一致率为None():
    m = aggregate([[_vuln(primary_judge="rule", true_positives=1)]])
    assert m.judge_rule_agreement is None
