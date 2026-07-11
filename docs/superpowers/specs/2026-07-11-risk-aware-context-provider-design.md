# 风险感知 ContextProvider(阶段 3)设计

**日期**: 2026-07-11
**状态**: 待实施
**前置阶段**: Phase 1(状态契约与拓扑)、Phase 2(风险标签规则与任务排序)已完成
**关联**: ADR-038(风险路由驱动的 ReviewTask 编排)、Phase 2 设计文档
`docs/superpowers/specs/2026-07-10-risk-triage-phase2-design.md`、ADR-036(ContextProvider AST 富化)

## 1. 背景与目标

Phase 1/2 已经把 `ReviewTask → RiskProfile → TaskSelection` 链路钉死,每个选中任务带有
`RiskProfile.tag_scores`(23 个具体 `RiskTag` + `GENERAL_REVIEW`)。但当前
`_context_provider_node` 只为每个选中任务建一个**空的** `TaskContextBundle(task_id=tid)`
——`facts` 恒为空列表,注释明确写着"按 RiskTag 定向填充留到 Phase 3"。

Phase 3 的目标:让 `_context_provider_node` 按每个任务的 `RiskProfile.tag_scores`,
用规则(不调 LLM)选择性调用现有 Java 工具,把结果分门别类填进该任务的
`TaskContextBundle.facts`。AST 只作为事实来源之一,不产出新的风险判断
(与 Phase 2 设计文档 §1 的边界一致)。

## 2. 边界原则

- **ContextProvider(确定性,0 token)**:只回答"给定这个 `RiskTag`,不需要推理判断
  '该读哪',就能确定'该看什么'"这一类事实——查表式必读项。
- **三路发现者 Agent(ReAct,消耗 token)**:保留"要不要再深挖、去哪读"的自主判断权。
  ContextProvider 不替它做"是不是问题相关"的判断,否则会变成一个隐藏的第四发现者,
  违反 ADR-032 决策 5 的边界。
- 预取的价值是省掉审查员重复走"看看有没有敏感 API"这类确定性步骤的 ReAct 轮次
  (呼应 ADR-036 的既有动机),不是替审查员下结论。

## 3. 两层事实来源

### Level 0:零新增调用,纯切片复用

`context_provider` 已经对整份 diff 调用一次 `find_sensitive_apis()`(结果含文件+行号)、
逐文件调用 `get_diff_ast()`(已切成单文件块,含方法签名/可见性/注解/调用边)。这两份数据
本来就是全量拿回来的,只是从未按 task 切片分发。

Level 0 动作:按 `task.file` + `task.changed_lines`,把这两份全局结果重新切片,分发进
对应 `TaskContextBundle.facts`——不产生新的网络调用,纯 Python 侧过滤。

覆盖:`AUTHORIZATION`/`AUTHENTICATION_SESSION`(AST 切片自带注解)、
`INJECTION`/`SQL_DATA_ACCESS`/`CONFIG_SECURITY`/`FILE_PATH_IO`/`SSRF_OUTBOUND`/
`DATA_EXPOSURE`(命中 `find_sensitive_apis` 清单的标签)、以及所有标签共享的 AST 结构基线。

### Level 1:定向新增调用,按 tag 触发

两类标签需要 Level 0 切片给不出的信息,各对应一个已有 Java 工具:

- **behavior 类**(`RESOURCE_LIFECYCLE`/`API_CONTRACT`/`TRANSACTION_ATOMICITY`/
  `CONCURRENCY_CONSISTENCY`/`IDEMPOTENCY_RETRY`/`MESSAGE_DELIVERY`/`CACHE_CONSISTENCY`):
  关心"这个改动影响谁",调 `find_callers("文件#方法名")`。方法名从该 task 的 AST 切片
  解析(已结构化,不用正则猜 diff 文本)。
- **maintainability 类**(`COMPLEXITY_CONTROL_FLOW`/`DUPLICATION_DESIGN`/
  `OBSERVABILITY_TESTABILITY`):调 `get_code_metrics(task.file)`。

`GENERAL_REVIEW` 只拿 Level 0 基线,不触发 Level 1(未知风险没有方向可定向)。

### 为什么不做"完整调用图"

`get_diff_ast` 的出边是未解析的裸文本(不知道 callee 定义在哪),`find_callers` 是简单名
匹配(不做全限定解析)。要拼出精确的调用图需要 SymbolSolver + classpath 配置——这条路
ADR-012 做 `get_repo_map` 时已经放弃过。真要做"完整调用图"性质上滑向 ROADMAP 阶段 3
待办清单里明确标为"暂缓"的 `get_method_definition`/`get_call_graph`,不是阶段 3 该做的事,
本次不引入新 Java 工具、不做 diff 内部调用图缝合增强。

## 4. 模块设计

### `pipeline/context_rules.py`(新增)

- `ContextStrategy`:声明某个 `RiskTag` 需要哪种 Level1 调用(`find_callers` 还是
  `get_code_metrics`)。
- `TAG_CONTEXT_STRATEGIES: dict[RiskTag, list[ContextStrategy]]`:23 个具体标签逐一定表,
  结构对称 `risk_rules/` 目录已有的 tag→reviewer 路由表。
- 纯函数 `plan_context_calls(tasks, risk_profiles) -> ContextPlan`:只计算"要调什么",
  不实际调用 `tool_client`,可单测。输出:
  - 每个 task 的 Level0 切片范围(file + changed_lines 过滤条件)。
  - 去重后的 Level1 调用清单:`{(file, method_or_none): {level, task_ids}}`。
  - 方法名从该 task 已切好的 AST 块里解析;解析不到就在计划里标
    `skip(reason="no_method_resolved")`,不生成调用、不做正则兜底猜测。

### `pipeline/concurrency.py`(新增)

- `run_bounded_parallel(items, fn, max_workers=8) -> list`:有界 `ThreadPoolExecutor` +
  逐项 try/except 隔离(单项失败不影响其它项)+ 按输入顺序回收结果。
- Level1 调用是第一个使用者:去重后的调用清单通过它并发派发到 Java Gateway。
- 并发度硬编码为 8,不新增环境变量(与项目现有其它并行点风格一致,避免过度工程)。
- **不是**为后续阶段的 async 迁移预留双接口。若阶段 4/5 审查员内部并发演变为
  "3 路审查员 × 每路数十个 task"的二维 fan-out 且需要全局限流,那是 ROADMAP 早已
  预登记的 async 切换时机,届时另行设计,不在本次预支。

### `_context_provider_node` 改动(`pipeline/graph.py`)

1. 保留现有 `ContextProviderStage` 逻辑,产出全局 `context_bundle`(阶段 4 前仍是审查员
   主要读取源,不动)。
2. 调 `plan_context_calls(tasks, risk_profiles)` 拿计划。
3. 用 `run_bounded_parallel` 执行计划里的 Level1 调用(才碰 `tool_client`)。
4. 把 Level0 切片 + Level1 结果一起装进对应 `TaskContextBundle`,写入
   `state["task_context_bundles"]`。
5. 每个 task 落一条 `council_trace`。

## 5. 预算与容错

- 复用 `ReviewBudget.max_context_chars_per_task`(现有字段,此前从未启用):每个 task 的
  facts 汇总后按此预算截断,标 `truncated=True`。
- Level1 工具调用失败/超时:该项 fact 缺省,不阻断整条链路(沿用项目一贯的 None/异常
  防御风格)。
- 方法名解析不到:跳过 `find_callers`,只记 trace,不做正则猜测兜底。

## 6. 可观测性

每个 task 一条 `CouncilTrace(node="context_provider", event="task_bundle_filled", ...)`,
包含 fact 数量、来源、触发的 Level1 调用、跳过原因——呼应 ADR-023"工具有没有用上要能
从报告看出"的教训,为阶段 6 Dashboard 预埋数据。

## 7. 验证范围

只做工程正确性,不跑真实 eval 质量对比:

- `tests/test_context_rules.py`:
  - Level0 切片正确性(task.file 匹配、changed_lines 范围过滤)。
  - Level1 策略去重(同文件多 task 命中同 tag → 只生成一次调用 key)。
  - 方法名解析不到 → 生成 skip 而非调用。
  - `GENERAL_REVIEW` 只拿 Level0,不触发 Level1。
  - 预算截断。
- `tests/test_concurrency.py`:`run_bounded_parallel` 的并发上限、单项失败隔离、结果顺序。
- `test_graph_orchestration.py` 补充:`_context_provider_node` 产出的
  `task_context_bundles` 非空、trace 含预期事件。

不跑 eval 质量对比的原因:`task_context_bundles` 本阶段还没有被发现者 Agent 消费
(接入 prompt 是阶段 4 的事),此时测质量测不出信号,留待阶段 4 一并验证
(与 ADR-004/008/009 一贯的"测不出就不硬凑"原则一致)。

## 8. 明确不做的事

- 不引入新 Java 工具。
- 不做 diff 内部调用图缝合增强(用户已确认暂缓)。
- 不把 `task_context_bundles` 接入 reviewer prompt(阶段 4 的事)。
- 不做任何"这条 fact 更重要"的 LLM 判断。
- 不设计审查员内部并发/高风险进 Agent 循环-低风险单次 LLM 调用的分级路由
  (阶段 4/5 单独讨论,届时判断是否触发 ROADMAP 登记的 async 切换时机)。

## 9. 给阶段 4 的接缝

- `task_context_bundles` 字段已填充真实数据,阶段 4 只需要在发现者 `_prepare` 节点里
  把 `state.get("context_bundle")`(全局)替换/补充为该发现者负责的 task 对应的
  `task_context_bundles[task_id]`(任务级),并调整工具策略。
- `run_bounded_parallel` 可能被阶段 4/5 复用,也可能被 async 方案替换,取决于届时的
  并发形状判断。
