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

from codeguard_agent.config import Settings
from codeguard_agent.git.diff_collector import collect_diff
from codeguard_agent.llm.client import build_llm
from codeguard_agent.models.schemas import ReviewResult, Severity
from codeguard_agent.pipeline.reviewer import review

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

    args = parser.parse_args(argv)

    if args.command == "review":
        settings = Settings.from_env()
        logger.info("provider=%s model=%s", settings.provider, settings.model)

        diff_text = collect_diff(args.repo, args.base)
        if not diff_text.strip():
            print("没有检测到代码变更,无需审查。")
            return 0

        llm = build_llm(settings)
        result = review(llm, diff_text, max_retries=settings.max_retries)
        _print_result(result)

        # 退出码约定:发现 CRITICAL 问题时返回非 0,方便接入 CI 做门禁
        has_critical = any(i.severity == Severity.CRITICAL for i in result.issues)
        return 1 if has_critical else 0

    return 0


logger = logging.getLogger("codeguard")


if __name__ == "__main__":
    raise SystemExit(main())
