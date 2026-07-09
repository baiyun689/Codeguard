"""追踪采集器：通过 LangGraph astream_events() 捕获图执行的全部事件。

_TraceCollector 是核心——它用 asyncio.run() 包装 astream_events() v2 异步流，
在同步 PipelineOrchestrator.run() 内可调用。每个 LangGraph/LangChain 事件被转换
为 TraceEvent 并聚合为 TraceReport。
"""

from __future__ import annotations

import asyncio
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
    """把任意值转成一行摘要字符串。"""
    if value is None:
        return "None"
    if isinstance(value, dict):
        keys = list(value.keys())
        return "{" + ", ".join(f"{k}=..." for k in keys[:8]) + "}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    s = str(value)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def _serialize_messages(messages_input: Any) -> list[dict]:
    """把 chat model 的输入消息转成可序列化的 dict 列表。"""
    result: list[dict] = []
    if not isinstance(messages_input, dict):
        return result
    msgs = messages_input.get("messages", [])
    for msg in msgs:
        try:
            if hasattr(msg, "type") and hasattr(msg, "content"):
                entry: dict = {
                    "role": msg.type,
                    "content": str(getattr(msg, "content", ""))[:3000],
                }
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    entry["tool_calls"] = [
                        {"name": tc.get("name", ""), "args": str(tc.get("args", {}))[:500]}
                        for tc in msg.tool_calls
                        if isinstance(tc, dict)
                    ]
                result.append(entry)
            elif isinstance(msg, (list, tuple)) and len(msg) >= 2:
                result.append({"role": str(msg[0]), "content": str(msg[1])[:3000]})
        except Exception:
            result.append({"role": "unknown", "content": str(msg)[:500]})
    return result


class _TraceCollector:
    """事件采集器：订阅 astream_events() 流，聚合为 TraceReport。"""

    def __init__(self, diff_text: str, run_id: str) -> None:
        self._events: list[TraceEvent] = []
        self._seq = 0
        self._start = time.time()
        self._run_id = run_id
        self._diff_size = len(diff_text)
        self._node_stack: list[str] = []
        self._node_starts: dict[str, float] = {}
        self._node_ends: dict[str, float] = {}
        self._llm_counts: dict[str, int] = {}
        self._tool_counts: dict[str, int] = {}
        self._tokens_by_node: dict[str, TokenUsage] = {}

    def run_with_tracing(self, graph, initial_state: dict, config: dict) -> dict:
        """同步入口：用 asyncio.run() 执行带追踪的图。返回最终 state。"""
        return asyncio.run(self._collect_and_return(graph, initial_state, config))

    def finalize(self) -> TraceReport:
        """聚合为 TraceReport。"""
        node_timeline: list[NodeStats] = []
        all_node_names = set(self._node_starts.keys()) | set(self._node_ends.keys())
        for name in sorted(all_node_names):
            start_ms = self._node_starts.get(name, 0)
            end_ms = self._node_ends.get(name, start_ms)
            tokens = self._tokens_by_node.get(name, TokenUsage(node_name=name))
            node_timeline.append(NodeStats(
                node_name=name, start_ms=start_ms, end_ms=end_ms,
                duration_ms=end_ms - start_ms,
                llm_calls=self._llm_counts.get(name, 0),
                tool_calls=self._tool_counts.get(name, 0),
                tokens=tokens,
            ))

        event_counts: dict[str, int] = {}
        for e in self._events:
            event_counts[e.event_type] = event_counts.get(e.event_type, 0) + 1

        total_tokens = TokenUsage(node_name="total")
        for t in self._tokens_by_node.values():
            total_tokens.input_tokens += t.input_tokens
            total_tokens.output_tokens += t.output_tokens
            total_tokens.total_tokens += t.total_tokens

        return TraceReport(
            run_id=self._run_id,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._start)),
            diff_size=self._diff_size, events=self._events,
            summary=TraceSummary(
                total_duration_ms=(time.time() - self._start) * 1000,
                total_tokens=total_tokens, tokens_by_node=self._tokens_by_node,
                event_counts=event_counts, node_timeline=node_timeline,
            ),
        )

    async def _collect_and_return(self, graph, initial_state: dict, config: dict) -> dict:
        """异步执行图并采集事件，返回最终 state。从最外层 LangGraph 链的 on_chain_end 捕获 output。"""
        final_state: dict = {}
        try:
            async for event in graph.astream_events(initial_state, config=config, version="v2"):
                try:
                    self._handle_event(event)
                except Exception:
                    logger.debug("处理追踪事件失败", exc_info=True)
                if (
                    event.get("event") == "on_chain_end"
                    and event.get("name") == "LangGraph"
                    and (event.get("tags") or []) == []
                ):
                    output = event.get("data", {}).get("output")
                    if isinstance(output, dict) and output:
                        final_state = output
            if not final_state:
                final_state = graph.invoke(initial_state, config=config)
        except Exception:
            logger.warning("追踪采集异常，降级为无追踪执行", exc_info=True)
            final_state = graph.invoke(initial_state, config=config)
        return final_state

    def _handle_event(self, event: dict) -> None:
        ev = event.get("event", "")
        name = event.get("name", "")
        tags = list(event.get("tags", []) or [])
        meta = event.get("metadata", {}) or {}
        data = event.get("data", {}) or {}
        lg_node_name = meta.get("langgraph_node", "")
        is_lg_node = bool(lg_node_name)

        if ev == "on_chain_start":
            if is_lg_node and lg_node_name:
                self._on_node_start(lg_node_name, data)
        elif ev == "on_chain_end":
            if is_lg_node and lg_node_name:
                self._on_node_end(lg_node_name, data)
        elif ev == "on_chat_model_start":
            self._on_llm_start(name, data)
        elif ev == "on_chat_model_end":
            self._on_llm_end(name, data)
        elif ev == "on_tool_start":
            self._on_tool_start(name, data)
        elif ev == "on_tool_end":
            self._on_tool_end(name, data)

    def _on_node_start(self, node_name: str, data: dict) -> None:
        self._node_stack.append(node_name)
        depth = len(self._node_stack) - 1
        now_ms = (time.time() - self._start) * 1000
        if node_name not in self._node_starts:
            self._node_starts[node_name] = now_ms
        input_data = data.get("input", {})
        input_keys = list(input_data.keys()) if isinstance(input_data, dict) else []
        self._add_event("node_start", node_name, _phase_for(node_name), depth,
                        f"输入: {', '.join(input_keys[:6])}", {"input_keys": input_keys})

    def _on_node_end(self, node_name: str, data: dict) -> None:
        depth = len(self._node_stack) - 1
        self._node_ends[node_name] = (time.time() - self._start) * 1000
        if self._node_stack and self._node_stack[-1] == node_name:
            self._node_stack.pop()
        output_data = data.get("output", {})
        output_keys = list(output_data.keys()) if isinstance(output_data, dict) else []
        self._add_event("node_end", node_name, _phase_for(node_name), depth,
                        f"输出: {', '.join(output_keys[:6])}", {"output_keys": output_keys})
        if isinstance(output_data, dict):
            route = output_data.get("council_route", "")
            if route:
                self._add_event("route_decision", node_name, _phase_for(node_name), depth,
                                f"路由 => {route}", {"route": route})

    def _on_llm_start(self, model_name: str, data: dict) -> None:
        current = self._node_stack[-1] if self._node_stack else "unknown"
        depth = len(self._node_stack)
        self._llm_counts[current] = self._llm_counts.get(current, 0) + 1
        call_num = self._llm_counts[current]
        messages_input = data.get("input", {})
        self._add_event("llm_start", current, _phase_for(current), depth,
                        f"LLM #{call_num} ({model_name})",
                        {"model": model_name, "messages": _serialize_messages(messages_input)})

    def _on_llm_end(self, model_name: str, data: dict) -> None:
        current = self._node_stack[-1] if self._node_stack else "unknown"
        depth = len(self._node_stack)
        output = data.get("output")
        usage = None
        response_text = ""
        if hasattr(output, "usage_metadata") and output.usage_metadata:
            um = output.usage_metadata
            usage = TokenUsage(
                input_tokens=um.get("input_tokens", 0),
                output_tokens=um.get("output_tokens", 0),
                total_tokens=um.get("total_tokens", 0),
                model=model_name, node_name=current,
            )
            if current not in self._tokens_by_node:
                self._tokens_by_node[current] = TokenUsage(node_name=current)
            t = self._tokens_by_node[current]
            t.input_tokens += usage.input_tokens
            t.output_tokens += usage.output_tokens
            t.total_tokens += usage.total_tokens
        if hasattr(output, "content"):
            response_text = str(output.content)
        elif isinstance(output, dict):
            response_text = str(output.get("content", output))[:3000]
        else:
            response_text = str(output)[:3000] if output else ""
        total = usage.total_tokens if usage else "?"
        self._add_event("llm_end", current, _phase_for(current), depth,
                        f"完成 ({total} tokens)",
                        {"model": model_name, "response": response_text[:3000]}, tokens=usage)

    def _on_tool_start(self, tool_name: str, data: dict) -> None:
        current = self._node_stack[-1] if self._node_stack else "unknown"
        depth = len(self._node_stack)
        self._tool_counts[current] = self._tool_counts.get(current, 0) + 1
        tool_input = data.get("input", {})
        self._add_event("tool_start", current, _phase_for(current), depth,
                        f"Tool: {tool_name}",
                        {"tool_name": tool_name, "input": _summarize_value(tool_input, 500)})

    def _on_tool_end(self, tool_name: str, data: dict) -> None:
        current = self._node_stack[-1] if self._node_stack else "unknown"
        depth = len(self._node_stack)
        output = data.get("output")
        output_str = ""
        if hasattr(output, "content"):
            output_str = str(output.content)
        else:
            output_str = str(output)[:3000] if output else ""
        self._add_event("tool_end", current, _phase_for(current), depth,
                        f"返回 ({len(output_str)} 字符)",
                        {"tool_name": tool_name, "output": output_str[:3000]})

    def _add_event(self, event_type, node_name, phase, depth, summary,
                   detail=None, tokens=None):
        self._seq += 1
        self._events.append(TraceEvent(
            sequence=self._seq,
            timestamp_ms=(time.time() - self._start) * 1000,
            event_type=event_type, node_name=node_name, phase=phase,
            depth=depth, summary=summary, detail=detail or {}, tokens=tokens,
        ))
