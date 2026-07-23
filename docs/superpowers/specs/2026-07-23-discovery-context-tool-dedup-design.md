# 发现阶段上下文契约与工具去重设计

## 背景

Codeguard 在 `ContextProvider` 之后按 task 并发运行 ThreatModel、Behavior 和 Maintainability 三类发现者。当前管线已经向发现者提供 task patch、风险画像、任务上下文和标签知识，但三个领域 prompt 没有准确解释这些输入的来源、覆盖范围、证明力和局限。

实际运行中，发现者会把已经提供的上下文当作普通背景，再调用 `get_file_content` 重读当前文件或同一依赖文件。最新 trace 中，发现阶段出现 16 次 `get_file_content`，其中 4 次为同一发现者内的完全重复调用，7 次读取的目标文件已经完整出现在新增文件的 task patch 中。重复调用同时增加 Gateway 压力、LLM 工具轮次和对话上下文体积。

本设计解决两个问题：

1. 让发现者明确理解前置节点提供的每类上下文，先消费已有事实，再决定是否需要工具。
2. 在单个发现者范围内对工具调用提供确定性去重，防止模型违反 prompt 时重复访问 Gateway 或重复注入大段内容。

三路发现者之间保持隔离。它们承担不同方法论，互不共享工具缓存或运行记忆，这是现有设计的不变量。

## 目标与非目标

### 目标

- 三个发现者的有效 system prompt 都包含同一份稳定上下文数据字典和硬工具调用契约；本次 task 的实际上下文只动态进入 user 消息。
- 工具只用于填补一个明确、会影响候选是否成立的事实缺口。
- 同一发现者在一次 review 中对相同工具和规范化参数最多实际访问 Gateway 一次；最终 `gathered_context` 聚合也使用相同 canonical key，避免路径写法差异留下重复事实。
- 同一次 ReAct 对话不重复注入已经返回过的完整工具内容。
- 并发 task 请求相同事实时使用 single-flight，避免并发击穿。
- `gathered_context` 只记录真实 Gateway 工具事实，不记录结构化输出使用的 `ReviewResult` ToolMessage。
- 保持跨文件探索能力和现有审查召回率。

### 非目标

- 不在 ThreatModel、Behavior、Maintainability 三个发现者之间共享缓存。
- 不修改 Java Gateway、工具 HTTP 协议或工具服务端缓存。
- 不修改风险路由、EvidencePlanner、EvidenceAgent 或 CouncilJudge 的职责。
- 不修改 `Issue` / `ReviewResult` 产品输出协议。
- 不要求本轮给所有工具增加结构化 `missing_fact` 参数。

## 方案选择

### 方案 A：只修改 prompt

优点是改动小；缺点是没有确定性兜底，模型仍可能重复调用，尤其在并发 task 互相不可见时。该方案不足以解决 Gateway 压力和重复上下文问题。

### 方案 B：prompt 硬门槛 + 发现者内部去重

Prompt 负责让模型正确消费上下文并减少无必要调用；执行层按发现者隔离地提供缓存和 single-flight，阻止完全重复的底层调用。这同时覆盖行为原因和确定性后果，是本设计采用的方案。

### 方案 C：所有工具强制增加缺失事实参数

要求每次工具调用携带 `missing_fact` 和 `context_gap`，可观测性最强，但会改变所有工具 schema，增加模型工具调用失败概率，并扩大本轮范围。本轮不采用；若后续仍无法从 trace 判断调用理由，可作为独立演进。

## 上下文数据契约

新增一份共享的发现者上下文契约 prompt 片段，由发现者 system prompt 构造逻辑统一注入。system 只解释稳定语义和工具门槛，不携带 task 值。三个领域 prompt 保留各自的角色边界、方法论和领域工具示例，避免复制的数据字典随时间漂移。

本次 task 的实际值由一个 user prompt renderer 统一动态渲染：

- `<task_patch>`：唯一一份当前 patch，并声明 hunk/full-new-file 覆盖范围；
- `<change_summary>`、`<risk_profile>`：明确标为非问题证据；
- `<prefetched_context>`：每条事实声明 kind/source/scope/truncated，包级声明 bundle_truncated；
- `<context_status>`：明确某类事实被跳过、失败或不可用的实际原因；
- `<tag_knowledge>`：本次实际标签知识，明确标为方法论而非仓库事实。

不得再把风险文本、`TaskContextBundle.render()` 或标签知识分别拼到不同消息角色；task patch 也不得在风险块中重复出现。

有效 prompt 必须准确解释以下输入。

### task patch

- 是当前 task/hunk 的 unified diff 片段，是本轮发现的主要代码证据和唯一合法 Issue 定位范围。
- 包含变更行和有限上下文行，不保证包含整个文件。
- 新增文件的 patch 可能已经包含完整文件；若候选判断所需代码已经出现，不得用 `get_file_content` 重读当前文件。
- 当 hunk 明确为 `/dev/null` 新增文件且 patch 覆盖完整文件时，发现者和 EvidenceAgent 都必须把该 patch 视为当前文件全文；即使模型仍请求当前文件，执行层也不得访问 Gateway 或把短标记记录成新事实。
- 只有判断依赖 patch 未包含的未修改字段、注解、辅助方法或周边实现时，当前文件全文才构成真实缺口。

### 整体变更摘要

- 由 Summary 节点基于已选择的变更范围生成，用于理解 PR 的整体意图和多个 task 的关系。
- 摘要可能省略细节，不能单独证明 Issue，也不能用于报告当前 task 之外的问题。

### 风险画像

- 由 RiskTriage 根据文件路径和 diff 文本变化方向生成，解释当前 task 为什么被路由到某个发现者以及应优先检查哪些风险。
- 风险标签是审查先验，不表示缺陷已经存在，不能代替代码或工具事实。

### AST structure

- 由 Java Gateway 的 `get_diff_ast` 对变更文件执行静态解析，再由 ContextProvider 切出当前 hunk 所在文件的文件级 AST 结构。
- 可能包含类、方法、方法行范围、控制流节点和可解析的调用边；具体字段取决于源文件和解析结果。
- AST 用于理解当前 hunk 所属类/方法、控制流和局部调用关系，不代表其中存在问题，也不保证覆盖动态调用、反射或无法解析的代码。
- AST 已经回答类、方法、控制流或调用关系问题时，不得为获取相同结构读取文件全文。

### sensitive API

- 由 Java Gateway 的 `find_sensitive_apis` 扫描变更文件，再筛选为当前 task 文件且位于当前 hunk 行范围内的命中项。
- 事实包含命中的 API、文件、行号、调用参数和规则危险等级。
- 规则危险等级描述 API 本身的敏感程度，不等于漏洞成立，不等于最终 Issue severity，也不能证明输入可控、调用可达或缺少防护。
- 发现者应继续结合 task patch、AST 和必要的调用方事实验证完整路径；不得为了重新确认扫描结果重复调用同一工具。

### find callers

- 仅在当前 RiskTag 需要调用关系，且 AST 能解析出当前 hunk 所属方法时，由 ContextProvider 预先查询该方法的直接调用方。
- 用于判断 API 契约、返回值、事务、资源所有权、幂等或状态变化的影响范围。
- “未找到直接调用方”只表示静态工具未发现，不证明方法绝对没有调用方；反射、框架绑定和动态调用可能不在结果中。
- 已有调用方结果覆盖当前问题时，不得再次查询相同方法。

### code metrics

- 仅在 RiskTag 需要复杂度事实时，由 `get_code_metrics` 预先计算当前文件的方法级圈复杂度、LOC、嵌套深度和参数数量。
- 用于辅助解释具体变化带来的复杂度或可测试性成本，不能仅因某个数值超过阈值就报告 Issue。
- 已有指标覆盖当前文件时，不得再次调用相同参数获取同一指标。

### 标签知识

- 是根据当前 task 命中的 RiskTag 注入的方法论、正例、反例和严重性提示。
- 它是审查清单，不是当前仓库事实；必须由 task patch 或可靠工具事实验证。

### 截断与缺失

- `truncated=true` 表示事实因字符预算被截断，不表示全部上下文不可用。
- 只有候选判断确实依赖被截掉的部分时，截断才构成工具调用理由。
- 某类上下文没有出现，可能是 RiskTag 未要求、AST 无法定位方法、工具失败或没有匹配事实。发现者只能在该缺失会影响当前候选时补充查询。

## 工具调用硬门槛

三个发现者在每次工具调用前都必须完成以下判断：

1. 明确当前候选缺少的具体事实。
2. 检查 task patch、整体摘要、风险画像、AST、敏感 API、调用方、代码指标和标签知识是否已经回答该事实。
3. 确认已有上下文无法回答，并指出缺口来自未覆盖范围、截断、解析局限还是其他文件。
4. 选择能够填补缺口的最小工具和最小参数范围。
5. 工具返回目标事实后停止扩展探索；只有新结果暴露了另一个会改变候选结论的具体缺口时，才允许继续调用。

以下理由不构成合法工具调用理由：

- “重新确认一下”；
- “了解完整代码”；
- “看看还有没有其他问题”；
- 已有 AST、敏感 API、调用方或指标已经回答相同问题；
- 当前 patch 已包含判断所需实现，但仍读取当前文件全文。

Prompt 要明确说明：每次工具调用都会增加执行成本，并把工具结果追加到当前对话上下文；上下文充分时必须略过调用。

领域 prompt 在共享契约之上补充各自规则：

- ThreatModel 只为输入来源、传播、防护、敏感 sink 或可达性缺口补工具事实。
- Behavior 只为调用方契约、状态变化、错误路径或业务不变量缺口补工具事实。
- Maintainability 只为未覆盖的复杂度、重复、所有权或跨文件设计事实补工具事实。

## 发现者内部工具去重

### 作用域

每次 pipeline review 为三个发现者分别创建独立的工具调用协调器：

```text
ThreatModel coordinator       独立缓存
Behavior coordinator          独立缓存
Maintainability coordinator   独立缓存
```

同一发现者节点并发运行的 task 共享其协调器。review 结束后协调器释放，不跨 review、仓库、工具 session 或发现者复用。

### 缓存键

缓存键为：

```text
(tool_name, canonical_arguments)
```

- 参数使用稳定 JSON 序列化。
- 文件路径统一为 `/` 并消除冗余 `.` 段。
- 不强制转小写，避免破坏 Linux 仓库的大小写语义。
- 不根据自然语言内容推测两个不同参数是否等价。

### single-flight

同一发现者的多个 task 并发请求相同缓存键时，只有第一个请求访问 Gateway；其他请求等待同一个结果。所有等待者收到相同的成功结果或同一次失败。

只将成功且非空的结果写入已完成缓存。失败、异常、空结果和工具拒绝不会长期缓存，因此之后的新调用可以重试。并发等待者共享当前失败，避免失败瞬间击穿 Gateway。

### 对话内重复与跨 task 复用

需要区分两种复用：

- 同一次 ReAct 对话已经收到过某缓存键的完整结果：再次调用时返回简短标记，要求复用前述内容，不再次注入全文。
- 同一发现者的另一个 task 首次请求该缓存键：底层 Gateway 结果可以从发现者缓存复用，但该 task 没见过其他 task 的消息，因此仍向它提供完整内容。

这样既避免接口重复执行，也不会让独立 task 因只收到“已读取”标记而缺少事实。

### 上下文收集

`_extract_gathered_context` 只接受实际暴露的 Gateway 工具名。LangChain 为结构化响应生成的 `ReviewResult` ToolMessage 不得进入 `gathered_context`。

最终 reducer 继续作为跨节点状态的防御性去重，但不再承担阻止底层调用的职责。

## 数据流

```text
ContextProvider
  ├─ task patch
  ├─ AST structure
  ├─ sensitive API
  ├─ callers / metrics
  ├─ risk profile
  └─ domain knowledge
          │
          ▼
共享上下文契约 + 领域 prompt
          │
          ├─ 上下文充分 ──────────────> 直接产生候选
          │
          └─ 存在明确事实缺口
                  │
                  ▼
           发现者独立协调器
           ├─ 对话内已见：短标记
           ├─ 缓存命中：返回内容
           ├─ 正在执行：等待结果
           └─ 未命中：访问 Gateway
```

## 错误处理

- 工具协调器异常不得拖垮发现者；无法使用协调能力时退化为原有工具调用路径并记录告警。
- 工具失败仍按现有 ToolResponse/ToolMessage 语义返回给 Agent，不伪装成成功事实。
- 失败不进入已完成缓存，后续明确需要同一事实时允许重试。
- 同一次 ReAct 对话的“已提供”短标记只在本地已实际收到完整结果后使用。
- 缓存不得跨 tool session，避免仓库或 SHA 变化后复用旧事实。

## 测试与验收

### 确定性单元测试

1. 同一 ReAct 调用连续两次读取同一路径：Gateway 实际调用一次，第二次不包含完整文件正文。
2. 同一发现者的两个并发 task 请求相同工具参数：底层执行一次，两个 task 都获得完整结果。
3. 同一发现者调用相同工具但参数不同：分别执行。
4. ThreatModel 与 Maintainability 请求同一文件：各自执行一次，证明三路隔离。
5. 首次工具调用失败：并发等待者共享失败，之后的新调用可以重试。
6. 空结果不进入成功缓存。
7. `ReviewResult` ToolMessage 不进入 `gathered_context`。
8. 最终发现者输出不包含重复的相同工具事实。

### Prompt 契约测试

三个发现者的有效 system prompt 必须包含：

- 本设计中每类上下文的来源、范围、用途和局限；
- AST 与敏感 API 的明确语义；
- 风险画像、摘要和标签知识不是问题证据；
- `truncated` 的含义；
- 工具调用前的五步硬门槛；
- 非法调用理由；
- 上下文充分时略过工具、取得目标事实后停止扩展的要求。

最终 user prompt 还必须验证：

- patch 只出现一次，且本次 summary/risk/fact/status/knowledge 分区明确；
- AST、敏感 API、调用方和指标事实携带实际 scope/source/truncated；
- 包级截断以及跳过、失败、不可用原因可见；
- task-scoped 标签知识不进入 system。

### Agent/eval 回归

增加一个新文件完整出现在 task patch、AST 已包含类/方法/控制流且仍暴露 `get_file_content` 的场景。只有实际传给发现者的 patch 未被大 diff 策略截断时，才能标为 `full_new_file` 并省略当前文件全文读取；截断后必须允许补读。期望 Agent 能发现 patch 内问题，但不做无意义的重复读取。

涉及真实 LLM 的行为不做严格 pytest 断言，使用对应 eval profile 比较：

- `get_file_content` 和总工具调用数下降；
- 重复工具调用数归零；
- 工具返回上下文字符数下降；
- Precision、Recall、F1 和关键问题 recall 不出现明显回退；
- 需要跨文件事实的用例仍能调用工具并保持召回。

## 变更边界

预计修改范围：

- `pipeline/engines.py`：发现者工具协调、对话内重复保护、上下文抽取过滤；
- `pipeline/graph.py`：为每个发现者节点建立独立的 review-scoped 协调器；
- `prompts/`：共享上下文契约及三个领域 prompt 的补缺规则；
- `tests/`：缓存、并发、隔离、失败重试、prompt 契约和上下文抽取测试；
- 必要的 eval fixture/profile 断言与工具使用统计。

不修改 Java Gateway、三路发现者隔离、EvidenceAgent 取证策略或产品输出结构。

## 成功标准

改动完成后，系统同时满足：

1. Agent 能准确解释并优先使用 ContextProvider 提供的 AST、敏感 API、调用方和指标事实。
2. 每次工具调用都服务于一个现有上下文无法回答的明确事实缺口。
3. 单个发现者内相同工具和参数只实际访问 Gateway 一次。
4. 同一次对话不重复注入完整工具结果。
5. 三个发现者仍保持互相隔离。
6. 工具调用和上下文体积下降，审查质量没有明显回退。
