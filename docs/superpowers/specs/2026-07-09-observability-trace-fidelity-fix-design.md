# Codeguard 可观测性追踪保真修复设计

**日期**：2026-07-09
**状态**：待实现
**关联设计**：`2026-07-09-observability-trace-module-design.md`

## 1. 问题与根因

当前 Dashboard 能生成 HTML，但无法展示审查过程的真实输入输出。问题不在样式层，而在事件采集契约：

1. 采集器只保存节点的 `input_keys` / `output_keys`，没有保存字段值。
2. 用全局 `_node_stack` 推断层级，无法处理 ReviewCouncil 的并行 fan-out，三个并行发现者被错误显示为逐层嵌套。
3. 仅凭 `metadata.langgraph_node` 判断节点事件，把 `name="LangGraph"` 的子图包装事件重复记为业务节点。
4. 当前 LangChain 的 `on_chat_model_start.data.input` 可以是消息列表，而非 `{"messages": ...}`，现有解析器因此产生空消息。
5. 结构化输出通常位于 `AIMessage.tool_calls`，`content` 可以合法为空；现有采集器只读取 `content`，丢失了真实模型输出。
6. 工具入参被压缩成摘要，LLM 与工具内容被硬截断到 3000 字符，且 `CODEGUARD_TRACE_MAX_LLM_CONTENT` 没有接入。
7. Dashboard 没有节点输入输出的渲染分支，即使后端补齐数据也只会显示“无详情”。
8. 测试只验证 HTML 文件和事件类型存在，没有验证内容保真、并行归属或 ReAct 步骤。

## 2. 目标与边界

### 2.1 目标

- 完整保存每个 LangGraph 节点实例的输入、输出和本次节点写回的字段。
- 正确呈现并行节点、子图节点以及 ReAct 内部 `model` / `tools` 等步骤。
- 完整保存每次 LLM 调用的消息、内容、工具调用决策、响应元数据和 token。
- 完整保存每次工具调用的入参与返回值。
- Dashboard 能展示结构化字段，同时保留原始 JSON 作为无损兜底。
- 默认不截断、不脱敏；生成文件是包含源码和模型上下文的本地敏感产物。
- 追踪失败不得改变审查结果或阻断审查流程。

### 2.2 本次不做

- 不新增实时 WebSocket/SSE。
- 不做多次运行对比。
- 不把 LLM 或“是不是问题”的判断移入 Java Gateway。
- 不改 ReviewCouncil 业务拓扑和最终 `ReviewResult` / `Issue` 产品契约。
- 不手工侵入每一个业务节点注入日志；优先使用原生 `astream_events(version="v2")`。

## 3. 方案选择

采用“事件原生血缘 + 无损序列化 + 通用详情渲染”。

未采用的方案：

- **局部修补消息解析器**：能补回部分 LLM 文本，但不能解决并行层级、重复节点和节点 State 缺失。
- **为每个业务节点手工埋点**：可控但侵入面大，新增节点时容易漏埋点，也违背独立可观测模块的目标。

## 4. 数据模型

`TraceEvent` 增加带默认值的血缘字段，保持已有构造调用兼容：

- `run_id`：原生事件运行标识。
- `parent_ids`：原生父运行链。
- `parent_run_id`：直接父运行标识。
- `node_path`：稳定、可读的逻辑路径，例如 `discover_threat_model/review/model`。
- `invocation_id`：节点实例标识，用于区分三个审查员各自的 `prepare/review/collect`。

`NodeStats` 按节点实例记录，而不是只按 `node_name` 合并。它包含实例标识、逻辑路径、层级和父节点信息。Token 汇总仍可按逻辑路径或所属审查员聚合。

所有新增字段提供默认值，已存在的追踪报告和测试数据仍可加载。

## 5. 采集与归属

### 5.1 节点识别

只有同时满足以下条件的 chain 事件才作为节点生命周期事件：

- `metadata.langgraph_node` 存在；
- `event.name == metadata.langgraph_node`。

这样可以排除继承了外层 metadata 的 `name="LangGraph"` 包装事件和条件函数事件。

### 5.2 并行与层级

- 生命周期以 `run_id` 为键保存，禁止使用全局栈。
- 通过 `parent_ids` 找到最近的已知节点实例，建立父子关系。
- 外层业务节点深度为 0；发现者子图的 `prepare/review/collect` 为 1；ReAct 图内部节点为 2 或更深。
- 相同名字的不同实例分别展示，例如三个 `prepare` 不再合并成一个统计节点。
- LLM/工具事件归属于其 `parent_ids` 中最近的节点；无法找到时再使用 metadata 作为兜底，并显式标记为未解析归属。

### 5.3 事件内容

- `node_start.detail.input`：完整节点输入。
- `node_end.detail.input`：完整节点输入快照。
- `node_end.detail.output`：完整节点输出；LangGraph 节点输出本身即本节点写回的 State 字段。
- `llm_start.detail.messages`：支持消息对象、消息元组、单批/多批嵌套列表。
- `llm_end.detail.response`：保存完整消息结构，包括 `content`、`tool_calls`、`invalid_tool_calls`、`additional_kwargs`、`response_metadata` 和 `usage_metadata`。
- `tool_start.detail.input` / `tool_end.detail.output`：保存完整值，不再摘要化。
- 每个事件额外保留必要的原生 metadata，便于诊断未来 LangChain/LangGraph 版本变化。

## 6. 无损序列化

新增单一序列化入口，将运行时对象转成 JSON 可表示值：

- Pydantic 模型使用 `model_dump(mode="json")`。
- dataclass 使用字段递归转换。
- Enum 保存其值。
- Mapping、list、tuple、set 递归转换。
- LangChain 消息优先使用自身模型序列化，以保留工具调用和元数据。
- 不可识别对象回退为带类型名的字符串表示，单个字段失败不会丢弃整个事件。
- 检测循环引用和最大递归深度，异常转成可见诊断值。

默认完整保存。`CODEGUARD_TRACE_MAX_LLM_CONTENT=0` 表示不截断；大于 0 时才对 LLM 文本字段应用显式截断，并在数据中标记原长度和已截断状态。节点 State 与工具结果本次始终完整保存。

## 7. HTML 嵌入与 Dashboard

### 7.1 安全嵌入

完整源码可能包含 `</script>`。JSON 嵌入 HTML 前必须转义 `<`、`>`、`&` 和 Unicode 行分隔符，确保内容不会提前闭合 `<script type="application/json">`，浏览器解析后仍恢复原值。

### 7.2 展示

- 时间线按事件发生顺序展示，节点实例使用 `node_path` 标识。
- 节点开始/结束事件展示完整 input/output。
- LLM 调用按消息角色展示，并单独展示工具调用决策、token 和响应元数据。
- 工具事件展示完整入参与返回值。
- 所有事件都提供“结构化详情”和“原始 JSON”两个折叠区；专用渲染器未识别的新字段仍能在原始 JSON 中看到。
- 拓扑区域按实际节点实例生成，不再依赖固定节点名列表；可按所属审查员分组。
- 搜索覆盖摘要、路径和完整 detail。

纯静态、自包含、`file://` 可打开的约束保持不变。

## 8. 错误处理

- 单个值序列化失败：在对应字段写入错误描述，继续采集。
- 单个事件处理失败：生成 `error` 事件，继续消费事件流。
- 追踪执行失败：保持现有降级策略，执行一次无追踪审查。
- HTML 写入失败：只记录警告，正常返回审查结果。
- 不允许因“未捕获最终 State”而静默执行第二次审查；最终 State 捕获需要有独立回归测试。

## 9. 测试策略

先写失败测试，再改生产代码：

1. 三个并行节点的深度都为 0，且 `LangGraph` 包装事件不产生重复节点。
2. 三个同名 `prepare` 分属三个发现者路径和三个实例。
3. 节点输入输出包含真实字段值，而非只有键名。
4. 列表形式、嵌套批次形式的 LLM 输入都能完整序列化。
5. `AIMessageChunk.tool_calls` 在 `content=""` 时仍完整显示。
6. 超过 3000 字符的 LLM 和工具内容默认不截断。
7. `</script>` 等源码内容不会破坏 HTML，解析后值保持一致。
8. Dashboard 对 node/LLM/tool/未知事件都能呈现详情或原始 JSON。
9. fake `astream_events` 端到端测试覆盖事件采集、报告生成和最终 State，只执行图一次。
10. 全量 pytest、ruff 和生成 HTML 的浏览器级/DOM 级验证通过。

## 10. 验收标准

一次真实工具审查生成的 HTML 中，用户可以：

- 看到三个发现者并行执行，而不是伪造的多层嵌套；
- 进入任意节点查看所有输入字段值和输出字段值；
- 沿某个发现者路径查看其每次 ReAct model/tool 步骤；
- 查看每次 LLM 的完整消息和结构化工具调用决策；
- 查看每次 Java 工具调用的完整参数与完整返回；
- 搜索任意源码片段并定位到包含它的事件；
- 查看精确 token 汇总；
- 在任何专用视图缺失时，从原始 JSON 找到全部已采集内容。
