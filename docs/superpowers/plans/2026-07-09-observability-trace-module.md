# Codeguard 可观测性追踪模块 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Codeguard 新增一个独立的可观测性追踪模块，采集 LangGraph 图执行期间的全部事件（节点流转、LLM 调用、工具调用、token 消耗），审查结束后产出纯静态 HTML Dashboard 供浏览器交互式回放。

**Architecture:** 5 个新文件（`observability/` 包）+ 3 个文件小改动（`config.py`、`orchestrator.py`、`cli.py`）。采集层用 LangGraph `astream_events()` v2 API 穿透多级子图，事件聚合成 `TraceReport` Pydantic 模型，Dashboard 为自包含 HTML 单文件（内联 CSS/JS，零外部依赖，`file://` 打开）。

**Tech Stack:** Python 3.10+、Pydantic、asyncio、LangGraph `astream_events` v2、纯 HTML/CSS/JS（无框架）

**Spec:** `docs/superpowers/specs/2026-07-09-observability-trace-module-design.md`

---

### Task 1: 创建数据模型 `observability/models.py`

**Files:**
- Create: `services/agent/src/codeguard_agent/observability/__init__.py`
- Create: `services/agent/src/codeguard_agent/observability/models.py`

- [ ] **Step 1: 创建 `__init__.py` 导出**

```python
"""Codeguard 可观测性追踪模块。

审查运行期间采集 LangGraph 图执行的全部事件（节点流转、LLM 调用、
工具调用、token 消耗），事后产出静态 HTML Dashboard 供交互式回放。
"""

from codeguard_agent.observability.models import (
    NodeStats,
    TokenUsage,
    TraceEvent,
    TraceReport,
    TraceSummary,
)

__all__ = [
    "NodeStats",
    "TokenUsage",
    "TraceEvent",
    "TraceReport",
    "TraceSummary",
]
```

- [ ] **Step 2: 创建 `models.py`**

```python
"""可观测性追踪的数据模型。

纯 Pydantic 模型——无外部依赖，可独立单测。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    """单次 LLM 调用的 token 消耗。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    node_name: str = ""


class TraceEvent(BaseModel):
    """一次追踪事件。"""

    sequence: int
    timestamp_ms: float
    event_type: str  # node_start | node_end | llm_start | llm_end | tool_start | tool_end | state_snapshot | route_decision | fallback | error
    node_name: str
    phase: str  # outer_graph | reviewer_subgraph | evidence | judge
    depth: int  # 图嵌套深度 (0=外层, 1=审查员子图, 2=ReAct 内部)
    summary: str
    detail: dict = Field(default_factory=dict)
    tokens: TokenUsage | None = None


class NodeStats(BaseModel):
    """单个节点的耗时与调用统计。"""

    node_name: str
    start_ms: float
    end_ms: float
    duration_ms: float
    llm_calls: int = 0
    tool_calls: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)


class TraceSummary(BaseModel):
    """聚合统计。"""

    total_duration_ms: float = 0.0
    total_tokens: TokenUsage = Field(default_factory=TokenUsage)
    tokens_by_node: dict[str, TokenUsage] = Field(default_factory=dict)
    event_counts: dict[str, int] = Field(default_factory=dict)
    node_timeline: list[NodeStats] = Field(default_factory=list)


class TraceReport(BaseModel):
    """一次审查的完整追踪报告。"""

    run_id: str
    timestamp: str
    diff_size: int = 0
    events: list[TraceEvent] = Field(default_factory=list)
    summary: TraceSummary = Field(default_factory=TraceSummary)
```

- [ ] **Step 3: 验证模型可导入**

Run: `cd services/agent && conda run -n codeguard python -c "from codeguard_agent.observability import TraceEvent, TraceReport, TokenUsage, TraceSummary, NodeStats; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add services/agent/src/codeguard_agent/observability/__init__.py services/agent/src/codeguard_agent/observability/models.py
git commit -m "feat(observability): 添加追踪数据模型 TraceEvent/TokenUsage/TraceReport/TraceSummary/NodeStats"
```

---

### Task 2: 实现采集器 `observability/collector.py`

**Files:**
- Create: `services/agent/src/codeguard_agent/observability/collector.py`

- [ ] **Step 1: 创建 `collector.py`**

```python
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

# 节点名 -> phase 映射
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
        # 嵌套追踪
        self._node_stack: list[str] = []
        self._node_starts: dict[str, float] = {}  # node_name -> first timestamp
        self._node_ends: dict[str, float] = {}
        self._llm_counts: dict[str, int] = {}
        self._tool_counts: dict[str, int] = {}
        self._tokens_by_node: dict[str, TokenUsage] = {}

    # ── public API ──

    def run_with_tracing(self, graph, initial_state: dict, config: dict) -> dict:
        """同步入口：用 asyncio.run() 执行带追踪的图。

        返回最终 state（与 graph.invoke() 同构）。
        """
        return asyncio.run(self._collect_and_return(graph, initial_state, config))

    def finalize(self) -> TraceReport:
        """聚合为 TraceReport。"""
        # 构建 node_timeline
        node_timeline: list[NodeStats] = []
        all_node_names = set(self._node_starts.keys()) | set(self._node_ends.keys())
        for name in sorted(all_node_names):
            start_ms = self._node_starts.get(name, 0)
            end_ms = self._node_ends.get(name, start_ms)
            tokens = self._tokens_by_node.get(name, TokenUsage(node_name=name))
            node_timeline.append(NodeStats(
                node_name=name,
                start_ms=start_ms,
                end_ms=end_ms,
                duration_ms=end_ms - start_ms,
                llm_calls=self._llm_counts.get(name, 0),
                tool_calls=self._tool_counts.get(name, 0),
                tokens=tokens,
            ))

        # 事件统计
        event_counts: dict[str, int] = {}
        for e in self._events:
            event_counts[e.event_type] = event_counts.get(e.event_type, 0) + 1

        total_tokens = TokenUsage(node_name="total")
        for t in self._tokens_by_node.values():
            total_tokens.input_tokens += t.input_tokens
            total_tokens.output_tokens += t.output_tokens
            total_tokens.total_tokens += t.total_tokens

        total_duration = (time.time() - self._start) * 1000

        return TraceReport(
            run_id=self._run_id,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._start)),
            diff_size=self._diff_size,
            events=self._events,
            summary=TraceSummary(
                total_duration_ms=total_duration,
                total_tokens=total_tokens,
                tokens_by_node=self._tokens_by_node,
                event_counts=event_counts,
                node_timeline=node_timeline,
            ),
        )

    # ── async core ──

    async def _collect_and_return(self, graph, initial_state: dict, config: dict) -> dict:
        """异步执行图并采集事件，返回最终 state。

        astream_events 不直接返回最终 state——从最外层 LangGraph 链的
        on_chain_end 事件中捕获 output。
        """
        final_state: dict = {}
        try:
            async for event in graph.astream_events(initial_state, config=config, version="v2"):
                try:
                    self._handle_event(event)
                except Exception:
                    logger.debug("处理追踪事件失败", exc_info=True)
                # 最外层 LangGraph 链完成时，其 output 即最终 state
                if (
                    event.get("event") == "on_chain_end"
                    and event.get("name") == "LangGraph"
                    and "langgraph_node" not in (event.get("tags") or [])
                ):
                    output = event.get("data", {}).get("output")
                    if isinstance(output, dict) and output:
                        final_state = output
            if not final_state:
                final_state = graph.invoke(initial_state, config=config)  # type: ignore[assignment]
        except Exception:
            logger.warning("追踪采集异常，降级为无追踪执行", exc_info=True)
            final_state = graph.invoke(initial_state, config=config)  # type: ignore[assignment]
        return final_state

    def _handle_event(self, event: dict) -> None:
        """处理单个 astream_events 事件。"""
        ev = event.get("event", "")
        name = event.get("name", "")
        tags = list(event.get("tags", []) or [])
        meta = event.get("metadata", {}) or {}
        data = event.get("data", {}) or {}

        is_lg_node = "langgraph_node" in tags
        lg_node_name = meta.get("langgraph_node", "")

        # ── 图 / 节点级 ──
        if ev == "on_chain_start":
            if is_lg_node and lg_node_name:
                self._on_node_start(lg_node_name, data)
        elif ev == "on_chain_end":
            if is_lg_node and lg_node_name:
                self._on_node_end(lg_node_name, data)
        # ── LLM 级 ──
        elif ev == "on_chat_model_start":
            self._on_llm_start(name, data)
        elif ev == "on_chat_model_end":
            self._on_llm_end(name, data)
        # ── 工具级 ──
        elif ev == "on_tool_start":
            self._on_tool_start(name, data)
        elif ev == "on_tool_end":
            self._on_tool_end(name, data)

    # ── node handlers ──

    def _on_node_start(self, node_name: str, data: dict) -> None:
        self._node_stack.append(node_name)
        depth = len(self._node_stack) - 1
        now_ms = (time.time() - self._start) * 1000
        if node_name not in self._node_starts:
            self._node_starts[node_name] = now_ms
        input_data = data.get("input", {})
        input_keys = list(input_data.keys()) if isinstance(input_data, dict) else []
        self._add_event(
            event_type="node_start",
            node_name=node_name,
            phase=_phase_for(node_name),
            depth=depth,
            summary=f"输入: {', '.join(input_keys[:6])}",
            detail={"input_keys": input_keys},
        )

    def _on_node_end(self, node_name: str, data: dict) -> None:
        depth = len(self._node_stack) - 1
        now_ms = (time.time() - self._start) * 1000
        self._node_ends[node_name] = now_ms
        if self._node_stack and self._node_stack[-1] == node_name:
            self._node_stack.pop()
        output_data = data.get("output", {})
        output_keys = list(output_data.keys()) if isinstance(output_data, dict) else []
        self._add_event(
            event_type="node_end",
            node_name=node_name,
            phase=_phase_for(node_name),
            depth=depth,
            summary=f"输出: {', '.join(output_keys[:6])}",
            detail={"output_keys": output_keys},
        )

        # 检测条件边路由
        if isinstance(output_data, dict):
            route = output_data.get("council_route", "")
            if route:
                self._add_event(
                    event_type="route_decision",
                    node_name=node_name,
                    phase=_phase_for(node_name),
                    depth=depth,
                    summary=f"路由 => {route}",
                    detail={"route": route},
                )

    # ── LLM handlers ──

    def _on_llm_start(self, model_name: str, data: dict) -> None:
        current = self._node_stack[-1] if self._node_stack else "unknown"
        depth = len(self._node_stack)
        self._llm_counts[current] = self._llm_counts.get(current, 0) + 1
        call_num = self._llm_counts[current]
        messages_input = data.get("input", {})
        self._add_event(
            event_type="llm_start",
            node_name=current,
            phase=_phase_for(current),
            depth=depth,
            summary=f"💬 LLM #{call_num} ({model_name})",
            detail={"model": model_name, "messages": _serialize_messages(messages_input)},
        )

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
                model=model_name,
                node_name=current,
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
            response_text = str(output)[:3000]

        total = usage.total_tokens if usage else "?"
        self._add_event(
            event_type="llm_end",
            node_name=current,
            phase=_phase_for(current),
            depth=depth,
            summary=f"完成 ({total} tokens)",
            detail={"model": model_name, "response": response_text[:3000]},
            tokens=usage,
        )

    # ── tool handlers ──

    def _on_tool_start(self, tool_name: str, data: dict) -> None:
        current = self._node_stack[-1] if self._node_stack else "unknown"
        depth = len(self._node_stack)
        self._tool_counts[current] = self._tool_counts.get(current, 0) + 1
        tool_input = data.get("input", {})
        self._add_event(
            event_type="tool_start",
            node_name=current,
            phase=_phase_for(current),
            depth=depth,
            summary=f"🔧 {tool_name}",
            detail={"tool_name": tool_name, "input": _summarize_value(tool_input, 500)},
        )

    def _on_tool_end(self, tool_name: str, data: dict) -> None:
        current = self._node_stack[-1] if self._node_stack else "unknown"
        depth = len(self._node_stack)
        output = data.get("output")
        output_str = ""
        if hasattr(output, "content"):
            output_str = str(output.content)
        else:
            output_str = str(output)[:3000] if output else ""
        self._add_event(
            event_type="tool_end",
            node_name=current,
            phase=_phase_for(current),
            depth=depth,
            summary=f"返回 ({len(output_str)} 字符)",
            detail={"tool_name": tool_name, "output": output_str[:3000]},
        )

    # ── helpers ──

    def _add_event(
        self,
        event_type: str,
        node_name: str,
        phase: str,
        depth: int,
        summary: str,
        detail: dict | None = None,
        tokens: TokenUsage | None = None,
    ) -> None:
        self._seq += 1
        self._events.append(TraceEvent(
            sequence=self._seq,
            timestamp_ms=(time.time() - self._start) * 1000,
            event_type=event_type,
            node_name=node_name,
            phase=phase,
            depth=depth,
            summary=summary,
            detail=detail or {},
            tokens=tokens,
        ))
```

- [ ] **Step 2: 验证 `_phase_for` 和 `_serialize_messages` 纯函数**

Run: `cd services/agent && conda run -n codeguard python -c "from codeguard_agent.observability.collector import _phase_for, _serialize_messages; print(_phase_for('discover_threat_model')); print(_phase_for('council_judge')); print(_phase_for('unknown_node'))"`
Expected:
```
reviewer_subgraph
judge
outer_graph
```

- [ ] **Step 3: Commit**

```bash
git add services/agent/src/codeguard_agent/observability/collector.py
git commit -m "feat(observability): 实现 _TraceCollector 采集器，基于 astream_events v2 捕获节点/LLM/工具事件"
```

---

### Task 3: 实现 Dashboard Python 包装 `observability/dashboard.py`

**Files:**
- Create: `services/agent/src/codeguard_agent/observability/dashboard.py`

- [ ] **Step 1: 创建 `dashboard.py`**

```python
"""Dashboard 生成：把 TraceReport 渲染为纯静态 HTML 文件。"""

from __future__ import annotations

import logging
from pathlib import Path

from codeguard_agent.observability.models import TraceReport

logger = logging.getLogger("codeguard.observability")

_TEMPLATE_DIR = Path(__file__).resolve().parent


def _load_template() -> str:
    """加载 HTML 模板文件。"""
    template_path = _TEMPLATE_DIR / "dashboard_template.html"
    if not template_path.exists():
        raise FileNotFoundError(f"Dashboard 模板不存在: {template_path}")
    return template_path.read_text(encoding="utf-8")


def render_dashboard(report: TraceReport) -> str:
    """把 TraceReport 渲染为完整的 HTML 字符串。

    模板中的 __TRACE_DATA__ 被替换为 JSON 数据。
    """
    template = _load_template()
    data_json = report.model_dump_json(indent=2)
    if "__TRACE_DATA__" not in template:
        logger.warning("Dashboard 模板缺少 __TRACE_DATA__ 占位符")
    return template.replace("__TRACE_DATA__", data_json)


def render_dashboard_file(report: TraceReport, output_dir: str, run_id: str) -> Path:
    """把 TraceReport 渲染为 HTML 文件，放到 output_dir 下。

    返回写入的文件路径。
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html = render_dashboard(report)
    file_path = out_dir / f"trace-{run_id[:8]}.html"
    file_path.write_text(html, encoding="utf-8")
    logger.info("追踪 Dashboard 已写入: %s", file_path)
    return file_path
```

- [ ] **Step 2: 验证 render_dashboard 可被调用（模板未创建时会抛 FileNotFoundError，这是预期的）**

Run: `cd services/agent && conda run -n codeguard python -c "from codeguard_agent.observability.dashboard import render_dashboard, render_dashboard_file; print('import OK')"`
Expected: `import OK`

- [ ] **Step 3: Commit**

```bash
git add services/agent/src/codeguard_agent/observability/dashboard.py
git commit -m "feat(observability): 实现 Dashboard HTML 渲染函数 render_dashboard/render_dashboard_file"
```

---

### Task 4: 创建 Dashboard HTML 模板 `observability/dashboard_template.html`

**Files:**
- Create: `services/agent/src/codeguard_agent/observability/dashboard_template.html`

- [ ] **Step 1: 创建完整的自包含 HTML 模板**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Codeguard Trace Viewer</title>
<style>
/* ── 全局 ── */
:root {
  --bg: #1a1b26; --panel: #24283b; --border: #414868;
  --text: #c0caf5; --dim: #565f89; --accent: #7aa2f7;
  --green: #9ece6a; --orange: #e0af68; --red: #f7768e; --purple: #bb9af7;
  --cyan: #7dcfff; --gray: #565f89;
  font-family: 'Segoe UI', system-ui, sans-serif; font-size: 13px;
  background: var(--bg); color: var(--text); margin: 0;
}
body { margin: 0; height: 100vh; display: flex; flex-direction: column; }

/* ── 顶部栏 ── */
.header { background: var(--panel); border-bottom: 1px solid var(--border);
  padding: 10px 16px; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; }
.header h1 { margin: 0; font-size: 16px; color: var(--accent); }
.header .meta { font-size: 11px; color: var(--dim); }
.filter-bar { padding: 6px 16px; background: var(--panel); border-bottom: 1px solid var(--border);
  display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
.filter-bar input { background: var(--bg); border: 1px solid var(--border); color: var(--text);
  padding: 4px 8px; border-radius: 4px; font-size: 12px; width: 200px; }
.filter-bar select { background: var(--bg); border: 1px solid var(--border); color: var(--text);
  padding: 4px 8px; border-radius: 4px; font-size: 12px; }
.filter-bar label { font-size: 11px; color: var(--dim); display: flex; align-items: center; gap: 4px; }

/* ── 主区域 ── */
.main { display: flex; flex: 1; overflow: hidden; }
.topology { width: 280px; flex-shrink: 0; background: var(--panel); border-right: 1px solid var(--border);
  overflow-y: auto; padding: 12px; }
.timeline { flex: 1; overflow-y: auto; padding: 12px; }

/* ── 拓扑图 ── */
.topo-node { padding: 6px 10px; margin: 3px 0; border-radius: 5px; cursor: pointer;
  font-size: 12px; transition: background 0.15s; border-left: 3px solid transparent; }
.topo-node:hover { background: rgba(122,162,247,0.1); }
.topo-node.active { background: rgba(122,162,247,0.2); border-left-color: var(--accent); }
.topo-node .name { font-weight: 600; }
.topo-node .stats { font-size: 10px; color: var(--dim); margin-top: 2px; }
.topo-label { font-size: 10px; color: var(--dim); text-transform: uppercase;
  letter-spacing: 0.5px; margin: 10px 0 4px; padding-left: 4px; }
.topo-line { border-left: 2px solid var(--border); margin-left: 14px; height: 8px; }
.color-T { border-left-color: var(--accent); }  /* ThreatModel */
.color-B { border-left-color: var(--green); }   /* Behavior */
.color-M { border-left-color: var(--orange); }  /* Maintainability */
.color-C { border-left-color: var(--purple); }  /* Coordinator/Judge */
.color-E { border-left-color: var(--cyan); }    /* Evidence */
.color-G { border-left-color: var(--gray); }    /* General/util */

/* ── 时间线 ── */
.event { margin: 2px 0; border-radius: 4px; border: 1px solid transparent; cursor: pointer; }
.event:hover { border-color: var(--border); }
.event-header { display: flex; align-items: center; gap: 8px; padding: 5px 8px; font-size: 12px; }
.event-header .time { color: var(--dim); font-size: 11px; min-width: 70px; font-family: monospace; }
.event-header .icon { width: 18px; text-align: center; font-size: 13px; }
.event-header .summary { flex: 1; }
.event-header .badge { font-size: 10px; padding: 1px 5px; border-radius: 3px;
  background: var(--bg); color: var(--dim); }
.event-body { display: none; padding: 8px 12px 12px 36px; background: rgba(0,0,0,0.15);
  border-top: 1px solid var(--border); font-size: 12px; }
.event.open .event-body { display: block; }
.event-body pre { background: rgba(0,0,0,0.3); padding: 8px; border-radius: 4px;
  overflow-x: auto; max-height: 400px; overflow-y: auto; font-size: 11px;
  white-space: pre-wrap; word-break: break-all; margin: 4px 0; }
.event.depth-0 { margin-left: 0; }
.event.depth-1 { margin-left: 16px; }
.event.depth-2 { margin-left: 32px; }
.event.route_decision { border-left: 3px solid var(--purple); }
.event.fallback { border-left: 3px solid var(--red); }
.event.error { border-left: 3px solid var(--red); }

/* ── 底栏 token ── */
.token-bar { background: var(--panel); border-top: 1px solid var(--border);
  padding: 8px 16px; flex-shrink: 0; display: flex; gap: 20px; align-items: center; font-size: 12px; }
.token-bar .total { font-weight: 700; color: var(--accent); }
.token-bar .breakdown { display: flex; gap: 12px; flex-wrap: wrap; }
.token-chip { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: var(--bg); }
.token-chip span { font-weight: 600; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Codeguard Trace Viewer</h1>
    <div class="meta" id="meta-info"></div>
  </div>
</div>

<div class="filter-bar">
  <input type="text" id="filter-search" placeholder="搜索事件..." oninput="applyFilter()">
  <select id="filter-type" onchange="applyFilter()">
    <option value="">全部类型</option>
    <option value="node_start">node_start</option>
    <option value="node_end">node_end</option>
    <option value="llm_start">llm_start</option>
    <option value="llm_end">llm_end</option>
    <option value="tool_start">tool_start</option>
    <option value="tool_end">tool_end</option>
    <option value="route_decision">route_decision</option>
    <option value="fallback">fallback</option>
    <option value="error">error</option>
  </select>
  <select id="filter-node" onchange="applyFilter()">
    <option value="">全部节点</option>
  </select>
  <label><input type="checkbox" id="filter-expand-all" onchange="toggleExpandAll()"> 展开全部</label>
</div>

<div class="main">
  <div class="topology" id="topology"></div>
  <div class="timeline" id="timeline"></div>
</div>

<div class="token-bar" id="token-bar"></div>

<script id="trace-data" type="application/json">__TRACE_DATA__</script>

<script>
// ── 数据加载 ──
const DATA = JSON.parse(document.getElementById('trace-data').textContent);
const events = DATA.events || [];
const summary = DATA.summary || {};
const tokensByNode = summary.tokens_by_node || {};
const nodeTimeline = summary.node_timeline || [];

// ── 节点颜色 ──
function nodeColorClass(name) {
  if (name.includes('threat')) return 'color-T';
  if (name.includes('behavior')) return 'color-B';
  if (name.includes('maintainability')) return 'color-M';
  if (name.includes('coordinator') || name.includes('judge')) return 'color-C';
  if (name.includes('evidence')) return 'color-E';
  return 'color-G';
}

function nodeLabel(name) {
  const m = {
    'summary': 'Summary', 'context_provider': 'ContextProvider',
    'discover_threat_model': 'ThreatModel', 'discover_behavior': 'Behavior',
    'discover_maintainability': 'Maint\'bility', 'council_coordinator': 'Coordinator',
    'evidence_agent': 'EvidenceAgent', 'council_judge': 'CouncilJudge',
    'prepare': '  └ prepare', 'review': '  └ review', 'collect': '  └ collect'
  };
  return m[name] || name;
}

function formatMs(ms) {
  if (ms < 1000) return ms.toFixed(1) + 'ms';
  return (ms / 1000).toFixed(2) + 's';
}

function formatTokens(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}

// ── 渲染 ──
document.getElementById('meta-info').textContent =
  `Run: ${DATA.run_id}  |  ${DATA.timestamp}  |  Diff: ${DATA.diff_size} 字符  |  ` +
  `总耗时: ${formatMs(summary.total_duration_ms)}  |  总 Token: ${formatTokens(summary.total_tokens?.total_tokens || 0)}`;

// ── 拓扑图 ──
const topo = document.getElementById('topology');
const TOPO_ORDER = [
  {label: '外层图', nodes: ['summary', 'context_provider']},
  {label: '发现者 Agent', nodes: ['discover_threat_model', 'discover_behavior', 'discover_maintainability']},
  {label: '发现者内部', nodes: ['prepare', 'review', 'collect']},
  {label: '编排与裁决', nodes: ['council_coordinator', 'evidence_agent', 'council_judge']},
];

TOPO_ORDER.forEach(group => {
  const label = document.createElement('div');
  label.className = 'topo-label';
  label.textContent = group.label;
  topo.appendChild(label);
  group.nodes.forEach(name => {
    const stats = nodeTimeline.find(n => n.node_name === name);
    if (!stats) return;
    const div = document.createElement('div');
    div.className = 'topo-node ' + nodeColorClass(name);
    div.innerHTML = `<div class="name">${nodeLabel(name)}</div>
      <div class="stats">⏱ ${formatMs(stats.duration_ms)}  |  💬 ${stats.llm_calls}  |  🔧 ${stats.tool_calls}  |  Tokens: ${formatTokens(stats.tokens?.total_tokens || 0)}</div>`;
    div.onclick = () => scrollToNode(name);
    div.id = 'topo-' + name;
    topo.appendChild(div);
  });
});

function scrollToNode(name) {
  document.querySelectorAll('.topo-node').forEach(n => n.classList.remove('active'));
  const tn = document.getElementById('topo-' + name);
  if (tn) tn.classList.add('active');
  const first = document.querySelector(`.event[data-node="${name}"]`);
  if (first) first.scrollIntoView({behavior: 'smooth', block: 'center'});
}

// ── 筛选 ──
let filterNode = '';
let filterType = '';
let filterSearch = '';

function applyFilter() {
  filterSearch = document.getElementById('filter-search').value.toLowerCase();
  filterType = document.getElementById('filter-type').value;
  filterNode = document.getElementById('filter-node').value;
  renderTimeline();
}

function toggleExpandAll() {
  const checked = document.getElementById('filter-expand-all').checked;
  document.querySelectorAll('.event').forEach(e => {
    if (checked) e.classList.add('open'); else e.classList.remove('open');
  });
}

// ── 填充节点筛选下拉 ──
const nodeNames = [...new Set(events.map(e => e.node_name))].sort();
const sel = document.getElementById('filter-node');
nodeNames.forEach(name => {
  const opt = document.createElement('option');
  opt.value = name;
  opt.textContent = nodeLabel(name);
  sel.appendChild(opt);
});

// ── 时间线 ──
const timeline = document.getElementById('timeline');

function eventIcon(type) {
  const icons = {node_start:'▶', node_end:'■', llm_start:'💬', llm_end:'✓',
    tool_start:'🔧', tool_end:'📋', route_decision:'↗', fallback:'⚠', error:'❌'};
  return icons[type] || '•';
}

function shouldAutoOpen(event) {
  return event.depth <= 1 && (event.event_type === 'node_start' || event.event_type === 'node_end');
}

function renderTimeline() {
  timeline.innerHTML = '';
  const filtered = events.filter(e => {
    if (filterNode && e.node_name !== filterNode) return false;
    if (filterType && e.event_type !== filterType) return false;
    if (filterSearch) {
      const haystack = (e.summary + ' ' + JSON.stringify(e.detail || {}) + ' ' + e.node_name).toLowerCase();
      if (!haystack.includes(filterSearch)) return false;
    }
    return true;
  });

  filtered.forEach(ev => {
    const div = document.createElement('div');
    div.className = `event depth-${ev.depth} ${ev.event_type}`;
    div.setAttribute('data-node', ev.node_name);
    if (shouldAutoOpen(ev)) div.classList.add('open');

    const phaseBadge = ev.phase !== 'outer_graph' ? `<span class="badge">${ev.phase}</span>` : '';
    div.innerHTML = `<div class="event-header">
      <span class="time">${formatMs(ev.timestamp_ms)}</span>
      <span class="icon">${eventIcon(ev.event_type)}</span>
      <span class="summary">${escHtml(ev.summary)}</span>
      ${phaseBadge}
      <span class="badge">${ev.node_name}</span>
    </div>
    <div class="event-body">${renderEventBody(ev)}</div>`;

    div.querySelector('.event-header').onclick = () => div.classList.toggle('open');
    timeline.appendChild(div);
  });
}

function escHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function renderEventBody(ev) {
  let html = '';
  const d = ev.detail || {};

  if (ev.event_type === 'llm_start' && d.messages) {
    html += '<strong>消息:</strong>';
    d.messages.forEach((m, i) => {
      const content = m.content || '';
      const roleLabel = m.role === 'system' ? 'System' : m.role === 'human' ? 'User' : m.role === 'ai' ? 'AI' : m.role;
      html += `<details><summary>${escHtml(roleLabel)} (${content.length} 字符)</summary><pre>${escHtml(content)}</pre></details>`;
    });
  }

  if (ev.event_type === 'llm_end' && d.response) {
    html += `<strong>响应:</strong><pre>${escHtml(d.response)}</pre>`;
  }

  if (ev.event_type === 'tool_start' && d.input) {
    html += `<strong>入参:</strong><pre>${escHtml(d.input)}</pre>`;
  }

  if (ev.event_type === 'tool_end' && d.output) {
    html += `<strong>返回:</strong><pre>${escHtml(d.output)}</pre>`;
  }

  if (ev.event_type === 'route_decision' && d.route) {
    html += `<strong>路由目标:</strong> <span style="color:var(--accent)">${escHtml(d.route)}</span>`;
  }

  if (ev.tokens) {
    html += `<div style="margin-top:4px;font-size:11px;color:var(--dim)">` +
      `Input: ${ev.tokens.input_tokens}  |  Output: ${ev.tokens.output_tokens}  |  ` +
      `Total: ${ev.tokens.total_tokens}  ${ev.tokens.model ? '|  Model: ' + escHtml(ev.tokens.model) : ''}</div>`;
  }

  return html || '<span style="color:var(--dim)">(无详情)</span>';
}

// ── Token 汇总 ──
const tbar = document.getElementById('token-bar');
const totalT = summary.total_tokens || {};
let barHtml = `<span class="total">总计: ${formatTokens(totalT.total_tokens || 0)} tokens</span><div class="breakdown">`;
Object.entries(tokensByNode || {}).forEach(([name, t]) => {
  barHtml += `<span class="token-chip">${escHtml(nodeLabel(name))}: <span>${formatTokens(t.total_tokens || 0)}</span></span>`;
});
barHtml += '</div>';
tbar.innerHTML = barHtml;

// 初始渲染
renderTimeline();
</script>
</body>
</html>
```

- [ ] **Step 2: 确认模板文件包含 `__TRACE_DATA__` 占位符**

Run: `cd services/agent && conda run -n codeguard python -c "from pathlib import Path; t = Path('src/codeguard_agent/observability/dashboard_template.html').read_text(); assert '__TRACE_DATA__' in t, 'missing placeholder'; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add services/agent/src/codeguard_agent/observability/dashboard_template.html
git commit -m "feat(observability): 创建 Dashboard HTML 模板（左侧拓扑图 + 右侧时间线 + 底部 token 汇总）"
```

---

### Task 5: 修改 `config.py` 新增 trace 配置

**Files:**
- Modify: `services/agent/src/codeguard_agent/config.py`

- [ ] **Step 1: 在 `Settings` dataclass 新增三个字段**

在 `Settings` 类的 `reasoning_effort` 字段之后添加：

```python
    # 追踪模块:默认开启(可通过 --no-trace 关闭)。
    trace_enabled: bool = True
    # 追踪文件输出目录。
    trace_dir: str = "trace"
    # LLM 输出截断字符数,0=不截断。
    trace_max_llm_content: int = 0
```

- [ ] **Step 2: 在 `from_env()` 方法中读取环境变量**

在 `from_env()` 的 return 语句之前（`reasoning_effort` 之后）添加：

```python
        trace_enabled = os.environ.get(
            "CODEGUARD_TRACE_ENABLED", "true"
        ).strip().lower() not in ("0", "false", "no", "off")
        trace_dir = os.environ.get("CODEGUARD_TRACE_DIR", "trace").strip()
        trace_max_llm_content = int(os.environ.get("CODEGUARD_TRACE_MAX_LLM_CONTENT", "0"))
```

并在 `return cls(...)` 中追加：

```python
            trace_enabled=trace_enabled,
            trace_dir=trace_dir,
            trace_max_llm_content=trace_max_llm_content,
```

- [ ] **Step 3: 验证配置可正常加载**

Run: `cd services/agent && conda run -n codeguard python -c "from codeguard_agent.config import Settings; s = Settings.from_env(); print(f'trace_enabled={s.trace_enabled} trace_dir={s.trace_dir}')"`
Expected: `trace_enabled=True trace_dir=trace`

- [ ] **Step 4: Commit**

```bash
git add services/agent/src/codeguard_agent/config.py
git commit -m "feat(observability): Settings 新增 trace_enabled/trace_dir/trace_max_llm_content 配置项"
```

---

### Task 6: 修改 `orchestrator.py` 集成追踪

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/orchestrator.py`

- [ ] **Step 1: 在 `run()` 方法签名新增 trace 参数**

在 `run()` 方法签名中，`enabled_tools` 参数之后添加：

```python
        trace_enabled: bool = False,
        trace_dir: str = "trace",
```

- [ ] **Step 2: 在 `run()` 方法体中添加 tracing 分支**

将现有的 `final_state = graph.invoke(initial, config=invoke_config)` 替换为条件分支。需要先拿到 `run_id`（可在方法开头生成或利用 `thread_id`）：

```python
        import uuid
        run_id = thread_id or str(uuid.uuid4())

        # ... build graph ...

        if trace_enabled:
            from codeguard_agent.observability.collector import _TraceCollector
            from codeguard_agent.observability.dashboard import render_dashboard_file

            tracer = _TraceCollector(diff_text, run_id)
            try:
                final_state = tracer.run_with_tracing(graph, initial, invoke_config)
            except Exception:
                logger.warning("追踪执行异常，降级为无追踪模式", exc_info=True)
                final_state = graph.invoke(initial, config=invoke_config)
            else:
                try:
                    report = tracer.finalize()
                    render_dashboard_file(report, trace_dir, run_id)
                except Exception:
                    logger.warning("追踪报告生成失败", exc_info=True)
        else:
            final_state = graph.invoke(initial, config=invoke_config)
```

- [ ] **Step 3: 验证 mock 模式下 tracing 开启仍然正常返回**

Run: `cd services/agent && conda run -n codeguard python -c "from codeguard_agent.config import Settings; from codeguard_agent.llm.client import build_llm; from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator; s = Settings(provider='mock', model='', api_key='', api_base_url='', max_retries=1, structured_method='function_calling', disable_thinking=False); llm = build_llm(s); orch = PipelineOrchestrator(); r = orch.run(llm, diff_text='diff --git a/Foo.java b/Foo.java\n+String x = null;', trace_enabled=True); print(f'issues={len(r.issues)} summary={r.summary[:50]}')"`
Expected: trace 目录下生成 HTML 文件，且 `issues=1 summary=...`

- [ ] **Step 4: Commit**

```bash
git add services/agent/src/codeguard_agent/pipeline/orchestrator.py
git commit -m "feat(observability): PipelineOrchestrator.run() 集成追踪采集与 Dashboard 生成"
```

---

### Task 7: 修改 `cli.py` 新增 `--trace`/`--no-trace` 开关

**Files:**
- Modify: `services/agent/src/codeguard_agent/cli.py`

- [ ] **Step 1: 添加 CLI 参数**

在 `review_parser.add_argument("--thread-id", ...)` 之后添加：

```python
    review_parser.add_argument(
        "--trace", action=argparse.BooleanOptionalAction, default=True,
        help="开启审查追踪，产出可视化 Dashboard HTML 文件（默认开），--no-trace 关闭",
    )
```

- [ ] **Step 2: 把 `trace_enabled` 和 `trace_dir` 传给 `orch.run()`**

在 `orch.run()` 调用中新增两个参数：

```python
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
            )
```

- [ ] **Step 3: 验证 CLI help 显示新参数**

Run: `cd services/agent && conda run -n codeguard python -m codeguard_agent review --help`
Expected: 输出中包含 `--trace / --no-trace` 说明

- [ ] **Step 4: Commit**

```bash
git add services/agent/src/codeguard_agent/cli.py
git commit -m "feat(observability): CLI 新增 --trace/--no-trace 开关（默认开启）"
```

---

### Task 8: 更新 `.env.example` 文档

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: 在文件末尾添加追踪相关环境变量**

```bash
# 追踪（可观测性）—— 审查过程可视化
CODEGUARD_TRACE_ENABLED=true
CODEGUARD_TRACE_DIR=trace/
CODEGUARD_TRACE_MAX_LLM_CONTENT=0
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs(observability): .env.example 新增追踪模块配置项说明"
```

---

### Task 9: 编写测试 `tests/test_observability.py`

**Files:**
- Create: `services/agent/tests/test_observability.py`

- [ ] **Step 1: 创建测试文件**

```python
"""追踪模块的确定性单元测试。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from codeguard_agent.observability.collector import (
    _NODE_PHASE_MAP,
    _phase_for,
    _serialize_messages,
    _summarize_value,
)
from codeguard_agent.observability.dashboard import render_dashboard, render_dashboard_file
from codeguard_agent.observability.models import (
    NodeStats,
    TokenUsage,
    TraceEvent,
    TraceReport,
    TraceSummary,
)


# ── 模型测试 ──


class TestTokenUsage:
    def test_defaults(self):
        t = TokenUsage()
        assert t.input_tokens == 0
        assert t.output_tokens == 0
        assert t.total_tokens == 0
        assert t.model == ""

    def test_serialization(self):
        t = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150, model="gpt-4", node_name="discover_threat_model")
        d = t.model_dump()
        assert d["input_tokens"] == 100
        assert d["output_tokens"] == 50
        assert d["total_tokens"] == 150


class TestTraceEvent:
    def test_minimal(self):
        e = TraceEvent(sequence=1, timestamp_ms=0.0, event_type="node_start", node_name="test", phase="outer_graph", depth=0, summary="test")
        assert e.detail == {}
        assert e.tokens is None

    def test_with_tokens(self):
        t = TokenUsage(total_tokens=42)
        e = TraceEvent(sequence=1, timestamp_ms=100.0, event_type="llm_end", node_name="test", phase="outer_graph", depth=1, summary="done", tokens=t)
        assert e.tokens.total_tokens == 42

    def test_detail_default(self):
        e = TraceEvent(sequence=1, timestamp_ms=0.0, event_type="tool_start", node_name="x", phase="outer_graph", depth=0, summary="")
        assert e.detail == {}


class TestNodeStats:
    def test_basic(self):
        s = NodeStats(node_name="discover_threat_model", start_ms=10.0, end_ms=30.0, duration_ms=20.0, llm_calls=2, tool_calls=3)
        assert s.duration_ms == 20.0
        assert s.tokens.total_tokens == 0

    def test_with_tokens(self):
        s = NodeStats(node_name="x", start_ms=0, end_ms=10, duration_ms=10, tokens=TokenUsage(total_tokens=500))
        assert s.tokens.total_tokens == 500


class TestTraceSummary:
    def test_defaults(self):
        s = TraceSummary()
        assert s.total_duration_ms == 0.0
        assert s.total_tokens.total_tokens == 0
        assert s.tokens_by_node == {}
        assert s.event_counts == {}
        assert s.node_timeline == []


class TestTraceReport:
    def test_full_roundtrip(self):
        events = [
            TraceEvent(sequence=1, timestamp_ms=10.0, event_type="node_start", node_name="summary", phase="outer_graph", depth=0, summary="输入: diff_text"),
            TraceEvent(sequence=2, timestamp_ms=20.0, event_type="node_end", node_name="summary", phase="outer_graph", depth=0, summary="输出: diff_summary"),
            TraceEvent(sequence=3, timestamp_ms=30.0, event_type="llm_start", node_name="summary", phase="outer_graph", depth=0, summary="💬 LLM #1", detail={"model": "deepseek"}),
            TraceEvent(sequence=4, timestamp_ms=100.0, event_type="llm_end", node_name="summary", phase="outer_graph", depth=0, summary="完成 (150 tokens)", tokens=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150)),
        ]
        summary = TraceSummary(
            total_duration_ms=200.0,
            total_tokens=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
            tokens_by_node={"summary": TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150, node_name="summary")},
            event_counts={"node_start": 1, "node_end": 1, "llm_start": 1, "llm_end": 1},
            node_timeline=[NodeStats(node_name="summary", start_ms=10, end_ms=100, duration_ms=90)],
        )
        report = TraceReport(run_id="test-1", timestamp="2026-07-09T00:00:00", diff_size=100, events=events, summary=summary)

        # 序列化 → 反序列化
        d = report.model_dump()
        report2 = TraceReport.model_validate(d)
        assert report2.run_id == "test-1"
        assert len(report2.events) == 4
        assert report2.summary.total_tokens.total_tokens == 150
        assert report2.summary.tokens_by_node["summary"].total_tokens == 150


# ── collector 纯函数测试 ──


class TestPhaseMapping:
    def test_all_nodes_have_phase(self):
        expected = {
            "summary", "context_provider", "discover_threat_model", "discover_behavior",
            "discover_maintainability", "council_coordinator", "evidence_agent", "council_judge",
            "prepare", "review", "collect",
        }
        assert set(_NODE_PHASE_MAP.keys()) == expected

    def test_unknown_node_falls_back_to_outer(self):
        assert _phase_for("nonexistent") == "outer_graph"

    def test_known_nodes(self):
        assert _phase_for("discover_threat_model") == "reviewer_subgraph"
        assert _phase_for("council_judge") == "judge"
        assert _phase_for("evidence_agent") == "evidence"
        assert _phase_for("summary") == "outer_graph"


class TestSummarizeValue:
    def test_none(self):
        assert _summarize_value(None) == "None"

    def test_dict(self):
        assert "diff_text" in _summarize_value({"diff_text": "long...", "enabled_tools": None})

    def test_list(self):
        assert "[3 items]" in _summarize_value([1, 2, 3])

    def test_string_truncation(self):
        assert _summarize_value("a" * 300, max_len=50).endswith("...")

    def test_string_no_truncation(self):
        assert _summarize_value("short", max_len=50) == "short"


class TestSerializeMessages:
    def test_empty(self):
        assert _serialize_messages({}) == []

    def test_not_dict(self):
        assert _serialize_messages("not a dict") == []

    def test_list_of_tuples(self):
        msgs = {"messages": [("system", "you are a reviewer"), ("human", "review this diff")]}
        result = _serialize_messages(msgs)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "human"

    def test_message_objects(self):
        """模拟 LangChain 消息对象。"""
        class FakeMsg:
            type = "ai"
            content = "I found an issue"
            tool_calls = [{"name": "get_file_content", "args": {"file_path": "Foo.java"}}]
        msgs = {"messages": [FakeMsg()]}
        result = _serialize_messages(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "ai"
        assert "tool_calls" in result[0]
        assert result[0]["tool_calls"][0]["name"] == "get_file_content"


# ── Dashboard 测试 ──


class TestDashboard:
    def test_render_with_placeholder(self):
        """验证 __TRACE_DATA__ 被替换且产出合法 HTML。"""
        # 创建一个最小 report
        report = TraceReport(
            run_id="test-dash",
            timestamp="2026-07-09T00:00:00",
            diff_size=42,
            events=[
                TraceEvent(sequence=1, timestamp_ms=10.0, event_type="node_start", node_name="summary", phase="outer_graph", depth=0, summary="start"),
                TraceEvent(sequence=2, timestamp_ms=100.0, event_type="node_end", node_name="summary", phase="outer_graph", depth=0, summary="end"),
            ],
            summary=TraceSummary(total_duration_ms=90.0, event_counts={"node_start": 1, "node_end": 1}),
        )
        html = render_dashboard(report)
        # 占位符已被替换
        assert "__TRACE_DATA__" not in html
        # JSON 数据被嵌入
        assert '"run_id": "test-dash"' in html
        assert '"events":' in html
        # HTML 结构完整
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_render_dashboard_file(self):
        """验证写文件功能。"""
        report = TraceReport(
            run_id="test-file",
            timestamp="2026-07-09T00:00:00",
            diff_size=10,
            events=[],
            summary=TraceSummary(),
        )
        with tempfile.TemporaryDirectory() as d:
            path = render_dashboard_file(report, d, "abc12345")
            assert path.exists()
            content = path.read_text(encoding="utf-8")
            assert "test-file" in content
            assert "</html>" in content


# ── 端到端集成测试 ──


class TestEndToEnd:
    def test_mock_review_with_trace(self):
        """跑一次 mock 审查 + trace，验证：
        1. ReviewResult 与无 trace 时一致
        2. trace 文件生成且包含事件
        """
        import os

        from codeguard_agent.config import Settings
        from codeguard_agent.llm.client import build_llm
        from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator

        settings = Settings(
            provider="mock", model="", api_key="", api_base_url="",
            max_retries=1, structured_method="function_calling", disable_thinking=False,
        )
        llm = build_llm(settings)
        diff_text = "diff --git a/Foo.java b/Foo.java\n@@ -10,6 +10,8 @@\n+    String password = \"hardcoded123\";\n+    Statement stmt = conn.createStatement();\n"

        with tempfile.TemporaryDirectory() as d:
            orch = PipelineOrchestrator(enable_summary=False)
            # 1) 无 trace
            r_no_trace = orch.run(llm, diff_text, trace_enabled=False)
            # 2) 有 trace
            r_trace = orch.run(llm, diff_text, trace_enabled=True, trace_dir=d)
            # ReviewResult 应一致
            assert r_no_trace.summary == r_trace.summary
            assert len(r_no_trace.issues) == len(r_trace.issues)

            # trace 文件生成
            html_files = list(Path(d).glob("trace-*.html"))
            assert len(html_files) == 1
            content = html_files[0].read_text(encoding="utf-8")
            assert "__TRACE_DATA__" not in content  # 已被替换
            assert '"events":' in content
            # 至少应有 node_start/node_end 事件
            assert '"node_start"' in content
            assert '</html>' in content
```

- [ ] **Step 2: 运行测试**

Run: `cd services/agent && conda run -n codeguard python -m pytest tests/test_observability.py -v`
Expected: 全部 PASS

- [ ] **Step 3: 验证全量单测不受影响**

Run: `cd services/agent && conda run -n codeguard python -m pytest tests/ -q`
Expected: 全部 PASS（新测试 + 旧测试）

- [ ] **Step 4: Commit**

```bash
git add services/agent/tests/test_observability.py
git commit -m "test(observability): 添加追踪模块单元测试与端到端集成测试"
```

---

### Task 10: 最终验证

- [ ] **Step 1: 跑全量测试**

Run: `cd services/agent && conda run -n codeguard python -m pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 2: Lint 检查**

Run: `cd services/agent && conda run -n codeguard ruff check src/codeguard_agent/observability/`
Expected: 无错误

- [ ] **Step 3: 跑 mock 模式验证 CLI --no-trace 关闭追踪**

Run: `cd services/agent && conda run -n codeguard python -m codeguard_agent review --no-trace`
Expected: 正常输出审查结果，且 trace/ 目录不新增文件

- [ ] **Step 4: 验证 `asyncio` 可用且 `run()` 是协程**

Run: `cd services/agent && conda run -n codeguard python -c "import asyncio; print(asyncio.__version__)"`
Expected: 输出版本号（Python 3.7+ 自带）

- [ ] **Step 5: Commit（如有 lint 修复）**

```bash
git add -u
git commit -m "chore(observability): lint 修复与最终验证"
```
