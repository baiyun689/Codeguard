# Large Diff Degradation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 大 diff 只对风险最高的有限 task 做有界摘要、上下文和发现审查，并在产品 summary 中明确部分覆盖。

**Architecture:** 新增纯函数策略模块，输入原始 diff、任务和用户预算，返回一次不可变决策；图节点重复派生该决策，不新增 ReviewState。Java 不参与风险降级，只保留 webhook 限流与执行护栏。

**Tech Stack:** Python 3.11+、Pydantic、LangGraph、pytest；Java 21、JUnit 5。

## Global Constraints

- 大 diff：行数超过 5000 或 task 数超过 50。
- 有效预算取用户预算与 20 tasks、3 tasks/file、2000 context chars/task 的较小值。
- 选中 diff 最多 60000 字符，单 task patch 最多 12000 字符。
- 不新增 ReviewState 或 ReviewResult 字段，不新增 Java 工具。

---

### Task 1: 纯策略模块

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/large_diff_policy.py`
- Create: `services/agent/tests/test_large_diff_policy.py`

**Interfaces:**
- Produces: `plan_large_diff(diff_text, tasks, configured_budget) -> LargeDiffPlan`
- Produces: `LargeDiffPlan.selected_diff(tasks, selection)`, `scoped_patch(patch)`, `coverage_notice(selection)`

- [x] 写失败测试：阈值边界、预算取较小值、稳定选中 diff、字符截断和覆盖提示。
- [x] 运行 `pytest tests/test_large_diff_policy.py -q`，确认因模块缺失失败。
- [x] 实现不可变 `LargeDiffPlan` 与唯一工厂函数。
- [x] 重跑单文件测试并保持通过。

### Task 2: 接入审查图

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py`
- Modify: `services/agent/src/codeguard_agent/pipeline/stages/context_provider.py`
- Test: `services/agent/tests/test_graph_orchestration.py`

**Interfaces:**
- Consumes: Task 1 的 `LargeDiffPlan`。
- Produces: `TaskRank` 动态预算、选中范围 Summary/Context、单 task patch 上限及确定性部分覆盖 summary。

- [x] 写失败测试：大 diff 只摘要选中 patch、Context AST 不接收未选中 patch、单 task 输入有界、最终 summary 有覆盖提示。
- [x] 运行目标测试，确认现有全量输入行为导致失败。
- [x] 将可选 Summary 移到 TaskRank 后，并在 Summary、Context、reviewer、Judge 节点消费同一派生策略。
- [x] 重跑 graph、task、context 测试并保持通过。

### Task 3: 清理 Java 重复策略并交付

**Files:**
- Modify: `services/gateway/src/main/java/com/codeguard/ci/guard/ReviewGuard.java`
- Modify: `services/gateway/src/main/java/com/codeguard/toolserver/GatewaySettings.java`
- Modify: `services/gateway/src/main/java/com/codeguard/toolserver/ToolServerApp.java`
- Modify: `.env.example`, `docker-compose.yml`, `AGENTS.md`, `README.md`, `DECISIONS.md`
- Test: Java guard/settings tests

**Interfaces:**
- ReviewGuard 只暴露 `tryAcquireWebhook(timeoutMs)`；大 diff 策略只有 Python 一个所有者。

- [x] 删除 Java 未接入的 diff 阈值、降级 JSON 与配置。
- [x] 更新测试和运维说明，记录 Python 策略阈值及部分覆盖语义。
- [x] 运行 Python pytest/ruff/mypy 与 Java `mvn verify`。
- [x] 自审 diff，提交 `feat(pipeline): 增加大diff风险降级审查`。

## 落地复盘

- **理解**：大 diff 的主要成本来自 Python 智能链，Java 按行数直接跳过既利用不了风险画像，
  也会把“未审查”伪装为结果；范围决策应紧跟 TaskRank，并成为后续节点的共同输入边界。
- **效果**：超过阈值后只保留风险最高的 20/3 任务范围，Summary、AST 和发现者输入均有界，
  最终结果明确披露覆盖缺口；正常 diff 的预算和输出语义保持不变。`pipeline-notools`
  mock eval 已跑通 31 条用例并存档；mock 不调用审查 LLM，零指标只证明评测链路可执行，
  不作为检出质量结论。
- **踩坑**：真实 diff 常以换行结尾，不能用 `count("\\n") + 1` 统计行数；发现者原 prompt
  还会在 task patch 与风险块中重复附加同一 patch，需在大 diff 模式确定性去重。
- **下一步**：先用 trace/eval 观察 selected/skipped、耗时与检出率，再决定是否将固定阈值移入
  Python Settings；在没有数据前不增加自适应模型或 Java 配置。
