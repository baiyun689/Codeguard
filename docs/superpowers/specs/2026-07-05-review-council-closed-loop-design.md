# ReviewCouncil 闭环裁决 · 设计文档

> 日期：2026-07-05
> 关联 ADR：ADR-032（外层拓扑与角色定义）
> 状态：设计审批中

---

## 1. 问题诊断

ADR-032 的图拓扑已落地，但三个 council 节点（coordinator / evidence_agent / challenge_agent）都是纯规则实现，self_checker 是旧 AggregationStage + FalsePositiveFilterStage 的薄包装，与 council 前半段脱节。具体问题：

| 节点 | 当前状态 | 核心缺陷 |
|------|---------|---------|
| **evidence_agent** | 只看 `kind=="related_snippet"` 调 `get_file_content`，其余在 ContextBundle 里搜字符串 | `preferred_tools` 字段存在但从未读取；4 个 Java 工具只用了 1 个；发现者的工具限制在此无法被弥补 |
| **challenge_agent** | 三条硬规则（confidence<0.35→drop, quality+missing→drop, no-evidence→needs_more） | 不做语义合并、不做降级、不做反证判断；`downgrade`/`merge` verdict 从未被使用 |
| **self_checker** | 包装旧 AggregationStage + FalsePositiveFilterStage | 不知道候选经历了 evidence 补充和 challenge 质疑；去重纯靠文件+行号规则，不看语义 |
| **整体** | `max_evidence_rounds=1`，evidence→challenge 一圈就强制结束 | 没有真正的"补证→再质疑"对抗循环 |

**旧管线（阶段 2）的致命缺陷**也在此暴露：三个发现者各只有 2 个工具（专属 + `get_file_content`），跨域交叉验证无法发生。ThreatModelAgent 不能查调用链，BehaviorAgent 不能查敏感 API——同一个可疑方法在两个发现者眼里各自只能看到一半事实。

---

## 2. 设计原则

> **规则负责稳定性，LLM 负责开放语义。**

| 层 | 做什么 | 不做什么 |
|----|--------|---------|
| 规则 | 确定性的、可复现的判断（无效文件、无证据、已命中保护逻辑……） | 不试图穷举所有代码审查场景 |
| LLM | 语义合并、证据综合评判、级别校准、规则判定不了的模糊情况 | 不替代规则做确定性判断 |
| 兜底 | 规则不命中 → LLM；LLM 失败 → 保守保留（宁可多报不漏报） | 不静默丢弃 |

---

## 3. 图拓扑变更

### 当前拓扑

```
discover_* ─→ council_coordinator ─→ evidence_agent ─┐
              ↑       ↑                              │
              └───────┴────── challenge_agent ←───────┘
                              │
                          self_checker → END
```

### 目标拓扑

```
                    ┌── evidence_agent ←──┐
                    │        │            │
discover_* ─→ council_coordinator        │ (needs_more + round < max)
                    │        │            │
                    │   council_judge ────┘
                    │        │
                    └── END (otherwise)
```

**边说明**：
- `discover_* → coordinator`：自动边（fan-in）
- `coordinator → evidence_agent`：条件边（`_route_after_coordinator`，仅 evidence_round==0）
- `coordinator → council_judge`：条件边（同上，其余情况）
- `evidence_agent → coordinator`：自动边
- `council_judge → coordinator`：条件边（`_route_after_council_judge`：needs_more 且 round<max→evidence_agent；否则→END）

**变更**：
- `challenge_agent` 和 `self_checker` 合并为一个 `council_judge` 节点
- council_judge 不直接到 END——回到 coordinator，由 coordinator 决定结束还是再补证
- State 新增 `judge_pass: int` 计数器（防止 council_judge→coordinator 死循环）

### 路由逻辑（coordinator，确定性）

```python
def _route_after_coordinator(state) -> str:
    """从 discover 节点或 evidence_agent 返回后，决定下一步。"""
    candidates = state.get("candidate_issues") or []
    if not candidates:
        return "council_judge"              # fast path

    # 仅第一轮（evidence_round==0）自动进 evidence_agent。
    # 后续轮次的补证由 _route_after_council_judge 触发；
    # 避免 evidence_requests 因 reducer 累积导致反复路由。
    evidence_round = state.get("evidence_round", 0)
    if evidence_round == 0:
        pending = state.get("evidence_requests") or []
        if pending:
            return "evidence_agent"

    return "council_judge"


def _route_after_council_judge(state) -> str:
    """council_judge 之后：需要补证且轮次未超 → 再进 evidence；否则 END。"""
    verdicts = state.get("council_verdicts") or []
    evidence_round = state.get("evidence_round", 0)
    max_rounds = state.get("max_evidence_rounds", 2)

    has_needs_more = any(
        getattr(v, "action", "") == "needs_more_evidence" for v in verdicts
    )
    if has_needs_more and evidence_round < max_rounds:
        # council_judge 产出的 needs_more_evidence 已附带 suggested_tools，
        # 这些在 council_judge 内部追加到 evidence_requests，供下轮 evidence_agent 使用
        return "evidence_agent"
    return "END"
```

**关键点**：
- `_route_after_coordinator` 只在 `evidence_round==0` 时自动进 evidence——避免 evidence_requests 累积导致死循环
- 后续补证只能由 `_route_after_council_judge` 触发（council_judge 判了 `needs_more_evidence`）
- council_judge 判 `needs_more_evidence` 时，附带 `suggested_tools` 并追加到 `evidence_requests`，下一轮 evidence_agent 调用对应工具

**从 council_judge 出来的边是条件边**（`_route_after_council_judge`），不是固定边。

**默认 `max_evidence_rounds=2`**：初始一轮 + council_judge 要求补证后最多再一轮。两轮后无论是否还有 needs_more_evidence 都进 END。

---

## 4. 节点详细设计

### 4.1 EvidenceAgent（确定性改造，零 LLM）

**职责**：按 `preferred_tools` 调用全量 Java 工具补证，产出 `EvidenceNote`。

**输入**：`evidence_requests`, `candidate_issues`, `context_bundle`
**输出**：`evidence_notes`, `gathered_context`, `evidence_round+1`

**路由表**（确定性，不查 kind，只查 preferred_tools）：

| preferred_tools 含 | 调用 | 参数 |
|---|---|---|
| `find_sensitive_apis` | `tool_client.find_sensitive_apis()` | candidate.file |
| `find_callers` | `tool_client.find_callers()` | candidate.file, candidate.line |
| `get_code_metrics` | `tool_client.get_code_metrics()` | candidate.file |
| `get_file_content` | `tool_client.get_file_content()` | req.target |

**处理流程**：

```
for each EvidenceRequest:
  1. 查 preferred_tools，调对应工具（去重：同一文件+工具不重复调）
  2. 工具结果分类写入 EvidenceNote：
     - 工具成功返回相关结果 → supports
     - 工具返回空/无关 → unknowns
     - 工具调用失败 → unknowns（标记 "tool_error"）
  3. ContextBundle 兜底：preferred_tools 为空或工具不可用时，
     在 ContextBundle.render() 中搜索 req.target
  4. 不需要 LLM——所有分支都是确定性的
```

**EvidenceNote 结构保持不变**（当前已有 supports/contradicts/unknowns/evidence_ids/status）。

**`from_issue()` 按 source_agent 分派 preferred_tools**：

| source_agent | 默认 preferred_tools |
|---|---|
| `threat_model` | `["find_sensitive_apis", "get_file_content"]` |
| `behavior` | `["find_callers", "get_file_content"]` |
| `maintainability` | `["get_code_metrics", "get_file_content"]` |

低置信度（<0.75）或无行号的候选额外追加 `get_file_content`。

---

### 4.2 CouncilJudge（合并 challenge + self_checker，规则 + LLM）

**职责**：对候选做裁决，直接产出 `final_issues`。

**输入**：`candidate_issues`, `evidence_notes`, `context_bundle`, `judge_pass`
**输出**：`council_verdicts`（供 coordinator 路由判断）, `final_issues`（最后一轮才有意义）, `council_stats`, `summary`, `evidence_requests`（needs_more_evidence 时追加）, `judge_pass+1`

**两阶段处理**：

#### 阶段 1：规则列表（确定性、可复现）

每条规则签名为 `(CandidateIssue, list[EvidenceNote]) -> Verdict | None`。返回 `None` 表示"不命中，交给下一条规则或 LLM"。

```
规则优先级从高到低：
1. 文件路径无效 → drop（"候选指向不存在的文件"）
2. 有 contradicts 证据 + 低置信度(<0.5) → drop（"证据直接否定候选"）
3. evidence 全部 not_found + confidence<0.5 → drop（"无法获取任何支持证据"）
4. quality 类 + evidence 无维护成本数据 → drop（"缺少维护性量化证据"）
5. 已命中保护逻辑（sanitize/try-catch/校验）在 evidence 中 → downgrade 或 drop
6. 两条候选 file+line 相邻(容差 5 行) + type 相同 → merge
7. CRITICAL 但 evidence 只有 partial → downgrade（"高危判定但证据不足"）
```

> 注：`needs_more_evidence` 不由规则层产出——规则层只能基于**已有证据**做判断。需要补证的情况由 LLM 阶段判断（语义上觉得证据不够→needs_more），规则层不代劳。

命中的规则产出 `Verdict(candidate_id, action, reason_code, reason, ...)`。

**Verdict 数据结构**：

```python
@dataclass
class Verdict:
    candidate_id: str
    action: Literal["keep", "drop", "downgrade", "merge", "needs_more_evidence"]
    reason_code: str          # 规则名，如 "invalid_file", "contradicted", "low_confidence_no_evidence"
    reason: str               # 人可读的理由
    suggested_target_id: str  # merge 时指向被合并方
    severity_override: Severity | None  # downgrade 时建议新级别
    suggested_tools: list[str]  # needs_more_evidence 时建议补证工具
```

**规则不命中 → 进入阶段 2（LLM）**。

#### 阶段 2：LLM 终审

**输入**（拼进 prompt）：
- 所有未被规则处理的候选（含 evidence_notes）
- 已被规则 merge/downgrade 的候选（供 LLM 参考，防止重复判断）
- ContextBundle.render(3000)

**LLM 职责**：
1. **语义去重**：判断两条候选是否在说同一件事（即使行号不同、来源 Agent 不同） → merge
2. **证据综合评判**：evidence 部分支持、部分 unknown → 综合判断该 keep/downgrade/drop
3. **级别校准**：候选声称 CRITICAL 但证据显示影响有限 → downgrade
4. **不确定兜底**：证据不足以判定 → keep（保守策略：宁可多报不漏报）

**LLM 输出结构**：

```python
class JudgeDecision(BaseModel):
    candidate_id: str
    action: Literal["keep", "drop", "downgrade", "merge"]
    reason: str
    merge_target_id: str = ""
    adjusted_severity: Severity | None = None
```

#### 合并与输出

1. 规则命中 + LLM 判定 → 合并为统一裁决列表（写入 `council_verdicts`）
2. merge：保留一条，合并 evidence_ids 和 claim
3. downgrade：调整 severity
4. drop：移除
5. keep：原样保留
6. **needs_more_evidence**：生成新的 `EvidenceRequest`，`preferred_tools` 来自 `Verdict.suggested_tools`，追加到 state 的 `evidence_requests`（下轮 evidence_agent 消费）
7. 转换为 `Issue` 列表 → `final_issues`
8. 汇总 `CouncilRunStats`
9. `judge_pass += 1`

> **注**：`final_issues` 每轮都产出（覆盖前一轮）。只有最后一轮（coordinator 路由到 END）的 `final_issues` 会被 `PipelineOrchestrator.run()` 读取返回。中间轮的 `final_issues` 被后续轮覆盖。

#### LLM 不可用或失败时的兜底

- 规则命中 → 采纳规则结果
- 规则未命中 → keep（保守保留）
- 不做语义去重（规则去重已在 AggregationStage 覆盖确定性情况）

---

### 4.3 Coordinator（保持确定性）

**改动**：
- `max_evidence_rounds` 默认值从 1 改为 2
- 路由目标从 `{evidence_agent, challenge_agent, self_checker}` 改为 `{evidence_agent, council_judge}`
- 路由逻辑简化（见第 3 节）

**不改**：coordinator 永远不做 LLM 调用。

---

## 5. 数据模型变更

### 删除

- **`EvidenceKind` 字面量**（10 个值，仅 `related_snippet` 被实际创建）
- **`EvidenceRequest.kind` 字段**

### 修改

**`EvidenceRequest`**：

```python
class EvidenceRequest(BaseModel):
    candidate_id: str
    target: str = ""
    question: str = ""
    reason: str = ""
    preferred_tools: list[str] = Field(default_factory=list)  # 路由依据
    reason_code: str = ""
```

**`CandidateIssue.from_issue()`**：按 `source_agent` 分派 `preferred_tools`（见 4.1）。

### 新增

**`Verdict`**（dataclass，规则层内部使用）：

```python
@dataclass
class Verdict:
    candidate_id: str
    action: Literal["keep", "drop", "downgrade", "merge", "needs_more_evidence"]
    reason_code: str
    reason: str
    suggested_target_id: str = ""
    severity_override: Severity | None = None
    suggested_tools: list[str] = field(default_factory=list)
```

**`JudgeDecision`**（pydantic，LLM 结构化输出）：

```python
class JudgeDecision(BaseModel):
    candidate_id: str
    action: Literal["keep", "drop", "downgrade", "merge"]
    reason: str
    merge_target_id: str = ""
    adjusted_severity: Severity | None = None
```

### ReviewState 新增字段

```python
council_verdicts: list[Verdict]    # council_judge 产出，供 coordinator 路由判断
judge_pass: int                    # council_judge 执行次数计数器，初始 0
```

### 不动

- `Challenge` 模型保留（Verdict 替代其功能，但保留旧模型兼容 eval schema 中的 `CouncilTraceStats`）
- `EvidenceNote` 结构不动
- `CandidateIssue` 除 `from_issue()` 外不动
- `CouncilRunStats` 不动

---

## 6. 不改的范围（明确边界）

| 不做的 | 原因 |
|--------|------|
| ContextProvider 升级为 v2（repo_graph / fact_index / static_facts） | 当前 ContextBundle 对 council 阶段够用；先跑稳 evidence/challenge/裁决闭环再考虑 |
| 规则注册表 / YAML 配置 / Rule 基类体系 | 规则不到 10 条，普通函数列表足够；真涨到 30+ 条再抽象 |
| HITL / checkpoint | ADR-032 决策 7 已明确后置 |
| 新 Java 工具 | 现有 4 个工具覆盖当前需求 |
| rempo/repo-map 从发现者工具降级为 ContextProvider 内部 | 改动面太大，且跟此轮改动无关 |
| 多轮无限循环 | 2 轮硬上限，不搞动态循环 |

---

## 7. 改动文件清单

| 文件 | 改动 |
|------|------|
| `models/council.py` | 删除 EvidenceKind；EvidenceRequest 去掉 kind；新增 Verdict dataclass、JudgeDecision pydantic；修改 from_issue() preferred_tools 分派 |
| `pipeline/graph.py` | 删除 `_challenge_agent_node()`；重写 `_evidence_agent_node()`（preferred_tools 路由）；新增 `_council_judge_node(llm)`；修改 coordinator 路由；修改 `build_review_graph()` 拓扑 |
| `pipeline/orchestrator.py` | `max_evidence_rounds` 默认值改为 2；challenge_agent→council_judge 命名对齐 |
| `pipeline/stages/self_checker.py` | 保留旧 `SelfCheckerStage` 不动（作为 fallback / 旧路径兼容）；新 `council_judge_node` 在 graph.py 中实现 |
| `tests/test_graph_orchestration.py` | 更新测试：challenge_agent → council_judge；新增 evidence preferred_tools 路由测试；新增 Verdict 规则命中/不命中测试 |

---

## 8. 测试策略

### 单元测试

- **EvidenceAgent 路由**：mock tool_client，验证 preferred_tools 含 `find_callers` 时调了 `find_callers`，含 `find_sensitive_apis` 时调了 `find_sensitive_apis`
- **规则列表**：每条规则独立测试——给定特定候选+evidence，验证返回正确的 Verdict 或 None
- **规则不命中兜底**：规则全部返回 None 时 → 候选进入 LLM 阶段
- **from_issue()**：各 source_agent 产出正确的 preferred_tools

### 集成测试

- **mock 端到端**：已有 `test_graph_orchestration.py` 中的 mock e2e 测试，更新节点名和路由预期
- **规则 + LLM fallback**：mock LLM 失败 → 验证兜底行为（规则命中采纳规则，规则未命中 keep）

### 不做的测试

- 不在本轮引入真实 LLM 的集成测试（已有 eval 框架 `evals/` 做评测，不在单测范围）

---

## 9. 与 ADR-032 的关系

本设计是 ADR-032 "后续优化策略"中 P1 项的落地：
- "EvidenceAgent 证据路由与证据质量"
- "CouncilCoordinator 调度规则"
- 部分覆盖 "SelfChecker 裁决语义"（合并进 council_judge）

ADR-032 的决策 1-7 均不推翻。本设计不另开新 ADR，作为 ADR-032 的深化实现文档。
