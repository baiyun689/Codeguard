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

    `run()` 内部建图 + invoke(见 graph.py)。
    """

    def __init__(
        self,
        enable_summary: bool = True,
        max_evidence_rounds: int = DEFAULT_MAX_EVIDENCE_ROUNDS,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
        checkpoint_backend: str = "",
        checkpoint_db: str = "codeguard_checkpoints.db",
        react_recursion_limit: int = 48,
    ) -> None:
        self._enable_summary = enable_summary
        self._max_evidence_rounds = max_evidence_rounds
        self._recursion_limit = recursion_limit
        self._checkpointer = _create_checkpointer(checkpoint_backend, checkpoint_db)
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
        trace_enabled: bool = False,
        trace_dir: str = "trace",
        trace_sink: list | None = None,
        metadata_sink: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> ReviewResult:
        """跑完整条管线,返回结构化的 ReviewResult。

        fp_verify_llm:裁决模型(异源千问 temperature=0);None 时回退到主 llm。
        tool_client 非 None 时发现者走 ReAct(可调工具),否则走直连基准。
        enabled_tools:暴露给审查员的工具白名单(评测 profile 控制);None=全开(CLI 默认)。
        trace_sink / metadata_sink:可选 eval 侧信道,刻意不进 ReviewResult(守 ADR-001)。
        thread_id:可选的检查点线程标识,用于中断恢复。
        """
        if not diff_text.strip():
            return ReviewResult(summary="没有检测到代码变更,无需审查。")

        import uuid
        _run_id = thread_id or str(uuid.uuid4())

        graph = build_review_graph(
            enable_summary=self._enable_summary,
            checkpointer=self._checkpointer,
            llm=llm,
            fp_verify_llm=fp_verify_llm,
            tool_client=tool_client,
        )
        initial: ReviewState = {
            "diff_text": diff_text,
            "enabled_tools": enabled_tools,
            "max_retries": max_retries,
            "structured_method": structured_method,
            "react_recursion_limit": self._react_recursion_limit,
            "max_evidence_rounds": self._max_evidence_rounds,
            # fan-in / 控制 初值
            "gathered_context": [],
            "review_summaries": [],
            "candidate_issues": [],
            "evidence_requests": [],
            "evidence_notes": [],
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
            effective_thread_id = thread_id or str(uuid.uuid4())
            invoke_config["configurable"] = {"thread_id": effective_thread_id}
        if trace_enabled:
            from codeguard_agent.observability.collector import _TraceCollector
            from codeguard_agent.observability.dashboard import render_dashboard_file

            tracer = _TraceCollector(diff_text, _run_id)
            try:
                final_state = tracer.run_with_tracing(graph, initial, invoke_config)
            except Exception:
                logger.warning("追踪执行异常，降级为无追踪模式", exc_info=True)
                final_state = graph.invoke(initial, config=invoke_config)
            else:
                try:
                    report = tracer.finalize()
                    render_dashboard_file(report, trace_dir, _run_id)
                except Exception:
                    logger.warning("追踪报告生成失败", exc_info=True)
        else:
            final_state = graph.invoke(initial, config=invoke_config)

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
