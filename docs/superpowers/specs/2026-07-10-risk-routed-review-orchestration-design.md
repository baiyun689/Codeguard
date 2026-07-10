# 风险路由驱动的 ReviewTask 编排设计大纲

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

目标形态:

```text
git diff
  → Summary?
  → DiffTaskBuilder
  → RiskTriage
  → TaskRank
  → ContextProvider
  → ReviewCouncil
  → CouncilCoordinator
  → EvidenceAgent
  → CouncilJudge
  → ReviewResult
```

核心原则:

- **ReviewTask 是最小调度单位**: 一个任务通常对应一个 hunk 或一个紧密相关的变更片段。
- **RiskTag 是路由信号，不是问题结论**: 风险标签只说明“应该从哪些角度审”，不说明“这里已经有问题”。
- **ContextBundle 按风险构建**: 上下文构建由 RiskProfile 驱动，而不是盲目读取全项目。
- **State 先稳定，节点内部后增强**: 第一阶段先打通纵向最小闭环，后续只扩充规则、工具和 prompt。
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
    selected_task_ids: list[str]
    skipped_tasks: list[SkippedTask]
    task_context_bundles: dict[str, TaskContextBundle]

    # 现有 ReviewCouncil 裁决链路
    candidate_issues: Annotated[list[CandidateIssue], candidate_reducer]
    evidence_requests: Annotated[list[EvidenceRequest], evidence_request_reducer]
    evidence_notes: Annotated[list[EvidenceNote], operator.add]
    council_verdicts: list[Verdict]
    evidence_round: int
    council_route: str

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
    hunk_header: str
    patch: str
    changed_lines: list[int]
    start_line: int
    end_line: int


class RiskProfile(BaseModel):
    task_id: str
    tag_scores: dict[RiskTag, int]
    signals: list[RiskSignal]
    total_score: int


class RiskSignal(BaseModel):
    tag: RiskTag
    score: int
    source: str
    reason: str
    line: int | None = None


class ReviewBudget(BaseModel):
    max_tasks_to_review: int = 30
    max_tasks_per_file: int = 5
    max_context_chars_per_task: int = 4000
    max_final_issues: int = 10


class SkippedTask(BaseModel):
    task_id: str
    reason: str
    risk_score: int = 0


class TaskContextBundle(BaseModel):
    task_id: str
    file: str
    changed_hunk: str
    risk_tags: list[RiskTag]
    facts: list[ContextFact]
    truncated: bool = False
```

### 3.3 状态写入边界

每个字段只能有一个主写节点。下游节点不得回写上游事实。

| 字段 | 主写节点 | 其他节点 |
|---|---|---|
| `review_tasks` | `DiffTaskBuilder` | 只读 |
| `risk_profiles` | `RiskTriage` | 只读 |
| `selected_task_ids` | `TaskRank` | 只读 |
| `skipped_tasks` | `TaskRank` | 只读 |
| `task_context_bundles` | `ContextProvider` | 只读 |
| `candidate_issues` | `ReviewCouncil` | Evidence / Judge 只读 |
| `evidence_requests` | `ReviewCouncil` / `CouncilJudge` | Coordinator / Evidence 只读 |
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
candidate_issues: list[CandidateIssue(task_id=...)]
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
- 如果无法解析行号，仍生成任务，但在 trace 中标记。

### 4.3 RiskTriage

职责:

- 为每个 `ReviewTask` 产出 `RiskProfile`。
- 规则优先，LLM 辅助留到后续。
- 解释每个风险标签来自哪些 `RiskSignal`。

第一版优先标签:

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

规则扩展接口:

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
- 输出 `selected_task_ids` 和 `skipped_tasks`。
- 集中处理大 diff 降级策略。

排序依据:

```text
risk_score
高危标签
删除保护逻辑
生产代码优先
测试/文档/低价值文件降权
每个文件任务数限制
```

第一版薄实现:

- 默认全选。
- 超过 `max_tasks_to_review` 时按 `total_score` 取 Top-K。

### 4.5 ContextProvider

职责:

- 根据 `selected_task_ids + risk_profiles` 构建 `TaskContextBundle`。
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

第一版薄实现:

- 无工具时: `changed_hunk + diff_summary + existing ContextBundle`。
- 有工具时: 读取当前文件或已有 AST fact，不做跨文件深挖。

### 4.6 ReviewCouncil

职责:

- 消费 `selected_task_ids + risk_profiles + task_context_bundles`。
- 产出 `CandidateIssue` 和 `EvidenceRequest`。
- 保留 ThreatModel / Behavior / Maintainability 三路发现者。

第一版策略:

- 三路审查员仍保留 ReAct 架构。
- 输入从整份 diff 改为 task-aware 审查材料。
- 工具 allowlist 暂时沿用当前发现者配置。

后续策略:

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

- 根据结构化字段决定进入 EvidenceAgent 还是 CouncilJudge。
- 不解析自然语言。

路由:

```text
无 candidate_issues
  → council_judge

有 evidence_requests 且 evidence_round == 0
  → evidence_agent

否则
  → council_judge
```

### 4.8 EvidenceAgent

职责:

- 根据候选问题和证据请求补充支持证据与反证。
- 证据关联 `candidate_id`，间接关联 `task_id`。
- 不直接生成最终 Issue。

后续增强:

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

## 5. 分阶段实施目标

### Phase 1: 状态契约与纵向骨架

目标:

先打通 `ReviewTask → RiskProfile → selected_task_ids → TaskContextBundle → CandidateIssue → ReviewResult` 的最小闭环。

实现内容:

- 新增核心模型和 State 字段。
- 新增 `diff_task_builder`、`risk_triage`、`task_rank` 节点。
- 调整 `context_provider` 和 `review_council` 读取 task-aware state。
- Trace Dashboard 展示 task 数量、risk tags、selected / skipped、context bundle 状态。

非目标:

- 不追求风险规则完整。
- 不追求上下文质量显著提升。
- 不改变最终 `Issue` 产品契约。

验收:

```text
mock 审查通过
现有单测通过
trace 能看到任务化状态流转
最终仍输出 ReviewResult
```

### Phase 2: 风险标签规则体系

目标:

把 `RiskTriage` 从薄实现升级为可扩展规则引擎。

实现内容:

- 建立 `RiskRule` registry。
- 为优先 RiskTag 添加确定性规则。
- 每个 `RiskSignal` 必须有 `source` 和 `reason`。

验收:

```text
新增风险规则不需要改 State 和图
每个 RiskTag 有确定性单测
trace 能解释 tag 来源
```

### Phase 3: TaskRank 与大 diff 预算控制

目标:

让大 diff 进入可控降级模式。

实现内容:

- 引入默认 `ReviewBudget`。
- 实现 Top-K 选择、单文件任务数限制、高风险任务优先。
- 将跳过原因写入 `skipped_tasks`。

验收:

```text
大 diff 不会全量进入 LLM
skipped_tasks 有明确原因
summary / trace 能说明审查范围
```

### Phase 4: ContextProvider 风险感知上下文

目标:

上下文构建从通用上下文升级为按 RiskTag 定向补上下文。

实现内容:

- 建立 tag 到 context strategy 的映射。
- 优先复用已有 `get_diff_ast`、`get_file_content`、`find_callers` 等工具。
- 每个 `TaskContextBundle` 记录来源与截断。

验收:

```text
ContextProvider 不盲目读全项目
不同 RiskTag 触发不同上下文策略
ContextBundle 可以被 trace 清楚解释
```

### Phase 5: ReviewCouncil 定向审查

目标:

三路审查员从全量扫 diff 变成按 task/risk 定向审查。

实现内容:

- Prompt 明确 task、risk tags、context。
- `CandidateIssue` 关联 `task_id`。
- 工具 allowlist 可由 RiskTag 收窄。
- 后续按 RiskTag 控制 reviewer fan-out。

验收:

```text
低风险任务可以少跑 reviewer
高风险任务可以多 reviewer 交叉审
候选问题能追溯到 task 和 risk profile
```

### Phase 6: Evidence / Judge 任务化裁决

目标:

证据校验和最终裁决按 `candidate_id / task_id` 做可解释判断。

实现内容:

- EvidenceAgent 读取 task context 和 risk profile。
- CouncilJudge 使用 task/risk/evidence 做最终裁决。
- 去重、合并、降级逻辑保留在 Judge。

验收:

```text
最终 Issue 不暴露内部 risk tag
裁决理由可在 trace 中解释
误报过滤不回写上游 State
```

### Phase 7: Eval 与 Trace 闭环

目标:

让新架构的效果可观察、可量化。

Trace 展示:

```text
Task 列表
RiskProfile
TaskRank selected/skipped
ContextBundle 来源
Reviewer fan-out
Evidence/Judge 裁决链路
```

Eval 指标:

```text
RiskTag 命中率
高风险任务召回率
selected/skipped 覆盖率
大 diff 降级行为
每类 RiskTag 的 precision / recall
证据覆盖率
```

验收:

```text
一次审查能解释为什么审这个、不审那个
eval 能对比 task-aware 改造前后效果
trace 能帮助调风险规则和上下文策略
```

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

## 8. 推荐实施顺序

不要按“单节点完整实现”推进，而是按纵向切片推进:

```text
1. 状态契约 + 最小全链路
2. 风险规则扩充
3. budget / 大 diff
4. context 策略
5. reviewer 路由
6. evidence / judge 强化
7. eval / trace 完善
```

第一阶段成功的标志不是审查更准，而是这条链路稳定跑通:

```text
ReviewTask
  → RiskProfile
  → selected_task_ids
  → TaskContextBundle
  → CandidateIssue
  → Evidence / Judge
  → ReviewResult
```

后续所有增强都应挂在这条链路上，而不是重新设计 State。
