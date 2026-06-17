"""历史视图报告渲染的工程正确性测试。

给定若干归档记录(dict),断言趋势 / profile 对照 / 能力切片三类视图区块生成且数据落位正确。
"""

from __future__ import annotations

from evals.report import render_history_views


def _record(ts, profile, *, tools_enabled, recall, cap_recall):
    return {
        "timestamp": ts,
        "git_sha": "abc",
        "profile": {"name": profile, "mode": "pipeline", "tools": [], "tools_enabled": tools_enabled},
        "metrics": {
            "precision": 0.5, "recall": recall, "f1": 0.5,
            "false_positives_on_clean": 0.3,
        },
        "by_capability": {
            "file": {"precision": 0.5, "recall": cap_recall, "f1": 0.5,
                     "false_positives_on_clean": 0.0},
        },
    }


def test_empty_history_renders_placeholder():
    out = render_history_views([])
    assert "暂无历史归档" in out


def test_three_views_present():
    records = [
        _record("2026-06-14T10-00-00", "pipeline-notools", tools_enabled=False, recall=0.6, cap_recall=0.3),
        _record("2026-06-14T11-00-00", "pipeline-file", tools_enabled=True, recall=0.8, cap_recall=0.9),
    ]
    out = render_history_views(records)
    assert "## 历史趋势" in out
    assert "## profile 横向对照" in out
    assert "## 按能力切片" in out
    # 两个 profile 都出现在对照/趋势里
    assert "pipeline-notools" in out
    assert "pipeline-file" in out
    # 工具开/关如实呈现
    assert "开" in out and "关" in out


def test_capability_slice_uses_latest_per_profile():
    # 同一 profile 两次,能力切片应取最近一次(cap_recall=0.95)。
    records = [
        _record("2026-06-14T10-00-00", "pipeline-file", tools_enabled=True, recall=0.7, cap_recall=0.5),
        _record("2026-06-14T12-00-00", "pipeline-file", tools_enabled=True, recall=0.9, cap_recall=0.95),
    ]
    out = render_history_views(records)
    assert "0.950" in out      # 最近一次的 file 能力 recall
    assert "0.500" not in out.split("## 按能力切片")[1]  # 旧值不出现在切片表


def test_trend_limit_caps_rows():
    records = [
        _record(f"2026-06-14T{h:02d}-00-00", "p", tools_enabled=False, recall=0.5, cap_recall=0.5)
        for h in range(10)
    ]
    out = render_history_views(records, trend_limit=3)
    # 趋势区块只取最近 3 行(09/08/07 时段),不含最早的 00。
    trend_block = out.split("## profile 横向对照")[0]
    assert "T09-00-00" in trend_block
    assert "T00-00-00" not in trend_block


# ---- render_report 新增段(eval-complex-behavior) ----

from types import SimpleNamespace  # noqa: E402

from evals.report import render_report  # noqa: E402
from evals.schema import AggregateMetrics, MatchOutcome  # noqa: E402


def _metrics(**kw) -> AggregateMetrics:
    base = dict(
        runs=1, num_cases=1, num_vuln_cases=1, num_clean_cases=0,
        precision=0.5, recall=0.5, f1=0.5,
        false_positives_on_clean=0.0, localization_accuracy=1.0, severity_accuracy=1.0,
    )
    base.update(kw)
    return AggregateMetrics(**base)


def _settings():
    return SimpleNamespace(provider="mock", model="m")


def test_报告_渲染行为诊断指标行():
    m = _metrics(distractor_hit_rate=0.25, vuln_noise_per_case=1.5,
                 report_inflation=2.0, severity_accuracy_complex=0.5)
    out = render_report(m, _settings(), [[MatchOutcome(case_id="v", is_clean=False)]], [])
    assert "诱饵命中率" in out
    assert "vuln 噪音/条" in out
    assert "报告膨胀比" in out
    assert "级别准确率·复杂用例" in out


def test_报告_None指标渲染占位符不报错():
    m = _metrics()  # 新指标全 None / 默认
    out = render_report(m, _settings(), [[MatchOutcome(case_id="v", is_clean=False)]], [])
    assert "诱饵命中率 | —" in out          # None → "—"


def test_报告_一致率百分比渲染():
    m = _metrics(judge_rule_agreement=0.5)
    run = [MatchOutcome(case_id="v", is_clean=False, primary_judge="llm",
                        true_positives=1, rule_true_positives=0, rule_false_positives=1)]
    out = render_report(m, _settings(), [run], [])
    assert "裁判↔规则一致率:50.0%" in out


def test_报告_过度上报诊断段():
    run = [MatchOutcome(case_id="cx", is_clean=False, false_positives=2,
                        distractor_total=1, distractor_hits=1)]
    out = render_report(_metrics(distractor_hit_rate=1.0), _settings(), [run], [])
    assert "## 过度上报诊断" in out
    assert "cx" in out


def test_报告_主次recall对照段():
    m = _metrics(recall_primary=0.8, recall_secondary=0.4)
    out = render_report(m, _settings(), [[MatchOutcome(case_id="v", is_clean=False)]], [])
    assert "## 主/次项 recall 对照" in out
    assert "0.800" in out and "0.400" in out
