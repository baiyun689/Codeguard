"""审查编排器入口。

构建并执行任务拆分、符号解析、受控调查、证据验证、裁决及结果合并流程，
对外返回 ReviewResult，并可额外记录 Trace 和评测元数据。
"""

from __future__ import annotations
import hashlib
import logging
from typing import Any
import uuid
from codeguard_agent.models.schemas import ReviewResult
from codeguard_agent.models.tasks import ReviewBudget
from codeguard_agent.observability.models import DegradationReport
from codeguard_agent.models.state import ReviewState
from codeguard_agent.pipeline.evidence.projection import graph_projection_focus
from codeguard_agent.pipeline.orchestration.graph import (
    DEFAULT_RECURSION_LIMIT,
    build_review_graph,
)

logger = logging.getLogger("codeguard")


def resolve_evidence_revision(
    evidence_revision: str, tool_client, diff_text: str
) -> str:
    """计算本次审查的有效证据 revision(证据账本内容寻址的锚)。

    优先级:显式传入 > 工具会话 revision > diff 内容摘要兜底。
    Artifact 与 Gateway session 身份由此保持一致。
    """
    if evidence_revision:
        return evidence_revision
    tool_revision = getattr(tool_client, "revision", "")
    if tool_revision:
        return tool_revision
    return "diff:" + hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


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
            from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore[import-not-found]
        except ImportError:
            logger.warning(
                "checkpoint 后端设为 sqlite 但 langgraph-checkpoint-sqlite 未安装;降级为不启用 checkpoint。安装: pip install langgraph-checkpoint-sqlite"
            )
            return None
        logger.info("checkpoint 后端:sqlite(%s)", db_path)
        return SqliteSaver.from_conn_string(db_path)
    logger.warning("未知的 checkpoint 后端 '%s',不启用 checkpoint", backend)
    return None


class PipelineOrchestrator:
    """封装 LangGraph 审查图的构建、执行与结果提取。"""

    def __init__(
        self,
        review_budget: ReviewBudget | None = None,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
        checkpoint_backend: str = "",
        checkpoint_db: str = "codeguard_checkpoints.db",
        discovery_mode: str = "controlled",
        controlled_max_path_depth: int = 3,
        controlled_execute_concurrency: int = 3,
        controlled_subtask_max_tool_calls: int = 10,
        controlled_subtask_max_rounds: int = 6,
        controlled_subtask_timeout_seconds: int = 120,
        controlled_task_max_tool_calls: int = 32,
        controlled_max_subtasks_per_task: int = 8,
    ) -> None:
        self._review_budget = (
            review_budget if review_budget is not None else ReviewBudget()
        )
        self._recursion_limit = recursion_limit
        self._checkpointer = _create_checkpointer(checkpoint_backend, checkpoint_db)
        self._discovery_mode = discovery_mode
        self._controlled_max_path_depth = controlled_max_path_depth
        self._controlled_execute_concurrency = controlled_execute_concurrency
        self._controlled_subtask_max_tool_calls = controlled_subtask_max_tool_calls
        self._controlled_subtask_max_rounds = controlled_subtask_max_rounds
        self._controlled_subtask_timeout_seconds = controlled_subtask_timeout_seconds
        self._controlled_task_max_tool_calls = controlled_task_max_tool_calls
        self._controlled_max_subtasks_per_task = controlled_max_subtasks_per_task

    def run(
        self,
        llm,
        diff_text: str,
        max_retries: int = 3,
        structured_method: str = "function_calling",
        fp_verify_llm=None,
        repo_path: str | None = None,
        tool_client=None,
        enabled_tools: list[str] | None = None,
        enabled_evidence_tools: list[str] | None = None,
        evidence_mode: str = "full",
        trace_enabled: bool = False,
        trace_dir: str = "trace",
        trace_max_llm_content: int = 0,
        trace_sink: list | None = None,
        metadata_sink: dict[str, Any] | None = None,
        thread_id: str | None = None,
        evidence_revision: str = "",
    ) -> ReviewResult:
        """执行审查管线并返回 ReviewResult。

        fp_verify_llm 指定裁决模型，为 None 时使用主模型。
        discovery_mode 选择受控调查或无工具直审。
        enabled_tools 控制审查工具白名单；evidence_mode 控制证据验证流程。
        trace_sink 和 metadata_sink 接收运行轨迹与评测元数据，不进入产品结果。
        thread_id 用于关联检查点。
        """
        if not diff_text.strip():
            return ReviewResult(summary="没有检测到代码变更,无需审查。")
        _run_id = thread_id or str(uuid.uuid4())
        effective_tool_client = (
            None if self._discovery_mode == "direct" else tool_client
        )
        graph = build_review_graph(
            checkpointer=self._checkpointer,
            llm=llm,
            fp_verify_llm=fp_verify_llm,
            tool_client=effective_tool_client,
            evidence_mode=evidence_mode,
            discovery_mode=self._discovery_mode,
            controlled_max_path_depth=self._controlled_max_path_depth,
            controlled_execute_concurrency=self._controlled_execute_concurrency,
            controlled_subtask_max_tool_calls=self._controlled_subtask_max_tool_calls,
            controlled_subtask_max_rounds=self._controlled_subtask_max_rounds,
            controlled_subtask_timeout_seconds=self._controlled_subtask_timeout_seconds,
            controlled_task_max_tool_calls=self._controlled_task_max_tool_calls,
            controlled_max_subtasks_per_task=self._controlled_max_subtasks_per_task,
        )
        initial: ReviewState = {
            "diff_text": diff_text,
            "evidence_revision": resolve_evidence_revision(
                evidence_revision, effective_tool_client, diff_text
            ),
            "enabled_tools": enabled_tools,
            "max_retries": max_retries,
            "structured_method": structured_method,
            "review_budget": self._review_budget,
            "controlled_max_path_depth": self._controlled_max_path_depth,
        }
        if enabled_evidence_tools is not None:
            initial["enabled_evidence_tools"] = enabled_evidence_tools
        invoke_config: dict = {"recursion_limit": self._recursion_limit}
        if self._checkpointer is not None:
            effective_thread_id = thread_id or str(uuid.uuid4())
            invoke_config["configurable"] = {"thread_id": effective_thread_id}
        if trace_enabled:
            from codeguard_agent.observability.collector import _TraceCollector
            from codeguard_agent.observability.dashboard import render_dashboard_file

            tracer = _TraceCollector(_run_id, max_llm_content=trace_max_llm_content)
            try:
                final_state = tracer.run_with_tracing(graph, initial, invoke_config)
            except Exception:
                logger.warning("追踪执行异常，降级为无追踪模式", exc_info=True)
                final_state = graph.invoke(initial, config=invoke_config)
            else:
                try:
                    report = tracer.finalize()
                    _inject_degradation(report, final_state)
                    from codeguard_agent.observability.artifacts import (
                        normalize_trace_report,
                    )

                    focus_by_task = {
                        task.id: graph_projection_focus(
                            task,
                            (final_state.get("task_symbol_contexts") or {}).get(
                                task.id
                            ),
                        )
                        for task in final_state.get("review_tasks") or []
                    }
                    normalize_trace_report(
                        report,
                        final_state.get("evidence_artifacts") or {},
                        focus_by_task=focus_by_task,
                    )
                    render_dashboard_file(report, trace_dir, _run_id)
                except Exception:
                    logger.warning("追踪报告生成失败", exc_info=True)
        else:
            final_state = graph.invoke(initial, config=invoke_config)
        if trace_sink is not None:
            trace_sink.extend(
                _artifact_tool_profile(final_state.get("evidence_artifacts") or {})
            )
        if metadata_sink is not None:
            stats = final_state.get("council_stats")
            if stats is None:
                metadata_sink["council"] = None
            elif hasattr(stats, "model_dump"):
                metadata_sink["council"] = stats.model_dump()
            else:
                metadata_sink["council"] = stats
            metadata_sink["symbol_resolution_diagnostics"] = dict(
                final_state.get("symbol_resolution_diagnostics") or {}
            )
        return ReviewResult(
            summary=final_state.get("summary", ""),
            issues=list(final_state.get("final_issues") or []),
        )


def _artifact_tool_profile(artifacts: dict) -> list:
    """从最终证据集合提取实际工具调用信息，供评测统计使用。

    仅统计首次执行的工具证据，复用记录、patch 和上下文不计为额外调用。
    """
    from codeguard_agent.models.evidence import EvidenceCaptureMode, EvidenceSourceKind
    from types import SimpleNamespace

    items: list[SimpleNamespace] = []
    seen: set[tuple[str, str]] = set()
    for artifact in artifacts.values():
        if (
            artifact.source_kind is not EvidenceSourceKind.TOOL_CALL
            or artifact.capture_mode is not EvidenceCaptureMode.EXECUTED
        ):
            continue
        key = (artifact.tool, _summarize_artifact_args(artifact.arguments))
        if key in seen:
            continue
        seen.add(key)
        items.append(
            SimpleNamespace(
                tool=artifact.tool,
                args=key[1],
                content=artifact.payload,
                status=artifact.availability.value,
            )
        )
    return items


def _summarize_artifact_args(arguments: dict[str, str]) -> str:
    import json

    return json.dumps(arguments, ensure_ascii=False, sort_keys=True)


def _inject_degradation(report: Any, final_state: dict) -> None:
    """从 final_state 的 council_trace 和 council_stats 提取降级数据注入 TraceReport。"""
    council_trace = final_state.get("council_trace") or []
    report.degradation = DegradationReport(
        direct_tier_tasks=sum((t.event == "tier_direct" for t in council_trace)),
        discoverer_failed=sum((t.event == "discover_failed" for t in council_trace)),
        task_review_failed=sum(
            (t.event == "task_review_failed" for t in council_trace)
        ),
        judge_synthesis_failed=sum(
            (
                t.event == "severity_resolved"
                and "severity_evidence_incomplete" in str(t.detail)
                for t in council_trace
            )
        )
        or (
            final_state.get("council_stats")
            and getattr(final_state["council_stats"], "judge_synthesis_failed_count", 0)
            or 0
        ),
    )
