# Prompt 契约审查与加固设计

## 背景

ADR-032 与风险路由 Phase 1–5 已将默认审查路径改造成 task-scoped ReviewCouncil、策略化证据规划和候选级裁决闭环，但 Prompt 没有随调用粒度、工具可用性和结构化输出模型同步完成一次系统审查。当前已确认存在几类漂移：发现者实际只接收单个 `ReviewTask.patch`，基础 Prompt 却仍声称接收完整 diff；DirectEngine 没有工具，Prompt 却声称工具必然可用；低置信输出规则相互矛盾；部分 RiskTag 已路由给 Agent，其基础职责却明确排除该领域；结构化输出只写了模型名，没有准确解释字段语义。

Prompt 是 LLM 行为契约。此次改动以当前代码的真实调用图和 Pydantic 模型为准，不以历史文档或旧编排文本为准。

## 范围与可达性

### 当前生产路径

- `threat-model-base.txt`、`behavior-base.txt`、`maintainability-base.txt`
- `summary-system.txt`、`summary-user.txt`
- `evidence-tag-classifier-system.txt`、`evidence-tag-classifier-user.txt`
- `evidence-analysis.txt`
- `council-judge.txt`
- `aggregation-system.txt`，以及当前语义聚合调用实际构造的 user message
- `prompts/knowledge/{threat_model,behavior,maintainability}/` 下全部 RiskTag 知识片段

### eval-only

- `evals/matcher.py` 中的 case judge Prompt

eval Prompt 不进入产品审查输出，但仍须与其结构化模型对齐，避免评测结果因旧字段语义失真。

### 非默认路径

- `aggregation-user.txt` 只由旧 `AggregationStage` 引用，默认 CouncilJudge 语义合并不读取它。
- `fp_verify.txt` 只由 `legacy/stages/fp_filter.py` 引用；当前 `fp_verify_llm` 被默认图复用为 CouncilJudge 模型，并不会执行该 Prompt。
- `src/codeguard_agent/legacy/` 下的历史 Prompt 与 Supervisor 图。

按用户确认，legacy 内容保持历史原貌，不修改、不删除。主 Prompt 目录中的非默认 Prompt 会保留并由测试/注释明确其可达性，避免误认为当前产品契约。

## 设计决策

### 1. 发现者改为真实的 task-scoped 契约

三个基础 Prompt 统一说明：每次调用只审查一个已路由任务的 patch、风险画像与任务上下文，而不是完整 diff。输出 Issue 必须定位到当前任务文件，并由当前变更引入或暴露；工具读取到的 diff 外代码只能作为证据，不能成为新的审查目标。

工具描述改为条件式：只有运行时实际暴露工具时才允许调用；DirectEngine 或无工具 profile 下必须仅依据输入事实判断，不得假装调用工具或因工具缺失输出未经证实的候选。

### 2. 结构化输出字段按模型语义说明

发现者 Prompt 明确 `ReviewResult` 包含 `summary` 与 `issues`，并解释每个 `Issue` 字段：

- `severity` 只能为 `CRITICAL`、`WARNING`、`INFO`；
- `file` 必须是当前任务文件的仓库相对路径；
- `line` 是当前 task patch 中问题对应的 new-side 行号，无法可靠定位时使用 `0`，不得猜测；
- `type` 是稳定、简洁的问题类型；
- `message` 描述根因、触发条件和实际影响；
- `suggestion` 给出针对根因的可执行修复；
- `confidence` 是 0 到 1 的证据确信度，低于 Agent 阈值的候选不得输出。

Prompt 只说明字段语义，不要求模型手写 JSON；序列化格式继续由 LangChain/Pydantic structured output 约束。

Summary、Evidence 分类、Evidence 分析、CouncilJudge、Aggregation 与 eval judge 同样以各自实际 Pydantic wrapper 为准，明确外层字段、枚举、必填条件和空结果语义。

### 3. 领域边界与 RiskTag 路由一致

对全部知识片段与 `risk_rules/catalog.py` 的 reviewer 路由逐项核对。基础 Prompt 不得排除已经路由给自己的标签：

- Behavior 负责可证明的运行错误、错误结果、契约破坏、资源耗尽或性能退化；
- Maintainability 负责结构性复杂度、容量/性能热点的可演进性、资源所有权边界、API 契约可维护性和可测试性；
- ThreatModel 负责存在攻击者、信任边界或安全影响链的风险。

跨领域标签允许多个发现者从各自方法论分析，但同一 Agent 不得越界生成另一领域的问题；同源候选由 CouncilJudge 后续聚合。知识片段中的排除项、严重度和误报判例必须与上述边界一致。

### 4. 证据闭环保持职责单一

- Evidence 分类器只从 `allowed_tags` 返回一个标签，不用 task RiskTag 覆盖候选语义。
- EvidenceAgent 只解释固定事实与固定候选的关系，`insufficient` 不得伪装成支持或反驳。
- CouncilJudge 返回 `JudgeDecisions.decisions`，每个 dossier 恰好一个 `JudgeDecision`。`needs_more_evidence` 只在系统明确允许且非最终轮时使用；候选级 Judge 不主动选择 `merge`，语义合并由后续聚合阶段负责。
- Aggregation 只返回重复组索引，不新增、改写或删除问题。

### 5. 防止再次漂移

新增 Prompt 契约测试，覆盖：

- 当前调用点引用的 Prompt 文件均存在；
- 三个发现者不再包含“完整 diff”、工具必然可用、允许 diff 外 Issue、允许低阈值候选等旧契约；
- `ReviewResult`/`Issue`、Evidence、Judge、Aggregation 与 eval judge 的关键字段及枚举在 Prompt 中有准确语义；
- 每个 RiskTag 的 reviewer 路由都有对应知识片段；基础职责没有排除已路由的 `PERFORMANCE`、`RESOURCE_LIFECYCLE`、`API_CONTRACT` 等标签；
- 非默认 Prompt 的分类被显式记录，测试不把 legacy 文本纳入当前契约断言。

测试检查契约事实而非整段文案快照，避免正常措辞调整造成脆弱失败。

## 不做的事

- 不修改或删除 legacy Prompt、Supervisor 图和历史评测结果。
- 不改变 `Issue`、`ReviewResult`、Council 数据模型或新增产品字段。
- 不改变 RiskTag 注册表、任务路由、工具 allowlist 或 Java Gateway 能力。
- 不引入运行时 schema-to-prompt 自动生成层。
- 不把 Prompt 审查扩大为提示词效果重写；没有代码契约依据的纯文风调整不纳入本次改动。

## 验证

1. 运行新增 Prompt 契约测试及相关 reviewer、evidence、judge、graph 测试。
2. 运行 Agent 全量 pytest、Ruff 与 mypy。
3. 在 mock/无工具路径执行最小管线验证，确认空结果、task-scoped 输出和无工具条件不破坏图运行。
4. 检查 git diff，确认未触碰 legacy 与工作树中已有的无关改动。
