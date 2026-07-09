# ADR-032 审查状态收敛设计

**日期：** 2026-07-09  
**状态：** 已确认，待实施

## 1. 背景

完整 Trace 已经能够忠实展示 LangGraph 节点输入、输出和状态写入，也因此暴露出 ADR-032 当前运行状态中的历史遗留：

- 同一份 `diff_summary` 同时存在于顶层 State、`ContextBundle.diff_summary` 和 `ContextFact(kind="summary")`；
- `changed_files` 同时以结构化列表和 `changed_file` fact 保存；
- `file_groups`、`focus_notes`、`enable_hitl`、`dispatched` 等字段仍进入发现者子图，但当前拓扑不读取它们；
- Summary LLM 仍生成文件分派、变更类型和风险等级，而图节点只保留摘要；
- `CandidateIssue` 内嵌证据字段与图 State 的 `EvidenceRequest[]`、`EvidenceNote[]` 重复；
- EvidenceAgent 把证据写入顶层 `evidence_notes`，CouncilJudge 的 LLM Prompt 却读取 `candidate.evidence_notes`，导致终审模型实际看不到已经收集的证据；
- `evidence_requests` 使用追加 reducer，但 CouncilJudge 返回“旧请求 + 新请求”，使历史请求被重复追加；
- 初始 State 主动填充大量空集合和空结果，增加每个节点输入 Trace 的噪音。

这些问题不会由 Trace 展示层解决。Trace 只是忠实呈现运行状态，修复必须发生在 ADR-032 的状态接口和节点数据流中。

## 2. 目标

1. 每类运行信息只有一个权威存储位置。
2. 删除没有当前消费方的字段、兼容别名、软路由实现和 Prompt 要求。
3. 保持 ADR-032 的发现、举证、裁决职责分离。
4. 让 CouncilJudge 的规则层和 LLM 层读取同一份 `EvidenceNote[]`。
5. 阻止 reducer 重复累积相同证据请求。
6. 缩小新 Trace 的节点输入输出，同时保留完整的真实数据。
7. 记录字段迁移、行为影响和验证结果。

## 3. 非目标

- 不修改 `services/agent/legacy/`。该目录只保存废弃实现。
- 不改变外层拓扑：`Summary → ContextProvider → ReviewCouncil → CouncilJudge`。
- 不改变 `ReviewResult`、`Issue` 或 Java Gateway 协议。
- 不重新引入文件软分派、Supervisor 或自由文本 Agent 对话。
- 不为已删除的内部字段提供兼容别名或弃用期。
- 不改写已有 Trace HTML；历史报告是自包含文件，仍可直接打开。

## 4. 设计原则

### 4.1 单一权威来源

状态中的结构化事实不得通过“字段 + fact”“父对象 + 子对象”重复保存。需要派生的信息在使用点计算，不写回持久 State。

### 4.2 消息与实体分离

`CandidateIssue` 表示候选主张；`EvidenceRequest` 表示补证命令；`EvidenceNote` 表示举证结果。三者通过 `candidate_id` 关联，不互相内嵌副本。

### 4.3 reducer 只接收增量

带 reducer 的 State 字段由节点返回新增值，不返回“当前 State + 新增值”。需要去重的 reducer 以稳定身份合并。

### 4.4 Trace 展示真实接口

不为让 Trace 看起来简洁而隐藏数据。通过收敛运行接口，让 Trace 自然变得简洁。

## 5. 状态字段审计

### 5.1 `ContextBundle`

| 字段 | 处理 | 原因 |
|---|---|---|
| `changed_files` | 保留 | 是从 diff 确定性派生的紧凑文件索引，也供无工具补证路径定位目标 |
| `facts` | 保留 | 保存 AST、敏感 API 等真正新增的共享事实 |
| `diff_summary` | 删除 | 顶层 `ReviewState.diff_summary` 已是唯一摘要 |
| `sources` | 删除 | 可由 `facts[].source` 推导 |
| `truncated` | 删除 | 可由 `any(fact.truncated for fact in facts)` 推导 |

ContextProvider 不再创建 `kind="changed_file"` 和 `kind="summary"` 的 fact。`ContextBundle.render()` 只渲染文件索引和真正的共享事实。

### 5.2 Summary 输出与 `PipelineContext`

Summary 的结构化输出模型只保留：

```text
summary: str
```

删除：

- `changed_files`
- `change_types`
- `estimated_risk_level`
- `file_focus`
- `_normalise_file_groups`

`PipelineContext` 同步删除不再参与当前运行链路的：

- `file_groups`
- `change_types`
- `risk_level`

`issues`、`summary`、`filter_stats` 等仍被当前聚合实现或废弃代码引用的通用字段不在本次删除范围内，避免通过修改共享数据类间接整理 `legacy/`。

### 5.3 `Reviewer` 与 `ReviewerState`

`Reviewer` 保留：

- `name`
- `prompt_file`
- `source_agent`
- `tool_allowlist`

删除不再消费的 `category`。

`ReviewerState` 删除：

- `file_groups`
- `focus_notes`
- `enable_hitl`
- `dispatched`
- `eff_diff`

同时删除 `_build_relevant_diff`、`_effective_diff`、`_file_group_for_reviewer` 和 `_CROP_ADOPT_RATIO`。发现者的 `prepare` 节点直接以完整 `diff_text` 构建 Prompt。

### 5.4 `CandidateIssue`

`CandidateIssue` 只保存候选问题本身：

- `id`
- `source_agent`
- `file`
- `line`
- `type`
- `severity_proposal`
- `claim`
- `suggestion`
- `confidence`

删除：

- `category`
- `evidence_ids`
- `evidence_status`
- `needs_evidence`
- `evidence_requests`
- `evidence_notes`
- `challenge`
- `agent` 兼容属性
- `from_issue(..., category=..., agent=...)` 兼容参数

新增纯函数：

```python
build_evidence_requests(candidate: CandidateIssue) -> list[EvidenceRequest]
```

该函数复用现有规则：候选置信度低于 `0.75` 或行号不完整时，按 `source_agent` 生成对应工具请求。发现者节点分别把 `CandidateIssue[]` 和 `EvidenceRequest[]` 写入 State。

### 5.5 `EvidenceRequest`

保留实际参与路由和工具调用的：

- `id`
- `candidate_id`
- `target`
- `question`
- `preferred_tools`

删除当前没有消费方的：

- `reason`
- `reason_code`

`id` 由 `candidate_id + target + question + preferred_tools` 确定性生成。同一语义请求拥有相同 ID，既用于 reducer 去重，也用于标识该请求是否已经被 EvidenceAgent 处理。

### 5.6 `EvidenceNote`

保留：

- `request_id`
- `candidate_id`
- `status`
- `supports`
- `contradicts`
- `unknowns`
- `evidence_ids`

`request_id` 指向产生该记录的 `EvidenceRequest.id`。删除顶层 `reasoning`；支持、反证和不足条目已经包含对应推理文本，额外摘要是重复信息。

### 5.7 已废弃的 Challenge 模型

当前 `challenge_agent` 已合并进 CouncilJudge，`Challenge`、`ChallengeVerdict` 和 `CandidateIssue.challenge` 均没有运行消费方，直接删除。

### 5.8 `ReviewState`

删除只初始化和递增、从未参与路由或输出的 `judge_pass`。

保留 `council_route`。它是 Coordinator 的显式结构化决策，既参与条件边，也对 Trace 有解释价值。

初始 State 只写入外部输入、运行配置和确有必要的控制初值。列表 reducer、计数 reducer 和最终结果不再主动写入空值，依赖 LangGraph channel 的类型初值以及各读取点现有的 `.get(..., default)` 防御。

## 6. 举证链路

修改后的权威数据流：

```text
发现者
  ├─ CandidateIssue[]          候选主张
  └─ EvidenceRequest[]         结构化补证请求
             │
             ▼
EvidenceAgent
  └─ EvidenceNote[]            唯一证据记录
             │
             ▼
CouncilJudge
  ├─ 规则层读取 EvidenceNote[]
  └─ LLM Prompt 读取同一份 EvidenceNote[]
```

CouncilJudge 先按 `candidate_id` 构建 `notes_by_candidate`，规则判断和 Prompt 渲染共享该映射。去重合并候选时不再尝试合并 Candidate 内不存在的证据副本；State 中的 `EvidenceNote[]` 保持独立证据账本。

该设计强化而非削弱证据驱动原则：

- 是否补证由 `EvidenceRequest` 的存在表示，不再由布尔值和请求列表同时表示；
- 工具事实仍由 Java Gateway 提供；
- EvidenceAgent 仍只解释证据，不发现新问题、不做最终裁决；
- CouncilJudge 的确定性规则和 LLM 终于基于同一批证据。

## 7. reducer 与多轮补证

`evidence_requests` reducer 改为：

1. 合并现有请求与节点新增请求；
2. 按 `EvidenceRequest.id` 去重；
3. 保留首次出现顺序；
4. 最后应用 `MAX_TOTAL_EVIDENCE_REQUESTS` 上限。

发现者 fan-out 仍可并行追加不同请求。CouncilJudge 在 `needs_more_evidence` 时只返回本轮新建请求，不再返回完整历史请求。

EvidenceAgent 每轮从 `EvidenceNote.request_id` 构造已处理 ID 集合，只执行尚未产生 EvidenceNote 的请求。成功、失败或证据不足都会生成一条 EvidenceNote，因此历史请求不会在下一轮重复调用工具。多轮新增的不同请求仍会正常执行，完整请求与证据账本也继续保留在 Trace 中。

## 8. Prompt 修改

### 8.1 `summary-system.txt`

只要求模型理解 diff 并输出 2–4 句中文 `summary`。删除：

- changed files 抄录；
- change type 分类；
- 风险等级；
- security / logic / quality 文件分派；
- 软路由原则和旧输出 schema。

### 8.2 `summary-user.txt`

输出要求改为只返回 `summary`。

### 8.3 发现者 Prompt

核对 `threat-model.txt`、`behavior.txt`、`maintainability.txt`：

- 删除旧 Supervisor、`focus_notes`、文件分派或裁剪语义；
- 明确输入由完整 diff、单份变更摘要和共享事实组成；
- 保留各发现方法和工具边界。

### 8.4 `council-judge.txt`

保持 JudgeDecision schema 不变，补充说明候选证据来自唯一 `EvidenceNote` 账本，并明确三类内容：

- `支持`：工具事实支持候选主张；
- `反驳`：工具事实与候选主张冲突；
- `不足`：当前事实无法支持或反驳。

### 8.5 `evidence-analysis.txt`

结构化输出仍为 `EvidenceJudgment`。Prompt 不要求额外生成一个会在 `EvidenceNote.reasoning` 中再次复制的整体摘要。

## 9. 行为影响

| 行为 | 预期影响 |
|---|---|
| 三个发现者执行 | 不变，仍全部执行并读取完整 diff |
| Summary 能力 | 保留摘要，删除未使用的分类和分派 |
| ContextProvider 工具/AST 事实 | 不变 |
| EvidenceAgent 工具选择 | 不变 |
| CouncilJudge 规则裁决 | 读取同一份既有 EvidenceNote，语义不变 |
| CouncilJudge LLM 裁决 | 修复后能看到真实证据，最终 keep/drop/downgrade 可能发生合理变化 |
| 多轮补证 | 保留，消除相同请求的重复累积 |
| 最终 `ReviewResult` / `Issue` | schema 不变 |
| 新 Trace | 字段更少、摘要不重复、证据来源一致 |
| 历史 Trace HTML | 不受影响 |

## 10. 测试与验证

实施遵循 TDD，每项行为先写失败测试。

### 10.1 状态模型

- `ContextBundle.model_dump()` 不包含已删除字段；
- `facts` 不再包含 changed file 或 summary 副本；
- `CandidateIssue.model_dump()` 只包含候选本体字段；
- `build_evidence_requests()` 保持三个发现者原有工具选择；
- EvidenceRequest ID 对相同请求稳定、对不同请求不同；
- `EvidenceRequest` 和 `EvidenceNote` 不包含已删除字段。

### 10.2 Prompt

- Summary Prompt 不出现 `changed_files`、`change_types`、`estimated_risk_level`、`file_focus`；
- 发现者最终 `user_prompt` 中完整摘要只出现一次；
- `ContextBundle.render()` 不重复摘要；
- CouncilJudge Prompt 包含 State 中真实的支持、反证和不足证据；
- Prompt 文件不引用已删除状态字段。

### 10.3 图状态

- 发现者子图输入不包含 `file_groups`、`focus_notes`、`enable_hitl`；
- 发现者输出不包含 `dispatched`、`eff_diff`；
- 初始 State 不包含无意义空结果；
- 相同 EvidenceRequest 经 reducer 合并后只保留一份；
- EvidenceAgent 不重复执行已有 `EvidenceNote.request_id` 的请求；
- CouncilJudge 只返回新增 EvidenceRequest；
- 无 Summary、无工具、mock 和真实 LLM 结构化输出失败路径继续可运行。

### 10.4 完整验证

```powershell
cd services/agent
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
conda run -n codeguard --no-capture-output ruff check src/ tests/
conda run -n codeguard --no-capture-output mypy src/
```

另外生成一次 Trace，检查：

- 每个发现者 Prompt 的摘要出现一次；
- ContextProvider 不输出重复 fact；
- CouncilJudge Prompt 能看到 EvidenceAgent 产出的证据；
- `file_groups`、`focus_notes`、`judge_pass` 等字段不再出现。

Prompt 修改可能改变非确定性的审查质量。工程测试通过后，使用同一真实 diff 做修改前后对照；若具备评测模型配置，再运行现有 pipeline eval 保存质量结果。

## 11. 修改记录

实施完成后在 `DECISIONS.md` 的 ADR-032 下追加“状态单一权威来源与举证账本收敛”补充决策，记录：

- 删除字段及其历史来源；
- `CandidateIssue`、`EvidenceRequest`、`EvidenceNote` 的职责；
- reducer 必须接收增量的约束；
- Prompt schema 与运行 schema 必须同步；
- CouncilJudge 统一读取顶层 EvidenceNote 的原因；
- 验证命令和结果。
