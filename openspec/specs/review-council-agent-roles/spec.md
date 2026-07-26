# review-council-agent-roles Specification

## Purpose
定义 ReviewCouncil 内部默认发现者的第一版方法论分工、兼容 category 映射、候选输出协议、证据请求 hint 与数量/轮次上限。该能力把旧 security / logic / quality 领域审查员升级为 `ThreatModelAgent`、`BehaviorAgent`、`MaintainabilityAgent` 三个可追踪的多 Agent 角色，同时保持最终 `Issue` / `ReviewResult` 产品输出结构不变。
## Requirements
### Requirement: ReviewCouncil 方法论发现者

ReviewCouncil SHALL 使用三个方法论发现者作为默认候选 issue 生成角色：`ThreatModelAgent`、`BehaviorAgent`、`MaintainabilityAgent`。它们 SHALL 在 ContextProvider 之后并行运行，并 SHALL 分别以 `threat_model`、`behavior`、`maintainability` 作为稳定 `source_agent` 标识。

#### Scenario: 三个方法论发现者并行运行

- **WHEN** ADR-032 默认审查路径运行到 ReviewCouncil 发现阶段
- **THEN** 系统并行运行 `ThreatModelAgent`、`BehaviorAgent`、`MaintainabilityAgent`
- **THEN** 每个发现者将候选问题写入共享 `candidate_issues`

#### Scenario: 发现者身份进入 trace

- **WHEN** 任一发现者完成审查
- **THEN** `CouncilTrace` 记录该发现者的稳定 `source_agent` 或等价角色标识

### Requirement: 发现者 category 兼容映射

ReviewCouncil SHALL 保持最终产品输出的 category 兼容性。`ThreatModelAgent` 产出的候选 SHALL 映射为 `security`，`BehaviorAgent` 产出的候选 SHALL 映射为 `logic`，`MaintainabilityAgent` 产出的候选 SHALL 映射为 `quality`。该映射 SHALL 只影响候选分类和最终 `Issue.type` / category 语义，不改变 `Issue` 数据结构。

#### Scenario: ThreatModelAgent 映射到 security

- **WHEN** `ThreatModelAgent` 产出一个候选问题
- **THEN** 该候选的 `source_agent` 为 `threat_model`
- **THEN** 该候选的 `category` 为 `security`

#### Scenario: BehaviorAgent 映射到 logic

- **WHEN** `BehaviorAgent` 产出一个候选问题
- **THEN** 该候选的 `source_agent` 为 `behavior`
- **THEN** 该候选的 `category` 为 `logic`

#### Scenario: MaintainabilityAgent 映射到 quality

- **WHEN** `MaintainabilityAgent` 产出一个候选问题
- **THEN** 该候选的 `source_agent` 为 `maintainability`
- **THEN** 该候选的 `category` 为 `quality`

### Requirement: 发现者输出多个 CandidateIssue

每个发现者 SHALL 一次性输出零个或多个 `CandidateIssue`，而不是只允许输出一个问题。发现者内部 MAY 使用 ReAct 工具循环形成候选主张，但外层 ReviewCouncil SHALL NOT 在 EvidenceAgent 补证后回调原发现者重新审查。

#### Scenario: 单个发现者返回多个候选

- **WHEN** 一个发现者在同一份 diff 中发现多个属于其职责范围的问题
- **THEN** 它可以返回多个 `CandidateIssue`
- **THEN** 这些候选全部写入共享黑板并进入后续 Evidence / Challenge / SelfChecker 流程

#### Scenario: EvidenceAgent 不回调发现者

- **WHEN** EvidenceAgent 为某个候选补充了 `EvidenceNote`
- **THEN** 该证据写入共享 State
- **THEN** 系统不因该证据自动重新运行产出该候选的发现者

### Requirement: CandidateIssue 状态协议

`CandidateIssue` SHALL 至少包含候选身份、来源角色、兼容 category、定位、候选主张、建议严重级别、置信度、证据状态、是否需要补证、证据请求、证据记录和可选 challenge 结果。中间态 SHALL 只进入 ReviewCouncil State、trace 或 eval metadata，MUST NOT 扩展最终 `Issue` / `ReviewResult` 产品输出结构。

#### Scenario: 候选包含来源与证据状态

- **WHEN** 发现者产出 `CandidateIssue`
- **THEN** 候选包含 `source_agent`、`category`、`claim`、`severity_proposal`、`confidence`、`evidence_status` 和 `needs_evidence`

#### Scenario: 中间态不进入产品输出

- **WHEN** SelfChecker 将候选问题转为最终 `ReviewResult`
- **THEN** 最终 `Issue` 不包含 `source_agent`、`evidence_requests`、`evidence_notes` 或 `challenge` 字段

### Requirement: EvidenceRequest 开放式证据请求

`EvidenceRequest.kind` SHALL 作为 EvidenceAgent 的路由 hint，而不是完整证据需求分类体系。系统 SHALL 支持 `related_snippet`、`caller_path`、`sensitive_sink`、`metric_context`、`open_question` 五类第一版 kind。无法稳定归类的证据需求 SHALL 使用 `open_question`，并通过 `question`、`reason`、`target` 表达完整意图。

#### Scenario: 使用固定 kind 路由常见请求

- **WHEN** 发现者需要常见证据类型，如调用路径、敏感 sink 或复杂度上下文
- **THEN** 它使用对应的 `caller_path`、`sensitive_sink` 或 `metric_context` kind

#### Scenario: 未知证据需求使用 open_question

- **WHEN** 发现者需要确认的事实无法稳定归入既有 kind
- **THEN** 它使用 `open_question`
- **THEN** 它填写 `question`、`reason` 和 `target` 说明需要确认什么以及为什么影响候选成立

### Requirement: ReviewCouncil 数量和轮次上限

ReviewCouncil SHALL 对发现者候选数量和证据请求数量设置第一版上限，以避免 LLM 输出膨胀和工具调用失控。默认上限 SHALL 为每个发现者最多 5 个候选、每个候选最多 2 个证据请求、每次审查最多 20 个证据请求、默认最多 1 轮证据补充。

#### Scenario: 候选数量被截断

- **WHEN** 单个发现者返回超过 5 个候选问题
- **THEN** 系统只保留排序最靠前的 5 个候选进入后续流程

#### Scenario: 达到证据轮次上限后停止补证

- **WHEN** `max_evidence_rounds` 已达到默认上限 1
- **THEN** Coordinator 不再运行 EvidenceAgent 补证
- **THEN** 系统进入 ChallengeAgent 或 SelfChecker 的后续流程

### Requirement: 角色图谱工具边界

ThreatModelAgent SHALL 使用 `inspect_security_path`，BehaviorAgent SHALL 使用 `inspect_change_impact`，MaintainabilityAgent SHALL 使用 `inspect_structure`；三者共享受图谱确认路径约束的 `get_file_content`。
