# 风险路由驱动的 ReviewTask 编排设计

**日期**: 2026-07-10  
**状态**: 设计草案  
**关联背景**: ADR-032 ReviewCouncil、ADR-036 ContextProvider AST 富化、当前 trace 可视化模块

---

## 1. 设计动机

当前 Codeguard 的默认编排已经从旧 Supervisor 迁移到确定性 ReviewCouncil 图，但核心调度单位仍接近“整份 diff + 三路发现者”。这在小 diff 下可运行，在大 diff 下会暴露几个问题：

1. 上下文构建缺少任务级目标，容易在“该补什么上下文”上变得泛化。
2. 三路审查员虽然有方法论分工，但没有明确的风险路由信号。
3. 大 diff 场景缺少统一预算入口，容易出现全量审查、成本不可控或漏掉高风险片段。
4. trace 能展示节点执行过程，但还不能清楚解释“为什么审这个 hunk，不审那个 hunk”。

本设计的目标是引入 **ReviewTask + RiskProfile + TaskContextBundle** 这条中间链路，将审查流程从“整份 diff 审查”升级为“风险路由驱动的任务审查”。

---

## 2. 总体目标

最终拓扑（Phase 1 即固定，后续阶段不再改变主路由）:

```text
git diff
  → Summary?
  → DiffTaskBuilder
  → RiskTriage
  → TaskRank
  → ContextProvider
  → [ThreatModelAgent | BehaviorAgent | MaintainabilityAgent]
  → CouncilCoordinator（显式 fan-in，只在三路完成后运行一次）
  → EvidenceAgent（固定运行一次；没有请求时记录 no-op）
  → CouncilJudge
  → EvidenceAgent（仅 Judge 明确要求补证且未超轮次时回环）
  → ReviewResult
```

核心原则:

- **ReviewTask 是最小调度单位**: 一个任务通常对应一个 hunk 或一个紧密相关的变更片段。
- **RiskTag 是路由信号，不是问题结论**: 风险标签只说明“应该从哪些角度审”，不说明“这里已经有问题”。
- **ContextBundle 按风险构建**: 上下文构建由 RiskProfile 驱动，而不是盲目读取全项目。
- **State 先稳定，节点内部后增强**: Phase 1 一次定义完整状态主干和稳定 ID；Phase 2 起不得新增改变主路由的业务 State 字段。
- **Evidence 必经一次**: 三路发现完成并汇合后，必须进入一次 EvidenceAgent；EvidenceAgent 可以 no-op，但不能被路由跳过。
- **产品输出保持不变**: 最终仍输出 `ReviewResult` / `Issue`，任务、风险、证据等中间态只进入 trace / eval。

---

## 3. 状态设计

### 3.1 最小新增 State 字段

第一版只引入必要字段，避免把 ReviewState 变成不可维护的大对象。

```python
class ReviewState(TypedDict, total=False):
    # 现有输入与配置
    diff_text: str
    diff_summary: str
    enabled_tools: Any
    max_retries: int
    structured_method: str
    react_recursion_limit: int
    max_evidence_rounds: int

    # 新增:任务化与风险路由
    review_budget: ReviewBudget
    review_tasks: list[ReviewTask]
    risk_profiles: dict[str, RiskProfile]
    task_selection: TaskSelection
    task_context_bundles: dict[str, TaskContextBundle]

    # 现有 ReviewCouncil 裁决链路
    candidate_issues: Annotated[list[CandidateIssue], candidate_reducer]
    evidence_requests: Annotated[list[EvidenceRequest], evidence_request_reducer]
    evidence_notes: Annotated[list[EvidenceNote], operator.add]
    council_verdicts: list[Verdict]
    evidence_round: int

    # 输出与诊断
    gathered_context: Annotated[list, dedup_gathered_reducer]
    council_trace: Annotated[list[CouncilTrace], operator.add]
    final_issues: list[Issue]
    summary: str
    council_stats: CouncilRunStats
```

### 3.2 核心新增模型

```python
class ReviewTask(BaseModel):
    id: str
    file: str
    hunk_header: str = ""
    patch: str
    changed_lines: list[int]


class RiskProfile(BaseModel):
    task_id: str
    tag_scores: dict[RiskTag, int] = Field(default_factory=dict)
    signals: list[RiskSignal] = Field(default_factory=list)


class RiskSignal(BaseModel):
    tag: RiskTag
    score: int
    source: str
    reason: str
    line: int | None = None


class ReviewBudget(BaseModel):
    # None 表示当前策略不施加该项限制；Phase 1 的基线是全选。
    max_tasks_to_review: int | None = None
    max_tasks_per_file: int | None = None
    max_context_chars_per_task: int | None = None
    max_final_issues: int | None = None


class SkippedTask(BaseModel):
    task_id: str
    reason: str
    risk_score: int = 0


class TaskSelection(BaseModel):
    selected_task_ids: list[str]
    skipped_tasks: list[SkippedTask] = Field(default_factory=list)


class TaskContextBundle(BaseModel):
    task_id: str
    facts: list[ContextFact] = Field(default_factory=list)
    truncated: bool = False
```

`TaskContextBundle` 不重复保存 file、patch 或 RiskTag。这些是 `ReviewTask` 和
`RiskProfile` 的唯一事实源；消费者通过 `task_id` 关联读取，避免多份状态漂移。

`RiskProfile` 不保存 `total_score`。分数是 `TaskRank` 基于 `tag_scores` 和当前
`ReviewBudget` 派生的临时计算结果，不应成为第二份可变事实。

`CandidateIssue.task_id` 在 Phase 1 即为必填字段。对仍以整份 diff 作为输入的
发现者，由收集节点以 `file + changed line` 映射到任务；DiffTaskBuilder 必须为每个
变更文件提供可映射的 fallback task。无法映射的候选不得进入共享黑板，并在 trace
中记录拒绝原因。

### 3.3 状态写入边界

事实类字段只能有一个所有者；下游节点不得回写上游事实。追加式工作队列可以有多个
列明的生产者，但只允许追加新条目，不允许修改或覆盖已有条目。

| 字段 | 主写节点 | 其他节点 |
|---|---|---|
| `review_tasks` | `DiffTaskBuilder` | 只读 |
| `risk_profiles` | `RiskTriage` | 只读 |
| `task_selection` | `TaskRank` | 只读 |
| `task_context_bundles` | `ContextProvider` | 只读 |
| `candidate_issues` | `ReviewCouncil` | Evidence / Judge 只读 |
| `evidence_requests` | `ReviewCouncil` / `CouncilJudge`（追加） | Coordinator / Evidence 只读 |
| `evidence_notes` | `EvidenceAgent` | Judge 只读 |
| `council_verdicts` | `CouncilJudge` | 只读 |
| `final_issues` | `CouncilJudge` | CLI / eval 只读 |

避免的设计:

```python
# 不推荐:胖任务对象
ReviewTask.risk_profile
ReviewTask.context_bundle
ReviewTask.findings
ReviewTask.verdict
```

推荐的 normalized state:

```python
review_tasks: list[ReviewTask]
risk_profiles: dict[task_id, RiskProfile]
task_context_bundles: dict[task_id, TaskContextBundle]
candidate_issues: list[CandidateIssue(task_id=...)]  # task_id 必填
evidence_notes: list[EvidenceNote(candidate_id=...)]
```

---

## 4. 节点职责

### 4.1 Summary

沿用现有 SummaryStage。它产出全局 diff 摘要，作为风险识别、上下文构建和审查 prompt 的辅助背景。

### 4.2 DiffTaskBuilder

职责:

- 解析 unified diff。
- 按文件和 hunk 生成 `ReviewTask`。
- 记录 changed line 范围，保留 GitHub diff 行级评论所需的未来扩展空间。

不负责:

- 不判断风险。
- 不读取仓库文件。
- 不调用 LLM。

第一版薄实现:

- 每个 hunk 一个 `ReviewTask`。
- 如果无法解析 hunk 行号，生成文件级 fallback task，并在 trace 中标记。

### 4.3 RiskTriage

职责:

- 为每个 `ReviewTask` 产出 `RiskProfile`。
- 规则优先，LLM 辅助留到后续。
- 解释每个风险标签来自哪些 `RiskSignal`。

Phase 2 优先标签:

```text
AUTHORIZATION
INPUT_VALIDATION
SQL_DATA_ACCESS
TRANSACTION
IDEMPOTENCY
CACHE_CONSISTENCY
MESSAGE_QUEUE
ERROR_HANDLING
NULL_SAFETY
CONFIG_SECURITY
MAINTAINABILITY
```

Phase 2 规则扩展接口:

```python
RiskRule = Callable[[ReviewTask], list[RiskSignal]]

RISK_RULES = [
    authorization_rule,
    transaction_rule,
    sql_access_rule,
    cache_rule,
    message_queue_rule,
]
```

不负责:

- 不决定是否审查任务。
- 不生成问题结论。
- 不改写 ReviewTask。

### 4.4 TaskRank

职责:

- 根据 `RiskProfile` 和 `ReviewBudget` 选择进入深审的任务。
- 输出唯一的 `TaskSelection` 决策。
- 集中处理大 diff 降级策略。

Phase 2 排序依据:

```text
risk_score
高危标签
删除保护逻辑
生产代码优先
测试/文档/低价值文件降权
每个文件任务数限制
```

Phase 1 基线:

- 默认全选。
- 不启用预算限制；Phase 2 再按派生风险分数和预算启用 Top-K。

### 4.5 ContextProvider

职责:

- 根据 `task_selection.selected_task_ids + risk_profiles` 构建 `TaskContextBundle`。
- 将上下文来源和截断情况写清楚。
- 工具调用仍走 Java Gateway 沙箱。

风险标签到上下文策略:

```text
AUTHORIZATION
  → Controller 方法、权限注解、拦截器、路由配置

TRANSACTION
  → 方法体、类注解、@Transactional、异常处理、写库操作

IDEMPOTENCY
  → 唯一索引、Redis setnx、重复提交保护

SQL_DATA_ACCESS
  → Mapper、SQL、实体字段、查询条件、索引线索

MESSAGE_QUEUE
  → consumer、ack、retry、dead letter、消费幂等

CACHE_CONSISTENCY
  → cache key、删除/更新时机、DB 更新逻辑
```

Phase 1 基线:

- 无工具时: `changed_hunk + diff_summary + existing ContextBundle`。
- 有工具时: 读取当前文件或已有 AST fact，不做跨文件深挖。

Phase 3 才按 RiskTag 替换为定向 context strategy。

### 4.6 ReviewCouncil

职责:

- 消费 `task_selection + risk_profiles + task_context_bundles`。
- 产出 `CandidateIssue` 和 `EvidenceRequest`。
- 保留 ThreatModel / Behavior / Maintainability 三路发现者。

Phase 1 基线:

- 三路审查员仍保留 ReAct 架构。
- 输入仍是整份 diff；收集节点为每个 CandidateIssue 回填必填 task_id。
- 工具 allowlist 暂时沿用当前发现者配置。

Phase 4 策略:

```text
AUTHORIZATION / CONFIG_SECURITY
  → ThreatModelAgent + BehaviorAgent

TRANSACTION / IDEMPOTENCY / MESSAGE_QUEUE
  → BehaviorAgent

SQL_DATA_ACCESS
  → ThreatModelAgent + BehaviorAgent

MAINTAINABILITY / PERFORMANCE / ERROR_HANDLING
  → MaintainabilityAgent + BehaviorAgent
```

### 4.7 CouncilCoordinator

职责:

- 作为三路发现者的显式 fan-in barrier，只在所有发现者结束后运行一次。
- 记录本轮候选和证据请求的批次统计。
- 固定转入 EvidenceAgent；不承担“是否跳过首次补证”的路由决策，也不解析自然语言。

### 4.8 EvidenceAgent

职责:

- 根据候选问题和证据请求补充支持证据与反证。
- 证据关联 `candidate_id`，间接关联 `task_id`。
- 不直接生成最终 Issue。

Phase 5 增强:

- 证据策略按 RiskTag 分流。
- 对 AUTHORIZATION 查上游鉴权。
- 对 TRANSACTION 查外层事务。
- 对 IDEMPOTENCY 查唯一索引和查重逻辑。

### 4.9 CouncilJudge

职责:

- 统一做最终裁决、去重、降级、合并和输出转换。
- 最终写入 `final_issues` 和 `summary`。
- 保持产品输出 `Issue` 不变。

裁决规则:

```text
不是本次 diff 引入 → drop
无法绑定 changed line → drop
证据不足 → drop / downgrade
存在上游保护 → drop
同根因重复 → merge
```

---

## 5. 分阶段实施计划

阶段不是独立产品版本，而是对同一最终编排的连续构建层。Phase 1 建立完整骨架；从
Phase 2 起，每个阶段只能填充已有对象、替换节点内部策略或增强派生观测，不能新增改变
业务路由的 State 字段。

### Phase 1: 冻结状态契约与最终拓扑

实现内容:

- 一次引入 `ReviewBudget`、`ReviewTask`、`RiskProfile`、`TaskSelection`、`TaskContextBundle`。
- 将 `CandidateIssue.task_id` 设为必填，完成候选到任务的确定性映射与 fallback task。
- 新增 DiffTaskBuilder、RiskTriage、TaskRank 节点的最小实现；全量任务默认选中，RiskProfile 可为空。
- 采用显式三路 fan-in；三路发现后固定进入一次 EvidenceAgent，再进入 CouncilJudge。
- 移除仅为条件跳转服务的 `council_route` 状态；首次 Evidence 不再由条件路由决定。

非目标:

- 不追求风险规则覆盖率、预算效果或上下文质量。
- ReviewCouncil 暂保持整份 diff 的审查粒度，task_id 由收集节点回填。
- 不改变 `ReviewResult` / `Issue` 产品契约。

完成条件:

- 图结构测试证明 coordinator 在三路发现结束后只运行一次，EvidenceAgent 首次必经。
- 所有候选均有 task_id，或被显式拒绝并留下 trace。
- 新旧 mock 路径仍能返回 `ReviewResult`。

### Phase 2: 完成任务准备链

实现内容:

- 完善 unified diff 到 hunk/fallback task 的解析与稳定 ID。
- 建立 `RiskRule` registry，逐步填充 AUTHORIZATION、INPUT_VALIDATION、SQL_DATA_ACCESS、
  TRANSACTION、IDEMPOTENCY、CACHE_CONSISTENCY、MESSAGE_QUEUE、ERROR_HANDLING、
  NULL_SAFETY、CONFIG_SECURITY、MAINTAINABILITY 等规则。
- 启用 ReviewBudget 的默认策略，实现 Top-K、单文件上限、生产代码优先与明确的跳过原因。

完成条件:

- 新风险规则只新增 rule，不修改 State、图或下游接口。
- TaskRank 的每个选择和跳过都能由 RiskProfile 与 ReviewBudget 解释。

### Phase 3: 完成风险感知上下文链

实现内容:

- 建立 RiskTag 到 context strategy 的注册表。
- ContextProvider 只为 `task_selection.selected_task_ids` 构建 TaskContextBundle。
- 按策略经 Java Gateway 收集事实，并在 bundle 中记录来源和截断；不把 task 或风险事实复制进去。

完成条件:

- 不同标签选择不同的补查策略，且工具调用受 task 预算限制。
- ContextProvider 不改变 ReviewTask、RiskProfile 或 TaskSelection。

### Phase 4: 完成定向发现链

实现内容:

- Reviewer 输入改为 task + risk profile + task context，而不是整份 diff 的自由扫描。
- reviewer fan-out 与工具 allowlist 从已有状态纯计算，不引入 `assignment` 类 State 字段。
- 低风险任务可减少 reviewer，高风险任务可交叉审；CandidateIssue 直接携带来源 task_id。

完成条件:

- 每个候选可回溯到 task、风险信号和上下文来源。
- 审查员分派变化不影响 Evidence/Judge 的输入契约。

### Phase 5: 完成任务化证据与裁决链

实现内容:

- EvidenceAgent 通过 `candidate_id → task_id` 读取风险和任务上下文，按 RiskTag 选择补证策略。
- CouncilJudge 将 task、risk、context、evidence 统一用于 keep/drop/downgrade/merge 决策。
- 保留 Judge 发起额外补证的有限回环；任何额外请求仍只追加到既有 `evidence_requests` 队列。

完成条件:

- 首次 Evidence 始终存在，额外 Evidence 只由 Judge 的结构化 verdict 触发。
- 最终 Issue 不暴露内部 RiskTag，且 Judge 不回写上游事实。

### Phase 6: 完成 Trace 与 Eval 闭环

实现内容:

- Trace Dashboard 展示任务、风险信号、选择/跳过、上下文来源、reviewer fan-out、
  Evidence/Judge 链路。
- 增加 RiskTag 命中率、高风险任务召回、选择覆盖率、大 diff 降级、证据覆盖率等评测指标。

完成条件:

- 一次审查能回答“为什么审这个任务、为什么跳过那个任务、证据如何影响裁决”。
- 能对比 task-aware 改造前后的质量和成本行为。

### 实施台账

每次完成一个阶段，必须在本节更新事实，不以设计文字替代实施记录：

| 阶段 | 当前状态 | 已落地内容 | State 变更 | 验证证据 | 刻意未做 |
|---|---|---|---|---|---|
| Phase 1 | planned | 无 | 未开始 | 无 | 见本阶段非目标 |
| Phase 2 | planned | 无 | 禁止新增主路由 State | 无 | 见本阶段实现内容之外的规则 |
| Phase 3 | planned | 无 | 禁止新增主路由 State | 无 | 跨项目深挖以外的策略 |
| Phase 4 | planned | 无 | 禁止新增主路由 State | 无 | Evidence/Judge 策略 |
| Phase 5 | planned | 无 | 禁止新增主路由 State | 无 | Dashboard 与质量指标 |
| Phase 6 | planned | 无 | 仅派生观测，不加业务 State | 无 | 无 |

台账的“已落地内容”必须写入具体节点/模型/配置；“验证证据”必须写入测试、评测或 trace
样本及对应 commit。没有这些证据时只能保持 `planned` 或 `in_progress`，不得标记完成。

---

## 6. ReAct 与工具策略

本设计不取消三路审查员的 ReAct 架构，也不取消工具。

变化是:

```text
旧模式:
  整份 diff → reviewer 自由探索 → 工具自由调用

新模式:
  selected task + risk profile + task context
    → reviewer 定向审查
    → 必要时少量补查
```

职责分层:

```text
RiskTriage
  判断该 task 应从哪些风险角度审

ContextProvider
  做第一轮确定性上下文收集

Reviewer ReAct
  在已有上下文上推理，并少量补查关键事实

EvidenceAgent
  对候选问题做支持证据和反证确认
```

后续可根据 RiskTag 收窄工具 allowlist，例如:

```text
AUTHORIZATION
  → get_file_content, find_callers, get_diff_ast

SQL_DATA_ACCESS
  → get_file_content, find_sensitive_apis, get_code_metrics

IDEMPOTENCY
  → get_file_content, find_callers, get_diff_ast

MAINTAINABILITY
  → get_file_content, get_code_metrics
```

---

## 7. 风险与约束

### 7.1 主要风险

- State 字段过多导致节点边界模糊。
- RiskTag 被误用为问题结论。
- ContextProvider 策略过早做复杂，导致工具调用成本不可控。
- ReviewCouncil 同时重构输入、prompt、工具路由，造成质量回归难定位。

### 7.2 约束

- 第一阶段只做薄实现，重点固定契约。
- 下游节点不得回写上游 State。
- 所有跨节点关联使用稳定 ID。
- 最终 `Issue` 契约不变。
- Java Gateway 仍只提供事实与护栏，不判断是不是问题。

---

## 8. 实施不变量

```text
ReviewTask
  → RiskProfile
  → TaskSelection
  → TaskContextBundle
  → CandidateIssue(task_id)
  → EvidenceRequest(candidate_id)
  → EvidenceNote(candidate_id)
  → Verdict(candidate_id)
  → ReviewResult
```

- Phase 1 后，任何新增能力必须说明它消费和填充这条链路的哪一项；不能创建平行状态。
- reviewer 分派、工具 allowlist、上下文策略、风险分数和评测指标均为已有状态的派生物。
- 工作队列只追加；事实源只由其所有者写入；下游不得改写上游事实。
- 每个阶段结束先更新实施台账，再讨论下一阶段；台账是后续工作的唯一实施事实来源。
