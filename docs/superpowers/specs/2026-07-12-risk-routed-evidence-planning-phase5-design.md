# Phase 5：风险标签驱动的证据规划与裁决链设计

**日期**：2026-07-12
**状态**：已实施
**前置**：[风险路由主设计](./2026-07-10-risk-routed-review-orchestration-design.md) Phase 1–4、[Phase 4 定向发现](./2026-07-11-risk-routed-discovery-phase4-design.md)、[标签知识注入子阶段](./2026-07-11-tag-scoped-knowledge-injection-design.md)

> 本稿是实施计划的接口基线。第 12 节的分子阶段计划（5A / 5B）是唯一的落地路径；
> 实施时不得只复述本文标题，必须按这里的接口、分支和测试行为落到具体 commit。

---

## 1. 要解决的问题

Phase 4 已经做到：每个 Reviewer 针对一个 `ReviewTask`，在风险标签、任务补丁和任务上下文的约束下提出 `CandidateIssue`。但 Review 之后的链路仍是 Phase 1 的薄实现，有四个真实缺陷：

1. `models/council.py:build_evidence_requests` 按候选置信度和 `source_agent` 选工具，它不知道 `task_id` 对应的 `RiskProfile` 或 `TaskContextBundle`。补证请求退化成"低置信候选的泛化补查"。
2. `_evidence_agent_node` 只按 `preferred_tools` 调通用工具；LLM 输入没有任务补丁、风险信号、任务上下文和"本请求要证明还是要反驳"的语义。
3. **无 LLM 时把任意非空工具输出写成 `supports`**——把"读到了代码"错当成"主张成立"，这是安全回退方向错误。
4. `_council_judge_node` 一边做候选裁决、全局去重/合并，一边直接拼接新的 `EvidenceRequest`；LLM 的 `suggested_tools` 因而绕开取证策略。

本阶段目标不是再做一次开放式审查，而是把一个已提出的候选，约束在它的任务和风险语义内，回答三个问题：

1. 该主张所依赖的风险事实是否真的存在？（**补证 support**）
2. 当前变更入口或紧邻调用链是否已有直接保护事实？（**反证 counter**）
3. 即使主张成立，影响级别是否足以匹配候选严重度？（**定级 severity**）

---

## 2. 已定决策（原第 10 节四问，现全部拍板）

| # | 决策 | 结论 | 连带影响 |
|---|---|---|---|
| A1 | 是否新增显式 `EvidencePlanner` 节点 | **接受**。规划与执行分离，第二轮补证也一定由统一策略选择，而非 Judge/LLM 临时指定工具 | 正式修订主设计"Phase 1 后固定主路由"，见第 11 节 |
| A2 | v1 是否完整覆盖 23 个具体标签 | **接受**。策略注册表是纯代码规则表，框架落地后逐标签填充是机械工作；完整性由测试强制 | `GENERAL_REVIEW` 只用于候选语义无法分类，不替代任何已知标签 |
| A3 | `EvidenceNote` 是否收敛为 `EvidenceFinding`（带 strength/limitation） | **接受，且一次切干净**。不保留 `supports/contradicts/unknowns` 字符串兼容层——同阶段迁移全部消费者（Judge/trace/eval），遵循"不留死代码" | 删除 `EvidenceNoteStatus`、`EvidenceJudgment`、`build_evidence_requests` |
| A4 | "未找到保护"是否只能是 `insufficient` | **接受**，作为硬安全规则。找不到调用方、工具失败、上下文截断、文本未命中，全部 `insufficient`，绝不反向支持漏洞 | 替换当前 raw output 默认 `supports` 回退 |

补充两项参数级决策：

- **classifier_llm 复用现有 judge 异源模型**（`judge_llm`，temperature=0），不新增 `CODEGUARD_*` 配置项。候选语义分类是一次受限枚举分类，成本极低，无需独立模型接线。
- **移除 `MAX_TOTAL_EVIDENCE_REQUESTS=20`**。总量由"每 Reviewer ≤10 候选 × 三路 × 每候选 ≤2 条初始请求"自然封顶 60 条，实际调用数由同轮工具缓存压低；成本行为进 eval 观测。若评测证明不可接受，再单独设计候选预聚合，不能静默牺牲部分候选的反证覆盖。

---

## 3. 图拓扑修订

主设计原把 `EvidenceAgent` 视为一个固定步骤。Phase 5 将它细化为"规划"和"执行"两个节点；**不新增主路由 State 字段，不改产品输出契约**。

```text
ContextProvider
  → Discover × 3
  → CouncilCoordinator（显式 fan-in，只运行一次）
  → EvidencePlanner            ★新增
  → EvidenceAgent
  → CouncilJudge
  → END

CouncilJudge -- needs_more_evidence 且 evidence_round < max --> EvidencePlanner   ★回环回到 Planner
```

- 首次仍必经 `EvidenceAgent`：没有候选或没有可用策略时，Planner 和 Agent 都记录 no-op 后进入 Judge。
- 回环改为回到 **Planner**（而非直接回到 Agent），保证第二轮由统一策略选择工具。
- 这是对主设计"Phase 1 后固定主路由"的**有意修订**。落地时必须同步更新主设计的拓扑图、节点职责和 State 写入表（见第 11 节清单）。

---

## 4. 职责边界

| 节点 / 模型 | 负责 | 不负责 |
|---|---|---|
| `ReviewNode`（三路发现者） | 从 task-scoped diff 提出 `CandidateIssue` 和初步理由 | 设计取证路径、给出最终结论 |
| `CouncilCoordinator` | fan-in 与批次 trace | 生成或筛选证据请求 |
| `EvidencePlanner` | 按候选证据主题选择补证/反证/定级策略，写入 `EvidenceRequest` | 调工具、解释工具输出、最终裁决 |
| `EvidenceAgent` | 执行确定的策略，记录可复核 finding 及其与主张的关系 | 扩展调查范围、追加请求、最终 keep/drop |
| `CouncilJudge` | 候选裁决、有限补证意图、全局去重/合并、最终 `Issue` | 直接选任意工具、改写上游 task/risk/context 事实 |
| `EvidenceStrategy` | 静态声明标签、目的、允许工具、问题模板、优先级、调用配方 | 进入 State 或存运行结果 |

`CandidateDossier` 只是 Planner/Agent/Judge 内部临时拼出的只读视图，**不进 State**：

```text
CandidateIssue + ReviewTask + RiskProfile + TaskContextBundle
  + 当前 EvidenceRequest / EvidenceNote + latest Verdict
  = CandidateDossier
```

---

## 5. 状态与模型契约

不增加顶层 `ReviewState` 字段。继续用既有 `candidate_issues`、`evidence_requests`、`evidence_notes`、`council_verdicts`、`evidence_round`，只重写内部模型。

### 5.1 字段有效性硬约束

Phase 5 不接受"也许未来有用"的字段。每个新增/扩展字段必须同时具备：唯一写入者、至少一个运行时消费者、可观察的行为影响、一条验证其影响的测试。四者缺一，字段不进模型。

| 字段 | 写入者 | 消费者 | 缺失时行为 | 验证重点 |
|---|---|---|---|---|
| `EvidenceRequest.strategy_id` | Planner | Agent | 拒绝未知策略并写 `insufficient` | 请求只能执行注册表声明的策略 |
| `EvidenceRequest.purpose` | Planner | Agent、Judge | 不得生成请求 | support/counter/severity 进 prompt 和裁决矩阵 |
| `EvidenceRequest.target` | Planner | Agent | 不得越过 task/candidate 文件边界 | 工具参数受 target 约束 |
| `EvidenceRequest.question` | Planner | Agent 关系判定 prompt、trace | 不得用空泛默认问题替代 | 策略问题可在 trace 复原 |
| `EvidenceRequest.preferred_tools` | Planner | Agent allowlist 校验 | 策略不执行未声明工具 | Judge/LLM 建议不能越权调用 |
| `EvidenceFinding.evidence_id` | Agent | Judge、trace | finding 无法进裁决 | 可追溯到实际工具调用 |
| `EvidenceFinding.source` | Agent | Judge、trace | finding 无法进裁决 | 区分 task patch / context / 新工具调用 |
| `EvidenceFinding.observation` | Agent | Judge prompt | 只保留 `insufficient` | 输出事实而非工具成功标记 |
| `EvidenceFinding.relation` | Agent | Judge 规则 | 只保留 `insufficient` | direct counter/support 改变裁决 |
| `EvidenceFinding.strength` | Agent | Judge 规则 | 默认 contextual | 只有 direct counter 可直接推翻候选 |
| `EvidenceFinding.limitation` | Agent | Judge prompt、trace | 不得从不完整证据作强结论 | 未找到/截断不被误判为反证 |
| `Verdict.requested_purpose` | Judge | Planner | 不产生第二轮请求 | 第二轮只选该 purpose 的未执行策略 |

`CandidateDossier` 不进 State，不受"持久字段"约束。`EvidenceNote.status`、请求级 `round`、重复的 `task_id`、自由文本 `suggested_tools` 都**不作为新事实字段**：status 从 findings 派生，其余由现有 `evidence_round`、`candidate_id → task_id`、静态策略注册表取代。

### 5.2 `EvidenceRequest`（重写）

```python
EvidencePurpose = Literal["support", "counter", "severity"]

class EvidenceRequest(BaseModel):
    id: str = ""
    candidate_id: str
    strategy_id: str              # 例如 authorization.counter
    purpose: EvidencePurpose
    target: str                   # 固定为 task.file
    question: str
    preferred_tools: list[str] = Field(default_factory=list)
    # stable id 必须纳入 strategy_id + purpose，否则同文件的补证/反证被 reducer 误去重
```

- `task_id` 不重复写入，由 `candidate_id → CandidateIssue.task_id` 唯一派生。
- `preferred_tools` 只是策略声明的 allowlist；实际工具与参数由 `strategy_id` 的静态配方构建，不信任 LLM 文本。

### 5.3 `EvidenceFinding` / `EvidenceNote`（重写，一次切干净）

```python
class EvidenceFinding(BaseModel):
    evidence_id: str
    source: str                   # task_patch / context:<kind> / tool:<name>
    observation: str
    relation: Literal["supports", "contradicts", "insufficient"]
    strength: Literal["direct", "contextual"]
    limitation: str = ""

class EvidenceNote(BaseModel):
    request_id: str
    candidate_id: str
    findings: list[EvidenceFinding] = Field(default_factory=list)
```

- **删除** `supports/contradicts/unknowns` 字符串列表、`status`、`EvidenceNoteStatus`、`evidence_ids`。所有消费者（Judge、trace、eval、单测）同阶段迁移到 `findings`。
- 证据 ID 引用实际工具调用结果；相同调用可被多请求复用同一 `evidence_id`，但**每个请求仍得到自己的 `EvidenceNote`**——修复当前"去重调用后跳过后续请求让其凭空 insufficient"的 bug。

### 5.4 Judge 补证意图

```python
# Verdict 与 JudgeDecision 增加：
requested_purpose: EvidencePurpose | None = None
```

`action == needs_more_evidence` 时，Judge 只能表达"还需补证/反证/定级证据"。**删除 `suggested_tools`**（不再作为执行权限）；Planner 依据 `requested_purpose` 和已执行策略选下一条允许的 `EvidenceStrategy`。

---

## 6. EvidenceStrategy：规则如何表达补证与反证

### 6.1 候选 RiskTag ≠ 任务 RiskTag

`RiskProfile.tag_scores` 是 RiskTriage 在 Review 之前按 diff 特征生成的**任务级先验**，只表示"这个 task 值得从哪些角度审"。Reviewer 可能发现先验之外的真实问题，因此 Planner **不得直接执行 `task RiskTag → EvidenceStrategy`**。

```text
CandidateIssue.type / claim / suggestion
  → 确定性候选语义规则（CANDIDATE_TAG_TERMS，23 标签专用术语表）
  → 明确：得到 candidate evidence tag
  → 歧义且有 LLM：受限枚举分类（复用 judge_llm）
  → 仍不确定 / 无 LLM：GENERAL_REVIEW

task RiskProfile
  → 只用于高风险补证条件、一致性 trace、提供上下文
  → 不得覆盖候选语义、不代表候选结论
```

`candidate evidence tag` 复用 `RiskTag` 枚举值作为策略主题词典，但只在 Planner 调用内使用，不新增 State 字段。分类依据和是否与 task RiskTag 一致写入 trace。

示例：task 因 `TRANSACTION_ATOMICITY` 被路由，但 BehaviorAgent 实际提出"异常分支可能空指针"→ 候选规则应解析为 `NULL_STATE_SAFETY` 并执行该标签的判空反证/可空解引用补证，而非错误执行事务策略。

### 6.2 策略注册表（深 module）

策略表位于 `pipeline/evidence_rules/`，镜像 Phase 2 `pipeline/risk_rules/` 范式：内部按三领域分文件，对外只暴露统一查表和分类 interface。

```python
@dataclass(frozen=True)
class EvidenceStrategy:
    id: str
    tags: frozenset[RiskTag]       # 匹配 candidate evidence tag，不匹配 task 先验
    purpose: EvidencePurpose
    priority: int
    question_template: str
    context_kinds: tuple[str, ...] # 可直接复用的 TaskContextBundle 事实种类
    allowed_tools: tuple[str, ...]
    build_tool_calls: Callable[[CandidateDossier], list[ToolCallSpec]]
```

- `build_tool_calls` 只为仍缺失的事实产生**现有四个 Gateway 工具**（`get_file_content` / `find_sensitive_apis` / `find_callers` / `get_code_metrics`）的调用，并受候选文件、任务文件、已有 task context 限制。Python 不直接读被审仓库文件，一切仓库探索经 Gateway。
- **完整性由测试强制**：`set(STRATEGIES_BY_TAG) == set(RiskTag)`（含 GENERAL_REVIEW）；每个具体标签同时存在 `counter` 与 `support`；每条策略 ID 唯一、问题非空、工具属现有 allowlist、候选分类词典非空。新增 RiskTag 未补齐时测试必须失败。

### 6.3 `ToolCallSpec` 与方法解析

```python
@dataclass(frozen=True)
class ToolCallSpec:
    tool_name: Literal[
        "get_file_content", "find_sensitive_apis", "find_callers", "get_code_metrics"
    ]
    arguments: tuple[tuple[str, str], ...]
```

工具配方必须复用 `context_rules.resolve_method_name`：从 dossier 的 `ast_structure` fact 解析当前 task 所属方法。解析不到方法时**不再用 `file#line` 伪查询调 `find_callers`**，而是产生 `limitation=no_method_resolved`。

需要外层语义的标签额外注册 `<slug>.counter_upstream`，只允许第二轮选择并用 `find_callers(file#method)`。其余标签没有可靠第二条策略时明确 `evidence_plan_exhausted`，不用同一工具换 question 重复执行。

### 6.4 全量策略语义表（首版必须完整实现，不允许 GENERAL 替代任一行）

| tag | counter：直接保护/排除事实 | support：成立条件 | context / 工具 |
|---|---|---|---|
| AUTHORIZATION | 方法/类/调用方已有鉴权或资源归属校验 | 路径真实执行敏感操作或访问受保护资源 | sensitive_api、AST；file、sensitive APIs、callers |
| AUTHENTICATION_SESSION | token/session 已校验有效期、撤销、绑定 | 变更真实影响认证凭据或会话生命周期 | AST；file、callers |
| WEB_SECURITY_CONFIG | 配置存在明确最小授权、CSRF/CORS 限制 | 变更真实扩大公开路由或关闭安全保护 | AST；file |
| INPUT_VALIDATION | 入口已有格式、范围、业务约束校验 | 外部输入真实到达敏感操作或状态修改 | sensitive_api、AST；file |
| INJECTION | 参数化、编码、allowlist 或安全 builder 已覆盖 | 不可信输入真实进入解释器/SQL/命令 sink | sensitive_api、AST；file、sensitive APIs |
| SQL_DATA_ACCESS | 参数绑定、租户条件、分页/索引约束已存在 | 路径真实执行查询/写入且具备候选所述数据条件 | sensitive_api、AST、caller；file、callers |
| FILE_PATH_IO | canonicalize、根目录约束、扩展名/大小 allowlist 已存在 | 外部可控路径真实进入文件系统操作 | sensitive_api、AST；file、sensitive APIs |
| SSRF_OUTBOUND | scheme/host/IP allowlist、redirect 限制已存在 | 外部可控 URL 真实进入出站客户端 | sensitive_api、AST；file、sensitive APIs |
| CONFIG_SECURITY | secret indirection、安全默认值、环境隔离已存在 | 变更真实引入弱配置、明文秘密或暴露开关 | AST；file |
| DATA_EXPOSURE | 脱敏、最小 DTO、访问控制或日志过滤已存在 | 敏感数据真实进入响应、日志或错误信息 | sensitive_api、AST；file |
| TRANSACTION_ATOMICITY | 当前/外层方法已有事务或可验证补偿 | 路径包含多个可部分成功的写入/外部副作用 | AST、caller；file、callers |
| CONCURRENCY_CONSISTENCY | 锁、原子结构、版本检查或线程封闭已存在 | 共享可变状态真实被并发路径访问 | AST、caller；file、callers |
| IDEMPOTENCY_RETRY | 幂等键、去重记录、唯一约束或重复处理保护已存在 | 路径存在重试、重复投递或重复写入触发条件 | AST、caller；file、callers |
| CACHE_CONSISTENCY | 失效、更新、版本或锁保护覆盖 DB 变化 | 路径同时涉及持久化变化与缓存访问 | AST、caller；file、callers |
| MESSAGE_DELIVERY | ack/retry/DLQ/outbox/消费去重保护已存在 | 路径真实发布/消费消息并产生副作用 | AST、caller；file、callers |
| ERROR_HANDLING | 异常被正确传播、转换、恢复或记录 | 路径存在候选所述吞异常/错误映射/恢复缺口 | AST、caller；file、callers |
| NULL_STATE_SAFETY | 判空、非空契约、Optional/默认值已覆盖路径 | 可空值真实到达解引用或状态使用点 | AST；file |
| RESOURCE_LIFECYCLE | try-with-resources/finally/框架托管释放已覆盖 | 路径真实获取需释放资源且存在提前退出/异常路径 | AST、caller；file、callers |
| API_CONTRACT | 兼容适配、默认值、版本或调用方同步修改已存在 | 请求/响应/公开签名真实发生不兼容变化 | AST、caller；file、callers |
| PERFORMANCE | 分页、批处理、缓存、边界或短路已控成本 | 路径真实存在循环 I/O/查询、无界集合或高复杂度 | AST、metrics、caller；file、metrics、callers |
| COMPLEXITY_CONTROL_FLOW | 提取方法、早返回或封装已实质降复杂度 | 变更真实增加分支、嵌套、难推理路径 | AST、metrics；file、metrics |
| DUPLICATION_DESIGN | 重复是有意隔离/专用实现，或已有共享抽象 | 相同业务规则真实在多处重复并可能漂移 | AST、metrics；file、metrics |
| OBSERVABILITY_TESTABILITY | 已有结构化日志、指标、trace、注入 seam 或测试覆盖 | 关键副作用/失败路径真实缺少可观测或可替换入口 | AST、caller、metrics；file、callers、metrics |
| GENERAL_REVIEW | task 中存在直接推翻候选的保护或前置条件 | task 中存在候选主张依赖的直接事实 | task facts；file |

表里的 `AST`、`sensitive_api`、`caller`、`metrics` **优先复用 `TaskContextBundle`**，缺失时才按允许工具补查。没有对应工具的事实交给文件内容 + LLM 解释，不虚构 Java 能力。

### 6.5 候选证据主题解析算法

```python
class CandidateTagResolution(BaseModel):
    tag: RiskTag
    confidence: float
    source: Literal["rule", "llm", "general"]
    reason: str

def resolve_candidate_evidence_tag(
    dossier: CandidateDossier, classifier_llm, *, structured_method: str,
) -> CandidateTagResolution: ...
```

`CandidateTagResolution` 不进 State，四字段全写入 `candidate_evidence_tag_resolved` trace。固定顺序：

1. 分别规范化 `candidate.type` / `claim` / `suggestion`（三字段可信度不同，不先拼成一段）。
2. 高精度主题规则。`CANDIDATE_TAG_TERMS` 为全部 23 标签登记 `exact_type_aliases` / `strong_phrases` / `weak_terms`；术语表候选分类专用，不复用 diff 风险命中词（避免"代码出现某 API"误当"候选在声称该类问题"）。
3. 计分（同义词重复出现不无限累加）：type 等于 exact alias → 8；type 命中 strong phrase → 6；claim 命中 strong → 4，否则命中 weak → 1；suggestion 命中 strong 最多 +1。
4. 歧义判定：最高分 `< 4`，或最高标签不唯一，或第一名与第二名分差 `< 2`。task RiskTag 不参与加分。
5. 无歧义直接返回规则结果：exact type 命中 confidence=0.95；其他唯一胜出 confidence=0.85。
6. 歧义且有 classifier LLM：给候选三字段、task patch 和"仅作先验"的 task tags，要求从 23 标签或 `GENERAL_REVIEW` 选择。结构化结果为 `None`、置信度 `< 0.75` 或未知枚举 → GENERAL。
7. 歧义且无 LLM → `GENERAL_REVIEW`。

```python
def is_ambiguous(scores: dict[RiskTag, int]) -> bool:
    ordered = sorted(scores.values(), reverse=True)
    top = ordered[0] if ordered else 0
    second = ordered[1] if len(ordered) > 1 else 0
    winners = sum(score == top for score in ordered)
    return top < 4 or winners != 1 or top - second < 2
```

候选分类词典最低语义锚点（实施可增同义词，不能删整类）：

| RiskTag | 候选问题术语锚点 |
|---|---|
| AUTHORIZATION | 鉴权、授权、越权、permission、access control、ownership |
| AUTHENTICATION_SESSION | 认证、登录、会话、token、session、credential |
| WEB_SECURITY_CONFIG | CSRF、CORS、permitAll、安全配置、security chain |
| INPUT_VALIDATION | 输入校验、参数校验、validation、untrusted input |
| INJECTION | 注入、SQL injection、命令注入、拼接查询、动态表达式 |
| SQL_DATA_ACCESS | SQL、查询条件、tenant filter、N+1、mapper、repository |
| FILE_PATH_IO | 路径穿越、文件读写、canonical path、upload、download |
| SSRF_OUTBOUND | SSRF、外部请求、URL、host allowlist、redirect |
| CONFIG_SECURITY | 密钥、默认密码、debug、配置泄露、insecure config |
| DATA_EXPOSURE | 敏感数据、日志泄露、返回过量、mask、PII |
| TRANSACTION_ATOMICITY | 事务、原子性、回滚、部分写入、transaction |
| CONCURRENCY_CONSISTENCY | 并发、竞态、锁、atomic、共享状态、race |
| IDEMPOTENCY_RETRY | 幂等、重复提交、重试、duplicate、retry |
| CACHE_CONSISTENCY | 缓存、失效、stale、evict、cache consistency |
| MESSAGE_DELIVERY | 消息、投递、消费、ack、Kafka、Rabbit、DLQ |
| ERROR_HANDLING | 异常、吞异常、错误码、恢复、exception handling |
| NULL_STATE_SAFETY | 空指针、null、未初始化、nullable、Optional |
| RESOURCE_LIFECYCLE | 资源泄漏、关闭、释放、try-with-resources、lifecycle |
| API_CONTRACT | 接口契约、兼容性、请求响应、版本、breaking change |
| PERFORMANCE | 性能、慢查询、循环 I/O、内存、分页、复杂度 |
| COMPLEXITY_CONTROL_FLOW | 圈复杂度、分支、嵌套、控制流、过长方法 |
| DUPLICATION_DESIGN | 重复代码、复制粘贴、抽象、duplication |
| OBSERVABILITY_TESTABILITY | 日志、指标、追踪、可测试性、mock、observability |

---

## 7. Planner 精确算法

```python
def plan_evidence(
    dossiers: Sequence[CandidateDossier], *,
    evidence_round: int, classifier_llm, structured_method: str,
) -> EvidencePlan: ...
```

**初始轮**（`evidence_round == 0`）采用两遍规划，不按候选逐个生成"反证+补证"，否则前面高风险候选可能耗尽预算，让后面候选连反证都没有：

1. 按 `candidate_issues` 稳定顺序组装 dossier。
2. 为全部有效 dossier 解析 candidate evidence tag。
3. **第一遍**：为每个候选各安排一条 `counter` 策略。反证是压制误报所必需，不因 Reviewer 自报高置信而跳过。
4. **第二遍**：只为满足任一条件的候选追加一条 `support`：`severity_proposal == CRITICAL`、`decide_tier(profile) == "react"`（沿用 Phase 4 `max(tag_scores) >= 2` 高风险定义）、或 `confidence < 0.9`。
5. request 的 `target = task.file`；`question` 由策略模板展开；`preferred_tools` = 实际生成 ToolCallSpec 的有序去重工具列表。
6. 同一候选同一 strategy 由 stable ID 去重。所有 counter 优先级高于任何 support。同一候选初始轮最多 2 条请求。

**回环轮**（`evidence_round > 0`）：

1. 只看 `latest_verdict.action == needs_more_evidence` 的 dossier。
2. `requested_purpose` 为空时不规划，记 `evidence_plan_invalid_verdict`。
3. 排除该候选已排队或已有 note 的 `strategy_id`，选相同 purpose 的下一条最高优先级策略。
4. 每候选每回环最多新增 1 条；两轮内仍无策略时记 `evidence_plan_exhausted`，不制造空泛请求。

预算常量：初始每候选 ≤2、回环每候选 ≤1、最多 2 次 EvidenceAgent 执行。**删除 `MAX_TOTAL_EVIDENCE_REQUESTS=20` 的 reducer 截断**，总量由候选数与上述上限自然封顶。

---

## 8. EvidenceAgent：规则取事实，LLM 只解释关系

```python
def collect_evidence(
    dossiers: Mapping[str, CandidateDossier],
    pending_requests: Sequence[EvidenceRequest], *,
    tool_client, analyst_llm, structured_method: str,
) -> EvidenceBatch: ...
```

执行规则：

1. pending 定义为 `request.id` 不在任何 `note.request_id` 中。
2. 按 `strategy_id` 重载 strategy，校验 purpose/target/preferred_tools 与策略展开一致；任一不一致不调工具，生成 `limitation=request_strategy_mismatch`。
3. 先按 `context_kinds` 复用 task facts，再为缺失事实调 Gateway；工具调用按 `(tool_name, canonical_json(args))` 缓存，命中复用同一 raw observation，但仍为当前 request 生成独立 `EvidenceNote`。
4. evidence_id：工具事实 = `sha256(tool_name + canonical_args + raw_content)`；既有 context fact = `sha256(fact.source + fact.kind + fact.content)`。相同事实被不同请求引用同一 id。
5. **安全回退（A4）**：工具失败、空结果、方法无法解析、TaskContext 截断、LLM 返回 None，均 `relation=insufficient`；禁止 raw output 默认 support。
6. 每条 request 恰好产生一条 `EvidenceNote`，即使无工具可执行——保证 processed request 判定不丢失。

**第一版确定性解释只覆盖可安全定位的强模式**：

- AUTHORIZATION local counter：当前方法/类作用域内的 `@PreAuthorize` / `@PostAuthorize` / `@Secured` / `@RolesAllowed` 为 `direct + contradicts`；只在文件其他位置出现为 `contextual`，不直接 drop。
- TRANSACTION local counter：当前方法或所属类的 `@Transactional` 为 `direct + contradicts`；任意字符串出现不是直接证据。
- 其余自定义校验、幂等 helper、缓存封装、消息基础设施交给 LLM；规则只把代码片段作为 observation，不猜语义。

LLM 输入必须含：候选主张、目的、策略问题、任务补丁、风险信号、任务上下文、工具输出、工具局限。输出只能是 `supports` / `contradicts` / `insufficient`，并说明依据与局限。LLM 不得添加候选中不存在的新漏洞主张——只判断给定主张与给定事实的关系。

---

## 9. CouncilJudge：先候选裁决，再全局收口

```text
每候选 dossier 裁决 → 全局精确去重/语义合并 → 最终 Issue 转换
```

Judge 先按 `request_id` 将 finding 与 request purpose 关联，再按顺序处理单候选：

| 条件 | 确定性动作 |
|---|---|
| dossier/task 无效或与任务不一致 | drop(`invalid_candidate_binding`) |
| 任一 counter finding 为 `direct + contradicts` | drop(`direct_counter_evidence`) |
| severity finding 为 `direct + contradicts` | 不直接决定级别；进 Judge LLM，只允许 downgrade/keep |
| support finding 为 `direct + supports`，但 counter 为 insufficient | **不走 fast keep**；进 Judge LLM（"敏感操作存在"≠"保护不存在"） |
| 所有 finding insufficient 且候选 CRITICAL | 无 LLM → downgrade 到 WARNING；有 LLM → 允许 needs_more/downgrade/keep |
| 所有 finding insufficient 且非 CRITICAL | 无 LLM → conservative keep；有 LLM → 正常裁决 |

- Judge LLM 的 `needs_more_evidence` 只在 `evidence_round < max_evidence_rounds` 时允许；到最后一轮 prompt 不提供该 action，模型仍返回时代码转 `keep`（非 CRITICAL）或 `downgrade WARNING`（CRITICAL），保证 END 前无悬而未决 verdict。
- Judge LLM 看到的是**候选级 dossier**（任务补丁、RiskProfile 信号、TaskContextBundle、策略目的、结构化 findings 与局限），而非只有全局 `ContextBundle`。
- 候选裁决后再做全局去重/合并，保留在 Judge，不移入 Planner/Agent。
- **删除 `_rule_strong_support`**（它把"所有 note supported"误当漏洞成立）；`_rule_contradicted` 改读 finding strength，不再与候选 confidence 绑定。
- **修复回映射 bug**：聚合回映射必须用 `best.file`，不能读内层循环遗留的 `c_file`（graph.py:1312 附近）。

---

## 10. Trace、评测与可验收行为

每个候选至少能在 trace 回答：

```text
candidate_id → task_id / risk tags
  → 选了哪些 strategy_id、为什么
  → 每策略用了哪些工具、得到哪些事实
  → 事实是 support/counter/insufficient，强度与局限
  → Judge 如何据此 keep/drop/downgrade/needs_more
```

新增 trace 事件：`candidate_evidence_tag_resolved`、`evidence_planned`、`evidence_plan_skipped`、`evidence_plan_exhausted`、`evidence_plan_invalid_verdict`、`evidence_finding_recorded`、`evidence_tool_reused`、`judge_requested_more_evidence`。

**行为验收**（不以"LLM 多报几个"为准）：

- 鉴权候选：敏感操作存在且入口有明确 `@PreAuthorize`/归属校验 → direct counter → drop 或 downgrade。
- 事务候选：多写操作与明确事务边界分别形成 support/counter；找不到外层调用方只能是 insufficient。
- 同一工具调用被两请求复用：两请求各得 `EvidenceNote` 与同一 evidence_id。
- 无 LLM、空工具结果、结构化输出失败：任何请求都不能自动 support。
- Judge 不能通过任何字段直接执行非策略允许的工具。
- `ReviewResult` / `Issue` 字段与 CLI 契约不变。

**新增评测指标**：有直接保护事实的诱饵误报率、证据不足误报率、每个最终 Issue 的策略/事实覆盖率、23 标签策略覆盖率、平均工具调用数。

---

## 11. 需要同步修订的主设计文档

落地时必须一并改 `2026-07-10-risk-routed-review-orchestration-design.md`，否则两稿冲突：

1. **§2 总体目标拓扑图 + "Phase 1 即固定主路由"表述**：插入 `EvidencePlanner` 节点，回环改为 `Judge → EvidencePlanner`；补一句"Phase 5 对首次固定拓扑做过一次受控修订（新增规划/执行分离），记录于本节"。
2. **§3.3 State 写入边界表**：`evidence_requests` 主写节点从 `ReviewCouncil / CouncilJudge` 改为 `EvidencePlanner`（Judge 只写 `requested_purpose`，不再追加请求）。
3. **§4.8 EvidenceAgent "Phase 5 增强"**：把"证据策略按 RiskTag 分流"改为"按候选证据主题（candidate evidence tag）分流，task RiskTag 仅作先验/一致性参考"。
4. **§5 实施台账**：先补记 Phase 4（定向发现 + 标签知识注入）实际已落地状态（当前台账仍标 `planned`，滞后于 git 已合并的 `f81d5d3…b7bd539`），再把 Phase 5 从 `planned` 推进为 `in_progress`。

---

## 12. 分子阶段实施计划（落地路径）

拆成两个可独立验收的子阶段。**5A 是纯函数地基，全程不改图运行时行为，pytest 全绿即可验收；5B 才接线换行为。** 这样先拿到一个零运行时风险的检查点，再叠加行为变更。

### Phase 5A：模型 + 策略注册表 + Planner（纯函数，可独立单测）

| commit | 内容 | 验证 |
|---|---|---|
| 5A-1 | `models/council.py` 重写：`EvidencePurpose`、`EvidenceRequest`(+strategy_id/purpose，stable id 纳入两者)、`EvidenceFinding`、`EvidenceNote`(findings)、`Verdict/JudgeDecision`(+requested_purpose)；**删** `EvidenceNoteStatus`/`EvidenceJudgment`/`build_evidence_requests`/`MAX_TOTAL_EVIDENCE_REQUESTS`。此步会暂时打断 graph.py 引用——用 `# TODO(5B)` 桩顶住 import，5B 拆除 | `test_council_models`：stable id 含 strategy_id+purpose、findings 派生、requested_purpose 可选 |
| 5A-2 | `pipeline/evidence_rules/`：`security.py`(前 10 标签)/`behavior.py`(事务至 API_CONTRACT 9 标签)/`maintainability.py`(PERFORMANCE 至 OBSERVABILITY 4 标签)/`__init__.py` 聚合，暴露 `resolve_candidate_evidence_tag` + `strategies_for`；`CANDIDATE_TAG_TERMS` 23 标签术语表；`ToolCallSpec` + `build_tool_calls`（复用 `context_rules.resolve_method_name`） | `test_evidence_rules`：`set(STRATEGIES_BY_TAG)==set(RiskTag)`、每标签 counter+support、ID 唯一、工具合法、词典非空、`no_method_resolved` 分支 |
| 5A-3 | `resolve_candidate_evidence_tag` 计分算法 + `is_ambiguous` + 受限 LLM 分类（复用 judge_llm，mock 下走规则/GENERAL） | `test_candidate_tag_resolution`：exact=8 不调 LLM、strong/weak 计分、歧义调 LLM、无 LLM→GENERAL、四个示例用例 |
| 5A-4 | `pipeline/evidence_planner.py`：`CandidateDossier`、`EvidencePlan`、`plan_evidence`（两遍初轮 + 回环轮） | `test_evidence_planner`：全候选先 counter、高风险追加 support、30 候选不被 20 截断、回环只处理 needs_more、exhausted/invalid_verdict |

**5A 验收**：`pytest tests/ -q` 全绿（graph 桩不破坏现有测试）、`ruff`/`mypy` clean。此时图行为未变，Planner 尚未接线。

### Phase 5B：图接线 + Agent/Judge 重写（改运行时行为）

| commit | 内容 | 验证 |
|---|---|---|
| 5B-1 | `pipeline/evidence_agent.py`：`collect_evidence`（工具缓存 + finding 生成 + 确定性强模式 + 安全回退 + 每 request 一 note）；确定性解释覆盖 AUTHORIZATION/TRANSACTION local counter | `test_evidence_agent`：缓存复用同 evidence_id、空结果=insufficient、None=insufficient、strategy_mismatch |
| 5B-2 | `pipeline/council_judge.py`：`judge_candidates`（候选裁决矩阵 + 全局去重合并 + 修 `c_file`→`best.file`）；删 `_rule_strong_support`、改 `_rule_contradicted` 读 strength | `test_council_judge`：direct counter→drop、support 不走 fast keep、最后一轮 needs_more 兜底、c_file bug 回归 |
| 5B-3 | `graph.py`：新增 `_evidence_planner_node`；回环边改 `_council_judge_node → _evidence_planner_node`；三节点变薄 adapter；删 reviewer 收集处两次 `build_evidence_requests` 及 `MAX_TOTAL` 截断；拆 5A-1 的 TODO 桩 | `test_graph_orchestration`：Planner 在 coordinator 后、Agent 首次必经、回环回 Planner、no-op 冒烟 |
| 5B-4 | trace 事件接入 + mock/None 全链路安全回退 + eval 用例（诱饵误报、证据不足、23 标签覆盖）+ 更新主设计台账与本文状态 | 全套 pytest 全绿；`ruff`/`mypy` clean；mock CLI EXIT=0；`pipeline-notools` mock eval 完成并出报告 |

**5B 验收**：全套单测全绿、mock 冒烟通过、eval 报告更新、主设计四处修订（第 11 节）落地、本文台账从 `planned` 改 `done`。

### 迁移顺序硬约束

- **不新增 Java 工具、不让 Java 判定问题、不让 LLM 自由探索仓库、不改最终产品输出。**
- 5A 一次覆盖 23 具体标签 + GENERAL_REVIEW，不把剩余标签留作无验收约束的后续工作。
- 每个 commit 后按 CLAUDE.md §6.8 跑 `pytest`；改 schemas/prompt 相关再视情况跑 eval。

---

## 13. 非目标与约束

- 不新增顶层 `ReviewState` 字段；不新增 Java 工具；不让 LLM 自由探索仓库。
- 下游节点不得回写上游 State；所有跨节点关联用稳定 ID。
- Java Gateway 仍只提供事实与护栏，不判断是不是问题。
- 最终 `Issue` 契约与 CLI 输出不变。

## 14. 实施不变量

```text
CandidateIssue(task_id)
  → candidate evidence tag（Planner 内部，不进 State）
  → EvidenceStrategy（静态规则）
  → EvidenceRequest(strategy_id, purpose)
  → EvidenceNote(findings[EvidenceFinding])
  → Verdict(requested_purpose?)
  → ReviewResult
```

- Phase 5 后，任何新增能力必须说明它消费/填充这条链路的哪一项，不能创建平行状态。
- 工作队列只追加；事实源只由所有者写入；下游不改写上游事实。
- 每个子阶段结束先更新主设计实施台账，再进下一子阶段。

---

## 15. 落地台账与复盘

| 子阶段 | 状态 | 实际落地 | commits |
|---|---|---|---|
| 5A-1 模型 | done | `EvidenceRequest(strategy_id,purpose)` 稳定 ID；`EvidenceFinding`；`EvidenceNote.findings`；Judge 补证目的 | `5737c08` |
| 5A-2/4 注册表 | done | 23 个具体 RiskTag + `GENERAL_REVIEW` 全量 counter/support/severity；上游反证与严格工具 allowlist | `574947f`, `4d95443` |
| 5A-3 分类 | done | exact/strong/weak 计分、歧义 LLM 受限分类、GENERAL 安全回退 | `545febc`, `63ec21d` |
| 5A-4 Planner | done | 初轮 counter-first 两遍规划、回环按 requested_purpose 选下一策略、exhausted/invalid trace | `6618c8d`, `d48372e` |
| 5B-1/2/3 运行时迁移 | done | dossier 绑定、策略执行/缓存、结构化 finding、purpose-aware Judge、回 Planner 图接线、旧路径删净 | `f5e98e6`, `0bc4f82` |
| 5B-4 可观测/eval | done | 六个过程指标、稳定 survivor 映射、真实新工具调用 trace、3 个行为样本、报告/归档接线与 CLI 拓扑日志同步 | `34a9b26`, `0a24d89`, `7b495b2` |

实施后顶层 `ReviewState` 与产品 `Issue` 契约均未变化；`EvidenceNote` 载荷一次切换为
`findings[EvidenceFinding]`，旧 builder、总请求截断、字符串 note 状态、自由工具建议和强支持
旧规则均已删除。AUTHORIZATION/TRANSACTION 的 direct counter 被限制在当前方法/类作用域，
无工具、空结果、截断、无法解析和 LLM `None` 都只会产生 `insufficient`。

最终回归为 **593 passed**，Ruff 与 mypy clean，mock CLI EXIT=0。`pipeline-notools` mock
评测完成 **31 cases**，质量 P/R/F1 均为 0（mock 只证明链路，不代表审查质量）；该档没有候选
或工具，过程指标观测到实际 EvidenceAgent 工具调用 **0**，注册表覆盖 **24/24**。配置的
`http://localhost:9090` Gateway 健康检查超时，因此没有伪造 repo-backed/tool-profile 成本数字；
确定性 graph 集成测试证明两个同 task 请求共享一次调用时统计为 **1 次实际调用**，缓存复用不计。
