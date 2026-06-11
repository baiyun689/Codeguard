"""评测跑批入口(CLI)。

用法:
    # 零成本验证评测骨架是否打通(不调真实 LLM)
    CODEGUARD_PROVIDER=mock python -m evals.runner

    # 调真实 LLM 跑 baseline,重复 3 次统计方差
    export CODEGUARD_API_KEY=sk-xxx
    python -m evals.runner --runs 3

    # 额外开启 LLM-as-judge 做语义复核 + 质量打分(更准,成本更高)
    python -m evals.runner --runs 3 --judge

    # 指定报告输出路径
    python -m evals.runner --runs 3 --report evals/reports/baseline.md

流程:加载数据集 → 对每条用例跑 review() → 用 matcher 判定 →
      重复 N 次 → metrics 聚合 → report 渲染 Markdown。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from codeguard_agent.config import Settings
from codeguard_agent.llm.client import build_llm
from codeguard_agent.pipeline.reviewer import review

from evals.dataset import load_cases
from evals.matcher import evaluate_case
from evals.metrics import aggregate
from evals.report import render_report
from evals.schema import MatchOutcome

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger("codeguard.evals")


def run_once(cases, llm, settings, judge_llm) -> list[MatchOutcome]:
    """跑一遍全数据集,返回每条用例的判定结果。"""
    outcomes: list[MatchOutcome] = []
    for case in cases:
        result = review(
            llm,
            case.diff,
            max_retries=settings.max_retries,
            structured_method=settings.structured_method,
        )
        outcome = evaluate_case(case, result.issues, judge_llm=judge_llm)
        logger.info(
            "[%s] TP=%d FP=%d FN=%d (报告 %d / 标答 %d)",
            case.id,
            outcome.true_positives,
            outcome.false_positives,
            outcome.false_negatives,
            outcome.reported_total,
            outcome.expected_total,
        )
        outcomes.append(outcome)
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codeguard-evals", description="Codeguard 审查质量评测")
    parser.add_argument("--runs", type=int, default=1, help="重复跑测次数(>1 才能统计方差)")
    parser.add_argument("--judge", action="store_true", help="开启 LLM-as-judge 语义复核+质量打分")
    parser.add_argument(
        "--report",
        default="evals/reports/baseline.md",
        help="Markdown 报告输出路径(相对 services/agent)",
    )
    parser.add_argument("--dataset", default="", help="自定义数据集目录(默认 evals/dataset)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    logger.info("provider=%s model=%s runs=%d judge=%s", settings.provider, settings.model, args.runs, args.judge)

    if settings.provider == "mock":
        logger.warning(
            "当前为 mock 模式:只验证评测链路是否打通,指标无业务含义。"
            "要量化真实效果请设 CODEGUARD_PROVIDER 与 CODEGUARD_API_KEY。"
        )

    cases = load_cases(Path(args.dataset) if args.dataset else None)
    logger.info("加载用例 %d 条", len(cases))

    llm = build_llm(settings)
    judge_llm = llm if args.judge else None
    if args.judge and llm is None:
        logger.warning("mock 模式下无法做 LLM-as-judge,已自动跳过 judge")
        judge_llm = None

    all_runs: list[list[MatchOutcome]] = []
    for i in range(args.runs):
        logger.info("===== 第 %d/%d 次跑测 =====", i + 1, args.runs)
        all_runs.append(run_once(cases, llm, settings, judge_llm))

    metrics = aggregate(all_runs)

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(metrics, settings, all_runs, cases), encoding="utf-8"
    )

    # 控制台速览
    print("\n" + "=" * 60)
    print("Codeguard 评测结果(baseline)")
    print("=" * 60)
    print(f"用例: {metrics.num_cases}(漏洞 {metrics.num_vuln_cases} / 干净 {metrics.num_clean_cases})  跑测: {metrics.runs} 次")
    print(f"Precision: {metrics.precision:.3f} (±{metrics.precision_std:.3f})")
    print(f"Recall:    {metrics.recall:.3f} (±{metrics.recall_std:.3f})")
    print(f"F1:        {metrics.f1:.3f}")
    print(f"误报率(每条干净 diff): {metrics.false_positives_on_clean:.3f}")
    print(f"定位准确率: {metrics.localization_accuracy:.3f}   级别准确率: {metrics.severity_accuracy:.3f}")
    if metrics.avg_judge_message_quality is not None:
        print(f"LLM-judge 描述质量: {metrics.avg_judge_message_quality:.2f}/5   建议质量: {metrics.avg_judge_suggestion_quality:.2f}/5")
    print(f"\n报告已写入: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
