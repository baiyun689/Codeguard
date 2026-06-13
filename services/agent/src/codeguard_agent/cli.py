"""命令行入口。

阶段 1 的最小可跑闭环:
    git diff → 一次 LLM 调用 → 结构化 issues → 终端打印

用法:
    python -m codeguard_agent review            # 审查当前仓库工作区改动
    python -m codeguard_agent review --repo /path/to/repo --base main
"""

from __future__ import annotations

import argparse
import logging
import sys

import os

from codeguard_agent.config import Settings
from codeguard_agent.git.diff_collector import collect_diff, parse_changed_files
from codeguard_agent.llm.client import build_llm
from codeguard_agent.models.schemas import ReviewResult, Severity
from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator
from codeguard_agent.pipeline.reviewer import review
from codeguard_agent.tools.tool_client import create_tool_session, destroy_tool_session

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s", stream=sys.stderr)

# 不同级别在终端用不同图标,便于人眼快速扫描
_SEVERITY_ICON = {
    Severity.CRITICAL: "🔴",
    Severity.WARNING: "🟡",
    Severity.INFO: "🔵",
}


def _print_result(result: ReviewResult) -> None:
    """把审查结果以易读的形式打印到终端。"""
    print("\n" + "=" * 60)
    print("Codeguard 审查报告")
    print("=" * 60)
    print(f"\n摘要:{result.summary}\n")

    if not result.issues:
        print("✅ 未发现问题。")
        return

    print(f"共发现 {len(result.issues)} 个问题:\n")
    for i, issue in enumerate(result.issues, 1):
        icon = _SEVERITY_ICON.get(issue.severity, "•")
        print(f"{icon} [{i}] {issue.severity.value} · {issue.type}")
        print(f"    位置:{issue.file}:{issue.line}")
        print(f"    问题:{issue.message}")
        if issue.suggestion:
            print(f"    建议:{issue.suggestion}")
        print(f"    置信度:{issue.confidence:.2f}\n")


def main(argv: list[str] | None = None) -> int:
    """CLI 主函数。返回进程退出码(0 成功)。"""
    parser = argparse.ArgumentParser(prog="codeguard", description="Codeguard - AI 代码审查")
    subparsers = parser.add_subparsers(dest="command", required=True)

    review_parser = subparsers.add_parser("review", help="审查代码变更")
    review_parser.add_argument("--repo", default=".", help="git 仓库路径(默认当前目录)")
    review_parser.add_argument("--base", default="HEAD", help="diff 对比基准(默认 HEAD)")
    review_parser.add_argument(
        "--mode",
        choices=["single", "pipeline"],
        default="single",
        help="审查方式:single=单次直接调用(阶段1 baseline);pipeline=多阶段管线(阶段2 起)。默认 single",
    )

    args = parser.parse_args(argv)

    if args.command == "review":
        settings = Settings.from_env()
        logger.info("provider=%s model=%s", settings.provider, settings.model)

        diff_text = collect_diff(args.repo, args.base)
        if not diff_text.strip():
            print("没有检测到代码变更,无需审查。")
            return 0

        llm = build_llm(settings)
        if args.mode == "pipeline":
            # 阶段2 起的多阶段管线。阶段1 默认管线只有 SecurityReviewerStage,
            # 结果应与 single 模式一致(验证管线骨架未改变审查结果)。
            logger.info("审查方式:pipeline(多阶段管线)")
            # 误报过滤第二段验证模型(开了才建,优先异源,见 ADR-005)。
            fp_verify_llm = None
            if settings.fp_llm_verify:
                fp_verify_llm = build_llm(Settings.judge_from_env(), temperature=0)

            # 阶段 3:配置了工具服务且为真实 LLM 时,为本次审查建工具会话,审查员走 ReAct;
            # 否则 tool_client 为 None,走无工具直连基准(见 design.md D1)。mock 模式不建会话。
            tool_client = None
            repo_abspath = os.path.abspath(args.repo)
            allowed_files = parse_changed_files(diff_text)
            if settings.tool_server_url and llm is not None:
                try:
                    tool_client = create_tool_session(
                        settings.tool_server_url, repo_abspath, allowed_files
                    )
                    logger.info(
                        "已创建工具会话(%s),审查员走 ReAct;允许文件 %d 个",
                        tool_client.session_id,
                        len(allowed_files),
                    )
                except Exception as exc:  # noqa: BLE001 工具服务不可用时降级为无工具,不中断审查
                    logger.warning("创建工具会话失败,降级为无工具直连: %s", exc)
                    tool_client = None

            try:
                result = PipelineOrchestrator(
                    fp_llm_verify=settings.fp_llm_verify,
                    enable_summary=settings.enable_summary,
                ).run(
                    llm,
                    diff_text,
                    max_retries=settings.max_retries,
                    structured_method=settings.structured_method,
                    fp_verify_llm=fp_verify_llm,
                    repo_path=repo_abspath,
                    allowed_files=allowed_files,
                    tool_client=tool_client,
                )
            finally:
                if tool_client is not None:
                    destroy_tool_session(tool_client)
        else:
            logger.info("审查方式:single(单次直接调用 · baseline)")
            result = review(
                llm,
                diff_text,
                max_retries=settings.max_retries,
                structured_method=settings.structured_method,
            )
        _print_result(result)

        # 退出码约定:发现 CRITICAL 问题时返回非 0,方便接入 CI 做门禁
        has_critical = any(i.severity == Severity.CRITICAL for i in result.issues)
        return 1 if has_critical else 0

    return 0


logger = logging.getLogger("codeguard")


if __name__ == "__main__":
    raise SystemExit(main())
