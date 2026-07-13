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
from evals.schema import CouncilTraceStats  # noqa: E402


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


def test_mock_no_llm报告隐藏配置模型并醒目标记质量指标无意义():
    out = render_report(
        _metrics(),
        SimpleNamespace(provider="mock", model="deepseek-v4-pro"),
        [[MatchOutcome(case_id="smoke", is_clean=True)]],
        [],
        model_label="(mock-no-llm)",
        quality_metrics_meaningful=False,
    )

    assert "Provider / Model:`mock` / `(mock-no-llm)`" in out
    assert "deepseek-v4-pro" not in out
    assert "**⚠️ Smoke only**" in out
    assert "Precision / Recall / F1 不具有质量意义" in out


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


def test_报告_ReviewCouncil统计展示裁决与Phase5过程指标():
    run = [
        MatchOutcome(
            case_id="v",
            is_clean=False,
            council_trace={
                "candidate_count": 3,
                "candidate_count_by_agent": {
                    "threat_model": 1,
                    "behavior": 1,
                    "maintainability": 1,
                },
                "evidence_request_count": 2,
                "truncated_candidates": 1,
                "evidence_rounds": 1,
                "verdict_count": 3,
                "removed_by_judge": 1,
                "removed_by_aggregation": 1,
                "direct_counter_candidate_count": 1,
                "direct_counter_retained_count": 0,
                "direct_counter_retained_rate": 0.0,
                "all_insufficient_candidate_count": 1,
                "all_insufficient_retained_count": 1,
                "all_insufficient_retained_rate": 1.0,
                "final_issue_count": 2,
                "final_issue_strategy_covered_count": 1,
                "final_issue_strategy_coverage": 0.5,
                "final_issue_fact_covered_count": 1,
                "final_issue_fact_coverage": 0.5,
                "registry_risk_tag_covered_count": 24,
                "registry_risk_tag_total": 24,
                "registry_risk_tag_coverage": 1.0,
                "actual_evidence_tool_calls": 1,
                "average_evidence_tool_calls": 0.333333,
                "trace_events": 9,
            },
        )
    ]

    out = render_report(_metrics(), _settings(), [run], [])

    assert "角色候选分布" in out
    assert "threat_model=1, behavior=1, maintainability=1" in out
    assert "证据请求" in out
    assert "Judge 裁决" in out
    assert "direct counter 保留率" in out
    assert "0/1 (0.000)" in out
    assert "全 insufficient 保留率" in out
    assert "1/1 (1.000)" in out
    assert "最终 Issue 策略覆盖率" in out
    assert "1/2 (0.500)" in out
    assert "最终 Issue 有效事实覆盖率" in out
    assert "RiskTag 策略覆盖率" in out
    assert "24/24 (1.000)" in out
    assert "平均实际证据工具调用" in out
    assert "1/3 (0.333)" in out
    assert "evidence_requests=" not in out


def test_旧归档缺少Phase5字段时使用默认值而不报错():
    stats = CouncilTraceStats.model_validate(
        {"candidate_count": 2, "challenge_count": 2, "removed_by_challenge": 1}
    )

    assert stats.candidate_count == 2
    assert stats.verdict_count == 0
    assert stats.direct_counter_retained_rate is None
    assert stats.average_evidence_tool_calls == 0.0
