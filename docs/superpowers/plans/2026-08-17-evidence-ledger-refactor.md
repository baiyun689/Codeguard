# Evidence Ledger 证据链重构实施计划

> **状态：** ✅ 已实现(2026-08-17,5 commits 先加后换,master 直推)
> **日期：** 2026-08-17
> **适用范围：** `services/agent` Python 审查管线
> **执行者说明：** 本文面向后续编码 Agent。按 Task 顺序实施；每个 Task 完成后执行指定测试，保持主分支始终可运行。不要恢复已经废弃的多阶段 Concern/Strategist/Researcher/ImpactAssessor 设计。
> **取代关系：** 本计划取代 `docs/superpowers/plans/2026-08-16-evidence-chain-simplification.md` 中基于 `EvidenceTraceStep + located + 全量重放 + recipe` 的实现方案。旧文档仅保留历史记录，不再是实现依据。

## 已落地修正(实现时拍板的与本文档的差异)

1. **任务重排 13→5 commit(先加后换)**:原 Task 1 删模型与 Task 5/7 换发现者/Verifier 之间管线必断,
   违反"master 每 commit 全绿"约定。实际切分为:①内容寻址 Artifact 基础设施 → ②P/C/T 目录注册
   → ③原子切换(模型删除+绑定器+Verifier+Judge+prompt) → ④图状态/统计/Trace 收口 → ⑤文档同步。
2. **两条门控结构性失效,显式移除**:evidence_insufficient / no_supporting_evidence 不再是"被删",
   而是新设计下物理无法开火——patch 自动绑定后"有无证据"无法确定性回答"支不支持",支持判定是
   语义判断,整体移交 Judge。补偿机制:Judge 合同校验(keep 必引用支持事实)+ fail-closed +
   `judge_no_support_drop_count` 观测。guard 反证(§7.5)保留为裁决前确定性淘汰。
3. **Catalog 渲染预算**:合成期 `<evidence_catalog>` 不渲染 patch 全文(P01 已在 task_patch 标签),
   图 payload 复用摘要压缩(保留 status/coverage/scope/subject/relationships/limitations),
   文件 payload 截 2000 字符,总硬上限按 Cxx→Txx 顺序逐条截断(渲染发生在引用已知前,
   截断规则只能是"顺序+长度"式)。
4. **别名回显**:工具返回消息追加"[证据编号 T0n]"(完整新文件读取回显 P01),LLM 探索时即知编号;
   引用声明仍在合成期统一做(方案甲)。实时笔记(方案乙)未实现,若单 case 验证发现"漏选/选错 ref"
   是主要失败模式再评估。
5. **Judge 合同补 LOCATION 规则**:keep 的 supporting 不能全是 LOCATION 角色(否则 §8.3 是死规则)。
6. **重放缓存批内共享**:相同异常 Artifact 被多个候选引用时只执行一次,后续候选复用确认结果。
7. **消融档保留 keep+提案基线语义**:direct_judge 在 LLM 不可用时仍 keep+提案严重度
   (与完整档 fail-closed 不同——这是刻意保留的消融基线行为)。

---

## 1. 背景与问题定义

ADR-046 已把旧证据链从多个 LLM 阶段收敛为：

```text
council_coordinator
→ evidence_verifier
→ council_judge
```

收敛方向正确，但当前实现仍把证据的真实性责任交给了 LLM：

1. 发现者在 `Issue.evidence_chain` 中重新填写工具名、参数和 `located` 代码片段；
2. `located` 是 LLM 对工具输出的二次加工，并非运行时捕获的原始事实；
3. verifier 再次调用工具，通过字符串或关系断言匹配验证 LLM 的描述；
4. 弱模型经常改写、压缩或解释工具结果，造成真实候选落入 `unverified/insufficient`；
5. 很多低置信候选没有工具链，当前只能进入与 claim 无关的固定配方查询；
6. 最近 `vul4j-42-command-injection` 单 case 运行中，候选最终全部成为 insufficient 并被删除。

根因不是某个匹配函数不够宽松，而是证据所有权放错了位置：

> LLM 可以选择“哪些真实事实支持我的主张”，但不能生成事实、调用记录或原始引文。

本计划把真实证据收进一个深模块 `EvidenceLedger`。调用方只需要理解 Artifact 和 Ref 两个概念，工具捕获、去重、revision、复用、异常重放、scope 护栏和 Trace 展示都隐藏在模块实现内。

---

## 2. 目标、非目标与已锁定决策

### 2.1 目标

- 真实工具调用结果由运行时代码捕获，LLM 无法伪造或改写。
- task diff 成为一等证据；没有工具调用的候选明确表达为 `patch-only`，而不是“空链”。
- 多工具协作通过多个 Artifact 引用表达，例如 `inspect_change_impact → get_file_content`。
- 发现者只选择短编号 `T01/T02`，离开发现子图前转换为内部稳定 ID。
- verifier 默认不重复调用正常工具，只验证引用、scope、revision 和响应结构。
- 仅异常 Artifact 重放，并受 `enabled_evidence_tools` 白名单约束。
- 删除逐事实关系分析 LLM；CouncilJudge 一次批量完成 support/counter、keep/drop 和 severity。
- 最终 keep 候选必须引用真实、有效、属于本候选可见范围的支持证据。
- Trace 能从候选引用反查真实工具调用，并显示 Verifier/Judge 的完整决策。

### 2.2 非目标

- 不修改 Java Gateway 的职责：Java 仍只提供事实、图谱和沙箱，不做问题判断。
- 不新增 Gateway 工具，不扩展 AST、RAG、调用图能力。
- 不修改 CouncilCoordinator 的候选分组与保守归并语义。
- 不修改 small PR 的整体直审路线；Evidence Ledger 作用于 medium/large 完整管线。
- 不恢复 ConcernAnalyzer、EvidenceStrategist、EvidenceResearcher、ImpactAssessor。
- 不做 profile 消融实验。
- 不为旧 `Issue.evidence_chain` 提供兼容期；本次直接删除。

### 2.3 已锁定决策

| 决策 | 结论 |
|---|---|
| 正常 Artifact 是否重放 | 不重放；只对异常、恢复或 revision 不一致的 Artifact 重放 |
| 关系分析与终审 | 合并为一次批量 EvidenceJudge |
| 无有效工具引用的候选 | 使用当前 task diff 作为证据，不执行固定配方补证 |
| Judge 最终失败 | fail-closed：不输出 Issue，但完整留痕 |
| 产品 `Issue.evidence_chain` | 本次直接删除 |
| 验证方式 | 单测/静态检查 + `selected-20-v2` 中一个 case 的带 Trace 审查 |
| 正式验证 case | `vul4j-42-command-injection` |

---

## 3. 最终管线与模块接口

### 3.1 图拓扑

```text
ContextProvider
    ↓
Discover × 3
    ├─ task patch              → P01
    ├─ prefetched context      → C01/C02/...
    └─ actual tool calls       → T01/T02/...
              ↓
       DiscoveryResult
       candidates + short refs
              ↓
       bind aliases to artifact IDs
              ↓
CouncilCoordinator
              ↓
EvidenceVerifier（正常路径零 LLM）
    ├─ candidate/task binding
    ├─ automatic patch evidence
    ├─ external ref validation
    ├─ graph/source-scope guards
    └─ exceptional replay only
              ↓
CouncilJudge（每批一次 LLM）
    ├─ supports/counters
    ├─ keep/drop
    └─ severity
              ↓
deterministic output validation
              ↓
ReviewResult
```

保留图节点名和顺序：

```text
council_coordinator → evidence_verifier → council_judge → END
```

节点职责重新定义：

- `evidence_verifier`：只证明 Artifact 真实、可用、属于候选范围；不判断 candidate claim 是否成立。
- `council_judge`：读取已经验证的事实，一次完成证据语义关系、候选去留和严重度。

### 3.2 深模块 seam

Evidence 模块对 graph 暴露两个主要接口：

```python
class EvidenceCatalogBuilder:
    def build_initial(
        self,
        *,
        task: ReviewTask,
        context_bundle: TaskContextBundle | None,
        reviewer: str,
        revision: str,
    ) -> EvidenceCatalog: ...

    def append_tool_records(
        self,
        catalog: EvidenceCatalog,
        records: Sequence[DiscoveryToolRecord],
    ) -> EvidenceCatalog: ...


class EvidenceGate:
    def verify(
        self,
        *,
        assembly: DossierAssembly,
        artifacts: Mapping[str, EvidenceArtifact],
        tool_client: Any | None,
        enabled_replay_tools: Sequence[str] | None,
    ) -> VerificationBatch: ...
```

Graph 不应直接解析 Artifact payload、计算 hash、处理 reused 调用或判断是否重放；这些复杂性必须留在 Evidence 模块实现内部。

---

## 4. 数据模型设计

建议新增 `services/agent/src/codeguard_agent/models/evidence.py`，集中定义证据内部模型。产品模型仍放在 `models/schemas.py`，候选/裁决模型仍放在 `models/council.py`。

### 4.1 产品模型调整

从 `models/schemas.py` 删除：

```python
class EvidenceTraceStep(BaseModel): ...

class Issue(BaseModel):
    ...
    evidence_chain: list[EvidenceTraceStep]
```

最终产品模型为：

```python
class Issue(BaseModel):
    severity: Severity
    file: str
    line: int = 0
    type: str
    message: str
    suggestion: str = ""
    confidence: float = 1.0
```

删除后同步：

- `CandidateIssue.from_issue/to_issue`；
- mock 数据；
- schema contract tests；
- JSON snapshot；
- Prompt 中所有 `evidence_chain` 文案。

### 4.2 发现阶段专用输出

发现者不能继续直接输出产品 `ReviewResult`，否则内部 evidence ref 会再次污染产品接口。

```python
class EvidenceRole(str, Enum):
    LOCATION = "location"
    MECHANISM = "mechanism"
    REACHABILITY = "reachability"
    IMPACT = "impact"
    COUNTER = "counter"


class EvidenceRefSelection(BaseModel):
    alias: str = Field(min_length=1)
    role: EvidenceRole


class DiscoveredIssue(BaseModel):
    severity: Severity
    file: str
    line: int = 0
    type: str
    message: str
    suggestion: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_refs: list[EvidenceRefSelection] = Field(
        default_factory=list,
        max_length=3,
    )


class DiscoveryReviewResult(BaseModel):
    summary: str = ""
    issues: list[DiscoveredIssue] = Field(default_factory=list)
```

语义：

- `evidence_refs=[]` 不代表没有证据；系统仍会自动绑定 task patch。
- `role` 只是发现者对用途的声明，Verifier 不据此直接判定 supports。
- `LOCATION` 只能说明如何找到位置，不能单独满足最终 keep 的支持要求。

### 4.3 Artifact 模型

```python
class EvidenceSourceKind(str, Enum):
    TASK_PATCH = "task_patch"
    PREFETCHED_CONTEXT = "prefetched_context"
    TOOL_CALL = "tool_call"


class ArtifactAvailability(str, Enum):
    AVAILABLE = "available"
    FAILED = "failed"
    REJECTED = "rejected"
    MISSING = "missing"
    INVALID = "invalid"


class EvidenceCaptureMode(str, Enum):
    GENERATED = "generated"   # patch/context 由管线确定性生成
    EXECUTED = "executed"     # 本次真实调用 Gateway
    REUSED = "reused"         # 复用本 review 已有真实结果


class EvidenceArtifact(BaseModel):
    id: str
    task_id: str
    reviewer: str
    revision: str

    source_kind: EvidenceSourceKind
    tool: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)

    payload: str
    payload_hash: str
    status: EvidenceArtifactStatus
    capture_mode: EvidenceCaptureMode

    call_id: str = ""
    reused_from_artifact_id: str = ""
    limitations: tuple[str, ...] = ()
```

Artifact ID 必须内容寻址，不能使用 LLM 可猜测的序号，也不能只使用随机 UUID：

```python
artifact_id = "ev-" + sha256(
    "\0".join([
        revision,
        task_id,
        source_kind.value,
        tool,
        stable_json(canonical_arguments),
        payload_hash,
    ]).encode("utf-8")
).hexdigest()[:16]
```

性质：

- 相同 revision/task/source/tool/args/payload 得到相同 ID；
- payload 或 revision 改变会生成新 ID；
- reused 调用解析到首次 Artifact，不复制 payload。

### 4.4 Catalog 与短别名

```python
class EvidenceCatalog(BaseModel):
    task_id: str
    reviewer: str
    revision: str
    artifacts: dict[str, EvidenceArtifact] = Field(default_factory=dict)
    alias_to_artifact_id: dict[str, str] = Field(default_factory=dict)
```

短别名顺序固定：

1. `P01`：当前 task patch；
2. `C01...`：ContextProvider facts，保持原顺序；
3. `T01...`：当前 task/reviewer 可见的唯一工具事实，按首次出现顺序。

别名只在一次 reviewer task 的结构化终止结果（或降级 synthesis）中有效。LLM 输出完成后立即绑定成内部 ID，外层 graph State 不依赖别名。

### 4.5 Candidate 引用模型

```python
class EvidenceRef(BaseModel):
    artifact_id: str
    declared_role: EvidenceRole
    auto_bound: bool = False


class EvidenceRefErrorReason(str, Enum):
    UNKNOWN_ALIAS = "unknown_alias"
    CROSS_TASK_REFERENCE = "cross_task_reference"
    CROSS_REVISION_REFERENCE = "cross_revision_reference"
    ARTIFACT_FAILED = "artifact_failed"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"


class EvidenceRefError(BaseModel):
    alias: str
    reason: EvidenceRefErrorReason
    detail: str = ""
```

`CandidateIssue` 改为：

```python
class CandidateIssue(BaseModel):
    ...
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    evidence_ref_errors: list[EvidenceRefError] = Field(default_factory=list)
```

候选创建时必须自动添加 patch 引用：

```python
EvidenceRef(
    artifact_id=patch_artifact.id,
    declared_role=EvidenceRole.MECHANISM,
    auto_bound=True,
)
```

即使发现者返回 `evidence_refs=[]`，候选仍明确表示为 patch-only。

### 4.6 Verifier 输出模型

```python
class EvidenceValidationStatus(str, Enum):
    VALID = "valid"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class VerifiedEvidence(BaseModel):
    artifact_id: str
    source_kind: EvidenceSourceKind
    tool: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)
    content: str
    validation_status: EvidenceValidationStatus
    limitations: tuple[str, ...] = ()


class CandidateVerification(BaseModel):
    candidate_id: str
    source_kinds: set[EvidenceSourceKind] = Field(default_factory=set)
    valid_evidence: list[VerifiedEvidence] = Field(default_factory=list)
    invalid_references: list[EvidenceRefError] = Field(default_factory=list)
    grounding_status: Literal[
        "grounded",
        "partially_grounded",
        "ungrounded",
    ]
    eligible_for_judge: bool
    rejection_reason: str = ""


class VerificationBatch(BaseModel):
    candidates: dict[str, CandidateVerification] = Field(default_factory=dict)
    replayed_artifact_ids: list[str] = Field(default_factory=list)
    trace: list[tuple[str, str]] = Field(default_factory=list)
```

展示层从 `source_kinds` 派生：

```text
{PATCH}                 → patch-only
{PATCH, CONTEXT}        → patch+context
{PATCH, TOOL}           → patch+tool
{PATCH, CONTEXT, TOOL}  → patch+context+tool
{}                      → ungrounded
```

### 4.7 Judge 输出模型

```python
class EvidenceJudgeAssessment(BaseModel):
    candidate_id: str
    action: Literal["keep", "drop"]
    severity: Severity | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    counter_evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class EvidenceJudgeBatch(BaseModel):
    assessments: list[EvidenceJudgeAssessment] = Field(default_factory=list)
```

`Verdict` 继续作为最终内部裁决记录，但 reason_code 更新为新证据语义。

### 4.8 Graph State

`ReviewState` 新增：

```python
evidence_revision: str
evidence_artifacts: Annotated[
    dict[str, EvidenceArtifact],
    merge_evidence_artifacts,
]
candidate_verifications: dict[str, CandidateVerification]
judge_assessments: dict[str, EvidenceJudgeAssessment]
```

删除：

```python
candidate_facts
candidate_relations
gathered_context
```

`ReviewerState` 新增：

```python
evidence_catalog: EvidenceCatalog
```

`ReviewOutcome` 调整为泛型或内部统一信封：

```python
@dataclass
class ReviewOutcome:
    result: ReviewResult | DiscoveryReviewResult
    evidence_catalog: EvidenceCatalog | None = None
    execution_events: list[str] = field(default_factory=list)
```

`tool_trace_records` 只在 EvidenceCatalog 构建前短暂存在，不再作为最终事实状态跨图传播。

---

## 5. EvidenceCatalog 构建规则

### 5.1 review revision

`PipelineOrchestrator.run()` 新增可选参数：

```python
evidence_revision: str = ""
```

回退规则：

```python
effective_revision = (
    evidence_revision
    or getattr(tool_client, "revision", "")
    or "diff:" + sha256(diff_text.encode("utf-8")).hexdigest()
)
```

CLI 创建工具 session 时，把已经计算的：

```text
head_revision:working_tree_digest
```

同时传给 ToolClient 和 PipelineOrchestrator。

Eval runner 对 repo-backed case 使用：

```text
case.provenance.head_revision:sha256(case.diff)
```

`ToolClient` 保存只读 `revision` 属性，确保 Artifact 与 Gateway session 身份一致。

### 5.2 Patch Artifact

每次 reviewer task `_prepare` 时创建 P01：

```python
EvidenceArtifact(
    task_id=task.id,
    reviewer=reviewer.source_agent,
    revision=revision,
    source_kind=EvidenceSourceKind.TASK_PATCH,
    tool="",
    arguments={"file_path": task.file},
    payload=task.patch,
    availability=ArtifactAvailability.AVAILABLE,
    capture_mode=EvidenceCaptureMode.GENERATED,
)
```

规则：

- 每个候选都自动绑定本 task 的 patch Artifact；
- candidate line 是有效 new-side changed line 时，Judge payload 使用该行所在 hunk；
- `line=0` 或不在 changed lines 中时使用完整 scoped task patch，并加 `candidate_line_unknown` limitation；
- patch Artifact 不调用 Gateway、不重放。

### 5.3 Context Artifact

每个 `TaskContextBundle.fact` 转为 Cxx：

- `source_kind=PREFETCHED_CONTEXT`；
- `tool` 从 `fact.source` 提取；
- `payload=fact.content`；
- `fact.truncated=True` 时 availability 仍为 AVAILABLE，并加 `context_truncated`；
- `<prefetched_context>` 中增加 `evidence_id="Cxx"`。

### 5.4 Tool Artifact

ReAct 探索完成后，根据 `DiscoveryToolRecord` 构建 Txx：

- 参数使用 `canonical_tool_key` 相同的规范化逻辑；
- output 必须来自运行时 record，不得读取 LLM 最终消息；
- COMPLETE_PATCH_RESULT 不创建新工具 Artifact，解析到 P01；
- REPEATED_TOOL_RESULT 不作为 payload；
- 相同 canonical tool+args 解析到首次真实 Artifact；
- failed/rejected/not_found 也可以留下 Artifact 供 Trace，但默认不可作为有效支持证据。

### 5.5 reused 记录修复

当前 `DiscoveryToolCoordinator` 复用时可能只把短 marker 写入 record。需要修改记录接口：

```python
class DiscoveryToolRecord:
    ...
    output: str              # 给 Agent 的可见输出或短 marker
    resolved_output: str     # 运行时真实原始结果
```

或者等价地让 `_record()` 接收 `resolved_response`。必须满足：

- ToolMessage 可以继续返回 marker，避免重复大文本；
- EvidenceCatalog 总能取得首次真实 payload；
- reused record 保存 `reused_from_call_id`；
- Trace 能显示复用关系；
- eval 工具调用数只计算首次真实执行。

### 5.6 ReAct 同轨迹结构化终止（后续修订）

ToolAgentEngine 的当前实现已用同轨迹结构化终止取代正常路径的二次对账：

```text
ReAct 探索（运行时登记全部 ToolRecords 并返回 Txx）
→ 停止调用工具
→ 最后一个 AIMessage 直接输出 DiscoveryReviewResult
→ 本地严格校验并绑定 evidence_refs
```

Direct tier 的审查员使用只有 P/C Artifact 的 Catalog。

仅当 ReAct 终止输出缺失、畸形或 schema 不合法，或达到递归上限且已有工具事实时，调用一次 DirectEngine synthesis。降级时必须把已捕获的 Catalog 传给 Direct synthesis，禁止丢失已经取得的工具事实；未知 alias 由 binder 记录错误，不触发修复 LLM。

---

## 6. Alias 绑定与候选创建规则

发现者输出仍使用短 alias。`make_reviewer_node` 把 `DiscoveredIssue` 转成 `CandidateIssue` 时执行：

```python
def bind_discovered_issue(
    issue: DiscoveredIssue,
    *,
    task: ReviewTask,
    reviewer: str,
    catalog: EvidenceCatalog,
    candidate_index: int,
) -> CandidateIssue:
    ...
```

步骤：

1. 生成稳定 candidate ID；
2. 校验 file 匹配 task；
3. 自动绑定 P01；
4. 按 LLM 原顺序解析外部 refs；
5. alias 不存在时记录 `UNKNOWN_ALIAS`；
6. Artifact task/revision 不匹配时记录对应错误；
7. 同一 Artifact 重复引用只保留第一次；
8. 最多保留 3 个外部 Artifact；
9. Candidate 离开 reviewer 子图后只保存内部 artifact ID，不依赖 alias。

重要语义：

- 无工具 ref：Candidate 是 patch-only，正常进入 Verifier/Judge；
- 工具 ref 全无效：Candidate 退化为 patch-only，同时保留 ref error；
- LLM 不能通过编造 T99 获得证据；
- LLM 选到真实但无关的 T01 时，Verifier 只确认 T01 真实，Judge 再判断它是否支持 claim。

---

## 7. EvidenceVerifier 规则

### 7.1 候选绑定

复用 `assemble_dossiers()` 的 task/file 绑定语义：

- missing task：drop `invalid_candidate_binding`；
- ambiguous task：drop；
- candidate file mismatch：drop；
- patch Artifact missing/corrupt：ungrounded，drop；
- 其他外部 ref 失败不直接 drop，继续使用 patch。

### 7.2 Artifact health

| 条件 | 结果 |
|---|---|
| TASK_PATCH + hash 正确 | VALID |
| 完整 ContextFact | VALID |
| truncated ContextFact | LIMITED |
| 成功工具调用 + 同 revision + 响应合法 | VALID |
| reused 且可解析到首次 Artifact | VALID |
| outcome=found + coverage=partial | LIMITED，保留已解析正事实 |
| outcome=indeterminate + coverage=partial | UNAVAILABLE，形成 evidence gap，不重放 |
| failed | 请求异常重放 |
| rejected/missing | UNAVAILABLE，不重放 |
| subject mismatch | INVALID |
| invalid source_scope | INVALID |
| MAIN/GENERATED 查询仅 TEST 关系却 outcome=found | INVALID |
| revision mismatch | 请求异常重放 |

### 7.3 图响应规范化

保留当前 verifier 中已经验证有效的护栏：

- `subject_symbol_id` 必须匹配参数；
- `source_scope` 只能是 MAIN/TEST/GENERATED；
- production relationships 不消费 TEST；
- schema_version 必须为 2，outcome/coverage 必须是合法组合；
- coverage partial 不整体作废；
- limitations 原样进入 Judge。

用于 hash/replay 比较时，JSON 必须规范化：

- object key 排序；
- relationships 按 `(sourceId, kind, targetId, file, line, source_set)` 排序；
- 忽略纯展示顺序差异；
- 不忽略 outcome/coverage/scope/subject/limitations。

### 7.4 异常重放

正常 COMPLETE Artifact 不重放。

以下场景进入重放队列：

- FAILED；
- revision 不一致；
- payload 无法解析；
- checkpoint 恢复后 Tool session 改变；
- graph response scope/subject 异常但调用参数仍可安全重试。

规则：

1. 只重放 `enabled_evidence_tools` 允许的工具；
2. None 表示沿用 discovery tools；空列表表示禁止重放；
3. 相同 canonical tool+args 全局只执行一次；
4. `get_file_content` 比较内容 hash；
5. inspect 工具比较规范化 JSON；
6. 重放结果重新执行完整验证，得到 VALID/LIMITED/UNAVAILABLE/INVALID；
7. 失败只产生 limitation，不把失败当 contradicts；
8. patch-only 候选永远不因“缺工具”进入补证。

### 7.5 Guard scan

保留确定性 guard scan，但输入改为真实 Artifact payload：

- 只扫描 candidate 相关 patch/file 内容；
- 发现明确保护机制时可直接产生 `direct_counter_guard`；
- 未发现保护不能反推保护不存在；
- guard 命中可在 Judge 前直接 drop；
- Trace 记录命中的 artifact ID、规则名和代码位置。

### 7.6 Grounding 计算

```python
if patch_invalid:
    grounding = "ungrounded"
elif invalid_refs or any(item.status == LIMITED for item in valid):
    grounding = "partially_grounded"
else:
    grounding = "grounded"
```

`eligible_for_judge`：

- task/file/patch 绑定有效；
- 未被确定性 counter guard 淘汰；
- 至少存在 patch Evidence。

这意味着 patch-only 候选默认 eligible。

### 7.7 删除旧实现

切换完成后删除：

- `validate_chain`；
- `replay_calls`；
- `recipe_calls`；
- `_located_match`；
- `_parse_assertions/_graph_assertions_match` 等 located 兼容逻辑；
- `_RelationBatch/analyze_relations`；
- `CandidateFact/FactRelation`；
- 对应旧 metrics 和 tests。

不要保留两套并行证据语义。

---

## 8. 批量 CouncilJudge

### 8.1 输入

每个候选输入包含：

```json
{
  "candidate_id": "C001",
  "candidate": {
    "file": "...",
    "line": 123,
    "type": "...",
    "claim": "...",
    "severity_proposal": "WARNING",
    "confidence": 0.72
  },
  "grounding_status": "grounded",
  "evidence": [
    {
      "evidence_id": "F001",
      "source_kind": "task_patch",
      "tool": "",
      "content": "...",
      "limitations": []
    },
    {
      "evidence_id": "F002",
      "source_kind": "tool_call",
      "tool": "inspect_change_impact",
      "arguments": {"symbol_id": "..."},
      "content": "...",
      "limitations": []
    }
  ]
}
```

Judge 使用 batch-local C/F 短 ID；代码维护到真实 candidate/artifact ID 的映射。

### 8.2 批次与预算

- 每批最多 8 个候选；
- 保持候选稳定顺序；
- 最多 4 批并行；
- 每候选最多 3 条外部 Evidence，加自动 patch；
- patch 优先提取 candidate line 所在 hunk；
- `line=0` 使用 scoped task patch；
- graph payload 使用现有 `_graph_summary` 思路，保留 status/coverage/scope/subject/relationships/limitations；
- 不在多个候选间重复同一 Artifact 内容，可在 batch 顶层建 facts 表，候选只列 visible IDs；
- batch payload 超预算时先截断非引用 context，再截断低优先级 graph nodes；不得删除被候选引用的关系和 limitation。

### 8.3 Judge 语义

Judge 一次输出：

- 哪些事实支持 claim；
- 哪些事实反驳 claim；
- keep/drop；
- keep 后 severity；
- 具体理由。

关键规则：

- Patch 可以独立证明局部语法、控制流、空值、边界和直接危险拼接；
- location Artifact 只说明位置，不自动支持问题成立；
- 跨文件调用、生产可达性、调用者契约、影响范围必须有对应 tool/context Evidence；
- TEST 关系只能证明测试事实；
- partial/indeterminate 范围中“没有看到保护”属于不足，不是 supports；
- keep 至少需要一个 supporting Evidence ID；
- severity 与 confidence 分离；
- 不得生成新候选、扩大原 claim 或请求工具。

### 8.4 输出确定性校验

对每个 batch：

1. 每个输入 candidate 必须且只能返回一次；
2. 不接受未知 candidate ID；
3. keep 必须至少一个 supporting ID；
4. supporting/counter ID 必须属于该候选的 verified facts；
5. supporting/counter 不能重叠；
6. drop 的 severity 必须为 None；
7. keep 的 severity 必须存在；
8. WARNING/CRITICAL 必须引用支持事实；
9. Maintainability 候选不得 CRITICAL；
10. 无法通过校验的 assessment 视为该候选 Judge 失败，不能部分信任。

### 8.5 失败策略

```text
invoke_with_retry 异常重试
→ None/结构非法再显式重试一次
→ 整批仍失败，二分拆批
→ 单候选仍失败，verification_failed + 不输出
```

禁止沿用当前：

```text
Judge 失败 → keep + 使用 severity_proposal
```

因为该行为会在证据系统故障时直接放行幻觉候选。

### 8.6 分组合并

CandidateGroup 中每个成员仍独立验证和裁决。完成 keep/drop 后复用现有组内合并：

- 只合并 keep 成员；
- severity 取 keep 成员中最高；
- 保留稳定 anchor candidate ID；
- 不把被 drop 成员的 Evidence 合并进最终 issue；
- 产品 Issue 不输出 Evidence 字段。

---

## 9. Prompt 变更

### 9.1 三路 base prompt

修改：

- `prompts/threat-model-base.txt`
- `prompts/behavior-base.txt`
- `prompts/maintainability-base.txt`

删除整个“取证溯源/evidence_chain”章节。

把输出模型从 `ReviewResult` 改为 `DiscoveryReviewResult`，并说明 `evidence_refs`。

三份 Prompt 不重复写 Evidence 详细规则，统一加载新共享 Prompt。

### 9.2 新增 `discovery-evidence-contract.txt`

建议完整语义如下，实现时可微调措辞但不得改变规则：

```text
## 证据引用契约

当前 task patch 由系统自动作为每个候选的基础证据，你不需要为 patch 填写编号。

如果你在判断候选时使用了 <evidence_catalog> 中的预取上下文或工具结果，
请在 evidence_refs 中引用对应的 Cxx/Txx 编号。你只负责选择编号，不得重新填写
工具参数、代码片段或工具原文。

每条引用包含：
- alias：必须逐字复制当前 Evidence Catalog 中存在的编号；
- role：location / mechanism / reachability / impact / counter。

规则：
- 最多选择 3 条与候选直接相关的外部事实；
- 多个工具共同发现问题时，同时引用各自编号；
- 仅用于探索但与最终主张无关的调用不要引用；
- location 只说明如何找到位置，不能单独证明问题成立；
- 没有使用外部事实时返回空 evidence_refs，系统会使用当前 task patch；
- 不得猜测、构造或修改编号；
- 不得把 summary、risk prior 或 knowledge bundle 当仓库证据。
```

增加两个示例：

```json
{
  "message": "新增分支对可能为空的返回值直接解引用",
  "evidence_refs": []
}
```

表示 patch-only。

```json
{
  "message": "未转义参数沿调用路径进入命令构造",
  "evidence_refs": [
    {"alias": "T01", "role": "location"},
    {"alias": "T02", "role": "mechanism"}
  ]
}
```

表示多工具协作。

### 9.3 降级 synthesis 的 EvidenceCatalog 渲染

仅在结构化终止失败的降级 synthesis user prompt 末尾增加：

```xml
<evidence_catalog task_id="..." revision="...">
  <artifact id="C01"
            source="prefetched_context"
            availability="available">
    ...
  </artifact>
  <artifact id="T01"
            source="tool_call"
            tool="inspect_change_impact"
            args="{...}"
            availability="available">
    ...
  </artifact>
  <artifact id="T02"
            source="tool_call"
            tool="get_file_content"
            args="{...}"
            availability="available">
    ...
  </artifact>
</evidence_catalog>
```

注意：

- task patch 已在原 `<task_patch>` 标签上增加 `evidence_id="P01"`，不重复全文；
- failed record 可以显示，但标 `citeable="false"`；
- reused Artifact 显示真实 payload 和 `capture_mode="reused"`；
- LLM 最终输出只允许 C/T alias；P01 由系统自动绑定。

### 9.4 新增 `evidence-judge.txt`

该 Prompt 取代完整档的 `evidence-analysis.txt + council-judge.txt`。

必须包含：

```text
你是批量候选证据裁决员。对每个候选独立完成：
1. 判断哪些事实支持主张；
2. 判断哪些事实反驳主张；
3. 决定 keep/drop；
4. 对 keep 候选确定 severity。

事实 ID 是唯一可引用证据。不得引用输入中不存在的 ID。
Patch 可以证明局部代码机制，但不能自动证明跨文件调用、生产可达性或外部契约。
定位事实不能单独证明缺陷成立。
未找到保护不等于证明保护不存在。
TEST 关系不能证明生产可达性。
所有 limited/partial/indeterminate 边界必须保留。
keep 必须至少引用一个 supporting_evidence_id。
不得请求工具、生成新候选或扩大原始 claim。
```

### 9.5 DirectJudge

为了保留 `evidence_mode=off` 配置兼容，新建 `direct-judge.txt`，只用于无证据档：

- 输入 candidate + patch；
- 不接受 Evidence IDs；
- 输出同类 keep/drop/severity；
- 不参与本次单 case 正式验证。

切换完成后删除旧 `evidence-analysis.txt` 和旧 `council-judge.txt`。

---

## 10. Trace 与可观测性适配

Trace 必须回答：

1. 审查员实际调用了什么工具；
2. 每次真实调用对应哪个 Artifact；
3. ReAct 终止结果或降级 synthesis 给 LLM 展示了哪些短 alias；
4. 候选选择了哪些 alias；
5. alias 是否成功绑定；
6. Verifier 是否重放、为何重放；
7. Judge 引用了哪些事实、为何 keep/drop。

### 10.1 Trace 事件

新增 CouncilTrace event：

```text
evidence_catalog_created
evidence_artifact_captured
evidence_artifact_reused
candidate_patch_bound
candidate_ref_bound
candidate_ref_invalid
candidate_verification_completed
evidence_replay_requested
evidence_replay_completed
evidence_replay_failed
evidence_verification_metrics
evidence_judge_batch_started
evidence_judge_assessment
evidence_judge_batch_failed
evidence_judge_metrics
```

event detail 一律稳定 JSON，至少带 candidate_id/artifact_id/task_id/reason 中适用字段。

### 10.2 工具调用卡片

View model 对 Artifact 渲染：

```text
Artifact ID
task-local alias
call_id
tool
canonical arguments
status
capture mode
reused-from
payload hash
payload length
payload preview
```

同一 Artifact 复用时显示到首次调用的链接。

### 10.3 候选卡片

每个候选显示：

```text
candidate ID / agent / task / file / line / confidence
automatic patch artifact
LLM selected aliases and declared roles
bound artifact IDs
invalid aliases and reasons
source profile: patch-only / patch+context / patch+tool / mixed
grounding status
```

Artifact ID 可点击跳转到工具/上下文卡片。

### 10.4 Verifier 卡片

替换旧摘要：

```text
request_count / fact_count
verified / unverified / failed / recipe
chain / recipe
```

新摘要：

```text
artifacts: patch/context/tool
candidates: patch-only/tool-backed/mixed/ungrounded
refs: selected/valid/limited/invalid
replay: requested/confirmed/failed
judge eligible/rejected
verification duration
```

### 10.5 Judge 卡片

显示：

- 批次数与每批候选；
- 每个候选 action；
- supporting/counter Evidence IDs；
- severity；
- reason；
- 输出校验错误；
- fail-closed 候选数。

### 10.6 序列化与体积控制

完整 Artifact payload 在 graph State 只保存一份。Trace 序列化时转换为只读视图：

```python
class EvidenceArtifactTraceView(BaseModel):
    id: str
    ...metadata...
    payload_hash: str
    payload_chars: int
    payload_preview: str  # 最多 4000 chars
```

要求：

- candidate/verifier/judge 卡片只保存 Artifact ID，不复制 payload；
- tool card 保存 preview；
- LLM prompt 按 `CODEGUARD_TRACE_MAX_LLM_CONTENT` 现有规则截断；
- Trace HTML 不泄露额外文件，只显示本次 Gateway 已允许读取的事实。

### 10.7 eval trace sink

`PipelineOrchestrator.trace_sink` 不再读取 `gathered_context`，改从最终 Artifact 集派生工具画像：

- 只包含 `source_kind=TOOL_CALL`；
- reused 不重复计算实际调用；
- patch/context 不计 tool_calls；
- 保持 eval 的 `tools_used/files_read/tool_calls` 字段可用；
- `_strict_tool_failures` 改读 Artifact status 和 context diagnostics。

### 10.8 Trace 测试

更新 `tests/test_observability.py`：

- Artifact 工具卡；
- alias → Artifact 链接；
- patch-only 候选；
- reused Artifact；
- Verifier/Judge 新 metrics；
- payload preview；
- hidden reviewer wrapper State 仍能索引 candidates/artifacts；
- evidence_mode=off 的 skipped 显示。

---

## 11. Metrics 调整

从 `CouncilRunStats` 和 eval `CouncilTraceStats` 删除旧字段：

```text
fact_count（旧 CandidateFact 语义）
replay_verified_count
replay_unverified_count
replay_failed_count
chain_used_count
recipe_fallback_count
all_insufficient_candidate_count
all_insufficient_retained_count/rate
```

新增：

```python
artifact_count: int
patch_artifact_count: int
context_artifact_count: int
tool_artifact_count: int
reused_artifact_count: int

candidate_patch_only_count: int
candidate_context_backed_count: int
candidate_tool_backed_count: int
candidate_ungrounded_count: int

selected_reference_count: int
valid_reference_count: int
limited_reference_count: int
invalid_reference_count: int

replay_requested_count: int
replay_valid_count: int
replay_limited_count: int
replay_failed_count: int
evidence_gap_count: int
graph_indeterminate_count: int

judge_batch_call_count: int
judge_failed_candidate_count: int
judge_no_support_drop_count: int
final_issue_supported_count: int
final_issue_support_coverage: float | None
```

定义：

```text
final_issue_support_coverage =
  final_issue_supported_count / final_issue_count
```

完整证据模式目标值必须为 1.0。

---

## 12. 实施任务清单

每个 Task 单独提交。不要在 Task 内 push，除非用户另行要求。提交遵循项目 Conventional Commits，中文描述，无 AI 署名。

### Task 1：模型契约切换

**主要文件：**

- `models/schemas.py`
- `models/evidence.py`（新建）
- `models/council.py`

- [ ] 先写新 EvidenceArtifact/DiscoveryResult 模型测试。
- [ ] 删除 EvidenceTraceStep 和 Issue.evidence_chain。
- [ ] CandidateIssue 改用 EvidenceRef。
- [ ] 更新 mock 和所有 schema 构造点。
- [ ] 运行模型与全量测试。

提交：

```text
refactor(schemas): 引入Evidence Artifact与发现阶段内部模型
```

### Task 2：revision 与 Artifact ID

- [ ] ToolClient 保存 revision 属性。
- [ ] CLI/runner/orchestrator 传递同一 evidence_revision。
- [ ] 实现 canonical hash 和 Artifact reducer。
- [ ] 测试相同输入稳定、revision/payload 改变时 ID 改变。

提交：

```text
feat(evidence): 建立revision绑定的内容寻址Artifact
```

### Task 3：Patch/Context Catalog

- [ ] reviewer `_prepare` 构造 P/C Artifact。
- [ ] task patch 标签增加 P01。
- [ ] prefetched fact 标签增加 Cxx。
- [ ] ReviewOutcome 携带 Catalog。
- [ ] Direct tier 可看到 Catalog。

提交：

```text
feat(discovery): task patch与预取上下文注册为证据
```

### Task 4：真实工具 Artifact

- [ ] 修复 DiscoveryToolRecord 的 resolved output。
- [ ] 捕获工具结果并生成 Txx。
- [ ] reused 解析到首次 Artifact。
- [ ] ReAct fallback 保留 Catalog。
- [ ] 工具白名单与现有 single-flight/cache 不变。

提交：

```text
feat(discovery): 捕获真实工具结果并生成证据目录
```

### Task 5：发现者结构化输出与 Prompt

- [ ] ReviewEngine 支持 DiscoveryReviewResult response model。
- [ ] 三路 Prompt 删除 evidence_chain。
- [ ] 新增 discovery-evidence-contract。
- [ ] 降级 synthesis 渲染 EvidenceCatalog；正常 ReAct 终止结果直接引用工具返回的 Txx。
- [ ] 测试 patch-only、多工具、未知 alias 输出。

提交：

```text
refactor(prompts): 审查员改为引用运行时证据编号
```

### Task 6：Alias 绑定

- [ ] DiscoveredIssue → CandidateIssue binder。
- [ ] 自动 patch ref。
- [ ] alias scope/revision 校验。
- [ ] invalid ref 留痕并退化 patch-only。
- [ ] 并行 reviewer Artifact reducer。

提交：

```text
feat(council): 绑定候选引用与运行时Artifact
```

### Task 7：Verifier 替换

- [ ] 实现 Artifact health 和 graph guards。
- [ ] 实现 source profile/grounding。
- [ ] 实现异常重放和 evidence tool 白名单。
- [ ] guard scan 改读 Artifact。
- [ ] 删除 chain/located/recipe/关系分析旧逻辑。

提交：

```text
refactor(evidence): 基于Artifact引用执行确定性验证
```

### Task 8：批量 Judge

- [ ] 新 EvidenceJudgeBatch 模型。
- [ ] 新 evidence-judge prompt。
- [ ] 每批 8 候选、最多 4 并发。
- [ ] 输出确定性校验。
- [ ] 重试、二分拆批、fail-closed。
- [ ] 保留 CandidateGroup 合并。

提交：

```text
refactor(council): 合并证据分析与批量终审裁决
```

### Task 9：Graph State 与 Metrics 收口

- [ ] ReviewState 切换到 artifacts/verifications/assessments。
- [ ] 删除 CandidateFact/FactRelation State。
- [ ] 更新 CouncilRunStats。
- [ ] eval schema/report/archive 同步。
- [ ] evidence_mode=off 编排保持可运行。

提交：

```text
refactor(pipeline): 收敛Evidence Ledger图状态与统计
```

### Task 10：Trace 适配

- [ ] Collector/serialization 支持 Artifact preview。
- [ ] View model 增加 Artifact/candidate/verifier/judge 视图。
- [ ] Dashboard 支持引用跳转。
- [ ] trace sink 改从 Artifact 派生。
- [ ] Trace 单测全绿。

提交：

```text
feat(observability): 展示证据编号绑定与裁决链路
```

### Task 11：旧代码和测试清理

- [ ] 删除旧 verifier relation/located tests。
- [ ] 删除 EvidenceTraceStep contract tests。
- [ ] 用 EvidenceGate 接口测试替代浅模块内部测试。
- [ ] rg 确认无 `located/evidence_chain/CandidateFact/FactRelation/recipe_calls` 活引用。
- [ ] 全量 pytest/ruff/mypy。

提交：

```text
test(evidence): 重写Artifact证据链契约与回归测试
```

### Task 12：文档同步

- [ ] AGENTS.md 改为 Evidence Ledger 两节点设计。
- [ ] README/CLAUDE.md 同步。
- [ ] 面试手册移除旧六阶段与 located 重放描述。
- [ ] 配置说明注明 evidence tools 仅用于异常重放。

提交：

```text
docs: 同步Evidence Ledger证据链设计
```

### Task 13：单 Case 正式 Trace 验证

- [ ] 确认确定性测试与静态检查全部通过。
- [ ] 启动 Gateway Tool Server。
- [ ] 执行 §14 命令，仅跑一个 case。
- [ ] 按 §14.3 清单人工审阅 Trace。
- [ ] 记录“奏效/未奏效”和失败所在阶段。

本 Task 不做消融、不扩展 case、不宣称统计性质量结论。

---

## 13. 确定性测试矩阵

### 13.1 Catalog/Artifact

- [ ] 每个 task 恰好一个 patch Artifact。
- [ ] P/C/T alias 顺序稳定。
- [ ] Artifact ID 内容寻址稳定。
- [ ] revision 变化生成新 ID。
- [ ] payload 变化生成新 ID。
- [ ] reused 取得首次真实 payload。
- [ ] COMPLETE_PATCH_RESULT 解析到 P01。
- [ ] marker 绝不成为真实 payload。

### 13.2 Candidate binding

- [ ] diff-only candidate 自动 patch-only。
- [ ] T01/T02 多工具引用正确绑定。
- [ ] unknown alias 记录错误。
- [ ] cross-task/cross-revision 引用拒绝。
- [ ] 重复 ref 去重。
- [ ] 无效工具 ref 退化 patch-only。
- [ ] file mismatch 仍拒绝候选。
- [ ] line=0 使用 scoped patch 并标 limitation。

### 13.3 Verifier

- [ ] complete Artifact 不重放。
- [ ] failed/revision mismatch 重放，indeterminate 形成 EvidenceGap。
- [ ] `enabled_evidence_tools=[]` 禁止重放。
- [ ] 相同调用只重放一次。
- [ ] graph 数组排序变化不造成 mismatch。
- [ ] subject mismatch invalid。
- [ ] TEST-only 不能证明 MAIN。
- [ ] partial 具体关系可以 limited 保留。
- [ ] replay 失败不是 contradicts。
- [ ] patch-only 正常 eligible。

### 13.4 Judge

- [ ] 多候选一次 batch。
- [ ] keep 无 support ID 非法。
- [ ] 引用未知 ID 非法。
- [ ] candidate 缺失/重复触发重试。
- [ ] batch 失败二分。
- [ ] 单候选失败不输出。
- [ ] patch 自证局部缺陷可以 keep。
- [ ] patch 无法证明跨文件 claim 时 drop。
- [ ] location-only 不得 keep。
- [ ] TEST-only 不得支持生产影响。
- [ ] group 只合并 keep members。

### 13.5 Trace

- [ ] 工具卡展示 Artifact ID 和 alias。
- [ ] candidate ref 可跳转 Artifact。
- [ ] patch-only 标签明确。
- [ ] reused 指向首次 Artifact。
- [ ] verifier/judge metrics 正确。
- [ ] payload preview 截断、hash 保留。
- [ ] eval trace sink 不重复计 reused。
- [ ] evidence_mode=off 显示按设计跳过。

### 13.6 全量命令

在 `services/agent`：

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
conda run -n codeguard --no-capture-output ruff check src/
conda run -n codeguard --no-capture-output mypy src/
```

任何失败都必须在正式 LLM case 运行前解决。

---

## 14. 单 Case 正式验证

### 14.1 Case 选择

固定使用：

```text
services/agent/evals/dataset/selected-20-v2/cases/vul4j-42-command-injection
```

原因：

- 真实 CVE-2017-1000487 / CWE-78；
- capability=`call-path`；
- 多文件 diff，涉及 Commandline/Shell/BourneShell；
- 适合验证 `inspect_change_impact → get_file_content` 多工具协作；
- 当前实现已经在该 case 上出现候选全部 insufficient 的回归。

### 14.2 运行命令

先启动 Java Tool Server，并配置真实审查 LLM。然后在 `services/agent` 执行：

```powershell
$env:CODEGUARD_TOOL_SERVER_URL="http://localhost:9090"
$env:CODEGUARD_TRACE_ENABLED="true"
$env:CODEGUARD_TRACE_DIR="trace/evidence-ledger-v1"
$env:CODEGUARD_TRACE_MAX_LLM_CONTENT="12000"
$env:CODEGUARD_FORCE_REACT="true"

conda run -n codeguard --no-capture-output python -m evals.runner `
  --dataset evals/dataset/selected-20-v2 `
  --profile eval-codeguard-full `
  --case vul4j-42-command-injection `
  --runs 1 `
  --report evals/reports/evidence-ledger-v1-single-case.md
```

不加 `--judge`，避免引入 eval matcher 的额外 LLM。这里观察的是 Codeguard 自身 EvidenceJudge 和 Trace。

### 14.3 Trace 人工验收清单

- [ ] 每个 reviewer task 生成 P01。
- [ ] `inspect_change_impact` 的真实调用生成 Txx。
- [ ] `get_file_content` 的真实调用生成 Txx。
- [ ] 工具卡的参数和 payload 来自 Gateway record。
- [ ] reused 调用能回到首次真实 Artifact。
- [ ] ReAct 终止结果或降级 synthesis 展示的 alias 与 Trace Catalog 一致。
- [ ] 审查员没有输出目录外 alias。
- [ ] 至少一个相关候选正确引用两个协作工具结果；若审查员没有使用某工具，Trace 必须如实显示 patch-only/单工具，而不能伪造。
- [ ] diff-only candidate 显示 patch-only，而不是空证据。
- [ ] Verifier 未重复调用正常 COMPLETE Artifact。
- [ ] 异常重放如发生，Trace 给出触发原因。
- [ ] 无效 alias 被降级，不直接成为 support。
- [ ] Judge 的 support/counter ID 都可回溯到 VerifiedEvidence。
- [ ] keep candidate 至少一个 support。
- [ ] 命令注入候选没有因字符串改写或 all-insufficient 被误删。
- [ ] 最终 ReviewResult 包含预期命令注入问题。
- [ ] 无有效 support 的候选没有进入最终结果。

### 14.4 奏效判定

同时满足以下条件才能标记新设计“奏效”：

1. 审查员选择的工具编号真实存在且参数一致；
2. Artifact payload 未经过 LLM 二次生成；
3. patch-only candidate 能正常裁决；
4. 多工具候选能引用多个真实 Artifact；
5. 正常工具结果没有被 verifier 重复调用；
6. Judge 只引用当前候选可见的有效事实；
7. 最终命令注入问题被保留；
8. `final_issue_support_coverage == 1.0`。

如果不满足，不得用“LLM 随机性”笼统解释。必须依据 Trace 分类：

```text
discovery_failed
tool_call_failed
artifact_capture_failed
alias_selection_failed
reference_binding_failed
verification_failed
judge_semantic_failed
judge_contract_failed
```

本工作项只使用这一个 case，不做消融、不跑完整 selected-20-v2、不宣称统计性 Precision/Recall 提升。

---

## 15. 完成定义

- [ ] 产品 Issue 已删除 evidence_chain。
- [ ] 发现者只输出 EvidenceRefSelection。
- [ ] task diff 自动成为基础 Artifact。
- [ ] Context/Tool 真实结果进入 EvidenceLedger。
- [ ] reused 调用保留真实 payload。
- [ ] Candidate 离开发现子图前完成 alias 绑定。
- [ ] Verifier 正常路径零 LLM、零重复工具调用。
- [ ] 异常重放白名单真正生效。
- [ ] CouncilJudge 每批一次完成关系、去留和定级。
- [ ] Judge 最终失败 fail-closed。
- [ ] 旧 located/recipe/FactRelation 路径全部删除。
- [ ] Trace 可以从候选定位到 Artifact 和真实工具调用。
- [ ] 全量 pytest、ruff、mypy 通过。
- [ ] `vul4j-42-command-injection` 单 case Trace 已完成审阅并形成明确结论。
- [ ] AGENTS.md/README/CLAUDE.md/面试手册与真实代码一致。

---

## 16. 实施注意事项

- 不要直接读取被审仓库文件；所有 repo 内容仍来自 task diff 或 Java Gateway。
- Artifact 是内部审查事实，不进入产品 Issue。
- 不要让 LLM 输出 raw payload、工具参数副本或代码引文。
- 不要把 EvidenceRole 当作可信 relation；它只是发现者提示。
- 不要因为工具未找到保护就产生 supports。
- 不要把 TEST 关系用于生产可达性或 severity。
- 不要在无工具引用时恢复固定 RiskTag recipe；用户已明确选择 patch-only。
- 不要在 Judge 故障时沿用候选提案 severity 并放行。
- 不要同时保留新旧证据状态，避免 graph State 再次膨胀。
- 不要修改 unrelated 用户文件；当前工作树已有未跟踪文件，实施时只提交本计划涉及的路径。
