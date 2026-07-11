# Phase 2 风险标签规则与任务排序设计

**日期**: 2026-07-10  
**状态**: 已实施
**前置阶段**: Phase 1 状态契约与最终拓扑已完成  
**关联设计**: [风险路由 ReviewTask 编排设计](./2026-07-10-risk-routed-review-orchestration-design.md)

## 1. 目标

Phase 2 完成风险标签规则、大 diff 任务排序，以及风险标签到审查员的实际路由，让每个
`ReviewTask` 在进入 ReviewCouncil 前拥有可解释的审查方向、优先级和审查员范围。

Phase 2 的风险标签是**高召回路由信号**，不是漏洞结论。规则只回答“这个任务值得从哪个
角度审查”，不回答“这里一定存在问题”。真假判断由 ContextProvider、三路审查员、
EvidenceAgent 和 CouncilJudge 后续完成。

Phase 2 固定以下主链路，不增加 State 字段、不改变图拓扑:

```text
ReviewTask
  → path + diff-text(change direction) signals
  → RiskProfile
  → TaskSelection
  → derive reviewer task scope from RiskTag
  → ContextProvider → ReviewCouncil
```

AST 风险信号不在 Phase 2 产生。后续 AST 作为 ContextProvider 的事实来源，补充完整方法、
注解、调用关系和上下文事实；它不改变 Phase 2 的风险标签契约。

## 2. 非目标

- 不调用 LLM 进行风险分类。
- 不读取完整仓库文件，不调用 Java AST 或调用图工具。
- 不生成 `Issue`、`CandidateIssue` 或漏洞结论。
- 不改变 `ReviewState`、`ReviewTask`、`RiskProfile`、`TaskSelection` 的字段形状。
- 不在 Phase 2 增加动态 LangGraph 节点或 `assigned_reviewers` State 字段；审查员范围由
  `RiskProfile.tag_scores` 和固定的 RiskTag 路由注册表派生。

## 3. 规则引擎边界

### 3.1 规则接口

风险规则是纯函数，只消费一个 `ReviewTask`:

```python
RiskRule = Callable[[DiffFeatures], list[RiskSignal]]
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

### 3.2 Phase 2 信号来源与变化方向

Phase 2 不把路径、增加、删除、修改当成四种最终风险类别。它们是规则观察变更的
来源和方向:

```text
path:<rule_id>                    文件路径和文件角色
text:added:<rule_id>              新增代码中的典型结构或 API
text:deleted:<rule_id>            被删除的保护、校验、事务或错误处理
text:changed:<rule_id>            修改前后代码组合出的语义变化
```

规则消费同一个 `ReviewTask.patch`，但要区分以下方向:

- `path`: 文件角色，例如 `controller/`、`mapper/`、`config/`。它只作为弱权重和排序
  依据，不能单独制造大量具体 RiskTag。
- `text:added`: 新增代码中出现了什么结构、关键字或 API。
- `text:deleted`: 被删除的代码中原来有什么保护逻辑。
- `text:changed`: 同一 hunk 中删除旧实现并新增新实现后，前后组合产生了什么变化。

例如删除 `@PreAuthorize`、输入校验、事务注解或异常处理时，新增代码里根本不会出现
这些内容，但删除本身就是需要审查的信号。修改通常在 unified diff 中表现为删除旧行
加新增新行，规则可以分别观察两侧，也可以在同一 hunk 内合并判断为 `text:changed`。

### 3.3 RiskSignal 语义

```python
RiskSignal(
    tag=RiskTag.AUTHORIZATION,
    score=3,
    source="text:deleted:authorization_guard_removed",
    reason="删除 UserController.update() 上的 @PreAuthorize 注解",
    line=42,
)
```

- `source` 是稳定的机器标识，格式为 `path:<规则编号>` 或
  `text:<变化方向>:<规则编号>`。
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

路径信号不能单独创建具体标签。只有当同一 task 已经存在 `text:added`、`text:deleted`
或 `text:changed` 的同标签信号时，`path` 信号才可以作为弱加权证据参与该标签聚合；
否则该路径提示不写入 `RiskProfile.signals`，该 task 仍然进入 `GENERAL_REVIEW`；如需
诊断规则覆盖率，可只在 trace 中记录该路径提示。

如果一个 task 没有任何具体文本信号，生成唯一的特殊兜底标签:

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

每个标签维护一个 reviewer 集合。Phase 2 直接根据选中任务的标签集合计算 reviewer 并集，
并把 task-scoped diff 交给对应审查员。这个集合是派生值，不写入 `RiskProfile`，避免在
标签和 reviewer 字段之间维护两份事实。

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

路由规则:

- 一个 task 命中多个标签时，审查员取这些标签对应集合的并集。
- 一个审查员只接收自己被分派的 selected tasks，不再对整个 diff 做无关扫描。
- selected tasks 中没有某审查员负责的标签时，该审查员节点记录 `no_tasks_routed` 并
  正常结束；三路 fan-out/fan-in 拓扑不变。
- `GENERAL_REVIEW` 固定分派给 T、B、M 三路，保证未知变化不会因为规则漏命中而无人审查。
- 路由只减少无关审查，不把标签当作最终结论；EvidenceAgent 和 CouncilJudge 仍可根据
  证据修正审查结果。

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
`config/`；路径信号只提高已经由文本规则命中的审查方向优先级，不能单独生成具体
RiskTag。若 task 没有任何具体文本命中，统一进入 `GENERAL_REVIEW`。

## 6. TaskRank 与预算

Phase 2 默认预算:

```text
max_tasks_to_review = 100
max_tasks_per_file = 10
```

默认配置应通过 Settings/环境变量进入初始 `review_budget`；State 字段不变。建议配置名:

```text
CODEGUARD_MAX_REVIEW_TASKS=100
CODEGUARD_MAX_TASKS_PER_FILE=10
```

预算按 task 计算，不按命中的标签数量计算。一个 task 即使命中多个标签，也只占一个
task 配额；它会把对应 reviewer 集合取并集。这样激进的高召回标签不会因为标签数量多而
额外放大预算消耗。

排序使用稳定的确定性键:

```text
1. 风险标签优先级与 tag_scores
2. 是否存在 score=3 的强信号
3. 是否包含 `text:deleted` 信号
4. 生产代码优先于测试、文档和生成文件
5. 文件内任务数量限制
6. task_id 作为最终稳定 tie-breaker
```

选择规则:

- 任务数不超过预算时全选。
- 超过总预算时按排序取前 100 个。
- 同一文件最多选 10 个，其余记录 `per_file_limit`。
- 所有被跳过任务都写入 `TaskSelection.skipped_tasks`，带原因和派生风险分数。
- `GENERAL_REVIEW` 任务属于低优先级，但仍参与剩余预算竞争；一旦被选中，Phase 2
  的路由逻辑直接分派给三路审查员。

## 7. 观测与错误处理

- 每条规则命中必须能从 `source` 和 `reason` 解释。
- 规则异常不能阻断整条审查链；单条规则失败记录 trace 并继续执行其他规则。
- 所有规则失败时，不把任务误判成“无风险”；生成 `GENERAL_REVIEW` 并记录失败原因。
- `RiskProfile` 为空只允许发生在规则执行前的内部瞬间，节点返回 State 时必须有具体标签
  或 `GENERAL_REVIEW`。
- `RiskTriage` 输出汇总 trace；完整信号保留在 `risk_profiles` State 写入中。

## 8. 验收标准

- 23 个具体路由标签和 `GENERAL_REVIEW` 的枚举、规则元数据、reviewer 映射确定并生效。
- 每个具体标签至少有一个可复现的 Java/Spring fixture。
- 规则能区分新增、修改和删除方向，`source` 和 `reason` 稳定可解释。
- 无具体文本信号的 task 自动进入 `GENERAL_REVIEW`，且不与具体标签重复。
- 默认预算为 100 个 task、单文件最多 10 个；跳过原因可追溯。
- ReviewCouncil 按 RiskTag 路由到对应审查员；没有匹配任务的审查员可空运行，未知任务
  通过 `GENERAL_REVIEW` 进入三路。
- State、图拓扑、`Issue` 和 `CandidateIssue` 契约不变。
- AST 不参与 Phase 2 风险标签判断，后续只能作为 ContextProvider 的事实增强来源。
- 现有测试、mock 审查、ruff 和 mypy 全部通过。

## 9. 实施记录

Phase 2 已按本设计落地，事实边界如下:

- `pipeline/risk_rules/features.py` 只从 `ReviewTask` 派生变化方向特征；规则不读取 AST、完整仓库或 LLM。
- `pipeline/risk_rules/security.py`、`behavior.py`、`maintainability.py` 以稳定注册顺序覆盖 23 个具体标签；
  `catalog.py` 负责单条规则异常诊断、信号去重、按标签最高 5 分聚合、path 信号并入和 `GENERAL_REVIEW` 兜底。
  `risk_rules/path.py` 将 controller、repository/mapper、config、consumer/listener、service 映射为
  弱路径证据；每条 `path:<role>` 信号固定为 1 分，只有对应文本风险已经命中时才并入该标签，
  路径单独出现仍然只生成 `GENERAL_REVIEW`。
- `task_prep.rank_tasks` 只派生临时排序键，使用默认总预算 100、单文件预算 10；预算跳过记录在既有
  `TaskSelection.skipped_tasks`，不增加 `ReviewState` 字段。
- `pipeline/risk_routing.py` 从 `RiskProfile.tag_scores` 计算 reviewer 并集；`GENERAL_REVIEW` 固定进入三路。
  `graph.py` 将每个 reviewer 的 selected task scope 传给既有 Direct/ReAct 子图，没有任务时记录
  `no_tasks_routed`，候选越界时记录 `candidate_rejected_unrouted`。
- 配置入口为 `CODEGUARD_MAX_REVIEW_TASKS` 和 `CODEGUARD_MAX_TASKS_PER_FILE`，产品输出 `ReviewResult/Issue`
  不变。

验证结果：全量 pytest `374 passed`，ruff 和 mypy 全绿；mock CLI 退出码为 0；
`pipeline-notools` mock eval 成功加载并运行 28 个样本，报告写入
`services/agent/evals/reports/pipeline.md`。mock 模式不生成真实审查问题，因此该次 eval 的
Precision/Recall 为 0 只表示零成本链路验证，不作为规则质量指标。

本阶段刻意没有实现 AST 风险判断、RiskTag 感知的 ContextProvider、Java Gateway 新工具、任务化 Evidence/Judge
或 Dashboard 专项指标；这些能力消费本阶段已经固定的 `RiskProfile` 和 `TaskSelection`，不会再新增一套并行状态。
