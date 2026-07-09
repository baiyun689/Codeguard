"""通过 LangGraph ``astream_events`` 采集完整审查执行轨迹。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any

from codeguard_agent.observability.models import (
    NodeStats,
    TokenUsage,
    TraceEvent,
    TraceReport,
    TraceSummary,
)
from codeguard_agent.observability.serialization import (
    serialize_llm_response,
    serialize_messages,
    serialize_trace_value,
)

logger = logging.getLogger("codeguard.observability")

_NODE_PHASE_MAP: dict[str, str] = {
    "summary": "outer_graph",
    "context_provider": "outer_graph",
    "discover_threat_model": "reviewer_subgraph",
    "discover_behavior": "reviewer_subgraph",
    "discover_maintainability": "reviewer_subgraph",
    "council_coordinator": "outer_graph",
    "evidence_agent": "evidence",
    "council_judge": "judge",
    "prepare": "reviewer_subgraph",
    "review": "reviewer_subgraph",
    "collect": "reviewer_subgraph",
}


def _phase_for(node_name: str) -> str:
    return _NODE_PHASE_MAP.get(node_name, "outer_graph")


def _summarize_value(value: Any, max_len: int = 200) -> str:
    """保留旧测试和摘要调用使用的一行值摘要。"""
    if value is None:
        return "None"
    if isinstance(value, dict):
        keys = list(value.keys())
        return "{" + ", ".join(f"{key}=..." for key in keys[:8]) + "}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    text = str(value)
    return text[:max_len] + "..." if len(text) > max_len else text


def _serialize_messages(messages_input: Any) -> list[dict[str, Any]]:
    """向后兼容旧私有 helper；新代码统一走无损序列化模块。"""
    return serialize_messages(messages_input)


@dataclass
class _NodeRun:
    run_id: str
    node_name: str
    parent_run_id: str
    node_path: str
    depth: int
    start_ms: float
    end_ms: float | None = None


class _TraceCollector:
    """订阅事件流，并按原生 run 血缘聚合为 ``TraceReport``。"""

    def __init__(
        self,
        diff_text: str,
        run_id: str,
        max_llm_content: int = 0,
    ) -> None:
        self._events: list[TraceEvent] = []
        self._seq = 0
        self._start = time.time()
        self._run_id = run_id
        self._diff_size = len(diff_text)
        self._max_llm_content = max(0, max_llm_content)
        self._node_runs: dict[str, _NodeRun] = {}
        self._llm_counts: dict[str, int] = {}
        self._tool_counts: dict[str, int] = {}
        self._tokens_by_run: dict[str, TokenUsage] = {}
        self._tokens_by_path: dict[str, TokenUsage] = {}
        self._root_graph_run_id = ""

    def run_with_tracing(
        self,
        graph: Any,
        initial_state: dict,
        config: dict,
    ) -> dict:
        """同步执行异步事件流并返回图最终 State。"""
        return asyncio.run(
            self._collect_and_return(graph, initial_state, config)
        )

    def finalize(self) -> TraceReport:
        """按节点实例聚合时间线、调用次数和 token。"""
        node_timeline: list[NodeStats] = []
        for node_run in sorted(
            self._node_runs.values(),
            key=lambda item: item.start_ms,
        ):
            end_ms = (
                node_run.end_ms
                if node_run.end_ms is not None
                else node_run.start_ms
            )
            tokens = self._tokens_by_run.get(
                node_run.run_id,
                TokenUsage(
                    node_name=node_run.node_path or node_run.node_name,
                ),
            )
            node_timeline.append(NodeStats(
                node_name=node_run.node_name,
                start_ms=node_run.start_ms,
                end_ms=end_ms,
                duration_ms=end_ms - node_run.start_ms,
                llm_calls=self._llm_counts.get(node_run.run_id, 0),
                tool_calls=self._tool_counts.get(node_run.run_id, 0),
                tokens=tokens,
                run_id=node_run.run_id,
                parent_run_id=node_run.parent_run_id,
                node_path=node_run.node_path,
                depth=node_run.depth,
                invocation_id=node_run.run_id,
            ))

        event_counts: dict[str, int] = {}
        for event in self._events:
            event_counts[event.event_type] = (
                event_counts.get(event.event_type, 0) + 1
            )

        total_tokens = TokenUsage(node_name="total")
        for usage in self._tokens_by_path.values():
            total_tokens.input_tokens += usage.input_tokens
            total_tokens.output_tokens += usage.output_tokens
            total_tokens.total_tokens += usage.total_tokens

        return TraceReport(
            run_id=self._run_id,
            timestamp=time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(self._start),
            ),
            diff_size=self._diff_size,
            events=self._events,
            summary=TraceSummary(
                total_duration_ms=(time.time() - self._start) * 1000,
                total_tokens=total_tokens,
                tokens_by_node=self._tokens_by_path,
                event_counts=event_counts,
                node_timeline=node_timeline,
            ),
        )

    async def _collect_and_return(
        self,
        graph: Any,
        initial_state: dict,
        config: dict,
    ) -> dict:
        """消费一次事件流，并用顶层图 run 精确捕获最终 State。"""
        final_state: dict | None = None
        async for event in graph.astream_events(
            initial_state,
            config=config,
            version="v2",
        ):
            if (
                event.get("event") == "on_chain_start"
                and event.get("name") == "LangGraph"
                and not (event.get("parent_ids") or [])
            ):
                self._root_graph_run_id = _event_id(event.get("run_id"))

            try:
                self._handle_event(event)
            except Exception:  # noqa: BLE001
                logger.debug("处理追踪事件失败", exc_info=True)

            if (
                event.get("event") == "on_chain_end"
                and _event_id(event.get("run_id"))
                == self._root_graph_run_id
            ):
                output = (event.get("data") or {}).get("output")
                if isinstance(output, dict):
                    final_state = output

        if final_state is None:
            raise RuntimeError("追踪事件流缺少顶层图最终输出")
        return final_state

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("event", "")
        if event_type == "on_chain_start" and self._is_real_node_event(event):
            self._on_node_start(event)
        elif (
            event_type == "on_chain_end"
            and self._is_real_node_event(event)
        ):
            self._on_node_end(event)
        elif event_type == "on_chat_model_start":
            self._on_llm_start(event)
        elif event_type == "on_chat_model_end":
            self._on_llm_end(event)
        elif event_type == "on_tool_start":
            self._on_tool_start(event)
        elif event_type == "on_tool_end":
            self._on_tool_end(event)

    @staticmethod
    def _is_real_node_event(event: dict[str, Any]) -> bool:
        metadata = event.get("metadata") or {}
        node_name = metadata.get("langgraph_node", "")
        return bool(node_name and event.get("name") == node_name)

    def _on_node_start(self, event: dict[str, Any]) -> None:
        metadata = event.get("metadata") or {}
        data = event.get("data") or {}
        node_name = str(metadata.get("langgraph_node") or event.get("name"))
        run_id = _event_id(event.get("run_id"))
        parent_ids = _parent_ids(event)
        parent = self._nearest_node_run(parent_ids)
        parent_run_id = parent.run_id if parent is not None else ""
        node_path = (
            f"{parent.node_path}/{node_name}"
            if parent is not None
            else node_name
        )
        depth = parent.depth + 1 if parent is not None else 0
        node_run = _NodeRun(
            run_id=run_id,
            node_name=node_name,
            parent_run_id=parent_run_id,
            node_path=node_path,
            depth=depth,
            start_ms=self._elapsed_ms(),
        )
        self._node_runs[run_id] = node_run

        input_value = serialize_trace_value(data.get("input"))
        input_keys = (
            list(input_value.keys())
            if isinstance(input_value, dict)
            else []
        )
        self._add_event(
            event_type="node_start",
            node_name=node_name,
            node_path=node_path,
            phase=self._phase_for_path(node_path, node_name),
            depth=depth,
            summary=f"输入: {', '.join(input_keys[:8])}",
            detail={
                "input": input_value,
                "metadata": serialize_trace_value(metadata),
            },
            raw_event=event,
            invocation_id=run_id,
        )

    def _on_node_end(self, event: dict[str, Any]) -> None:
        metadata = event.get("metadata") or {}
        data = event.get("data") or {}
        node_name = str(metadata.get("langgraph_node") or event.get("name"))
        run_id = _event_id(event.get("run_id"))
        node_run = self._node_runs.get(run_id)
        if node_run is None:
            parent = self._nearest_node_run(_parent_ids(event))
            node_run = _NodeRun(
                run_id=run_id,
                node_name=node_name,
                parent_run_id=parent.run_id if parent is not None else "",
                node_path=(
                    f"{parent.node_path}/{node_name}"
                    if parent is not None
                    else node_name
                ),
                depth=parent.depth + 1 if parent is not None else 0,
                start_ms=self._elapsed_ms(),
            )
            self._node_runs[run_id] = node_run
        node_run.end_ms = self._elapsed_ms()

        input_value = serialize_trace_value(data.get("input"))
        output_value = serialize_trace_value(data.get("output"))
        output_keys = (
            list(output_value.keys())
            if isinstance(output_value, dict)
            else []
        )
        self._add_event(
            event_type="node_end",
            node_name=node_name,
            node_path=node_run.node_path,
            phase=self._phase_for_path(node_run.node_path, node_name),
            depth=node_run.depth,
            summary=f"输出: {', '.join(output_keys[:8])}",
            detail={
                "input": input_value,
                "output": output_value,
                "metadata": serialize_trace_value(metadata),
            },
            raw_event=event,
            invocation_id=run_id,
        )

        route = (
            output_value.get("council_route", "")
            if isinstance(output_value, dict)
            else ""
        )
        if route:
            self._add_event(
                event_type="route_decision",
                node_name=node_name,
                node_path=node_run.node_path,
                phase=self._phase_for_path(node_run.node_path, node_name),
                depth=node_run.depth,
                summary=f"路由 => {route}",
                detail={"route": route},
                raw_event=event,
                invocation_id=run_id,
            )

    def _on_llm_start(self, event: dict[str, Any]) -> None:
        metadata = event.get("metadata") or {}
        data = event.get("data") or {}
        owner = self._owner_for(event)
        owner_id = owner.run_id if owner is not None else ""
        node_name = owner.node_name if owner is not None else "unknown"
        node_path = owner.node_path if owner is not None else "unknown"
        self._llm_counts[owner_id] = self._llm_counts.get(owner_id, 0) + 1
        call_number = self._llm_counts[owner_id]
        model_name = str(
            metadata.get("ls_model_name") or event.get("name") or ""
        )
        self._add_event(
            event_type="llm_start",
            node_name=node_name,
            node_path=node_path,
            phase=self._phase_for_path(node_path, node_name),
            depth=(owner.depth + 1 if owner is not None else 0),
            summary=f"LLM #{call_number} ({model_name})",
            detail={
                "model": model_name,
                "messages": serialize_messages(
                    data.get("input"),
                    max_content_length=self._max_llm_content,
                ),
                "metadata": serialize_trace_value(metadata),
            },
            raw_event=event,
            invocation_id=owner_id,
        )

    def _on_llm_end(self, event: dict[str, Any]) -> None:
        metadata = event.get("metadata") or {}
        data = event.get("data") or {}
        output = data.get("output")
        owner = self._owner_for(event)
        owner_id = owner.run_id if owner is not None else ""
        node_name = owner.node_name if owner is not None else "unknown"
        node_path = owner.node_path if owner is not None else "unknown"
        model_name = str(
            metadata.get("ls_model_name") or event.get("name") or ""
        )
        usage = _token_usage_from(
            output,
            model_name=model_name,
            node_name=node_path,
        )
        if usage is not None:
            self._accumulate_tokens(owner_id, node_path, usage)
        total = usage.total_tokens if usage is not None else "?"
        self._add_event(
            event_type="llm_end",
            node_name=node_name,
            node_path=node_path,
            phase=self._phase_for_path(node_path, node_name),
            depth=(owner.depth + 1 if owner is not None else 0),
            summary=f"完成 ({total} tokens)",
            detail={
                "model": model_name,
                "response": serialize_llm_response(
                    output,
                    max_content_length=self._max_llm_content,
                ),
                "metadata": serialize_trace_value(metadata),
            },
            raw_event=event,
            invocation_id=owner_id,
            tokens=usage,
        )

    def _on_tool_start(self, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        metadata = event.get("metadata") or {}
        owner = self._owner_for(event)
        owner_id = owner.run_id if owner is not None else ""
        node_name = owner.node_name if owner is not None else "unknown"
        node_path = owner.node_path if owner is not None else "unknown"
        self._tool_counts[owner_id] = (
            self._tool_counts.get(owner_id, 0) + 1
        )
        tool_name = str(event.get("name") or "")
        self._add_event(
            event_type="tool_start",
            node_name=node_name,
            node_path=node_path,
            phase=self._phase_for_path(node_path, node_name),
            depth=(owner.depth + 1 if owner is not None else 0),
            summary=f"Tool: {tool_name}",
            detail={
                "tool_name": tool_name,
                "input": serialize_trace_value(data.get("input")),
                "metadata": serialize_trace_value(metadata),
            },
            raw_event=event,
            invocation_id=owner_id,
        )

    def _on_tool_end(self, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        metadata = event.get("metadata") or {}
        owner = self._owner_for(event)
        owner_id = owner.run_id if owner is not None else ""
        node_name = owner.node_name if owner is not None else "unknown"
        node_path = owner.node_path if owner is not None else "unknown"
        tool_name = str(event.get("name") or "")
        output = serialize_trace_value(data.get("output"))
        self._add_event(
            event_type="tool_end",
            node_name=node_name,
            node_path=node_path,
            phase=self._phase_for_path(node_path, node_name),
            depth=(owner.depth + 1 if owner is not None else 0),
            summary=f"返回: {_summary_length(output)}",
            detail={
                "tool_name": tool_name,
                "output": output,
                "metadata": serialize_trace_value(metadata),
            },
            raw_event=event,
            invocation_id=owner_id,
        )

    def _nearest_node_run(
        self,
        parent_ids: list[str],
    ) -> _NodeRun | None:
        for parent_id in reversed(parent_ids):
            node_run = self._node_runs.get(parent_id)
            if node_run is not None:
                return node_run
        return None

    def _owner_for(self, event: dict[str, Any]) -> _NodeRun | None:
        return self._nearest_node_run(_parent_ids(event))

    @staticmethod
    def _phase_for_path(node_path: str, node_name: str) -> str:
        root = node_path.split("/", 1)[0]
        if root.startswith("discover_"):
            return "reviewer_subgraph"
        return _phase_for(node_name)

    def _accumulate_tokens(
        self,
        owner_id: str,
        node_path: str,
        usage: TokenUsage,
    ) -> None:
        for bucket, key in (
            (self._tokens_by_run, owner_id),
            (self._tokens_by_path, node_path),
        ):
            if key not in bucket:
                bucket[key] = TokenUsage(node_name=node_path)
            target = bucket[key]
            target.input_tokens += usage.input_tokens
            target.output_tokens += usage.output_tokens
            target.total_tokens += usage.total_tokens
            target.model = usage.model

    def _add_event(
        self,
        *,
        event_type: str,
        node_name: str,
        node_path: str,
        phase: str,
        depth: int,
        summary: str,
        detail: dict[str, Any],
        raw_event: dict[str, Any],
        invocation_id: str,
        tokens: TokenUsage | None = None,
    ) -> None:
        self._seq += 1
        parent_ids = _parent_ids(raw_event)
        self._events.append(TraceEvent(
            sequence=self._seq,
            timestamp_ms=self._elapsed_ms(),
            event_type=event_type,
            node_name=node_name,
            phase=phase,
            depth=depth,
            summary=summary,
            detail=detail,
            tokens=tokens,
            run_id=_event_id(raw_event.get("run_id")),
            parent_ids=parent_ids,
            parent_run_id=parent_ids[-1] if parent_ids else "",
            node_path=node_path,
            invocation_id=invocation_id,
        ))

    def _elapsed_ms(self) -> float:
        return (time.time() - self._start) * 1000


def _event_id(value: Any) -> str:
    return "" if value is None else str(value)


def _parent_ids(event: dict[str, Any]) -> list[str]:
    return [_event_id(item) for item in (event.get("parent_ids") or [])]


def _token_usage_from(
    output: Any,
    *,
    model_name: str,
    node_name: str,
) -> TokenUsage | None:
    usage = getattr(output, "usage_metadata", None)
    if usage is None and isinstance(output, dict):
        usage = output.get("usage_metadata")
    if not isinstance(usage, dict):
        return None
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
        model=model_name,
        node_name=node_name,
    )


def _summary_length(value: Any) -> str:
    if isinstance(value, str):
        return f"{len(value)} 字符"
    if isinstance(value, (list, dict)):
        return f"{len(value)} 项"
    return type(value).__name__
