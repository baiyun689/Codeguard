# Phase 2 风险标签规则与任务排序设计

**日期**: 2026-07-10  
**状态**: 设计草案  
**前置阶段**: Phase 1 状态契约与最终拓扑已完成  
**关联设计**: [风险路由 ReviewTask 编排设计](./2026-07-10-risk-routed-review-orchestration-design.md)

## 1. 目标

Phase 2 完成风险标签规则和大 diff 任务排序，让每个 `ReviewTask` 在进入 ReviewCouncil
前拥有可解释的审查方向和优先级。

Phase 2 的风险标签是**高召回路由信号**，不是漏洞结论。规则只回答“这个任务值得从哪个
角度审查”，不回答“这里一定存在问题”。真假判断由 ContextProvider、三路审查员、
EvidenceAgent 和 CouncilJudge 后续完成。

Phase 2 固定以下主链路，不增加 State 字段、不改变图拓扑:

```text
ReviewTask
  → path/text/diff-deletion signals
  → RiskProfile
  → TaskSelection
  → ContextProvider
```

AST 风险信号不在 Phase 2 产生。后续 AST 作为 ContextProvider 的事实来源，补充完整方法、
注解、调用关系和上下文事实；它不改变 Phase 2 的风险标签契约。

## 2. 非目标

- 不调用 LLM 进行风险分类。
- 不读取完整仓库文件，不调用 Java AST 或调用图工具。
- 不生成 `Issue`、`CandidateIssue` 或漏洞结论。
- 不改变 `ReviewState`、`ReviewTask`、`RiskProfile`、`TaskSelection` 的字段形状。
- 不在 Phase 2 让 reviewer 按风险标签改变 fan-out；路由元数据在本阶段固定，实际定向
  fan-out 留给 Phase 4。

## 3. 规则引擎边界

### 3.1 规则接口

风险规则是纯函数，只消费一个 `ReviewTask`:

```python
RiskRule = Callable[[ReviewTask], list[RiskSignal]]
```

规则可以使用:

- `task.file`: 文件路径、扩展名、目录和文件名。
- `task.patch`: hunk 或文件级 fallback 的原始 diff。
- `task.hunk_header`: hunk 行号范围。
- `task.changed_lines`: 当前文件中新增行号。

规则不能使用:

- 完整仓库文件内容。
- 跨文件调用关系。
- AST、调用图、RAG 或 LLM。
- 其他 task 的判断结果。

### 3.2 三类 Phase 2 信号

实现上可以共用同一个规则模块，但信号来源必须区分:

```text
path:<rule_id>             文件路径和文件角色
text:<rule_id>             新增/修改代码中的典型结构或 API
diff_deletion:<rule_id>    被删除的保护、校验、事务或错误处理
```

删除行为不作为独立模块，但必须作为独立来源保留。因为同一段代码被新增、保留或删除
具有不同的路由含义。

### 3.3 RiskSignal 语义

```python
RiskSignal(
    tag=RiskTag.AUTHORIZATION,
    score=3,
    source="diff_deletion:authorization_guard_removed",
    reason="删除 UserController.update() 上的 @PreAuthorize 注解",
    line=42,
)
```

- `source` 是稳定的机器标识，格式为 `<来源类别>:<规则编号>`。
- `reason` 由规则用固定模板和匹配结果生成，不由 LLM 生成。
- `score` 表示路由优先级，不表示漏洞真实性。
- `line` 是可选的变更行定位；无法定位时保持为空。

### 3.4 分数和聚合

单条信号分级:

```text
1 分: 结构相关，值得从该方向看一眼
2 分: 明确涉及控制流、数据流、资源生命周期或一致性
3 分: 删除保护、暴露入口、危险 sink 或明显高风险变化
```

同一标签的信号分数累加，但单个标签最高为 5，避免长 hunk 中重复模式无限放大。
不同标签分别保留，不能把所有信号压成一个模糊的 `GENERAL_RISK`。

如果一个 task 完全没有具体信号，生成唯一的特殊兜底标签:

```text
GENERAL_REVIEW
  score: 1
  source: fallback:unclassified
  reason: 未命中已有风险规则，执行通用审查
```

`GENERAL_REVIEW` 不是漏洞类别，只表示规则未知。它只在 task 没有其他具体标签时生成。

## 4. 风险标签词典与审查员路由

审查员缩写:

```text
T = ThreatModelAgent
B = BehaviorAgent
M = MaintainabilityAgent
```

每个标签有一个 primary reviewer，可选 secondary reviewer。后续 Phase 4 根据标签集合
取 reviewer 并集；Phase 2 只固定元数据，不改变当前三路执行图。

| RiskTag | 唯一审查问题 | 路由 |
|---|---|---|
| `AUTHORIZATION` | 用户是否有权限执行操作 | T + B |
| `AUTHENTICATION_SESSION` | 身份、Token、Session 是否正确 | T + B |
| `WEB_SECURITY_CONFIG` | CORS、CSRF、`permitAll`、Actuator 是否暴露 | T |
| `INPUT_VALIDATION` | 外部输入是否校验和归一化 | T + B |
| `INJECTION` | 不可信输入是否进入解释器或危险 API | T + B |
| `SQL_DATA_ACCESS` | 查询条件、数据范围、ORM/Mapper 使用是否正确 | B |
| `FILE_PATH_IO` | 路径、文件访问和资源边界是否安全 | T + B |
| `SSRF_OUTBOUND` | 外部输入是否控制服务端请求目标 | T + B |
| `CONFIG_SECURITY` | 密钥、密码、Token 和敏感配置是否暴露 | T |
| `DATA_EXPOSURE` | 敏感数据是否被返回、记录或越权暴露 | T + B |
| `TRANSACTION_ATOMICITY` | 多步写操作是否具备正确事务边界 | B |
| `CONCURRENCY_CONSISTENCY` | 并发更新是否可能丢失、覆盖或读到错误状态 | B |
| `IDEMPOTENCY_RETRY` | 重试、重复请求和重复消费是否安全 | B |
| `CACHE_CONSISTENCY` | 缓存与数据库是否保持一致 | B |
| `MESSAGE_DELIVERY` | 消息 ack、retry、dead letter 是否正确 | B |
| `ERROR_HANDLING` | 异常是否被吞掉或错误转换 | B |
| `NULL_STATE_SAFETY` | 空值和状态缺失是否导致错误行为 | B |
| `RESOURCE_LIFECYCLE` | 连接、线程、文件、锁等资源是否正确释放 | B + M |
| `API_CONTRACT` | 入参、返回值和兼容性是否被破坏 | B + M |
| `PERFORMANCE` | 查询、循环、IO 和缓存是否引入明显性能风险 | B + M |
| `COMPLEXITY_CONTROL_FLOW` | 控制流是否变复杂且容易遗漏分支 | M |
| `DUPLICATION_DESIGN` | 是否引入重复逻辑或不合理设计 | M |
| `OBSERVABILITY_TESTABILITY` | 关键行为是否难以监控或测试 | M |
| `GENERAL_REVIEW` | 未命中已知规则，需要通用审查 | T + B + M |

标签之间的边界:

- `SQL_DATA_ACCESS` 关注数据访问正确性；`INJECTION` 关注不可信输入进入危险 sink。
- `TRANSACTION_ATOMICITY` 关注原子性；`CONCURRENCY_CONSISTENCY` 关注并发冲突；
  `IDEMPOTENCY_RETRY` 关注重复执行。
- `AUTHORIZATION` 关注访问决策；`AUTHENTICATION_SESSION` 关注身份建立和会话保持；
  `WEB_SECURITY_CONFIG` 关注全局 Web 安全配置。
- 不保留宽泛的 `MAINTAINABILITY` 作为路由标签，由 `COMPLEXITY_CONTROL_FLOW`、
  `DUPLICATION_DESIGN`、`OBSERVABILITY_TESTABILITY` 等细化标签替代。

## 5. Java/Spring 规则范围

规则优先覆盖以下模式，不把每个 API 变成一个新标签:

```text
AUTHORIZATION
  @PreAuthorize / @Secured / hasRole / 权限判断删除或弱化

INPUT_VALIDATION
  Controller 入参 / @Valid / @Validated / 校验注解和边界检查

INJECTION
  SQL、命令、模板、路径或外部 URL 中的不可信值拼接

SQL_DATA_ACCESS
  @Query / MyBatis / JdbcTemplate / Mapper / 查询条件和数据范围

TRANSACTION_ATOMICITY
  @Transactional / save / update / delete / commit / rollback

CONCURRENCY_CONSISTENCY
  共享状态写入 / 条件更新 / 锁 / synchronized / 乐观锁版本字段

IDEMPOTENCY_RETRY
  幂等键 / setnx / 去重查询 / retry / 重复提交保护

CACHE_CONSISTENCY
  @Cacheable / @CacheEvict / Redis key / DB 更新与缓存失效顺序

MESSAGE_DELIVERY
  @KafkaListener / Rabbit listener / ack / retry / dead letter

ERROR_HANDLING
  catch / throws / 空 catch / 异常吞掉 / 错误结果转换
```

路径规则补充文件角色信号，例如 `controller/`、`mapper/`、`repository/`、`consumer/`、
`config/`；路径信号只提高审查方向优先级，不能单独生成漏洞结论。

## 6. TaskRank 与预算

Phase 2 默认预算:

```text
max_tasks_to_review = 30
max_tasks_per_file = 5
```

默认配置应通过 Settings/环境变量进入初始 `review_budget`；State 字段不变。建议配置名:

```text
CODEGUARD_MAX_REVIEW_TASKS=30
CODEGUARD_MAX_TASKS_PER_FILE=5
```

排序使用稳定的确定性键:

```text
1. 风险标签优先级与 tag_scores
2. 是否存在 score=3 的强信号
3. 是否包含 diff_deletion 信号
4. 生产代码优先于测试、文档和生成文件
5. 文件内任务数量限制
6. task_id 作为最终稳定 tie-breaker
```

选择规则:

- 任务数不超过预算时全选。
- 超过总预算时按排序取前 30 个。
- 同一文件最多选 5 个，其余记录 `per_file_limit`。
- 所有被跳过任务都写入 `TaskSelection.skipped_tasks`，带原因和派生风险分数。
- `GENERAL_REVIEW` 任务属于低优先级，但仍参与剩余预算竞争；一旦被选中，Phase 4
  必须启动三路审查员。

## 7. 观测与错误处理

- 每条规则命中必须能从 `source` 和 `reason` 解释。
- 规则异常不能阻断整条审查链；单条规则失败记录 trace 并继续执行其他规则。
- 所有规则失败时，不把任务误判成“无风险”；生成 `GENERAL_REVIEW` 并记录失败原因。
- `RiskProfile` 为空只允许发生在规则执行前的内部瞬间，节点返回 State 时必须有具体标签
  或 `GENERAL_REVIEW`。
- `RiskTriage` 输出汇总 trace；完整信号保留在 `risk_profiles` State 写入中。

## 8. 验收标准

- 23 个具体路由标签和 `GENERAL_REVIEW` 的枚举、规则元数据、reviewer 映射确定。
- 每个具体标签至少有一个可复现的 Java/Spring fixture。
- 规则能区分新增/修改与删除行为，`source` 和 `reason` 稳定可解释。
- 无信号 task 自动进入 `GENERAL_REVIEW`，且不与具体标签重复。
- 默认预算为 30 个 task、单文件最多 5 个；跳过原因可追溯。
- State、图拓扑、`Issue` 和 `CandidateIssue` 契约不变。
- AST 不参与 Phase 2 风险标签判断，后续只能作为 ContextProvider 的事实增强来源。
- 现有测试、mock 审查、ruff 和 mypy 全部通过。

