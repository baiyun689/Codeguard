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
import hashlib
import json
import logging
import sys
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from codeguard_agent.config import Settings
from codeguard_agent.git.diff_collector import parse_changed_files
from codeguard_agent.llm.client import build_llm
from codeguard_agent.models.tasks import ReviewBudget
from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator
from codeguard_agent.pipeline.engines import DirectEngine
from codeguard_agent.models.schemas import ReviewResult
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
from evals.schema import CouncilTraceStats, EvalCase, MatchOutcome
from evals.tool_usage import summarize_tool_usage

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger("codeguard.evals")


def case_evidence_revision(case: EvalCase) -> str:
    """repo-backed 用例的证据 revision:head_revision + diff 内容摘要。

    证据账本内容寻址锚点(源文档 §5.1):Artifact 与 Gateway session 身份一致。
    合成用例无 head_revision 时返回空串,由编排器按 diff 摘要兜底。
    """
    if case.provenance and case.provenance.head_revision:
        digest = hashlib.sha256(case.diff.encode("utf-8")).hexdigest()
        return f"{case.provenance.head_revision}:{digest}"
    return ""


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


def run_once(
    cases,
    review_fn,
    judge_llm,
    *,
    existing_outcomes: list[MatchOutcome] | None = None,
    on_checkpoint: Callable[[list[MatchOutcome]], None] | None = None,
) -> list[MatchOutcome]:
    """跑一遍全数据集,返回每条用例的判定结果。

    review_fn: 接收一条 EvalCase、返回 (ReviewResult, 工具上下文 trace, 元数据) 三元组的可调用对象。
        由 main() 按 profile 注入(single=baseline 单次调用 / pipeline=多阶段管线,
        工具会话按用例自带的 repo_path 建立)。trace 为本次审查员获取的工具上下文列表
        (无工具档为空),据此算工具使用画像。
    """
    outcomes_by_id = {
        outcome.case_id: outcome for outcome in (existing_outcomes or [])
    }
    for case in cases:
        if case.id in outcomes_by_id:
            logger.info("[%s] 从 checkpoint 恢复，跳过模型调用", case.id)
            continue
        started = perf_counter()
        result, trace, metadata = review_fn(case)
        outcome = evaluate_case(case, result.issues, judge_llm=judge_llm)
        outcome.total_duration_ms = float(
            (metadata or {}).get(
                "total_duration_ms",
                (perf_counter() - started) * 1000,
            )
        )
        # 工具使用画像:有工具活动才挂(空 trace → None,避免无工具档报告/归档出现满是 '—' 的行)。
        if trace:
            outcome.tool_usage = summarize_tool_usage(trace)
        council_meta = (metadata or {}).get("council")
        if council_meta:
            outcome.council_trace = CouncilTraceStats(**council_meta)
        logger.info(
            "[%s] TP=%d FP=%d FN=%d (报告 %d / 标答 %d)",
            case.id,
            outcome.true_positives,
            outcome.false_positives,
            outcome.false_negatives,
            outcome.reported_total,
            outcome.expected_total,
        )
        outcomes_by_id[case.id] = outcome
        if on_checkpoint is not None:
            on_checkpoint(
                [
                    outcomes_by_id[item.id]
                    for item in cases
                    if item.id in outcomes_by_id
                ]
            )
    return [outcomes_by_id[case.id] for case in cases]


# 沙箱护栏拒绝类失败:agent 传参误用(如把目录当文件读、读白名单外路径),
# 工具服务本身正常。这类失败不构成"工具侧不可用",严格评测不中断,记警告。
_AGENT_MISUSE_MARKERS = (
    "文件类型不可读", "仅限源码文件", "不在白名单", "不允许访问",
    "文件不存在", "not allowed", "not in whitelist", "sandbox",
)

# 基础设施降级类失败:图谱/上下文/超时/网络,评测失真,严格评测必须中断。
_INFRA_FAILURE_MARKERS = (
    "graph_unavailable", "graph_coverage_", "Timeout", "timed out",
    "ConnectionError", "invalid_graph_response", "unavailable",
)


def _strict_tool_failures(trace: list, metadata: dict) -> tuple[list[str], list[str]]:
    """返回会使严格代码图谱 profile 失真的降级事实。

    - failures:基础设施级降级(图谱/上下文/超时/网络/节点失败),评测失真,必须中断;
    - warnings:agent 误用类失败(沙箱护栏正常拒绝),工具侧正常,不中断,只记录。
    """
    failures: list[str] = []
    warnings: list[str] = []
    for name, detail in (metadata.get("context_diagnostics") or {}).items():
        if detail and any(marker in str(detail) for marker in _INFRA_FAILURE_MARKERS):
            failures.append(f"{name}:{detail}")
        elif detail:
            warnings.append(f"{name}:{detail}")
    for item in trace:
        if getattr(item, "status", "") != "failed":
            continue
        content = str(getattr(item, "content", "") or "")
        tool = getattr(item, "tool", "unknown")
        if any(marker in content for marker in _AGENT_MISUSE_MARKERS):
            warnings.append(f"tool_rejected:{tool}")
        else:
            failures.append(f"tool_failed:{tool}")
    council = metadata.get("council") or {}
    for key in (
        "react_degraded_recursion_count",
        "react_degraded_empty_count",
        "discoverer_failed_count",
        "task_review_failed_count",
    ):
        count = int(council.get(key, 0))
        if count:
            failures.append(f"{key}={count}")
    return failures, warnings


def _checkpoint_identity(
    *,
    profile: Any,
    provider: str,
    model: str,
    judge_provider: str,
    judge_model: str,
    cases: list,
    code_digest: str,
    git_sha: str,
) -> dict:
    case_digest = hashlib.sha256(
        "\n".join(
            f"{case.id}:{hashlib.sha256(case.diff.encode('utf-8')).hexdigest()}"
            for case in cases
        ).encode("utf-8")
    ).hexdigest()
    return {
        "profile": profile.name,
        "execution": getattr(profile, "execution", "pipeline"),
        "tools": profile.tools,
        "evidence_tools": getattr(profile, "evidence_tools", None),
        "provider": provider,
        "model": model,
        "judge_provider": judge_provider,
        "judge_model": judge_model,
        "case_digest": case_digest,
        "code_digest": code_digest,
        "git_sha": git_sha,
        "orchestration": getattr(profile, "orchestration", "adr-032"),
        "fp_verify": getattr(profile, "fp_verify", False),
        "strict_tools": getattr(profile, "strict_tools", False),
    }


def _dataset_digest(cases: list) -> str:
    payload = [
        case.model_dump(mode="json")
        if hasattr(case, "model_dump")
        else vars(case)
        for case in cases
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _code_digest() -> str:
    """指纹化会影响评测行为的本地代码、prompt 与 profile 配置。"""
    agent_root = Path(__file__).resolve().parents[1]
    candidates = list((agent_root / "src" / "codeguard_agent").rglob("*.py"))
    candidates += list(
        (agent_root / "src" / "codeguard_agent" / "prompts").glob("*.txt")
    )
    candidates += list((agent_root / "evals").glob("*.py"))
    candidates.append(agent_root / "evals" / "profiles.yaml")
    gateway_root = agent_root.parent / "gateway"
    candidates += list(gateway_root.rglob("*.java"))
    candidates += list(gateway_root.rglob("pom.xml"))
    candidates += list(gateway_root.rglob("*.yaml"))
    candidates += list(gateway_root.rglob("*.yml"))
    digest_root = agent_root.parent.resolve()
    digest = hashlib.sha256()
    for path in sorted({path.resolve() for path in candidates if path.is_file()}):
        digest.update(path.relative_to(digest_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _load_checkpoint(path: Path, identity: dict) -> list[list[MatchOutcome]]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("identity") != identity:
        raise ValueError(f"checkpoint 与本次评测身份不一致:{path}")
    return [
        [MatchOutcome.model_validate(outcome) for outcome in run]
        for run in raw.get("runs", [])
    ]


def _save_checkpoint(
    path: Path,
    identity: dict,
    runs: list[list[MatchOutcome]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": 1,
                "identity": identity,
                "runs": [
                    [outcome.model_dump(mode="json") for outcome in run]
                    for run in runs
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


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
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="只运行指定 case id；可重复传入，适合真实模型冒烟",
    )
    parser.add_argument(
        "--archive-dir",
        default="",
        help="结构化归档目录；统一跑批用它隔离本次四个 profile",
    )
    parser.add_argument(
        "--checkpoint",
        default="",
        help="逐案例断点文件；身份一致时自动跳过已完成模型调用",
    )
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
    if args.case:
        requested = set(args.case)
        known = {case.id for case in cases}
        unknown = requested - known
        if unknown:
            logger.error("数据集中不存在用例:%s", ", ".join(sorted(unknown)))
            return 2
        cases = [case for case in cases if case.id in requested]
    logger.info("加载用例 %d 条", len(cases))

    # 裁判模型:独立配置(CODEGUARD_JUDGE_*),temperature=0 锁确定性,尽量与审查器异源(见 ADR-005)。
    judge_llm = None
    judge_provider = ""
    judge_model = ""
    judge_same_source: bool | None = None
    if args.judge:
        judge_settings = Settings.judge_from_env()
        judge_llm = build_llm(judge_settings, temperature=0)
        judge_provider = judge_settings.provider
        judge_model = judge_settings.model or "(provider-default)"
        if judge_llm is None:
            logger.warning("裁判为 mock,无法做 LLM 主判,已自动跳过(只用规则尺)")
        else:
            same = (judge_settings.provider == settings.provider
                    and judge_settings.model == settings.model)
            judge_same_source = same
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
        if profile.strict_tools:
            logger.error("严格评测 profile 禁止降级，终止本次运行")
            return 2
    if use_tools:
        logger.info("工具开档:%s。仅对有真实 repo 根的用例建会话(见 case_repo_root)", profile.tools)

    # 注入审查函数:统一走多阶段管线。review_fn 接收整条 case,
    # 以便工具会话用该用例自带的 repo_path。
    # enable_supervisor 由 profile 控制(默认关):受控对照档保持确定性全派、不引入路由
    # 非确定性;仅 pipeline-supervisor 观测档置开(见 design D9)。
    # CODEGUARD_FORCE_REACT 与 CLI 同语义:诊断/验证时强制 ReAct(仍受预算约束)。
    orchestrator = PipelineOrchestrator(
        review_budget=ReviewBudget(force_react=settings.force_react)
    )
    direct_prompt_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "codeguard_agent"
        / "prompts"
        / "eval-direct-reviewer.txt"
    )
    direct_system_prompt = direct_prompt_path.read_text(encoding="utf-8")

    def review_fn(case):
        review_started = perf_counter()
        diff = case.diff
        if profile.execution == "direct":
            if llm is None:
                return ReviewResult(summary=""), [], {
                    "total_duration_ms": (perf_counter() - review_started) * 1000
                }
            direct = DirectEngine().review(
                llm,
                system_prompt=direct_system_prompt,
                user_prompt=f"请审查以下代码变更：\n\n{diff}",
                reviewer_name="eval-direct-diff",
                max_retries=settings.max_retries,
                structured_method=settings.structured_method,
            )
            return direct.result, [], {
                "total_duration_ms": (perf_counter() - review_started) * 1000
            }
        # 工具仅在该用例有**真实** repo 根时启用(repo-backed 快照,或用户显式 --repo-base)。
        # 合成用例无快照时返回 None → 本条按无工具直连跑,避免工具扫到 cwd(agent 源码树/评测
        # 夹具)返回无关内容、诱使审查员无界乱逛撞 recursion_limit(ADR-016 根因)。
        repo_root = case_repo_root(case.repo_path, args.repo_base) if use_tools else None
        if profile.strict_tools and not repo_root:
            raise RuntimeError(f"[{case.id}] 严格工具 profile 要求 repo-backed 快照")
        case_revision = case_evidence_revision(case)
        tool_client = None
        if repo_root:
            try:
                tool_client = create_tool_session(
                    settings.tool_server_url,
                    repo_root,
                    parse_changed_files(diff),
                    timeout=settings.graph_build_timeout_seconds + 15,
                    revision=case_revision,
                )
            except Exception as exc:  # noqa: BLE001 工具服务不可用则降级无工具,不中断评测
                if profile.strict_tools:
                    raise RuntimeError(f"[{case.id}] 创建严格工具会话失败") from exc
                logger.warning("[%s] 创建工具会话失败,本条按无工具跑: %s", case.id, exc)
        trace: list = []  # 工具调用侧信道:编排器从证据 Artifact 派生工具画像追加进来。
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
                enabled_evidence_tools=(
                    profile.evidence_tools
                    if tool_client is not None
                    else None
                ),
                allow_direct_fallback=not profile.strict_tools,
                evidence_mode=profile.evidence_mode,
                triage_enabled=profile.triage != "off",
                evidence_revision=case_revision,
                trace_enabled=settings.trace_enabled,
                trace_dir=settings.trace_dir,
                trace_max_llm_content=settings.trace_max_llm_content,
                trace_sink=trace,
                metadata_sink=metadata,
            )
            if profile.strict_tools:
                failures, warnings = _strict_tool_failures(trace, metadata)
                if warnings:
                    logger.warning("[%s] 工具误用警告(不中断): %s", case.id, "; ".join(warnings))
                if failures:
                    raise RuntimeError(
                        f"[{case.id}] 严格工具 profile 检测到降级:"
                        + "; ".join(failures)
                    )
            return result, trace, metadata
        finally:
            if tool_client is not None:
                destroy_tool_session(tool_client)
            metadata["total_duration_ms"] = (perf_counter() - review_started) * 1000

    checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
    dataset_digest = _dataset_digest(cases)
    code_digest = _code_digest()
    current_git_sha = git_short_sha()
    try:
        if checkpoint_path:
            checkpoint_identity = _checkpoint_identity(
                profile=profile,
                provider=runtime_identity.provider,
                model=runtime_identity.model,
                judge_provider=judge_provider,
                judge_model=judge_model,
                cases=cases,
                code_digest=code_digest,
                git_sha=current_git_sha,
            )
            checkpoint_runs = _load_checkpoint(
                checkpoint_path, checkpoint_identity
            )
        else:
            checkpoint_identity = {}
            checkpoint_runs = []
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("无法恢复 checkpoint:%s", exc)
        return 2
    while len(checkpoint_runs) < args.runs:
        checkpoint_runs.append([])

    all_runs: list[list[MatchOutcome]] = []
    for i in range(args.runs):
        logger.info("===== 第 %d/%d 次跑测 =====", i + 1, args.runs)
        def save_progress(rows: list[MatchOutcome], run_index: int = i) -> None:
            checkpoint_runs[run_index] = rows
            if checkpoint_path:
                _save_checkpoint(
                    checkpoint_path,
                    checkpoint_identity,
                    checkpoint_runs,
                )

        completed = (
            run_once(
                cases,
                review_fn,
                judge_llm,
                existing_outcomes=checkpoint_runs[i],
                on_checkpoint=save_progress,
            )
            if checkpoint_path
            else run_once(cases, review_fn, judge_llm)
        )
        checkpoint_runs[i] = completed
        all_runs.append(completed)

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
        all_runs=all_runs,
        git_sha=current_git_sha,
        timestamp=archive_now_timestamp(),
        judge_provider=judge_provider,
        judge_model=judge_model,
        judge_same_source=judge_same_source,
        dataset_digest=dataset_digest,
        code_digest=code_digest,
    )
    archive_path = (
        write_archive(record, runs_dir=Path(args.archive_dir))
        if args.archive_dir
        else write_archive(record)
    )
    logger.info("归档已写入: %s", archive_path)

    # 报告 = 本次详细报告 + 从历史归档(含本次)渲染的趋势/对照/能力切片三视图。
    history = (
        load_archives(Path(args.archive_dir))
        if args.archive_dir
        else load_archives()
    )
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
