# Observability Trace Fidelity Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Codeguard 追踪模块，使静态 HTML 无损展示 LangGraph 节点 State、并行血缘、ReAct 内部 LLM/工具步骤及完整输入输出。

**Architecture:** 使用 `astream_events(version="v2")` 原生 `run_id/parent_ids` 建立节点实例树，以独立无损序列化器处理 Pydantic、dataclass、LangChain 消息与普通容器。采集器只接收真实节点生命周期事件，Dashboard 同时提供专用视图和原始 JSON 兜底，并安全嵌入任意源码文本。

**Tech Stack:** Python 3.10+、Pydantic、LangGraph 1.2+、LangChain 1.3+、pytest、纯 HTML/CSS/ES5 JavaScript

## Global Constraints

- 默认完整保存，不截断、不脱敏；`CODEGUARD_TRACE_MAX_LLM_CONTENT=0` 表示 LLM 内容不截断。
- Python 仍负责智能编排，Java Gateway 仍只负责事实与护栏。
- 不修改 `ReviewResult` / `Issue` 产品输出契约。
- 追踪失败不得改变正常审查结果；HTML 写入失败只记录警告。
- 不新增外部前端依赖，不引入 WebSocket/SSE。
- Windows 测试命令统一使用 `conda run -n codeguard --no-capture-output ...`。

---

### Task 1: 无损运行时值序列化

**Files:**
- Create: `services/agent/src/codeguard_agent/observability/serialization.py`
- Modify: `services/agent/tests/test_observability.py`

**Interfaces:**
- Produces: `serialize_trace_value(value: Any) -> Any`
- Produces: `serialize_messages(value: Any, *, max_content_length: int = 0) -> list[dict[str, Any]]`
- Produces: `serialize_llm_response(value: Any, *, max_content_length: int = 0) -> dict[str, Any]`
- Consumes: Pydantic `model_dump`, dataclass fields, Enum values, Mapping/sequence values and LangChain message-like objects.

- [ ] **Step 1: Write failing serializer tests**

Append tests that express the required public behavior:

```python
from dataclasses import dataclass

from codeguard_agent.observability.serialization import (
    serialize_llm_response,
    serialize_messages,
    serialize_trace_value,
)


def test_serialize_trace_value_preserves_long_nested_values():
    @dataclass
    class Payload:
        body: str

    value = {"payload": Payload(body="x" * 5000), "items": (1, 2)}
    serialized = serialize_trace_value(value)

    assert serialized["payload"]["body"] == "x" * 5000
    assert serialized["items"] == [1, 2]


def test_serialize_messages_accepts_direct_tuple_message_list():
    messages = [("system", "system text"), ("human", "user text")]

    assert serialize_messages(messages) == [
        {"role": "system", "content": "system text"},
        {"role": "human", "content": "user text"},
    ]


def test_serialize_messages_flattens_single_batch():
    messages = [[("system", "system text"), ("human", "user text")]]

    result = serialize_messages(messages)

    assert [item["role"] for item in result] == ["system", "human"]
    assert result[1]["content"] == "user text"


def test_serialize_llm_response_keeps_tool_calls_when_content_empty():
    class FakeAIMessage:
        type = "ai"
        content = ""
        tool_calls = [
            {
                "id": "call-1",
                "name": "get_file_content",
                "args": {"file_path": "src/Foo.java"},
            }
        ]
        invalid_tool_calls = []
        additional_kwargs = {"reasoning_content": "need source"}
        response_metadata = {"finish_reason": "tool_calls"}
        usage_metadata = {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }

    result = serialize_llm_response(FakeAIMessage())

    assert result["content"] == ""
    assert result["tool_calls"][0]["name"] == "get_file_content"
    assert result["tool_calls"][0]["args"]["file_path"] == "src/Foo.java"
    assert result["additional_kwargs"]["reasoning_content"] == "need source"
```

- [ ] **Step 2: Run the serializer tests and verify RED**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: collection fails because `codeguard_agent.observability.serialization` does not exist.

- [ ] **Step 3: Implement the serializer**

Create `serialization.py` with a cycle-safe recursive converter. The implementation must:

```python
def serialize_trace_value(value: Any) -> Any:
    return _serialize(value, seen=set(), depth=0)
```

`_serialize` must follow this order:

1. Return `None`, `str`, `int`, `float`, `bool` unchanged.
2. Return `Enum.value` recursively.
3. For objects exposing `model_dump`, call `model_dump(mode="python")` and recurse.
4. For dataclasses, iterate `dataclasses.fields` and recurse.
5. Convert `Mapping` keys to strings and recurse.
6. Convert list/tuple/set/frozenset to lists and recurse.
7. For other objects exposing `__dict__`, serialize public attributes.
8. Fall back to `{"__type__": type(value).__name__, "__repr__": repr(value)}`.
9. At depth greater than 40 return a visible `{"__trace_error__": "max_depth", ...}` marker.
10. On a repeated container/object identity return a visible `{"__trace_error__": "cycle", ...}` marker.

Implement message helpers with these exact semantics:

```python
def serialize_messages(
    value: Any, *, max_content_length: int = 0
) -> list[dict[str, Any]]:
    raw = value.get("messages", []) if isinstance(value, Mapping) else value
    # Flatten only batch wrappers; preserve each actual message.
    ...


def serialize_llm_response(
    value: Any, *, max_content_length: int = 0
) -> dict[str, Any]:
    data = serialize_trace_value(value)
    # Always expose content/tool_calls/invalid_tool_calls/additional_kwargs/
    # response_metadata/usage_metadata when attributes exist.
    ...
```

When `max_content_length > 0`, truncate only message/LLM textual `content` using:

```python
{
    "content": text[:max_content_length],
    "content_truncated": True,
    "content_original_length": len(text),
}
```

At `0`, preserve the original text exactly.

- [ ] **Step 4: Run serializer tests and verify GREEN**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: all observability tests pass, including the four new serializer tests.

- [ ] **Step 5: Commit the serializer**

```powershell
git add services/agent/src/codeguard_agent/observability/serialization.py services/agent/tests/test_observability.py
git commit -m "fix(observability): 无损序列化追踪运行时数据"
```

---

### Task 2: 事件血缘、并行节点与完整 State

**Files:**
- Modify: `services/agent/src/codeguard_agent/observability/models.py`
- Modify: `services/agent/src/codeguard_agent/observability/collector.py`
- Modify: `services/agent/tests/test_observability.py`

**Interfaces:**
- Consumes: `serialize_trace_value`, `serialize_messages`, `serialize_llm_response`
- Produces: `_TraceCollector(diff_text: str, run_id: str, max_llm_content: int = 0)`
- Produces: `TraceEvent` instances with `run_id`, `parent_ids`, `parent_run_id`, `node_path`, `invocation_id`
- Produces: `NodeStats` per node invocation rather than one entry per repeated node name.

- [ ] **Step 1: Write failing event-lineage tests**

Add a helper and focused tests:

```python
def _chain_event(
    event_type,
    *,
    name,
    run_id,
    parent_ids,
    node_name,
    data,
    checkpoint_ns="",
):
    return {
        "event": event_type,
        "name": name,
        "run_id": run_id,
        "parent_ids": parent_ids,
        "tags": [],
        "metadata": {
            "langgraph_node": node_name,
            "langgraph_checkpoint_ns": checkpoint_ns,
        },
        "data": data,
    }


def test_parallel_nodes_are_siblings_and_wrapper_events_are_ignored():
    collector = _TraceCollector("diff", "trace-run")
    root = "graph-root"
    for name in (
        "discover_threat_model",
        "discover_behavior",
        "discover_maintainability",
    ):
        collector._handle_event(_chain_event(
            "on_chain_start",
            name=name,
            run_id=f"run-{name}",
            parent_ids=[root],
            node_name=name,
            data={"input": {"diff_text": "full diff"}},
        ))
        collector._handle_event(_chain_event(
            "on_chain_start",
            name="LangGraph",
            run_id=f"wrapper-{name}",
            parent_ids=[root, f"run-{name}"],
            node_name=name,
            data={"input": {"diff_text": "full diff"}},
        ))

    starts = [e for e in collector.finalize().events if e.event_type == "node_start"]

    assert len(starts) == 3
    assert {e.depth for e in starts} == {0}
    assert {e.node_path for e in starts} == {
        "discover_threat_model",
        "discover_behavior",
        "discover_maintainability",
    }


def test_same_named_subgraph_nodes_keep_distinct_reviewer_paths():
    collector = _TraceCollector("diff", "trace-run")
    root = "graph-root"
    for reviewer in ("discover_threat_model", "discover_behavior"):
        reviewer_run = f"run-{reviewer}"
        collector._handle_event(_chain_event(
            "on_chain_start",
            name=reviewer,
            run_id=reviewer_run,
            parent_ids=[root],
            node_name=reviewer,
            data={"input": {}},
        ))
        collector._handle_event(_chain_event(
            "on_chain_start",
            name="prepare",
            run_id=f"prepare-{reviewer}",
            parent_ids=[root, reviewer_run, f"wrapper-{reviewer}"],
            node_name="prepare",
            checkpoint_ns=f"{reviewer}:uuid|prepare:uuid",
            data={"input": {"diff_text": reviewer}},
        ))

    prepares = [
        e for e in collector.finalize().events
        if e.event_type == "node_start" and e.node_name == "prepare"
    ]

    assert len(prepares) == 2
    assert {e.depth for e in prepares} == {1}
    assert {e.node_path for e in prepares} == {
        "discover_threat_model/prepare",
        "discover_behavior/prepare",
    }
    assert len({e.invocation_id for e in prepares}) == 2


def test_node_events_store_complete_input_and_output_values():
    collector = _TraceCollector("diff", "trace-run")
    start = _chain_event(
        "on_chain_start",
        name="context_provider",
        run_id="context-run",
        parent_ids=["graph-root"],
        node_name="context_provider",
        data={"input": {"diff_text": "actual diff", "enabled_tools": ["get_file_content"]}},
    )
    end = _chain_event(
        "on_chain_end",
        name="context_provider",
        run_id="context-run",
        parent_ids=["graph-root"],
        node_name="context_provider",
        data={
            "input": start["data"]["input"],
            "output": {"context_bundle": {"facts": [{"content": "fact text"}]}},
        },
    )

    collector._handle_event(start)
    collector._handle_event(end)
    events = collector.finalize().events

    assert events[0].detail["input"]["diff_text"] == "actual diff"
    assert events[1].detail["output"]["context_bundle"]["facts"][0]["content"] == "fact text"
```

- [ ] **Step 2: Write failing LLM/tool ownership tests**

```python
def test_llm_and_tool_events_attach_to_nearest_node_and_keep_full_data():
    collector = _TraceCollector("diff", "trace-run")
    collector._handle_event(_chain_event(
        "on_chain_start",
        name="review",
        run_id="review-run",
        parent_ids=["root", "discover-run", "subgraph-root"],
        node_name="review",
        checkpoint_ns="discover_threat_model:uuid|review:uuid",
        data={"input": {"user_prompt": "review me"}},
    ))
    collector._handle_event({
        "event": "on_chat_model_start",
        "name": "ChatOpenAI",
        "run_id": "llm-run",
        "parent_ids": ["root", "discover-run", "subgraph-root", "review-run"],
        "metadata": {"ls_model_name": "deepseek-v4-pro"},
        "data": {"input": [("human", "prompt" * 1000)]},
    })
    collector._handle_event({
        "event": "on_tool_start",
        "name": "get_file_content",
        "run_id": "tool-run",
        "parent_ids": ["root", "discover-run", "subgraph-root", "review-run"],
        "metadata": {},
        "data": {"input": {"file_path": "src/Foo.java", "content": "x" * 5000}},
    })

    events = collector.finalize().events
    llm = next(e for e in events if e.event_type == "llm_start")
    tool = next(e for e in events if e.event_type == "tool_start")

    assert llm.node_path == "review"
    assert llm.detail["messages"][0]["content"] == "prompt" * 1000
    assert tool.node_path == "review"
    assert tool.detail["input"]["content"] == "x" * 5000
```

- [ ] **Step 3: Run lineage tests and verify RED**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: failures show missing bloodline fields, duplicate wrapper nodes, incorrect parallel depths, and key-only node details.

- [ ] **Step 4: Extend the models compatibly**

Add defaulted fields:

```python
class TraceEvent(BaseModel):
    ...
    run_id: str = ""
    parent_ids: list[str] = Field(default_factory=list)
    parent_run_id: str = ""
    node_path: str = ""
    invocation_id: str = ""


class NodeStats(BaseModel):
    ...
    run_id: str = ""
    parent_run_id: str = ""
    node_path: str = ""
    depth: int = 0
    invocation_id: str = ""
```

- [ ] **Step 5: Replace stack-based collection with run lineage**

Introduce a private node-run record:

```python
@dataclass
class _NodeRun:
    run_id: str
    node_name: str
    parent_run_id: str
    node_path: str
    depth: int
    start_ms: float
    end_ms: float | None = None
```

Replace `_node_stack`, node-name keyed timestamps, and node-name keyed call counts with:

```python
self._node_runs: dict[str, _NodeRun] = {}
self._llm_counts: dict[str, int] = {}
self._tool_counts: dict[str, int] = {}
self._tokens_by_path: dict[str, TokenUsage] = {}
self._root_graph_run_id: str = ""
```

Implement:

```python
def _nearest_node_run(self, parent_ids: list[str]) -> _NodeRun | None:
    for parent_id in reversed(parent_ids):
        if parent_id in self._node_runs:
            return self._node_runs[parent_id]
    return None


def _is_real_node_event(event: dict[str, Any]) -> bool:
    metadata = event.get("metadata") or {}
    node_name = metadata.get("langgraph_node", "")
    return bool(node_name and event.get("name") == node_name)
```

For node start, create a `_NodeRun` using the nearest parent node. Use `parent.node_path + "/" + node_name` and `parent.depth + 1`; when no parent node exists use `node_name` and depth `0`.

For node end, look up the same `run_id`, update the instance end time, and store:

```python
detail={
    "input": serialize_trace_value(data.get("input")),
    "output": serialize_trace_value(data.get("output")),
}
```

Route decisions must read the serialized output but preserve the full output in the node event.

- [ ] **Step 6: Capture complete LLM and tool events**

Resolve each event owner through its `parent_ids`. Record:

```python
detail={
    "model": metadata.get("ls_model_name") or event.get("name", ""),
    "messages": serialize_messages(
        data.get("input"), max_content_length=self._max_llm_content
    ),
    "metadata": serialize_trace_value(metadata),
}
```

For LLM end:

```python
detail={
    "model": model_name,
    "response": serialize_llm_response(
        data.get("output"), max_content_length=self._max_llm_content
    ),
    "metadata": serialize_trace_value(metadata),
}
```

For tool start/end, store `serialize_trace_value(data.get("input"))` and `serialize_trace_value(data.get("output"))` without truncation.

Every `_add_event` call must pass raw `run_id`, `parent_ids`, the resolved `node_path`, and the owner invocation id.

- [ ] **Step 7: Build per-invocation summary statistics**

In `finalize`, emit one `NodeStats` per `_NodeRun`, ordered by `start_ms`. Aggregate LLM/tool counts by owner run id and tokens by `node_path`. Repeated `prepare` nodes remain distinct in `node_timeline`.

- [ ] **Step 8: Run observability tests and verify GREEN**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: all observability tests pass.

- [ ] **Step 9: Commit lineage collection**

```powershell
git add services/agent/src/codeguard_agent/observability/models.py services/agent/src/codeguard_agent/observability/collector.py services/agent/tests/test_observability.py
git commit -m "fix(observability): 按事件血缘采集完整节点状态"
```

---

### Task 3: 单次图执行与最终 State 捕获

**Files:**
- Modify: `services/agent/src/codeguard_agent/observability/collector.py`
- Modify: `services/agent/tests/test_observability.py`

**Interfaces:**
- Consumes: `_TraceCollector._handle_event`
- Produces: `_TraceCollector.run_with_tracing(graph, initial_state, config) -> dict`
- Guarantees: 正常事件流只执行图一次；顶层图输出作为最终 State。

- [ ] **Step 1: Write a failing fake-stream test**

```python
class _FakeGraph:
    def __init__(self):
        self.stream_calls = 0
        self.invoke_calls = 0

    async def astream_events(self, initial_state, *, config, version):
        self.stream_calls += 1
        yield {
            "event": "on_chain_start",
            "name": "LangGraph",
            "run_id": "root-run",
            "parent_ids": [],
            "tags": [],
            "metadata": {},
            "data": {"input": initial_state},
        }
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "run_id": "root-run",
            "parent_ids": [],
            "tags": [],
            "metadata": {},
            "data": {"output": {"final_issues": [], "review_summary": "done"}},
        }

    def invoke(self, initial_state, *, config):
        self.invoke_calls += 1
        raise AssertionError("normal tracing must not invoke graph a second time")


def test_run_with_tracing_returns_root_output_without_second_execution():
    graph = _FakeGraph()
    collector = _TraceCollector("diff", "trace-run")

    result = collector.run_with_tracing(graph, {"diff_text": "diff"}, {})

    assert result["review_summary"] == "done"
    assert graph.stream_calls == 1
    assert graph.invoke_calls == 0
```

- [ ] **Step 2: Run the fake-stream test and verify RED**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py::test_run_with_tracing_returns_root_output_without_second_execution -q
```

Expected: failure shows the current root detection does not reliably return the root output and may call `invoke`.

- [ ] **Step 3: Track the root graph run explicitly**

During collection:

```python
if (
    event.get("event") == "on_chain_start"
    and event.get("name") == "LangGraph"
    and not (event.get("parent_ids") or [])
):
    self._root_graph_run_id = str(event.get("run_id") or "")
```

Capture final output only when:

```python
event.get("event") == "on_chain_end"
and event.get("run_id") == self._root_graph_run_id
```

Remove the collector-internal `graph.invoke` fallback. If the stream completes without root output, raise `RuntimeError("追踪事件流缺少顶层图最终输出")`; the orchestrator remains the only explicit degradation boundary.

- [ ] **Step 4: Run the fake-stream test and verify GREEN**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py::test_run_with_tracing_returns_root_output_without_second_execution -q
```

Expected: PASS with `stream_calls == 1` and `invoke_calls == 0`.

- [ ] **Step 5: Commit single-execution behavior**

```powershell
git add services/agent/src/codeguard_agent/observability/collector.py services/agent/tests/test_observability.py
git commit -m "fix(observability): 精确捕获顶层图最终状态"
```

---

### Task 4: 安全 JSON 嵌入与完整详情 Dashboard

**Files:**
- Modify: `services/agent/src/codeguard_agent/observability/dashboard.py`
- Modify: `services/agent/src/codeguard_agent/observability/dashboard_template.html`
- Modify: `services/agent/tests/test_observability.py`

**Interfaces:**
- Consumes: enriched `TraceReport`
- Produces: `render_dashboard(report: TraceReport) -> str`
- Guarantees: 任意源码文本不能闭合 trace-data script；所有事件都有结构化详情与 raw JSON。

- [ ] **Step 1: Write failing safe-embedding tests**

```python
def test_dashboard_json_embedding_preserves_script_like_source():
    dangerous = "</script><script>window.pwned=true</script>\u2028\u2029"
    report = TraceReport(
        run_id="safe-json",
        timestamp="2026-07-09T00:00:00",
        events=[
            TraceEvent(
                sequence=1,
                timestamp_ms=0,
                event_type="node_start",
                node_name="review",
                phase="reviewer_subgraph",
                depth=1,
                summary="input",
                detail={"input": {"diff_text": dangerous}},
            )
        ],
    )

    html = render_dashboard(report)
    payload = re.search(
        r'<script id="trace-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    ).group(1)

    assert "</script><script>" not in payload
    assert json.loads(payload)["events"][0]["detail"]["input"]["diff_text"] == dangerous


def test_dashboard_template_renders_generic_node_and_raw_details():
    template = Path(
        "src/codeguard_agent/observability/dashboard_template.html"
    ).read_text(encoding="utf-8")

    assert "节点输入" in template
    assert "节点输出" in template
    assert "原始 JSON" in template
    assert "renderJsonValue" in template
```

Add `import re` at the test module top.

- [ ] **Step 2: Run Dashboard tests and verify RED**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: the dangerous payload breaks the current extraction or remains unescaped, and the template lacks generic detail labels/functions.

- [ ] **Step 3: Safely encode embedded JSON**

Replace `model_dump_json` embedding with:

```python
import json


def _json_for_html_script(report: TraceReport) -> str:
    payload = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )
    return (
        payload
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
```

`render_dashboard` must replace the placeholder with `_json_for_html_script(report)`.

- [ ] **Step 4: Add generic structured and raw renderers**

In the template, implement ES5-compatible helpers:

```javascript
function pretty(value) {
  try { return JSON.stringify(value, null, 2); }
  catch (e) { return String(value); }
}

function renderJsonValue(title, value, open) {
  if (typeof value === 'undefined') return '';
  return '<details class="detail-block"'+(open ? ' open' : '')+'>'+
    '<summary>'+esc(title)+'</summary><pre>'+esc(pretty(value))+'</pre></details>';
}
```

`renderEventBody` must:

- Render `d.input` as “节点输入” for `node_start`.
- Render `d.input` and `d.output` as “节点输入” / “节点输出” for `node_end`.
- Render `d.messages` and full structured `d.response` for LLM events.
- Render full `d.input` / `d.output` for tool events.
- Append `renderJsonValue("原始 JSON", ev, false)` for every event type.
- Use `node_path || node_name` in badges and filters.

Generate topology entries from `node_timeline` rather than the fixed `GROUPS` allowlist. Each row must use `invocation_id` as DOM identity and show `node_path`, depth, duration, LLM calls, tool calls and tokens.

- [ ] **Step 5: Run Dashboard tests and verify GREEN**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: all Dashboard and observability tests pass.

- [ ] **Step 6: Commit Dashboard fidelity**

```powershell
git add services/agent/src/codeguard_agent/observability/dashboard.py services/agent/src/codeguard_agent/observability/dashboard_template.html services/agent/tests/test_observability.py
git commit -m "fix(observability): 展示完整追踪详情并安全嵌入源码"
```

---

### Task 5: 配置接线、端到端覆盖与最终验证

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/orchestrator.py`
- Modify: `services/agent/src/codeguard_agent/cli.py`
- Modify: `services/agent/tests/test_observability.py`

**Interfaces:**
- Produces: `PipelineOrchestrator.run(..., trace_max_llm_content: int = 0)`
- Consumes: `Settings.trace_max_llm_content`
- Guarantees: CLI 配置实际控制 collector；默认值 `0` 保留完整内容。

- [ ] **Step 1: Write failing configuration propagation test**

Monkeypatch the collector constructor and assert the configured value reaches it:

```python
def test_orchestrator_passes_trace_max_llm_content(monkeypatch, tmp_path):
    observed = {}

    class FakeCollector:
        def __init__(self, diff_text, run_id, max_llm_content=0):
            observed["max_llm_content"] = max_llm_content

        def run_with_tracing(self, graph, initial, config):
            return graph.invoke(initial, config=config)

        def finalize(self):
            return TraceReport(run_id="fake", timestamp="now")

    monkeypatch.setattr(
        "codeguard_agent.observability.collector._TraceCollector",
        FakeCollector,
    )
    monkeypatch.setattr(
        "codeguard_agent.observability.dashboard.render_dashboard_file",
        lambda *args, **kwargs: tmp_path / "trace.html",
    )

    PipelineOrchestrator(enable_summary=False).run(
        None,
        "diff --git a/Foo.java b/Foo.java\n-old\n+new\n",
        trace_enabled=True,
        trace_dir=str(tmp_path),
        trace_max_llm_content=1234,
    )

    assert observed["max_llm_content"] == 1234
```

- [ ] **Step 2: Run propagation test and verify RED**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py::test_orchestrator_passes_trace_max_llm_content -q
```

Expected: `PipelineOrchestrator.run` rejects the unknown `trace_max_llm_content` argument.

- [ ] **Step 3: Wire the configuration**

Add:

```python
trace_max_llm_content: int = 0,
```

to `PipelineOrchestrator.run`, and construct:

```python
tracer = _TraceCollector(
    diff_text,
    _run_id,
    max_llm_content=trace_max_llm_content,
)
```

Pass from CLI:

```python
trace_max_llm_content=settings.trace_max_llm_content,
```

- [ ] **Step 4: Strengthen end-to-end assertions**

Update the mock tracing test to parse the embedded JSON and assert:

```python
match = re.search(
    r'<script id="trace-data" type="application/json">(.*?)</script>',
    content,
    re.DOTALL,
)
report_data = json.loads(match.group(1))

assert report_data["events"]
assert any(
    event["event_type"] == "node_start"
    and "input" in event["detail"]
    for event in report_data["events"]
)
assert all(event["depth"] >= 0 for event in report_data["events"])
assert len({
    item["invocation_id"]
    for item in report_data["summary"]["node_timeline"]
}) == len(report_data["summary"]["node_timeline"])
```

- [ ] **Step 5: Run observability tests**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: all observability tests pass.

- [ ] **Step 6: Run full Python test suite**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 7: Run lint and type checks on changed Python modules**

Run:

```powershell
conda run -n codeguard --no-capture-output ruff check src/codeguard_agent/observability/ src/codeguard_agent/pipeline/orchestrator.py src/codeguard_agent/cli.py tests/test_observability.py
conda run -n codeguard --no-capture-output mypy src/codeguard_agent/observability/ src/codeguard_agent/pipeline/orchestrator.py
```

Expected: both commands exit `0`.

- [ ] **Step 8: Generate and inspect a real trace**

Run one existing CLI review with current local provider/tool configuration. Parse the generated `<script id="trace-data">` JSON and verify:

- three discoverer paths are siblings at depth `0`;
- `prepare/review/collect` paths include their owning discoverer;
- at least one LLM event contains non-empty messages or structured tool calls;
- every tool event retains its complete input/output;
- no event detail contains the legacy key-only shape as its sole detail;
- the generated HTML contains no literal payload `</script>` sequence inside the trace-data JSON.

- [ ] **Step 9: Commit integration and tests**

```powershell
git add services/agent/src/codeguard_agent/pipeline/orchestrator.py services/agent/src/codeguard_agent/cli.py services/agent/tests/test_observability.py
git commit -m "fix(observability): 接通完整追踪配置与端到端验证"
```

- [ ] **Step 10: Review worktree scope**

Run:

```powershell
git status --short
git log -6 --oneline
```

Expected: only pre-existing untracked user artifacts remain; implementation changes are committed with Conventional Commit messages.
