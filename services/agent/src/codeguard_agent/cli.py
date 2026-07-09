"""命令行入口。

可跑闭环:
    git diff → 多阶段管线(摘要 → 并行审查 → 聚合 → 误报过滤)→ 结构化 issues → 终端打印

用法:
    python -m codeguard_agent review            # 审查当前仓库工作区改动
    python -m codeguard_agent review --repo /path/to/repo --base main
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid

import os

from codeguard_agent.config import Settings
from codeguard_agent.git.diff_collector import collect_diff, parse_changed_files
from codeguard_agent.llm.client import build_llm
from codeguard_agent.models.schemas import ReviewResult, Severity
from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator
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
        "--format", default="text", choices=["text", "json"],
        help="输出格式:text=人类可读(默认),json=结构化 JSON(供 CI 消费)",
    )
    review_parser.add_argument(
        "--thread-id",
        default=None,
        help="检查点线程标识(需配 CODEGUARD_CHECKPOINT_BACKEND)。",
    )
    review_parser.add_argument(
        "--trace", action=argparse.BooleanOptionalAction, default=True,
        help="开启审查追踪，产出可视化 Dashboard HTML 文件（默认开），--no-trace 关闭",
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
        logger.info("审查方式:ADR-032 ReviewCouncil(summary → context → discover×3 → coordinator → evidence/council_judge)")
        # 裁决模型(优先异源+低温,供 council_judge 去重与终审使用;误报验证也复用)。
        # 只要配置了 CODEGUARD_JUDGE_* 就创建,不再仅依赖 fp_llm_verify 开关。
        fp_verify_llm = None
        try:
            judge_settings = Settings.judge_from_env()
            fp_verify_llm = build_llm(judge_settings, temperature=0)
            logger.info("裁决模型:%s/%s", judge_settings.provider, judge_settings.model)
        except Exception as exc:
            logger.debug("无法创建裁决模型,回退到主 LLM: %s", exc)

        # 配置了工具服务且为真实 LLM 时,为本次审查建工具会话,审查员走 ReAct;
        # 否则 tool_client 为 None,走无工具直连(见 design.md D1)。mock 模式不建会话。
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

        orch = PipelineOrchestrator(
            enable_summary=settings.enable_summary,
            max_evidence_rounds=settings.max_evidence_rounds,
            checkpoint_backend=settings.checkpoint_backend,
            checkpoint_db=settings.checkpoint_db,
            react_recursion_limit=settings.react_recursion_limit,
        )

        effective_thread_id = args.thread_id or str(uuid.uuid4())

        try:
            result = orch.run(
                llm,
                diff_text,
                max_retries=settings.max_retries,
                structured_method=settings.structured_method,
                fp_verify_llm=fp_verify_llm,
                repo_path=repo_abspath,
                allowed_files=allowed_files,
                tool_client=tool_client,
                thread_id=effective_thread_id,
                trace_enabled=args.trace,
                trace_dir=settings.trace_dir,
                trace_max_llm_content=settings.trace_max_llm_content,
            )
        finally:
            if tool_client is not None:
                destroy_tool_session(tool_client)

        if args.format == "json":
            print(result.model_dump_json(indent=2))
        else:
            _print_result(result)

        # 退出码约定:发现 CRITICAL 问题时返回非 0,方便接入 CI 做门禁
        has_critical = any(i.severity == Severity.CRITICAL for i in result.issues)
        return 1 if has_critical else 0

    return 0


logger = logging.getLogger("codeguard")


if __name__ == "__main__":
    raise SystemExit(main())
