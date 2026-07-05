"""审查编排器门面。

ADR-032 默认路径内部执行 ReviewCouncil 图:
summary? → context_provider → review_council → council_judge → END。
对外仍返回稳定的 `ReviewResult`。
"""

from __future__ import annotations

import logging
from typing import Any
import uuid

from codeguard_agent.models.schemas import ReviewResult
from codeguard_agent.pipeline.graph import (
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_EVIDENCE_ROUNDS,
    DEFAULT_RECURSION_LIMIT,
    ReviewState,
    build_review_graph,
)

logger = logging.getLogger("codeguard")


def _create_checkpointer(backend: str, db_path: str):
    """按配置创建 LangGraph checkpointer。

    backend 取值:
        "memory" — 内存(MemorySaver),进程内有效,零依赖
        "sqlite" — 本地 SQLite 文件(SqliteSaver),需安装 langgraph-checkpoint-sqlite 包
        "" 或其他 — 不启用 checkpoint,返回 None
    """
    if not backend:
        return None
    if backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        logger.info("checkpoint 后端:memory(内存)")
        return MemorySaver()
    if backend == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import (  # type: ignore[import-not-found]
                SqliteSaver,
            )
        except ImportError:
            logger.warning(
                "checkpoint 后端设为 sqlite 但 langgraph-checkpoint-sqlite 未安装;"
                "降级为不启用 checkpoint。安装: pip install langgraph-checkpoint-sqlite"
            )
            return None
        logger.info("checkpoint 后端:sqlite(%s)", db_path)
        return SqliteSaver.from_conn_string(db_path)
    logger.warning("未知的 checkpoint 后端 '%s',不启用 checkpoint", backend)
    return None


class PipelineOrchestrator:
    """审查编排器(内部为 LangGraph 状态图,门面不变)。

    `run()` 内部建图 + invoke(见 graph.py);构造参数控制拓扑与调度策略。

    参数:
        fp_llm_verify:误报过滤是否启用第二段 LLM 复核(默认关)。
        enable_summary:是否启用前置摘要/分派节点(默认开)。
        enable_supervisor:旧参数,ADR-032 默认路径忽略它;旧实现仅保存在 legacy 目录。
        max_review_rounds:旧参数,保留签名兼容;ADR-032 默认路径不再使用 supervisor 轮次。
        max_evidence_rounds:ReviewCouncil 证据补充轮次上限,第一版默认 1。
        recursion_limit:图总步数硬上限(兜底护栏)。
        checkpoint_backend:checkpoint 后端("memory"/"sqlite"/""),默认空=不启用。
        checkpoint_db:SqliteSaver 数据库文件路径(仅 checkpoint_backend="sqlite" 生效)。
        enable_human_in_the_loop:是否在关键决策点暂停等待人工确认(默认关,向后兼容)。
    """

    def __init__(
        self,
        fp_llm_verify: bool = False,
        enable_summary: bool = True,
        enable_supervisor: bool = False,
        max_review_rounds: int = DEFAULT_MAX_ROUNDS,
        max_evidence_rounds: int = DEFAULT_MAX_EVIDENCE_ROUNDS,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
        checkpoint_backend: str = "",
        checkpoint_db: str = "codeguard_checkpoints.db",
        enable_human_in_the_loop: bool = False,
        react_recursion_limit: int = 24,
        orchestration_profile: str = "adr-032",
    ) -> None:
        self._fp_llm_verify = fp_llm_verify
        self._enable_summary = enable_summary
        self._enable_supervisor = enable_supervisor
        self._max_review_rounds = max_review_rounds
        self._max_evidence_rounds = max_evidence_rounds
        self._recursion_limit = recursion_limit
        self._checkpointer = _create_checkpointer(checkpoint_backend, checkpoint_db)
        self._hitl_enabled = False
        self._orchestration_profile = orchestration_profile or "adr-032"
        if self._orchestration_profile != "adr-032":
            logger.warning(
                "未知编排 profile '%s',ADR-032 已是唯一运行路径;继续使用 adr-032。",
                self._orchestration_profile,
            )
            self._orchestration_profile = "adr-032"
        if enable_supervisor:
            logger.warning(
                "CODEGUARD_ENABLE_SUPERVISOR/enable_supervisor 已退役;ADR-032 默认路径不运行旧 Supervisor。"
            )
        if enable_human_in_the_loop:
            logger.warning(
                "ADR-032 第一版暂不迁移 HITL interrupt;本次运行将忽略 CODEGUARD_ENABLE_HITL。"
            )
        self._react_recursion_limit = react_recursion_limit

    def run(
        self,
        llm,
        diff_text: str,
        max_retries: int = 3,
        structured_method: str = "function_calling",
        fp_verify_llm=None,
        repo_path: str | None = None,
        allowed_files: list[str] | None = None,
        tool_client=None,
        enabled_tools: list[str] | None = None,
        trace_sink: list | None = None,
        metadata_sink: dict[str, Any] | None = None,
        thread_id: str | None = None,
        resume: dict[str, Any] | None = None,
    ) -> ReviewResult:
        """跑完整条管线,返回结构化的 ReviewResult。

        fp_verify_llm:误报过滤第二段的验证模型(建议异源);None 时回退到 llm。
        repo_path / allowed_files / tool_client:阶段 3 工具调用上下文;
            tool_client 非 None 时审查员走 ReAct(可调工具),否则走直连基准(见 design.md D1)。
        enabled_tools:暴露给审查员的工具白名单(评测 profile 控制);None=全开(CLI 默认)。
        trace_sink:可选的工具调用侧信道——传入一个列表时,管线结束后把本次审查员获取的
            工具上下文(gathered_context)追加进去,供评测做"工具使用画像"。这是**只读侧信道**,
            刻意不进 ReviewResult(产品输出不掺工具痕迹,守 ADR-001)。
        metadata_sink:可选 eval 元数据侧信道,写入 council 统计和 trace 计数。
        thread_id:可选的检查点线程标识。非空且 checkpointer 已配置时,用相同 thread_id
            重复调用可从上次中断点恢复继续执行;为 None 则一次性执行(当前行为,向后兼容)。
        resume:可选的 HITL resume 指令。传入时表示恢复调用,以 Command(resume=resume)
            从上次 interrupt 点继续执行。
        """
        # 空 diff 直接短路(图无需启动)。
        if not diff_text.strip():
            return ReviewResult(summary="没有检测到代码变更,无需审查。")

        graph = build_review_graph(
            enable_summary=self._enable_summary,
            checkpointer=self._checkpointer,
            llm=llm,
            fp_verify_llm=fp_verify_llm,
            tool_client=tool_client,
        )
        initial: ReviewState = {
            # 静态输入(llm/fp_verify_llm/tool_client 不可序列化,由闭包传入,不进 state)
            "diff_text": diff_text,
            "enabled_tools": enabled_tools,
            "max_retries": max_retries,
            "structured_method": structured_method,
            "enable_supervisor": self._enable_supervisor,
            "enable_hitl": self._hitl_enabled,
            "react_recursion_limit": self._react_recursion_limit,
            "max_review_rounds": self._max_review_rounds,
            "max_evidence_rounds": self._max_evidence_rounds,
            "fp_llm_verify": self._fp_llm_verify,
            # fan-in / 控制 初值
            "issues": [],
            "gathered_context": [],
            "review_summaries": [],
            "dispatched": set(),
            "iteration": 0,
            "candidate_issues": [],
            "evidence_requests": [],
            "evidence_notes": [],
            "challenges": [],
            "council_verdicts": [],
            "council_trace": [],
            "evidence_round": 0,
            "judge_pass": 0,
            "truncated_candidates": 0,
            "truncated_evidence_requests": 0,
            "final_issues": [],
        }
        invoke_config: dict = {"recursion_limit": self._recursion_limit}
        if self._checkpointer is not None:
            # LangGraph 在配置了 checkpointer 时强制要求 thread_id(或 checkpoint_ns/checkpoint_id)。
            # 调用方没传时自动生成 UUID,保证图正常执行且仍享受中途故障恢复能力(只是外部无法
            # 主动 resume——需要主动 resume 的场景由调用方显式传 thread_id 覆盖)。
            effective_thread_id = thread_id or str(uuid.uuid4())
            invoke_config["configurable"] = {"thread_id": effective_thread_id}
        if resume is not None:
            logger.warning("ADR-032 默认路径暂无 HITL resume 点,忽略 resume 参数。")
        final_state = graph.invoke(initial, config=invoke_config)

        # LangGraph 1.x:interrupt() 不抛 GraphInterrupt,而是在返回的 state 中放 __interrupt__ 键。
        # 此处手动检测并向上抛,让 CLI 层能进入交互式对话。(见 ADR-027 HITL)
        interrupt_data = final_state.get("__interrupt__")
        if interrupt_data and self._hitl_enabled:
            from langgraph.errors import GraphInterrupt
            raise GraphInterrupt(interrupt_data[0].value)

        # 侧信道:把工具上下文交给评测层(不进 ReviewResult,守 ADR-001)。
        if trace_sink is not None:
            trace_sink.extend(final_state.get("gathered_context") or [])
        if metadata_sink is not None:
            stats = final_state.get("council_stats")
            metadata_sink["council"] = (
                stats.model_dump() if hasattr(stats, "model_dump") else stats
            )
            metadata_sink["council_trace_events"] = len(final_state.get("council_trace") or [])

        return ReviewResult(
            summary=final_state.get("summary", ""),
            issues=list(final_state.get("final_issues") or []),
        )
