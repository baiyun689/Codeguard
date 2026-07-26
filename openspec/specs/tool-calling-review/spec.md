# tool-calling-review

## Purpose

把领域审查员从单次直连 LLM 调用升级为可调工具的 ReAct Agent,使其能在审查过程中自主获取 diff 之外的上下文(当前实装 `get_file_content` + 三个专属工具:`find_sensitive_apis`/`find_callers`/`get_code_metrics`)。每个审查员的工具集由其 `Reviewer.tool_allowlist` 字段声明(不对称分配),不再使用全局统一白名单。Java 工具服务以会话为单位、通过通用 `POST /api/v1/tools/{name}` 协议,提供受文件访问沙箱护栏约束的工具回调。是否启用工具调用由配置(`CODEGUARD_TOOL_SERVER_URL`)决定:配置则走 ReAct,未配置则回退到与阶段 2 一致的直连基准——二者构成"无工具 vs 有工具"的对照。Java 侧只做事实与护栏(沙箱、地面真值),不调 LLM、不判断"是不是问题"。
## Requirements
### Requirement: 工具调用审查执行引擎与基准分流

审查员 SHALL 支持两种执行路径:**直连调用**(单次结构化 LLM 调用,无工具)与 **ReAct Agent**(可经工具服务获取上下文)。`ReviewerStage` SHALL 在每次运行时,依据是否配置了可用的工具客户端选择路径:配置了则走 ReAct,未配置则走直连。直连路径 SHALL 与阶段 2 行为保持一致,作为"无工具"对照基准予以保留,不被替换。`--mode single` 的冻结基准(`pipeline/reviewer.py`)MUST 不受本能力影响。

每个审查员 Agent 可用的工具集 SHALL 由其 `Reviewer.tool_allowlist` 字段声明,SHALL NOT 再使用全局统一的 `enabled_tools` 列表。不同审查员可拥有不同的工具集,使 supervisor 的派发决策产生实质后果。

#### Scenario: 未配置工具服务时走直连基准

- **WHEN** 运行 `--mode pipeline` 且未配置工具服务(无工具客户端)
- **THEN** 每个领域审查员走单次结构化 LLM 调用,行为与阶段 2 一致,不发起任何工具调用

#### Scenario: 配置工具服务时走 ReAct Agent

- **WHEN** 运行 `--mode pipeline` 且已配置可用工具服务
- **THEN** 每个领域审查员以 ReAct Agent 形态运行,可在审查过程中自主发起工具调用

#### Scenario: 不同审查员拥有不同工具集

- **WHEN** 配置了工具服务且各审查员的 `tool_allowlist` 不同(如 security 有 `find_sensitive_apis`,logic 有 `find_callers`,quality 有 `get_code_metrics`)
- **THEN** 各审查员构造的 ReAct Agent 仅暴露其声明的工具,不暴露其他审查员的专属工具

#### Scenario: mock 模式不受影响

- **WHEN** `CODEGUARD_PROVIDER=mock`
- **THEN** 审查员返回既有 mock 假数据,既不构造 Agent 也不发起工具调用,管线端到端跑通

#### Scenario: 单次直连冻结基准不变

- **WHEN** 运行 `--mode single`
- **THEN** 走 `pipeline/reviewer.py` 原有逻辑,不引入工具、不构造 Agent

### Requirement: ReAct 审查结果的结构化与健壮性

ReAct Agent 的最终输出 SHALL 被解析为与直连路径同构的结构化审查结果(`ReviewResult` / issue 列表)。当 Agent 输出无法直接解析为结构化结果时,系统 SHALL 兜底为空结果而非抛错中断管线。单个审查员的失败(含工具调用异常、Agent 迭代耗尽)SHALL 不拖垮其余审查员或整条管线。

#### Scenario: Agent 输出可解析

- **WHEN** ReAct Agent 完成并产出符合结构的结果
- **THEN** 解析为 issue 列表并并入管线上下文,与直连路径产物同构

#### Scenario: Agent 输出不可解析时兜底

- **WHEN** ReAct Agent 的最终输出无法解析为结构化结果
- **THEN** 该审查员按空结果处理并记录告警,管线继续执行,不抛出未捕获异常

#### Scenario: 单个审查员失败被隔离

- **WHEN** 某个领域审查员在 ReAct 过程中抛出异常或迭代耗尽
- **THEN** 记录告警并跳过该审查员,其余审查员与后续阶段(聚合、误报过滤)正常完成

### Requirement: 工具会话生命周期

工具服务 SHALL 以会话为单位管理一次审查的工具上下文。系统 SHALL 提供创建会话端点(接收 repo 路径与本次 diff 涉及的文件集合,返回唯一 `session_id`)与销毁会话端点。所有工具调用 SHALL 通过 `X-Session-Id` 关联到具体会话;会话不存在或已过期时,工具调用 SHALL 返回错误而非读取任意状态。会话 SHALL 具备过期回收机制。一次审查运行内的多个并行审查员 SHALL 共享同一会话。

#### Scenario: 创建会话

- **WHEN** 客户端请求创建会话并提供 repo 路径与改动文件集合
- **THEN** 服务返回成功标志与唯一 `session_id`,并在服务端持有该会话的上下文与工具实例

#### Scenario: 工具调用关联会话

- **WHEN** 工具调用请求携带有效 `X-Session-Id`
- **THEN** 服务在该会话上下文内执行对应工具

#### Scenario: 会话缺失或过期

- **WHEN** 工具调用请求缺少 `X-Session-Id`,或其指向的会话不存在/已过期
- **THEN** 服务返回结构化错误(success=false + 错误信息),不执行任何文件访问

#### Scenario: 销毁会话

- **WHEN** 客户端请求销毁指定会话
- **THEN** 服务释放该会话资源;此后用该 `session_id` 的工具调用按会话缺失处理

#### Scenario: 并行审查员共享会话

- **WHEN** 一次审查运行内三个领域审查员并行发起工具调用
- **THEN** 它们使用同一 `session_id`,互不干扰地在同一会话上下文内读取

### Requirement: 通用工具分发协议与工具注册

工具服务 SHALL 通过统一的工具注册表按名称管理工具,并以通用路由 `POST /api/v1/tools/{name}` 分发调用,而非为每个工具硬编码独立路由。新增工具 SHALL 只需注册一个工具实现,无需改动分发协议。请求与响应 SHALL 使用统一信封:成功返回 `{success:true, result:...}`,失败返回 `{success:false, error:...}`。请求未知工具名时 SHALL 返回结构化错误。

#### Scenario: 按名称分发已注册工具

- **WHEN** 向 `POST /api/v1/tools/get_file_content` 发起带有效会话的请求
- **THEN** 服务经注册表查到该工具并执行,返回统一信封的结果

#### Scenario: 未知工具名

- **WHEN** 请求的工具名未在注册表中
- **THEN** 服务返回 `{success:false, error:...}`,不抛出未处理异常

#### Scenario: 新增工具不改协议

- **WHEN** 向服务注册一个新的工具实现
- **THEN** 该工具立即可经同一 `POST /api/v1/tools/{name}` 路由调用,分发逻辑与客户端协议无需改动

### Requirement: get_file_content 工具与文件访问护栏

系统 SHALL 提供 `get_file_content` 工具,读取仓库内指定相对路径文件的内容。该工具 SHALL 受文件访问沙箱约束:拒绝路径穿越(如包含 `..` 或规范化后逃逸出 repo 根目录的路径);**仅允许读取 repo 根目录内的源码文件**(由源码扩展名白名单约束,不再限制为本次 diff 改动文件集合),使审查员能读取 `get_repo_map` 指向的、diff 之外的定义文件;对超过大小上限的文件拒绝读取并返回可读的提示。所有拒绝 SHALL 以结构化错误返回,而非读取或抛出未处理异常。

#### Scenario: 读取范围内文件

- **WHEN** 请求读取一个存在于 repo 根目录内、扩展名属于源码白名单的文件
- **THEN** 返回该文件内容

#### Scenario: 读取 diff 之外的源码文件

- **WHEN** 请求读取一个位于 repo 根目录内、不属于本次 diff 改动集合、但扩展名属于源码白名单的文件(如 `get_repo_map` 指向的定义文件)
- **THEN** 返回该文件内容,而非以"不在审查范围内"为由拒绝

#### Scenario: 拒绝路径穿越

- **WHEN** 请求的路径包含 `..` 或规范化后逃逸出 repo 根目录
- **THEN** 返回结构化错误,不读取任何文件

#### Scenario: 拒绝非源码或仓库外文件

- **WHEN** 请求读取一个扩展名不在源码白名单内的文件,或规范化后位于 repo 根目录之外的文件
- **THEN** 返回结构化错误,不读取该文件

#### Scenario: 拒绝超大文件

- **WHEN** 请求读取的文件超过大小上限
- **THEN** 返回带大小提示的结构化错误,不返回文件内容

#### Scenario: 文件不存在

- **WHEN** 请求读取 repo 根目录内、但磁盘上不存在的文件
- **THEN** 返回"文件不存在"的结构化错误

### Requirement: 管线携带 repo 上下文并从 diff 派生改动文件集合

管线上下文 SHALL 携带本次审查的 repo 路径,并 SHALL 从 diff 文本派生出改动文件集合(`allowed_files`),用于创建工具会话与授权沙箱。派生逻辑 SHALL 为确定性纯函数,可独立单测。当 diff 为空或无可解析的文件头时,SHALL 产出空集合且不报错。

#### Scenario: 从 diff 解析改动文件集合

- **WHEN** 给定一段包含多文件改动的 unified diff
- **THEN** 解析出其中所有改动文件的相对路径集合

#### Scenario: 空 diff 派生空集合

- **WHEN** diff 文本为空或不含可解析的文件头
- **THEN** 派生出空的改动文件集合,不抛错

### Requirement: 配置驱动启用工具调用

是否启用工具调用 SHALL 由配置决定:配置了工具服务地址(`CODEGUARD_TOOL_SERVER_URL`)时审查员走 ReAct,未配置时走直连基准。配置 SHALL 经 `Settings.from_env()` 读取,不在代码中硬编码地址。新增配置项 SHALL 同步反映在 `.env.example` 中。

#### Scenario: 配置了工具服务地址

- **WHEN** 设置了 `CODEGUARD_TOOL_SERVER_URL`
- **THEN** 管线为本次审查创建工具会话并让审查员走 ReAct 路径

#### Scenario: 未配置工具服务地址

- **WHEN** 未设置 `CODEGUARD_TOOL_SERVER_URL`
- **THEN** 审查员走直连基准路径,不尝试连接工具服务

### Requirement: 工具开关两档对照评测

评测框架 SHALL 支持在"工具关"与"工具开"两档下,对同一数据集运行同一管线并产出可对比的质量指标(Precision / Recall / 误报率等),用于量化工具调用对审查质量的影响。两档之间 SHALL 仅工具开关一个变量不同,其余阶段(聚合、误报过滤)与判分逻辑保持一致。

#### Scenario: 两档对照产出可比指标

- **WHEN** 在工具关与工具开两档下对同一数据集运行评测
- **THEN** 分别产出同口径的质量指标,且两次运行仅"是否启用工具"一个变量不同

### Requirement: ReAct 引擎捕获并暴露工具获取的上下文

ReAct 审查引擎在执行工具调用后,SHALL 捕获工具返回的上下文内容(如 `get_file_content` 读到的文件、`find_sensitive_apis` 的危险 API 扫描结果、`find_callers` 的调用方列表、`get_code_metrics` 的度量报告),并随审查产物一并向管线暴露,供下游阶段(误报复核)做实证判定。无工具的直连引擎 SHALL 产出空的上下文集合。该上下文 MUST NOT 写入 `Issue` 数据结构,只在管线上下文中流转(守核心数据结构不变量)。捕获 MUST 对缺失/异常健壮:取不到时按空上下文处理,绝不抛断或拖垮审查。

#### Scenario: 工具档捕获读到的上下文

- **WHEN** ReAct 审查员调用工具(如 `get_file_content` / `find_sensitive_apis` / `find_callers` / `get_code_metrics`)读到了 diff 之外的内容
- **THEN** 这些内容被捕获为该次审查的"获取上下文",随结构化审查结果一并暴露给管线

#### Scenario: 直连档上下文为空

- **WHEN** 审查走无工具的直连引擎(`DirectEngine`)
- **THEN** 其暴露的"获取上下文"为空集合,下游行为与本能力引入前一致

#### Scenario: 多审查员上下文汇总去重

- **WHEN** 多个领域审查员各自读取了文件(可能读到同一文件)
- **THEN** 管线将各审查员获取的上下文汇总,并按(工具,参数)去重,同一读取只保留一份

### Requirement: ReviewCouncil 第一版沿用现有工具
ADR-032 第一版 SHALL NOT 要求新增工具。ReviewCouncil 内部 Agent MAY 沿用现有 `get_file_content`、`find_sensitive_apis`、`find_callers`、`get_code_metrics` 工具能力。工具会话生命周期、通用工具分发协议、文件访问沙箱和 Java 不判断问题的边界 SHALL 继续沿用现有能力。

#### Scenario: 现有工具可被 ReviewCouncil 复用
- **WHEN** ADR-032 默认路径配置了工具服务
- **THEN** ReviewCouncil 内部 Agent 可以使用现有工具能力获取事实或代码片段

#### Scenario: 不配置工具服务时仍有直连基准
- **WHEN** 未配置 `CODEGUARD_TOOL_SERVER_URL`
- **THEN** ReviewCouncil 内部 Agent 可走无工具直连路径，作为基准对照

### Requirement: 发现者工具边界已细化，补证工具仍后续设计
ADR-032 默认发现者 SHALL 使用 `review-council-agent-roles` 与 `asymmetric-tool-assignment` 能力定义的第一版 role-tool matrix。该矩阵 SHALL 只约束 `ThreatModelAgent`、`BehaviorAgent`、`MaintainabilityAgent` 的发现阶段工具暴露；EvidenceAgent 补证工具、ContextBundle 查询工具、工具预算、失败策略和禁止行为 SHALL 在后续 change 中继续细化。

#### Scenario: 发现者工具细化不新增工具 schema
- **WHEN** ReviewCouncil 默认发现者使用第一版 role-tool matrix
- **THEN** 不需要新增 Java Gateway 工具或修改通用工具协议

### Requirement: 方法论发现者保持 ReAct/Direct 双路径

`ThreatModelAgent`、`BehaviorAgent`、`MaintainabilityAgent` SHALL 继续支持 DirectEngine 与 ToolAgentEngine 两种执行路径。未配置工具服务时，发现者 SHALL 走无工具直连基准；配置工具服务时，发现者 SHALL 以 ReAct Agent 运行，但 MUST 受各自 `tool_allowlist` 约束。

#### Scenario: 未配置工具服务时方法论发现者走直连

- **WHEN** ADR-032 默认路径运行且未配置 `CODEGUARD_TOOL_SERVER_URL`
- **THEN** 三个方法论发现者不构造 ToolAgentEngine
- **THEN** 它们走 DirectEngine 并返回结构化候选结果

#### Scenario: 配置工具服务时方法论发现者走 ReAct

- **WHEN** ADR-032 默认路径运行且已配置可用工具服务
- **THEN** 三个方法论发现者以 ReAct Agent 运行
- **THEN** 每个发现者只能调用其 `tool_allowlist` 声明的工具

### Requirement: ReAct 输出转换为 CandidateIssue

方法论发现者的 ReAct 最终输出 SHALL 被解析为现有结构化审查结果，并 SHALL 转换为 ReviewCouncil 内部 `CandidateIssue`。当 ReAct 输出无法解析、单个发现者失败或工具调用异常时，系统 SHALL 将该发现者按空候选处理并记录 trace，MUST NOT 中断其他发现者或整条管线。

#### Scenario: ReAct 输出可转换为候选

- **WHEN** 一个方法论发现者完成 ReAct 并返回结构化 issue 列表
- **THEN** 系统将每个 issue 转换为 `CandidateIssue`
- **THEN** 候选带有正确的 `source_agent` 和兼容 `category`

#### Scenario: 单个方法论发现者失败被隔离

- **WHEN** 某个方法论发现者在 ReAct 过程中失败
- **THEN** 该发现者贡献空候选并记录 trace
- **THEN** 其他发现者、EvidenceAgent、ChallengeAgent 和 SelfChecker 继续运行

### Requirement: 工具服务协议不因角色细分改变

本 change SHALL NOT 改变 Java Gateway 工具服务协议、工具会话生命周期或通用工具分发路由。方法论发现者 SHALL 复用现有工具服务、会话和沙箱能力。

#### Scenario: 角色细分不新增 Gateway 协议

- **WHEN** 方法论发现者调用 `get_file_content`、`find_sensitive_apis`、`find_callers` 或 `get_code_metrics`
- **THEN** 调用仍通过现有工具客户端、会话 ID 和 `POST /api/v1/tools/{name}` 协议完成

### Requirement: 图谱角色工具

通用分发协议 SHALL 新增 `resolve_change_context`、`inspect_security_path`、`inspect_change_impact` 与 `inspect_structure`。三路发现者 SHALL 使用各自的角色任务工具，并 SHALL 仅消费 ContextProvider 提供的稳定 `symbol_id`。
