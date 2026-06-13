"""评测跑批入口(CLI)。

用法:
    # 零成本验证评测骨架是否打通(不调真实 LLM)
    CODEGUARD_PROVIDER=mock python -m evals.runner

    # 调真实 LLM 跑 baseline,重复 3 次统计方差
    export CODEGUARD_API_KEY=sk-xxx
    python -m evals.runner --runs 3

    # 额外开启 LLM 裁判做案例级语义配对(更准,成本更高)。
    # 裁判默认沿用主模型;强烈建议另配一家"不同/更强"的模型当裁判,降低自我评判偏差:
    #   CODEGUARD_JUDGE_PROVIDER=claude CODEGUARD_JUDGE_MODEL=claude-sonnet-4-20250514 \
    #   CODEGUARD_JUDGE_API_KEY=sk-ant-... python -m evals.runner --runs 3 --judge
    python -m evals.runner --runs 3 --judge

    # 指定报告输出路径
    python -m evals.runner --runs 3 --report evals/reports/baseline.md

流程:加载数据集 → 对每条用例跑 review() → 用 matcher 判定 →
      重复 N 次 → metrics 聚合 → report 渲染 Markdown。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from codeguard_agent.config import Settings
from codeguard_agent.git.diff_collector import parse_changed_files
from codeguard_agent.llm.client import build_llm
from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator
from codeguard_agent.pipeline.reviewer import review
from codeguard_agent.tools.tool_client import create_tool_session, destroy_tool_session

from evals.dataset import load_cases
from evals.matcher import evaluate_case
from evals.metrics import aggregate
from evals.report import render_report
from evals.schema import MatchOutcome

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger("codeguard.evals")


def run_once(cases, review_fn, judge_llm) -> list[MatchOutcome]:
    """跑一遍全数据集,返回每条用例的判定结果。

    review_fn: 接收一段 diff 文本、返回 ReviewResult 的可调用对象。
        由 main() 按 --mode 注入(single=baseline 单次调用 / pipeline=多阶段管线)。
    """
    outcomes: list[MatchOutcome] = []
    for case in cases:
        result = review_fn(case.diff)
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
    parser.add_argument(
        "--mode",
        choices=["single", "pipeline"],
        default="single",
        help="审查方式:single=单次安全审查(阶段1 baseline);pipeline=并行多领域审查(阶段2)。默认 single",
    )
    parser.add_argument("--judge", action="store_true", help="开启 LLM 裁判做案例级语义配对(主判);规则尺仍并行作交叉校验")
    parser.add_argument(
        "--tools",
        action="store_true",
        help="工具开档:pipeline 审查员走 ReAct,可调 Java 工具服务(需配 CODEGUARD_TOOL_SERVER_URL)。"
        "用于做'工具开 vs 关'两档对照(仅此一个变量不同)。",
    )
    parser.add_argument(
        "--repo-base",
        default="",
        help="工具开档下,工具会话的 repo 根路径。注意:当前数据集是合成 diff、磁盘上无对应文件,"
        "get_file_content 会返回'文件不存在'——真要量化工具增益需用 repo-backed 用例(见 README)。",
    )
    parser.add_argument(
        "--report",
        default="evals/reports/baseline.md",
        help="Markdown 报告输出路径(相对 services/agent)",
    )
    parser.add_argument("--dataset", default="", help="自定义数据集目录(默认 evals/dataset)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    logger.info(
        "provider=%s model=%s mode=%s runs=%d judge=%s",
        settings.provider, settings.model, args.mode, args.runs, args.judge,
    )

    if settings.provider == "mock":
        logger.warning(
            "当前为 mock 模式:只验证评测链路是否打通,指标无业务含义。"
            "要量化真实效果请设 CODEGUARD_PROVIDER 与 CODEGUARD_API_KEY。"
        )

    cases = load_cases(Path(args.dataset) if args.dataset else None)
    logger.info("加载用例 %d 条", len(cases))

    llm = build_llm(settings)

    # 裁判模型:独立配置(CODEGUARD_JUDGE_*),temperature=0 锁确定性,尽量与审查器异源(见 ADR-005)。
    judge_llm = None
    if args.judge:
        judge_settings = Settings.judge_from_env()
        judge_llm = build_llm(judge_settings, temperature=0)
        if judge_llm is None:
            logger.warning("裁判为 mock,无法做 LLM 主判,已自动跳过(只用规则尺)")
        else:
            same = (judge_settings.provider == settings.provider
                    and judge_settings.model == settings.model)
            logger.info(
                "裁判 provider=%s model=%s%s",
                judge_settings.provider, judge_settings.model,
                "  ⚠️ 与审查器同源,存在自我评判偏差(建议另配 CODEGUARD_JUDGE_*)" if same else "",
            )

    # 误报过滤第二段的验证模型:开了 fp_llm_verify 才建,优先异源(复用独立模型配置,
    # 避免审查器核查自己的结论 → 自我确认偏差,见 ADR-005)。temperature=0 锁确定性。
    fp_verify_llm = None
    if settings.fp_llm_verify:
        verify_settings = Settings.judge_from_env()
        fp_verify_llm = build_llm(verify_settings, temperature=0)
        same = (verify_settings.provider == settings.provider
                and verify_settings.model == settings.model)
        logger.info(
            "误报过滤验证模型 provider=%s model=%s%s",
            verify_settings.provider, verify_settings.model,
            "  ⚠️ 与审查器同源,存在自我确认偏差(建议配 CODEGUARD_JUDGE_* 异源)" if same else "",
        )

    # 工具开档:仅 pipeline 生效,需配置工具服务地址且为真实 LLM。
    use_tools = args.tools and args.mode == "pipeline" and llm is not None and bool(settings.tool_server_url)
    if args.tools and not use_tools:
        logger.warning(
            "--tools 未生效:需 --mode pipeline + 真实 LLM + CODEGUARD_TOOL_SERVER_URL 三者齐备,本次按无工具跑"
        )
    if use_tools:
        repo_base = os.path.abspath(args.repo_base or ".")
        logger.warning(
            "工具开档:repo 根=%s。当前合成数据集磁盘上无对应文件,get_file_content 多半返回'文件不存在';"
            "真要量化工具增益需 repo-backed 用例(见 evals/README.md)。",
            repo_base,
        )

    # 按 --mode 注入审查函数:single=baseline 单次调用 / pipeline=多阶段管线。
    if args.mode == "pipeline":
        orchestrator = PipelineOrchestrator(fp_llm_verify=settings.fp_llm_verify)
        def review_fn(diff: str):
            tool_client = None
            if use_tools:
                try:
                    tool_client = create_tool_session(
                        settings.tool_server_url, repo_base, parse_changed_files(diff)
                    )
                except Exception as exc:  # noqa: BLE001 工具服务不可用则降级无工具,不中断评测
                    logger.warning("创建工具会话失败,本条按无工具跑: %s", exc)
            try:
                return orchestrator.run(
                    llm, diff,
                    max_retries=settings.max_retries,
                    structured_method=settings.structured_method,
                    fp_verify_llm=fp_verify_llm,
                    repo_path=repo_base if use_tools else None,
                    allowed_files=parse_changed_files(diff) if use_tools else None,
                    tool_client=tool_client,
                )
            finally:
                if tool_client is not None:
                    destroy_tool_session(tool_client)
    else:
        def review_fn(diff: str):
            return review(
                llm, diff,
                max_retries=settings.max_retries,
                structured_method=settings.structured_method,
            )

    all_runs: list[list[MatchOutcome]] = []
    for i in range(args.runs):
        logger.info("===== 第 %d/%d 次跑测 =====", i + 1, args.runs)
        all_runs.append(run_once(cases, review_fn, judge_llm))

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
