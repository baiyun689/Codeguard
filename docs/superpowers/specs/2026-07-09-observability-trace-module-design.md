# Codeguard 可观测性追踪模块 · 设计文档

**日期**: 2026-07-09
**状态**: 待实现
**关联 ADR**: 待分配编号

---

## 1. 动机

当前 Codeguard 审查过程对开发者是一个"黑盒"：审查跑完只能看到最终的 `Issue` 列表，中间发生了什么——图的节点如何流转、每个发现者 Agent 内部的 ReAct 循环做了什么、LLM 每次调用的完整输出是什么、工具调用了哪些参数返回了什么、token 消耗分布如何——完全不可见。调试一个漏报或误报时，只能翻 Java 网关日志 + Python 日志拼凑线索。

本次设计引入一个**独立的可观测性模块**，在审查运行时采集完整的追踪数据，审查结束后产出一个**纯静态 HTML Dashboard**，浏览器打开即可交互式回放整个审查过程。

---

## 2. 设计目标

1. **图状态流转可视化**：外层 LangGraph 拓扑中每个节点接收了什么输入、产出了什么输出、经条件边路由到了谁
2. **ReAct 内部全透明**：发现者 Agent 内部的每一次 LLM 调用（完整输出含思考过程+工具调用决策）、每一次工具调用（请求参数+返回内容）全部可查看
3. **Token 用量追踪**：每次 LLM 调用的 input/output token，按节点/Agent 维度汇总
4. **独立模块，零侵入生产路径**：通过可选参数注入，默认关闭；开启时生产路径只有一行 `if` 分支切换，不影响性能
5. **事后回看，可存档分享**：产出单个自包含 HTML 文件，无网络依赖，`file://` 直接打开

---

## 3. 整体架构

```
审查运行中                              审查运行后
┌──────────────────────────┐      ┌──────────────────────┐
│ PipelineOrchestrator.run │      │ trace/<run_id>.html  │
│  + trace_enabled=True    │ ──→  │ (自包含 HTML)         │
│  + trace_dir="trace/"    │      └──────────┬───────────┘
└──────────┬───────────────┘                 │
           │                                 │ 双击打开
           ▼                                 ▼
┌──────────────────────────┐      ┌──────────────────────┐
│ _TraceCollector          │      │ Dashboard 交互:       │
│  asyncio.run(astream)    │      │ 左侧拓扑图 + 右侧时间线 │
│  处理每个 LangGraph 事件   │      │ Token 汇总栏          │
│  聚合 TraceEvent[]        │      │ LLM/工具调用展开/折叠   │
└──────────────────────────┘      └──────────────────────┘
```

核心原则：**采集层用 LangGraph/LangChain 原生钩子，不侵入业务代码；输出一个自包含 HTML 文件。**

---

## 4. 事件模型

### 4.1 数据模型

```python
class TokenUsage(BaseModel):
    """单次 LLM 调用的 token 消耗。"""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    node_name: str = ""


class TraceEvent(BaseModel):
    """一次追踪事件。"""
    sequence: int            # 全局自增序号
    timestamp_ms: float      # 距审查开始的毫秒数
    event_type: str          # node_start | node_end | llm_start | llm_end |
                             # tool_start | tool_end | state_snapshot |
                             # route_decision | fallback | error
    node_name: str           # 所属节点 (graph node name)
    phase: str               # outer_graph | reviewer_subgraph | evidence | judge
    depth: int               # 图嵌套深度 (0=外层, 1=审查员子图, 2=ReAct 内部)
    summary: str             # 一行人类可读摘要
    detail: dict             # 事件专属数据 (消息全文/工具参数/State diff 等)
    tokens: TokenUsage | None


class TraceReport(BaseModel):
    """一次审查的完整追踪报告。"""
    run_id: str
    timestamp: str
    diff_size: int
    events: list[TraceEvent]
    summary: TraceSummary


class TraceSummary(BaseModel):
    """聚合统计。"""
    total_duration_ms: float
    total_tokens: TokenUsage
    tokens_by_node: dict[str, TokenUsage]  # node_name → 汇总
    event_counts: dict[str, int]           # event_type → 数量
    node_timeline: list[NodeStats]         # 每个节点的耗时统计


class NodeStats(BaseModel):
    node_name: str
    start_ms: float
    end_ms: float
    duration_ms: float
    llm_calls: int
    tool_calls: int
    tokens: TokenUsage
```

### 4.2 事件层级

| 层级 | event_type | 来源 | 说明 |
|------|-----------|------|------|
| 0 - 图级 | `graph_start` / `graph_end` | `on_chain_start/end` (外层图) | 审查开始/完成 |
| 1 - 节点级 | `node_start` / `node_end` | `on_chain_start/end` (LangGraph 节点) | 每个节点的输入 State 键和输出 State diff |
| 1 - 路由 | `route_decision` | `on_chain_end` 中的 `channel_values` | 条件边的路由选择（coordinator/judge 到哪个下游节点） |
| 2 - 子图 | `node_start/end` (reviewer) | `on_chain_start/end` (reviewer 子图) | 发现者子图的 prepare → review → collect |
| 2 - 降级 | `fallback` | 手动事件 | ReAct 撞递归上限降级为 DirectEngine 直连复审 |
| 2 - 抽取 | `structured_result` | 手动事件 | 结构化结果从消息流中成功/失败抽取 |
| 3 - LLM 调用 | `llm_start` / `llm_end` | `on_chat_model_start/end` | prompt 全文、响应全文、token 用量 |
| 3 - 工具调用 | `tool_start` / `tool_end` | `on_tool_start/end` | 工具名、入参、返回内容 |

---

## 5. 采集机制

### 5.1 核心 API

使用 LangGraph 的 `graph.astream_events(initial, config=..., version="v2")`。

`version="v2"` 模式自动穿透多层子图——外层 ReviewCouncil 图 → 审查员子图 (`build_reviewer_subgraph`) → `create_agent` 内部 ReAct 图——所有层级的事件都在同一个事件流中产出，无需手动递归。

### 5.2 事件处理逻辑

```python
async for event in graph.astream_events(initial_state, config=config, version="v2"):
    match (event["event"], event["name"]):
        # ── 图/节点 启动/完成 ──
        case ("on_chain_start", _):
            tracer.on_chain_start(event)
        case ("on_chain_end", _):
            tracer.on_chain_end(event)

        # ── LLM 调用 ──
        case ("on_chat_model_start", _):
            tracer.on_llm_start(event)   # 提取 input messages
        case ("on_chat_model_end", _):
            tracer.on_llm_end(event)     # ★ 提取 usage_metadata

        # ── 工具调用 ──
        case ("on_tool_start", _):
            tracer.on_tool_start(event)  # 工具名 + 入参
        case ("on_tool_end", _):
            tracer.on_tool_end(event)    # 工具返回内容
```

### 5.3 关键细节

**穿透子图**：`astream_events` v2 模式产生的 `on_chain_start/end` 事件包含 `event["name"]` 字段，直接对应 LangGraph 节点名：
- 外层图节点：`"summary"`, `"context_provider"`, `"discover_threat_model"`, `"council_coordinator"`, `"evidence_agent"`, `"council_judge"`
- 审查员子图内部：`"prepare"`, `"review"`, `"collect"`（它们的 `"parent_name"` 是对应的发现者节点名）
- create_agent 内部的 LLM/Tool 事件深度为 2

通过 `event["tags"]` 和 `event["metadata"]` 可区分事件的来源层级和所属节点。需要根据实际 `astream_events` 产出的 `name` 和 `tags` 做名称映射（如 `langgraph_node` tag 对应节点名、`chain_type` tag 区分 agent/tool/llm）。

**Token 提取**：`on_chat_model_end` 中 `event["data"]["output"]` 是 `AIMessage` 对象，其上 `usage_metadata` 字典包含 `{"input_tokens": N, "output_tokens": N, "total_tokens": N}`。OpenAI/DeepSeek/千问 均支持此格式。

**State 变更**：在 `on_chain_end` 中对比节点输入和输出的 State 键，产出 `state_snapshot` 事件。仅记录发生变化的键，避免全量 State 打印爆炸。

**ReAct 降级**：`ToolAgentEngine.review` 中的 `except GraphRecursionError` 路径和 `ReAct 未产出 issue` 降级路径——目前无法被 `astream_events` 直接捕获（它是引擎内的同步异常处理），需要通过 `tracer` 手动注入事件。

**同步兼容**：`PipelineOrchestrator.run()` 是同步方法。tracing 开启时内部用 `asyncio.run()` 包装异步事件循环。这与现有线程池 fan-out（`ThreadPoolExecutor`）不冲突——`astream_events` 调用的是整个外层图的执行，图内部的并行 Send fan-out 由 LangGraph 运行时托管。

### 5.4 与现有 thread_id/checkpoint 的关系

tracing 与 checkpoint（ADR-026）**互斥**——两者都要求替换 `graph.invoke()` 为不同调用方式。当前 checkpoint 默认关闭（`CODEGUARD_CHECKPOINT_BACKEND` 为空），tracing 也默认关闭，互不影响。同时开启时，`astream_events` 支持传 `config.configurable.thread_id`，可以共存，但首版不做此组合的测试覆盖。

---

## 6. 集成方式

### 6.1 PipelineOrchestrator 改动

`PipelineOrchestrator.run()` 新增一个可选参数：

```python
def run(
    self,
    ...
    trace_enabled: bool = False,
    trace_dir: str = "trace",
) -> ReviewResult:
```

新增分支 (<10 行改动)：

```python
if trace_enabled:
    tracer = _TraceCollector(diff_text, run_id)
    final_state = tracer.run_with_tracing(graph, initial, invoke_config)
    report = tracer.finalize()
    _write_trace_html(report, trace_dir, run_id)
else:
    final_state = graph.invoke(initial, config=invoke_config)  # 现有路径，不变
```

### 6.2 CLI 改动

`--trace` 默认开启，加 `--no-trace` 显式关闭：

```python
review_parser.add_argument("--trace", action=argparse.BooleanOptionalAction,
    default=True, help="开启审查追踪（默认开），--no-trace 关闭")
```

### 6.4 CI 路径（无需改动）

CI 模式同样默认开启追踪（`ReviewExecutorImpl.runProcess()` 已透传 `CODEGUARD_*` 环境变量，Python CLI 子进程继承 `--trace` 默认值）。trace 文件落盘在 CI 工作目录下，方便事后下载排查问题。`start-ci.ps1`、`docker-compose.yml`、`ReviewExecutorImpl.buildCommand()` 均无需改动。

### 6.3 环境变量

```bash
CODEGUARD_TRACE_ENABLED=true       # 是否开启追踪（默认 true，CLI --no-trace 覆盖）
CODEGUARD_TRACE_DIR=trace/         # 追踪文件输出目录（默认 trace/）
CODEGUARD_TRACE_MAX_LLM_CONTENT=0  # LLM 输出截断字符数，0=不截断（默认 0）
```

---

## 7. Dashboard HTML

### 7.1 实现方式

纯静态单文件 HTML，内联 CSS + JS，零外部依赖。追踪数据以 JSON 格式嵌入 `<script id="trace-data" type="application/json">` 标签中。浏览器 `file://` 直接打开。

```python
def render_dashboard(report: TraceReport) -> str:
    template = _load_dashboard_template()
    data_json = report.model_dump_json(indent=2)
    return template.replace("__TRACE_DATA__", data_json)
```

### 7.2 布局

```
┌──────────────────────────────────────────────────────────┐
│  Codeguard Trace Viewer            run_id / 2026-07-09   │
├─────────────────┬────────────────────────────────────────┤
│                 │                                        │
│  图拓扑面板     │         事件时间线面板                   │
│  (左侧 ~30%)   │         (右侧 ~70%)                     │
│                 │                                        │
│  ┌───────────┐  │  ⏱ 00:00.000 ▼ context_provider       │
│  │  START    │  │     输入: diff(5.2KB), 2 files         │
│  │   │       │  │     输出: context_bundle (5 facts)      │
│  │ summary?  │  │  ────────────────────────────────────  │
│  │   │       │  │  ⏱ 00:01.234 ▼ discover_ThreatModel   │
│  │ ctx_prov  │  │     └─ prepare (0.1ms)                 │
│  │ ┌─┼─┼─┐  │  │     └─ 💬 LLM call #1 (3.2s)          │
│  │ T│B│M  │  │  │        输入: 3200 tokens              │
│  │ │││││  │  │  │        输出: 450 tokens                │
│  │ └─┼─┼─┘  │  │        [点击展开完整消息]               │
│  │ coord    │  │     └─ 🔧 find_sensitive_apis (0.05s)  │
│  │   │      │  │        → HIGH: executeQuery(Line42)     │
│  │ evidence │  │     └─ 🔧 get_file_content (0.03s)     │
│  │   │      │  │        → src/.../UserService.java(15KB) │
│  │ judge    │  │     └─ 💬 LLM call #2 (2.1s)           │
│  │   │      │  │        ...                              │
│  │  END     │  │     └─ collect: 3 candidates            │
│  └───────────┘  │     └─ ⚠ 降级: ReAct 未产出→直连复审     │
│                 │  ────────────────────────────────────  │
│                 │  ⏱ 00:15.678 ▼ coordinator             │
│  ● ThreatModel │     路由: evidence_agent                │
│  ● Behavior    │  ⏱ 00:15.690 ▼ evidence_agent          │
│  ● Maint'bility│     请求: 2 evidence_requests           │
│  ● Coordinator │     ...                                 │
│  ● Evidence    │  ⏱ 00:18.234 ▼ council_judge           │
│  ● Judge       │     阶段1: 规则淘汰 2条                  │
│                 │     阶段2: 去重 (无合并)                │
│                 │     阶段3: LLM 终审 (3条)              │
│                 │     最终: final_issues=5               │
├─────────────────┴────────────────────────────────────────┤
│  Token 汇总  │  总计 52.3K tokens                        │
│   ThreatModel: 18.2K  │  Behavior: 15.7K                │
│   Maintainability: 10.1K  │  Evidence: 3.5K  │  Judge: 4.8K │
└──────────────────────────────────────────────────────────┘
```

### 7.3 交互功能

#### 左侧图拓扑面板
- 基于 `node_timeline` 渲染 LangGraph 拓扑：节点名、耗时、颜色标注节点类型（发现者/协调/证据/裁决）
- 鼠标 hover：显示节点耗时/token 摘要 tooltip
- 点击节点 → 右侧时间线滚动到该节点第一个事件
- 每类节点用不同颜色（蓝=ThreatModel，绿=Behavior，橙=Maintainability，紫=协调/裁决，灰=上下文）

#### 右侧事件时间线
- 按时间顺序展示全部 `TraceEvent`
- 层级 0/1 事件（节点级）默认展开，层级 2/3（LLM/工具）默认折叠
- 点击可展开查看完整内容：
  - LLM 事件：展开显示完整的 system/user prompt 和 AI 响应（JSON 格式化 + 等宽字体）
  - Tool 事件：展开显示工具入参和返回内容
- 支持过滤：按节点名、事件类型（llm/tool/node）、关键词搜索

#### 底部 Token 汇总栏
- 固定底栏，显示总 token 消耗和按节点/Agent 的分解
- 横向堆叠柱状图（纯 CSS/div 实现）

---

## 8. 目录结构

```
services/agent/src/codeguard_agent/
├── observability/                    # ★ 新模块
│   ├── __init__.py
│   ├── models.py                     # TraceEvent / TokenUsage / TraceReport / TraceSummary / NodeStats
│   ├── collector.py                  # _TraceCollector: 事件聚合 + asyncio.run 包装 + State diff 计算
│   ├── dashboard.py                  # render_dashboard() / render_dashboard_file() / _load_template()
│   └── dashboard_template.html       # HTML 模板（内联 CSS/JS，~500-800 行）
├── pipeline/
│   ├── orchestrator.py               # + trace_enabled / trace_dir 参数，<10 行改动
│   └── graph.py                      # 不改动（图本身不变）
├── cli.py                            # + --trace 开关
└── config.py                         # + trace_enabled / trace_dir / trace_max_llm_content 三个 Settings 字段
```

---

## 9. 错误处理

1. **采集失败不影响审查**：`_TraceCollector` 内全部 `try/except`，任何一个事件处理出错只记录 `error` 类型事件，不抛异常
2. **HTML 写入失败不影响审查**：文件写失败只打 `logger.warning`，审查结果正常返回
3. **trace 目录不存在时自动创建**：`Path.mkdir(parents=True, exist_ok=True)`
4. **`asyncio.run()` 异常**：捕获后降级为无 tracing 的 `graph.invoke()`，输出警告

---

## 10. 测试策略

1. **模型单元测试**：`tests/test_observability.py`
   - `TraceEvent` / `TraceReport` 序列化/反序列化
   - `TokenUsage` 聚合逻辑
   - State diff 计算纯函数
2. **Collector 单元测试**：mock 事件 dict，验证 `_TraceCollector` 正确处理各 `event_type`
3. **Dashboard 生成测试**：用 mock report 跑 `render_dashboard()`，验证产出合法的 HTML（含 `__TRACE_DATA__` 被替换、`</html>` 闭合）
4. **端到端集成测试**：`provider=mock` + `trace_enabled=True` 跑一次完整审查，验证：
   - `trace/<run_id>.html` 文件生成
   - 文件中包含预期的事件数量
   - `ReviewResult` 与无 trace 时一致（坐实零副作用）
5. **不新增 evals 评测指标**：本模块是开发工具，不衡量审查质量

---

## 11. 限制与后续

### 首版不做

- **WebSocket/SSE 实时推送**：采用事后回看模式，架构简单
- **多次运行对比**：首版只支持单次运行查看
- **checkpoint + trace 共存测试**：两者同时开启的测试覆盖留待后续
- **LLM 输出截断配置的 Dashboard 端调整**：截断只在采集时生效
- **ReAct 降级事件**：`ToolAgentEngine.review` 内的手动降级路径无法被 `astream_events` 直接捕获，需要在引擎内注入 tracer 调用（hack），首版可能在降级处打一个简单的 warning 事件

### 后续可扩展

- 历史运行列表对比
- 按 `--runs N` 的多次运行方差分析
- 支持 checkpoint mode 的 step-by-step 回放
- 与 evals 评测指标的联动（点击某个漏报/误报跳转到对应事件）

---

## 12. 审批记录

| 日期 | 审批者 | 决策 |
|------|--------|------|
| 2026-07-09 | — | 待审批 |
