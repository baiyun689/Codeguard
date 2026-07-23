# ReviewCouncil 候选归并设计

日期：2026-07-23

## 1. 背景

三个 task-scoped 发现者会并行产出 `CandidateIssue`。当前
`_candidate_dedup_reducer` 在 LangGraph fan-in 期间直接执行语义去重：

1. 将候选转换为 `Issue`，按 basename、行号和自由文本 `type` 去重；
2. 再按 basename、原始 `type` 和相邻行容差强制合并。

该实现会把不同目录下的同名文件视为同一文件，也会把同文件、同类型、相邻行但
根因不同的候选删除。它还依赖候选到达顺序：反转 fan-in 顺序可能改变最终保留
哪一个候选。Reducer 在状态合并时做不可逆语义判断，既不满足稳定收集的职责，
也无法借助 task patch 和候选语义判断是否真是同一底层问题。

## 2. 目标

- 在三个发现者 fan-in 完成后，对候选执行一次显式归并。
- 接受极小概率的 LLM 误归并，但显著降低现有规则误删。
- 不改造组级证据，不要求 CouncilJudge 执行 merge/split。
- 不改变最终 `Issue` 产品结构。
- 复用现有 `RiskTag` 解析和 EvidencePlanner 策略，不建立第二套分类体系。
- 不同候选块并行调用 LLM，并保持输出顺序稳定。
- LLM 不可用、失败或输出非法时安全保留候选。

## 3. 非目标

- 不追求发现阶段零重复。
- 不让 LLM 生成新的 claim、suggestion、severity 或候选。
- 不按 basename、自由文本相似度或 RiskTag 相等直接删除候选。
- 不引入 CandidateGroup 产品输出。
- 不改变 `EvidenceRequest`、`EvidenceNote`、`EvidenceFinding` 或 `Verdict`
  的候选级绑定。
- 不让 Java Gateway 参与语义归并。

## 4. 核心决策

### 4.1 Reducer 只收集，不做语义去重

发现者写入 `raw_candidate_issues`。该字段使用稳定 ID reducer：

```python
raw_candidate_issues: Annotated[
    list[CandidateIssue],
    collect_candidates_by_id,
]
```

Reducer 只按 `candidate.id` 去除状态传播产生的同一对象重复。Coordinator 随后按
规范顺序重排候选，因此并行到达顺序不进入归并语义。
它不读取 file、line、type、claim、severity 或 source agent。

`candidate_issues` 改为普通列表，由 CouncilCoordinator 在 fan-in 后写入一次：

```python
candidate_issues: list[CandidateIssue]
```

EvidencePlanner 及后续节点继续只消费 `candidate_issues`。

### 4.2 CouncilCoordinator 成为候选归并的唯一写入点

CouncilCoordinator 在三路发现者全部完成后执行：

```text
raw candidates
  → resolve RiskTag
  → build candidate blocks
  → parallel LLM grouping
  → validate groups
  → apply accepted groups
  → candidate_issues
```

归并实现放入独立深模块 `pipeline/candidate_dedup.py`。它向图暴露一个主要
interface：

```python
def deduplicate_candidates(
    candidates: Sequence[CandidateIssue],
    tasks_by_id: Mapping[str, ReviewTask],
    tag_resolutions: Mapping[str, CandidateTagResolution],
    *,
    llm: Any,
    structured_method: str,
    max_workers: int = 8,
) -> CandidateDedupResult:
    ...
```

`CandidateDedupResult` 返回稳定排序后的候选、接受/拒绝的归并记录、LLM 调用数
和 trace 数据。路径规范化、构块、prompt 渲染、结构校验和归并应用均隐藏在模块
实现内，调用者不需要了解这些细节。

## 5. RiskTag 解析

### 5.1 复用现有解析器

继续使用：

- `pipeline/evidence_rules/terms.py:CANDIDATE_TAG_TERMS`
- `pipeline/evidence_rules/classify.py:resolve_candidate_evidence_tag`
- `CandidateTagResolution`

现有规则对 type、claim 和 suggestion 打分：

| 命中 | 分数 |
|---|---:|
| type 精确别名 | 8 |
| type 强语义词 | 6 |
| claim 强语义词 | 4 |
| claim 弱语义词 | 1 |
| suggestion 强语义词 | 1 |

只有最高分至少为 4、最高分唯一且领先第二名至少 2 分时，才采用规则结果。歧义
候选通过受限结构化 LLM 从现有 25 个 `RiskTag` 中选择；非法枚举、置信度低于
0.75、异常或 `None` 回退 `GENERAL_REVIEW`。

### 5.2 前移并复用解析结果

将 EvidencePlanner 当前的批量解析能力提取为公开 interface：

```python
def resolve_candidate_tags(
    dossiers: Sequence[CandidateDossier],
    *,
    classifier_llm: Any,
    structured_method: str,
    max_workers: int = 8,
) -> dict[str, CandidateTagResolution]:
    ...
```

CouncilCoordinator 在归并前调用一次，并把结果写入：

```python
candidate_tag_resolutions: dict[str, CandidateTagResolution]
```

EvidencePlanner 优先复用该映射。兼容调用没有提供映射时，才沿用现有内部解析
路径。这样不会因归并新增一轮重复的 RiskTag 分类调用。

### 5.3 RiskTag 不是硬归并条件

RiskTag 只是归并 LLM 的语义提示：

- 相同 RiskTag 不代表同一问题；
- 不同 RiskTag 也可能描述同一底层问题；
- `GENERAL_REVIEW` 不阻止候选进入 LLM 归并。

原始 `Issue.type` 保持自由文本，用于最终展示，不强制改成 RiskTag。

## 6. 候选块

### 6.1 路径规范化

使用现有仓库路径规范化语义：

- `\` 转为 `/`；
- 消除冗余 `.` 段；
- 使用完整 repo-relative path；
- 不取 basename；
- 不转小写，保留 Git 路径大小写。

### 6.2 邻接规则

两个不同候选在满足以下全部条件时建立无向连接：

1. 规范化完整路径相同；
2. 满足下列任一条件：
   - `task_id` 相同；
   - 两者行号均大于 0，且绝对距离不超过 5 行。

不使用 type、RiskTag、claim 文本相似度或 source agent 作为邻接前提。

对邻接图取连通分量：

- 单元素分量直接保留，不调用 LLM；
- 多元素分量形成一个候选块，每块调用一次 LLM；
- 每个 candidate 只属于一个块。

连通分量可能因 A 邻近 B、B 邻近 C 而包含彼此不邻近的 A 和 C。候选块只限定
LLM 的比较范围，不意味着整个块会被合并；输出校验仍逐组检查所有成员。

### 6.3 规范顺序

Coordinator 在构块前按以下键排序候选：

1. 规范化完整文件路径；
2. 有效行号；无有效行号排在同文件有效行号之后；
3. task ID；
4. 固定 source agent 顺序：threat_model、behavior、maintainability、其他；
5. candidate ID。

构块、prompt、归并应用和最终候选列表都以该规范顺序为准。这样反转发现者到达
顺序不会改变 LLM 输入或最终输出。

## 7. LLM 归并

### 7.1 并发

不同候选块互不依赖，使用 `run_bounded_parallel` 并行调用：

- 默认最大并发数为 8；
- 块按规范化文件路径、最小有效行号、最小 candidate ID 稳定排序；
- 块内候选按第 6.3 节规范顺序排序；
- 并发结果按输入块顺序组装，不按完成时间组装；
- 单块失败只影响该块。

### 7.2 Prompt 输入

system prompt 保存在独立文件 `prompts/candidate-dedup-system.txt`，声明：

- 只能判断已有候选是否同源；
- 禁止生成新候选或改写任何字段；
- 禁止改变 severity；
- 禁止使用工具；
- 有疑问时不归并；
- “一次代码修复是否能消除所有成员”是核心判断标准。

user prompt 对动态文本进行安全转义，并为每个块提供：

- candidate ID；
- source agent；
- file、line、task ID；
- 原始 type；
- 已解析 RiskTag、解析来源和置信度；
- claim；
- suggestion；
- 相关 task patch。

同一 task patch 在块内只提供一次。大 diff 下继续使用当前已截断并标注覆盖范围
的 scoped patch。

### 7.3 结构化输出

```python
class DuplicateGroup(BaseModel):
    member_ids: list[str]
    representative_id: str
    same_root_cause: bool
    same_affected_behavior: bool
    single_fix_resolves_all: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: NonBlankStr


class CandidateDedupDecision(BaseModel):
    groups: list[DuplicateGroup] = Field(default_factory=list)
```

LLM 直接输出完整分组，不输出 pairwise 边，不使用 union-find 传递合并。它只能从
组内成员中选择一个 `representative_id`，不能生成 canonical claim。

### 7.4 LLM 选择代表候选

代表候选应是现有成员中最清晰、最具体、最能表达共同底层问题的一条。LLM 可以
参考 claim、suggestion 和 confidence，但不能修改候选内容。

若 `representative_id` 非法，不使用本地启发式替补，而是拒绝该归并组并保留所有
成员。

## 8. 输出校验

常量：

```python
MIN_DEDUP_CONFIDENCE = 0.90
MAX_DEDUP_WORKERS = 8
CANDIDATE_LINE_WINDOW = 5
```

一个 LLM 分组只有满足以下全部条件才接受：

1. 至少有两个不同 member ID；
2. 所有 ID 均属于当前候选块；
3. `representative_id` 属于 member；
4. 所有成员规范化完整路径相同；
5. 任意两个成员满足“同 task 或有效行号距离不超过 5 行”；
6. `same_root_cause` 为 true；
7. `same_affected_behavior` 为 true；
8. `single_fix_resolves_all` 为 true；
9. confidence 不低于 0.90；
10. reason 非空；
11. 不与其他接受组共享 member。

若多个输出组重叠，所有涉及冲突 member 的组都拒绝；不采用“先到先得”。其他无
冲突且合法的组仍可接受。

Pydantic 解析失败、LLM 异常、超时、返回 `None`、空理由、非法 ID、低置信度或
任何校验失败，均不得删除相关候选。

## 9. 应用归并

对每个接受组：

- 保留 `representative_id` 对应的原始 CandidateIssue；
- 移除其余 member；
- 在该组最早成员原本的位置输出代表候选；
- 未归并候选保持规范相对顺序。

归并过程不改变代表候选的 ID、task ID、type、claim、suggestion、severity 或
confidence。

该行为对 LLM 块完成顺序无感，但 LLM 自身判断仍可能存在模型不确定性。固定模型、
结构化 schema、稳定输入顺序和高置信阈值用于降低波动。

## 10. Trace 与指标

CouncilTrace 至少记录：

- `candidate_tags_resolved`：规则、LLM、GENERAL_REVIEW 的数量；
- `candidate_dedup_blocks_built`：raw、singleton、multi-member block 数量；
- `candidate_dedup_group_accepted`：代表 ID、被移除 IDs、置信度、理由；
- `candidate_dedup_group_rejected`：成员 IDs 和拒绝原因；
- `candidate_dedup_block_failed`：块 ID 和安全回退原因；
- `candidate_dedup_completed`：输入数、输出数、移除数、LLM 调用数。

不得把完整 patch 或敏感工具结果写入 trace。

`CouncilRunStats` 增加或复用内部诊断字段时，应从结构化归并结果派生，不进入最终
产品 `ReviewResult`。

## 11. 失败策略

| 场景 | 行为 |
|---|---|
| 没有 LLM | 只按 candidate ID 收集，不做语义归并 |
| RiskTag 规则明确 | 使用规则 resolution |
| RiskTag 分类 LLM 失败 | 使用 GENERAL_REVIEW，候选仍可参与归并 |
| 某候选块归并 LLM 失败 | 该块全部保留 |
| 某个输出组非法 | 只拒绝该组 |
| 输出组重叠 | 拒绝涉及冲突 member 的所有组 |
| 代表 ID 非法 | 拒绝该组 |
| confidence < 0.90 | 拒绝该组 |

失败回退的唯一方向是保留更多候选，不得静默删除。

## 12. 测试策略

### 12.1 稳定收集

- reducer 只按 candidate ID 去重；
- 不同 ID 即使 file、line、type 全部相同也先保留；
- 反转发现者到达顺序后，Coordinator 输入经稳定排序得到相同块。

### 12.2 路径和构块

- 不同目录下同 basename 不进入同一块；
- Windows 和 POSIX 分隔符规范化为同一路径；
- 同 task 的无行号候选进入同一块；
- 不同 task、相距 5 行进入同一块；
- 不同 task、相距 6 行不进入同一块；
- 连通链形成一个候选块，但不自动形成一个归并组。

### 12.3 RiskTag 复用

- 精确 alias、strong phrase 和歧义回退沿用现有测试；
- Coordinator 解析结果进入 ReviewState；
- EvidencePlanner 复用已有 resolution，不重复调用分类 LLM；
- 没有预解析结果的兼容路径保持原行为。

### 12.4 LLM 归并

- 同文件相邻行、同 type、不同方法：LLM 不归并；
- 同行同 RiskTag、不同根因：LLM 不归并；
- 不同 type、同一根因：允许归并；
- 不同 RiskTag、同一底层问题：允许归并；
- 一次修复不能同时解决：不归并；
- confidence 0.89：不归并；
- confidence 0.90：允许归并；
- 非法 ID、重复 ID、重叠组、非法代表、空理由：安全保留；
- LLM 返回 None 或抛异常：该块全部保留。

### 12.5 并发和顺序

- 两个以上候选块实际并行执行；
- 最大并发不超过 8；
- 人为反转各块完成顺序，最终候选顺序不变；
- 单块失败不影响其他块接受合法归并。

### 12.6 图集成和评测

- 三路 discover 写 raw candidates，Coordinator 写最终 candidates；
- EvidencePlanner 只看到归并后的候选；
- CouncilTrace 记录接受、拒绝和失败；
- 新增包含“真实重复”和“相邻独立同类问题”的 eval 用例；
- 对比归并前后候选压缩率、最终 recall、重复报告率和误归并率。

## 13. 兼容性与迁移

- `Issue` schema 不变。
- CandidateIssue 可保持现有字段；RiskTag resolution 放在 ReviewState 映射中。
- EvidenceRequest、EvidenceNote、EvidenceFinding、Verdict 不变。
- 历史 `AggregationStage` 保持 legacy 行为，不复用新的候选归并模块。
- mock/no-LLM 路径不做语义归并，仍能完整运行。
- 现有 `_candidate_dedup_reducer` 的跨维度测试迁移为 reducer 收集测试和
  CandidateDeduplicator 行为测试。

## 14. 验收标准

1. fan-in reducer 不再按 basename、line、type 或邻行删除不同 ID 候选。
2. 语义归并只在 CouncilCoordinator fan-in 后执行一次。
3. 不同候选块以最多 8 个 worker 并行调用 LLM。
4. RiskTag 使用现有解析器，EvidencePlanner 不重复分类。
5. 只有通过全部结构校验且 confidence 不低于 0.90 的组才删除非代表候选。
6. LLM 失败或输出非法时不删除候选。
7. 输入和块完成顺序变化不改变最终排序规则。
8. 不改变 Evidence/Judge 的候选级数据模型和最终 Issue 产品结构。
9. 确定性测试、ruff 和 mypy 全部通过。
10. eval 同时报告候选压缩率、重复报告率、误归并率和最终 recall，防止仅以压缩率
    判断效果。
