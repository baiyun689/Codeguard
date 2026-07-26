# 举证批量分析与 Trace 可观测性改造大纲

## 背景与问题

当前 EvidencePlanner 会为每个候选一次性规划 counter、support、severity
证据请求；EvidenceAgent 再把每个请求拥有的事实展平成
`(EvidenceRequest, Fact)`，逐事实调用一次 LLM。最近一次真实审查中，19
个候选产生 68 个证据请求、404 条事实判断，EvidenceAgent 耗时
202.245 秒，占 287.695 秒总耗时的 70.3%。

同时，HTML Trace 只把 LangChain 外层事件流中的 `tool_start/tool_end`
渲染为工具步骤。发现者内部的同步 ReAct `agent.invoke()` 和
EvidenceAgent 直接执行的 Gateway 调用没有进入该事件流，因此实际发生的
工具调用只能从节点输出中的 `gathered_context/council_trace` 间接查找，
页面计数错误地显示为 0。

## 目标

- 保留 EvidencePlanner 的完整证据覆盖和每个 `EvidenceRequest` 的审计身份。
- 将 LLM 分析单位从“每条事实”提升为“每个证据请求”，一次综合请求内的全部
  相关事实。
- 对模型返回执行严格的 `evidence_id` 对齐；缺失、重复、越权或非法输出安全
  降级为 `insufficient`。
- 只向请求提供候选变更符号、所属类型以及策略明确要求的局部事实，避免把 task
  中全部 `symbol_context` 重复交给每个请求。
- Trace 统一展示发现者和 EvidenceAgent 的工具名称、入参、输出、耗时、复用与
  失败，不依赖嵌套 LangChain 是否传播工具事件。
- Trace 明确区分 EvidencePlanner、工具收集和证据 LLM 分析耗时。

## 非目标

- 本期不削减 counter/support/severity 三类证据目的，也不把每候选请求上限从
  4 调低。
- 不修改 Java Gateway 的事实语义，不允许 Gateway 判断“是不是问题”。
- 不引入第二套 EvidenceAgent 或长期新旧路径开关。
- 不改变 `EvidenceRequest`、`EvidenceNote`、CouncilJudge 对外编排顺序。

## 设计

### 1. 请求级批量分析

EvidenceAgent 继续执行四个确定性阶段：

1. 校验请求与策略绑定，组装 request work。
2. 跨请求规范化、去重并并发执行缺失的 Gateway 工具调用。
3. 将共享工具结果按请求作用域回填并去重事实。
4. 以 request work 为并发单位批量分析事实，稳定组装一条
   `EvidenceNote`。

批量分析输入包含候选主张、证据目的、策略问题和带稳定
`evidence_id/source/raw/limitation` 的事实数组。输出为 findings 数组，每个
finding 必须引用输入中的一个 `evidence_id`。

确定性事实在调用 LLM 前处理：

- 已有 prior finding 直接复用。
- 带 `partial/unknown/timeout/unresolved` limitation 的事实直接成为
  `insufficient`。
- 已有可安全判定的注解反证规则继续确定性执行。

仅把剩余事实一次性发送给 LLM。输出按输入事实顺序恢复；模型漏掉的事实生成
`analysis_missing_evidence`，未知 ID、重复 ID 和非法结构被忽略并写 Trace。
整次调用失败时，该请求所有待分析事实统一降级为 `analyst_error`。

### 2. 事实范围

`symbol_context` 以候选所在行和稳定 `symbol_id` 为边界：

- 保留覆盖候选行的最内层方法、构造器或字段。
- 保留上述符号的所属类型。
- 没有可定位局部符号时，保守保留文件级最小上下文并标记限制。
- 上游、框架入口、安全路径、结构指标等事实只由声明相应能力的策略工具补充。

task patch 仍作为当前实现的地面事实，每个请求最多保留一次。事实排序保持
`task_patch → local symbol → owner type → tool result`，确保输出可重放。

### 3. Trace 工具事件

新增与 LangChain 无关的应用级工具调用记录，字段至少包含：

- owner/node/reviewer
- tool name
- canonical arguments
- output 或 error
- start/end/duration
- status：`complete/failed/reused`
- reuse key 与首次调用 ID

发现者从 ReAct 返回消息中已经能够恢复工具名、参数和输出；在 reviewer 节点
完成时把这些结构化记录写入 Trace。EvidenceAgent 使用其唯一调用缓存和
`council_trace` 生成同一信封，复用项不伪造 Gateway 执行耗时。

Trace view model 将应用级记录规范化为现有 tool step，页面继续使用统一的
“工具入参/工具输出”卡片。原生 `tool_start/tool_end` 仍兼容，但相同规范键只
展示一次。

### 4. Evidence 阶段指标

EvidenceAgent 输出/Trace 增加以下确定性指标：

- request_count
- fact_count
- llm_analysis_calls
- tool_unique_calls
- tool_reused_calls
- tool_collection_ms
- fact_analysis_ms

主流程时间线以真实节点边界为准，不把 EvidenceAgent 耗时标到
EvidencePlanner。

## TDD 接缝与验收

### `collect_evidence(...)`

- 一个请求包含多条可分析事实时，外部 LLM 只调用一次，并仍返回按事实 ID
  对齐的多个 finding。
- 两个请求并行批量分析，EvidenceNote 顺序保持请求顺序。
- 模型漏掉、重复或伪造 evidence ID 时安全降级且留下结构化 Trace。
- partial/unknown 工具事实不进入 LLM，最终保持 `insufficient`。
- 与现有工具去重、复用、purpose/strategy 校验和 Judge 证据门槛兼容。

### `TraceReport → build_trace_view/render_dashboard`

- 即使没有原生 `tool_start/tool_end`，应用级发现者工具记录仍生成工具卡片。
- 每个审查员显示正确调用次数、逐次参数和输出。
- EvidenceAgent 显示新调用、复用、失败及阶段指标。
- 原生与应用级记录重复时只展示一次。

### 性能验收

使用最近一次规模作为回归基准：

- 19 候选、68 请求不再产生 404 次 LLM 调用。
- 请求级实现的 LLM 调用上限为 68；无待分析事实的请求不调用 LLM。
- 工具调用次数和 EvidenceRequest 数量不因批量化而丢失。
- Trace 能直接解释 EvidenceAgent 的工具与 LLM 耗时构成。

## 交付顺序

1. 请求级批量分析及契约测试。
2. `symbol_context` 局部事实选择及回归测试。
3. 应用级工具 Trace 与阶段指标。
4. 定向测试、完整 pytest、ruff、mypy。
5. 按 `code-review` 规范审查并修复后提交。
