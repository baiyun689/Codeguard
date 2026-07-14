# LangSmith 测试追踪设计

## 目标

在不继续维护本地 HTML Trace 的前提下，使用 LangSmith 对 Codeguard 当前五阶段 LangGraph 审查链路做完整测试追踪；默认关闭旧 Trace，避免它与 LangSmith 同时执行。

## 决策

1. 采用 LangSmith 原生自动追踪，不新增 Codeguard 专用 observability backend、包装器或自定义 span。
2. 保持 `PipelineOrchestrator` 对 `graph.invoke()` 的正常调用。当前运行环境已安装 LangSmith，且 LangGraph/LangChain 会在 `LANGSMITH_TRACING=true` 时自动上报图节点、LLM 调用、LangChain 工具调用、输入输出、耗时和错误。
3. 将 `Settings.trace_enabled` 和 `CODEGUARD_TRACE_ENABLED` 的默认值从 `true` 改为 `false`。用户仍可通过 `--trace` 或 `CODEGUARD_TRACE_ENABLED=true` 显式生成历史本地 HTML Trace。
4. 不修改 `observability/`、`PipelineOrchestrator`、产品模型或 Phase 5 `CouncilRunStats`。本次不把候选/证据/裁决语义事件转换为 LangSmith feedback 或自定义 Dashboard 指标。
5. 测试项目允许上传完整内容，不配置 LangSmith 输入、输出或 metadata 脱敏。运行者自行通过标准环境变量设置 `LANGSMITH_TRACING`、`LANGSMITH_API_KEY` 和 `LANGSMITH_PROJECT`。

## 配置与运行

```powershell
$env:LANGSMITH_TRACING="true"
$env:LANGSMITH_API_KEY="<LangSmith API key>"
$env:LANGSMITH_PROJECT="codeguard-phase5-test"

conda run -n codeguard --no-capture-output python -m codeguard_agent review --repo <repo> --mode pipeline
```

旧 Trace 默认关闭，因此上述命令不会生成 `trace/*.html`。如需临时对照旧页面，可追加 `--trace`；这会同时运行本地采集器与 LangSmith，故只用于短期诊断。

## 验证

1. Settings 单测确认缺省值为 `trace_enabled=False`，显式环境变量和 CLI `--trace` 仍能开启本地追踪。
2. 全量 Agent pytest、Ruff、mypy 通过。
3. 使用真实 LangSmith key 和真实非 mock LLM 运行一次审查，确认 LangSmith 项目中出现包含 LangGraph 节点与 LLM 子 run 的 trace。此 smoke test 依赖用户的外部凭据，不纳入离线 pytest。

## 边界

- 不提交 LangSmith key，不新增 `.env` 中的真实密钥。
- 不删除旧 Trace，保留短期回退和对照能力。
- 不宣称 LangSmith 已替代定制 Dashboard 的 Phase 5 语义指标；若测试后确有需求，另起 change 实现 feedback/custom spans。
