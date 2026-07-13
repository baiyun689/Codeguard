"""评测跑批入口(CLI)。

用法:
    # 零成本验证评测骨架是否打通(不调真实 LLM)
    CODEGUARD_PROVIDER=mock python -m evals.runner

    # 调真实 LLM 跑 pipeline 评测,重复 3 次统计方差
    export CODEGUARD_API_KEY=sk-xxx
    python -m evals.runner --runs 3

    # 额外开启 LLM 裁判做案例级语义配对(更准,成本更高)。
    # 裁判默认沿用主模型;强烈建议另配一家"不同/更强"的模型当裁判,降低自我评判偏差:
    #   CODEGUARD_JUDGE_PROVIDER=claude CODEGUARD_JUDGE_MODEL=claude-sonnet-4-20250514 \
    #   CODEGUARD_JUDGE_API_KEY=sk-ant-... python -m evals.runner --runs 3 --judge
    python -m evals.runner --runs 3 --judge

    # 指定报告输出路径
    python -m evals.runner --runs 3 --report evals/reports/pipeline.md

流程:加载数据集 → 对每条用例跑管线 → 用 matcher 判定 →
      重复 N 次 → metrics 聚合 → report 渲染 Markdown。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import sys
from pathlib import Path
from typing import Any

from codeguard_agent.config import Settings
from codeguard_agent.git.diff_collector import parse_changed_files
from codeguard_agent.llm.client import build_llm
from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator
from codeguard_agent.tools.tool_client import create_tool_session, destroy_tool_session

from evals.archive import (
    archive_now_timestamp,
    build_archive_record,
    git_short_sha,
    load_archives,
    write_archive,
)
from evals.dataset import load_cases
from evals.matcher import evaluate_case
from evals.metrics import aggregate, aggregate_by_capability
from evals.profiles import case_repo_root, resolve_profile, tools_effective
from evals.report import render_history_views, render_report
from evals.schema import CouncilTraceStats, MatchOutcome
from evals.tool_usage import summarize_tool_usage

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger("codeguard.evals")


@dataclass(frozen=True)
class _RuntimeIdentity:
    """本次评测实际执行的模型身份，不复述未调用的配置值。"""

    provider: str
    model: str
    quality_metrics_meaningful: bool


def _runtime_identity(settings: Any, llm: Any) -> _RuntimeIdentity:
    """根据真实 LLM 实例生成报告/归档共用身份。"""
    if llm is None:
        label = "(mock-no-llm)" if settings.provider == "mock" else "(no-llm)"
        return _RuntimeIdentity(settings.provider, label, False)
    return _RuntimeIdentity(
        settings.provider,
        settings.model or "(provider-default)",
        True,
    )


def run_once(cases, review_fn, judge_llm) -> list[MatchOutcome]:
    """跑一遍全数据集,返回每条用例的判定结果。

    review_fn: 接收一条 EvalCase、返回 (ReviewResult, 工具上下文 trace, 元数据) 三元组的可调用对象。
        由 main() 按 profile 注入(single=baseline 单次调用 / pipeline=多阶段管线,
        工具会话按用例自带的 repo_path 建立)。trace 为本次审查员获取的工具上下文列表
        (无工具档为空),据此算工具使用画像。
    """
    outcomes: list[MatchOutcome] = []
    for case in cases:
        result, trace, metadata = review_fn(case)
        outcome = evaluate_case(case, result.issues, judge_llm=judge_llm)
        # 工具使用画像:有工具活动才挂(空 trace → None,避免无工具档报告/归档出现满是 '—' 的行)。
        if trace:
            outcome.tool_usage = summarize_tool_usage(trace)
        council_meta = (metadata or {}).get("council")
        if council_meta:
            outcome.council_trace = CouncilTraceStats(
                **council_meta,
                trace_events=int((metadata or {}).get("council_trace_events", 0)),
            )
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
        "--profile",
        default="",
        help="被测目标 profile(见 evals/profiles.yaml,如 pipeline-file)。"
        "指定后覆盖 --tools;不指定则用 --tools 合成 ad-hoc profile(管线 + 工具开/关)",
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
        default="evals/reports/pipeline.md",
        help="Markdown 报告输出路径(相对 services/agent)",
    )
    parser.add_argument("--dataset", default="", help="自定义数据集目录(默认 evals/dataset)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()

    # 解析被测目标 profile:指定 --profile 从 profiles.yaml 取;否则用 --tools 合成
    # ad-hoc profile(管线 + 工具开/关)。profile 决定启用哪些工具、可选模型覆盖。
    try:
        profile = resolve_profile(args.profile or None, tools=args.tools)
    except KeyError as exc:
        logger.error("%s", exc)
        return 2
    if profile.model:
        settings.model = profile.model  # profile 显式覆盖模型

    llm = build_llm(settings)
    runtime_identity = _runtime_identity(settings, llm)
    logger.info(
        "profile=%s mode=%s orchestration=%s tools=%s fp_verify=%s provider=%s model=%s runs=%d judge=%s",
        profile.name, profile.mode, profile.orchestration, profile.tools or "(无)", profile.fp_verify,
        runtime_identity.provider, runtime_identity.model, args.runs, args.judge,
    )

    if not runtime_identity.quality_metrics_meaningful:
        logger.warning(
            "当前未调用审查 LLM:只验证评测链路是否打通,指标无业务含义。"
            "要量化真实效果请设 CODEGUARD_PROVIDER 与 CODEGUARD_API_KEY。"
        )

    cases = load_cases(Path(args.dataset) if args.dataset else None)
    logger.info("加载用例 %d 条", len(cases))

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

    # 误报过滤第二段的验证模型:由 profile.fp_verify 驱动(evals 的被测目标全由 profile 描述,
    # 不再依赖全局 CODEGUARD_FP_LLM_VERIFY,见 design.md D1/D2)。优先异源(复用独立模型配置,
    # 避免审查器核查自己的结论 → 自我确认偏差,见 ADR-005)。temperature=0 锁确定性。
    fp_verify_llm = None
    if profile.fp_verify:
        verify_settings = Settings.judge_from_env()
        fp_verify_llm = build_llm(verify_settings, temperature=0)
        same = (verify_settings.provider == settings.provider
                and verify_settings.model == settings.model)
        logger.info(
            "误报过滤验证模型 provider=%s model=%s%s",
            verify_settings.provider, verify_settings.model,
            "  ⚠️ 与审查器同源,存在自我确认偏差(建议配 CODEGUARD_JUDGE_* 异源)" if same else "",
        )

    # 工具实际启用 = profile 想开工具 + 真实 LLM + 配了工具服务地址,三者齐备。
    # 任一不满足则自动降级为无工具(沿用现有 harness 行为),并如实记录"工具实际启用状态"。
    use_tools = tools_effective(profile, has_llm=llm is not None, tool_server_url=settings.tool_server_url)
    if profile.wants_tools and not use_tools:
        logger.warning(
            "profile %s 想开工具但本次降级为无工具:需真实 LLM + CODEGUARD_TOOL_SERVER_URL",
            profile.name,
        )
    if use_tools:
        logger.info("工具开档:%s。仅对有真实 repo 根的用例建会话(见 case_repo_root)", profile.tools)

    # 注入审查函数:统一走多阶段管线。review_fn 接收整条 case,
    # 以便工具会话用该用例自带的 repo_path。
    # enable_supervisor 由 profile 控制(默认关):受控对照档保持确定性全派、不引入路由
    # 非确定性;仅 pipeline-supervisor 观测档置开(见 design D9)。
    orchestrator = PipelineOrchestrator()
    def review_fn(case):
        diff = case.diff
        # 工具仅在该用例有**真实** repo 根时启用(repo-backed 快照,或用户显式 --repo-base)。
        # 合成用例无快照时返回 None → 本条按无工具直连跑,避免工具扫到 cwd(agent 源码树/评测
        # 夹具)返回无关内容、诱使审查员无界乱逛撞 recursion_limit(ADR-016 根因)。
        repo_root = case_repo_root(case.repo_path, args.repo_base) if use_tools else None
        tool_client = None
        if repo_root:
            try:
                tool_client = create_tool_session(
                    settings.tool_server_url, repo_root, parse_changed_files(diff)
                )
            except Exception as exc:  # noqa: BLE001 工具服务不可用则降级无工具,不中断评测
                logger.warning("[%s] 创建工具会话失败,本条按无工具跑: %s", case.id, exc)
        trace: list = []  # 工具调用侧信道:管线把 gathered_context 追加进来供算画像。
        metadata: dict = {}
        try:
            result = orchestrator.run(
                llm, diff,
                max_retries=settings.max_retries,
                structured_method=settings.structured_method,
                fp_verify_llm=fp_verify_llm,
                repo_path=repo_root if tool_client is not None else None,
                allowed_files=parse_changed_files(diff) if tool_client is not None else None,
                tool_client=tool_client,
                # profile.tools 即工具白名单:让"开哪些工具"成为对照的唯一变量。
                enabled_tools=profile.tools if tool_client is not None else None,
                trace_sink=trace,
                metadata_sink=metadata,
            )
            return result, trace, metadata
        finally:
            if tool_client is not None:
                destroy_tool_session(tool_client)

    all_runs: list[list[MatchOutcome]] = []
    for i in range(args.runs):
        logger.info("===== 第 %d/%d 次跑测 =====", i + 1, args.runs)
        all_runs.append(run_once(cases, review_fn, judge_llm))

    metrics = aggregate(all_runs)

    # 按能力切片聚合(归因维度:在"需要某能力"的用例上各 profile 的表现)。
    case_caps = {c.id: c.capability for c in cases}
    by_capability = aggregate_by_capability(all_runs, case_caps)

    # 历史归档:每次运行落一份带时间/gitsha/profile 的结构化结果,追加累积,作趋势底座。
    record = build_archive_record(
        profile_name=profile.name,
        profile_mode=profile.mode,
        profile_tools=profile.tools,
        profile_orchestration=profile.orchestration,
        tools_enabled=use_tools,
        fp_verify=profile.fp_verify,
        provider=runtime_identity.provider,
        model=runtime_identity.model,
        runs=args.runs,
        metrics=metrics,
        by_capability=by_capability,
        last_run=all_runs[-1],
        git_sha=git_short_sha(),
        timestamp=archive_now_timestamp(),
    )
    archive_path = write_archive(record)
    logger.info("归档已写入: %s", archive_path)

    # 报告 = 本次详细报告 + 从历史归档(含本次)渲染的趋势/对照/能力切片三视图。
    history = load_archives()
    report_body = (
        render_report(
            metrics,
            settings,
            all_runs,
            cases,
            model_label=runtime_identity.model,
            quality_metrics_meaningful=runtime_identity.quality_metrics_meaningful,
        )
        + "\n"
        + render_history_views(history)
    )
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_body, encoding="utf-8")

    # 控制台速览
    print("\n" + "=" * 60)
    print("Codeguard 评测结果(pipeline)")
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
