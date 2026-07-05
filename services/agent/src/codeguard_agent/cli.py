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
from typing import Any

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


def _hitl_supervisor_finish_dialog(payload: dict[str, Any]) -> dict[str, Any]:
    """HITL supervisor finish 交互式对话。返回 resume dict。"""
    print("\n" + "=" * 60)
    print("审查完成 — 等待确认")
    print("=" * 60)
    print(f"发现 {payload['issues_count']} 个问题。已派发: {payload['dispatched']}")
    print(f"supervisor 理由: {payload.get('reason', '无')}")
    print()
    print("[回车] 确认聚合  [list] 查看发现  [retry <审查员>] 追加派发  [help] 帮助")

    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消,进入聚合。")
            return {"action": "continue"}

        if not cmd:
            return {"action": "continue"}
        parts = cmd.split()
        verb = parts[0].lower()

        if verb == "list":
            issues = payload.get("issues") or []
            if not issues:
                print("  (暂无发现)")
            else:
                print(f"\n  ── 当前发现 ({len(issues)} 条) ──\n")
                for idx, iss in enumerate(issues, 1):
                    sev = iss.get("severity", "")
                    icon = _SEVERITY_ICON.get(Severity(sev), "•") if sev else "•"
                    fname = iss.get("file", "?")
                    line = iss.get("line", 0)
                    loc = f"{fname}:{line}" if line else fname
                    msg = (iss.get("message") or "")[:120]
                    print(f"  {icon} [{idx}] {iss.get('type', '?')}  {loc}")
                    print(f"      {msg}")
                    sug = iss.get("suggestion")
                    if sug:
                        print(f"      建议: {str(sug)[:100]}")
                print()
            continue
        elif verb == "retry" and len(parts) >= 2:
            reviewers = [n for n in parts[1:] if n in ("security", "logic", "quality")]
            if not reviewers:
                print("  可用审查员: security, logic, quality")
                continue
            print(f"  已追加派发: {reviewers}")
            return {"action": "retry", "reviewers": reviewers}
        elif verb == "focus" and len(parts) >= 3:
            reviewer = parts[1]
            if reviewer not in ("security", "logic", "quality"):
                print("  可用审查员: security, logic, quality")
                continue
            note = " ".join(parts[2:])
            print(f"  已聚焦 {reviewer}: {note}")
            return {"action": "retry", "reviewers": [reviewer], "focus_notes": {reviewer: note}}
        elif verb == "help":
            print("  [回车] 确认进入聚合")
            print("  list     查看当前发现列表")
            print("  retry <审查员> [审查员 ...]  追加派发(security/logic/quality)")
            print("  focus <审查员> <说明>  带聚焦指令重派")
            print("  help     打印本帮助")
        else:
            print(f"  未知命令: {cmd} (输入 help 查看帮助)")


def _hitl_reviewer_limit_dialog(payload: dict[str, Any]) -> dict[str, Any]:
    """HITL reviewer_hit_limit 交互式对话。返回 resume dict。"""
    reviewer = payload.get("reviewer", "?")
    gathered = payload.get("gathered_count", 0)
    print(f"\n{'=' * 60}")
    print(f"⚠ {reviewer} 审查员撞递归上限")
    print(f"{'=' * 60}")
    print(f"已收集 {gathered} 个文件上下文。")
    print()
    print("[回车] 以已有上下文收尾  [retry] 放宽步数重跑  [skip] 跳过")

    while True:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消,以已有上下文收尾。")
            return {"action": "continue"}

        if not cmd or cmd == "continue":
            return {"action": "continue"}
        elif cmd == "retry":
            return {"action": "retry"}
        elif cmd == "skip":
            return {"action": "skip"}
        else:
            print(f"  未知命令: {cmd} (回车=收尾 / retry=重跑 / skip=跳过)")


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
        "--thread-id",
        default=None,
        help="检查点线程标识(需配 CODEGUARD_CHECKPOINT_BACKEND)。"
             "相同 thread_id 重复调用可从上次中断点恢复继续执行。",
    )
    review_parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=False,
        help="非交互式模式:HITL interrupt 触发时打印状态并退出(退出码 2),"
             "不带此参数则进入终端交互式对话。",
    )
    review_parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="恢复模式:从上次 HITL interrupt 点继续执行。",
    )
    review_parser.add_argument(
        "--resume-action",
        default="continue",
        choices=["continue", "retry", "skip"],
        help="resume 时的动作(默认 continue)。",
    )
    review_parser.add_argument(
        "--resume-reviewers",
        default=None,
        help="retry 时追加的审查员列表,逗号分隔(如 security,logic)。",
    )
    review_parser.add_argument(
        "--resume-focus",
        default=None,
        help="retry 时的聚焦指令,格式: reviewer:说明(如 'security:重点审Payment.java')。",
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
        logger.info("审查方式:ADR-032 ReviewCouncil(摘要 → 上下文 → 多 Agent Council → SelfChecker)")
        # 误报过滤第二段验证模型(开了才建,优先异源,见 ADR-005)。
        fp_verify_llm = None
        if settings.fp_llm_verify:
            fp_verify_llm = build_llm(Settings.judge_from_env(), temperature=0)

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
            fp_llm_verify=settings.fp_llm_verify,
            enable_summary=settings.enable_summary,
            enable_supervisor=settings.enable_supervisor,
            max_review_rounds=settings.max_review_rounds,
            max_evidence_rounds=settings.max_evidence_rounds,
            checkpoint_backend=settings.checkpoint_backend,
            checkpoint_db=settings.checkpoint_db,
            enable_human_in_the_loop=settings.enable_human_in_the_loop,
            react_recursion_limit=settings.react_recursion_limit,
            orchestration_profile=settings.review_orchestration,
        )

        # thread_id:用户没传则自动生成,保证中断后能打印准确的恢复命令。
        effective_thread_id = args.thread_id or str(uuid.uuid4())

        # HITL resume:构造 resume dict 从命令行参数。
        resume: dict[str, Any] | None = None
        if args.resume:
            resume = {"action": args.resume_action}
            if args.resume_reviewers:
                resume["reviewers"] = [r.strip() for r in args.resume_reviewers.split(",")]
            if args.resume_focus:
                if ":" in args.resume_focus:
                    reviewer, note = args.resume_focus.split(":", 1)
                    resume["focus_notes"] = {reviewer.strip(): note.strip()}
                else:
                    resume["focus_notes"] = {"security": args.resume_focus.strip()}

        try:
            from langgraph.errors import GraphInterrupt

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
                resume=resume,
            )
        except GraphInterrupt as gi:
            payload = gi.args[0] if gi.args else {}
            ptype = payload.get("type", "") if isinstance(payload, dict) else ""

            # 非交互式:打印状态 + 退出码 2。
            if args.non_interactive:
                print(f"\n[审查暂停] HITL interrupt: {ptype}")
                print(f"payload: {payload}")
                print("恢复命令:")
                print(f"  python -m codeguard_agent review --repo {args.repo} "
                      f"--thread-id {effective_thread_id} "
                      f"--resume --resume-action continue")
                if tool_client is not None:
                    destroy_tool_session(tool_client)
                return 2

            # 交互式:进入对话循环。
            if ptype == "supervisor_finish":
                resume = _hitl_supervisor_finish_dialog(payload)
            elif ptype == "reviewer_hit_limit":
                resume = _hitl_reviewer_limit_dialog(payload)
            else:
                logger.warning("未知 interrupt 类型 '%s',默认 continue", ptype)
                resume = {"action": "continue"}

            # 用 resume 重新调用(同一 thread_id)。
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
                    resume=resume,
                )
            except GraphInterrupt:
                # 第二次 interrupt(如 retry 后又撞限):递归走交互式。
                logger.info("二次 interrupt,进入交互式确认...")
                if tool_client is not None:
                    destroy_tool_session(tool_client)
                return 2  # 简化处理:非交互式退出,下次可 resume
        finally:
            if tool_client is not None:
                destroy_tool_session(tool_client)

        _print_result(result)

        # 退出码约定:发现 CRITICAL 问题时返回非 0,方便接入 CI 做门禁
        has_critical = any(i.severity == Severity.CRITICAL for i in result.issues)
        return 1 if has_critical else 0

    return 0


logger = logging.getLogger("codeguard")


if __name__ == "__main__":
    raise SystemExit(main())
