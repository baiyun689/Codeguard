# AGENTS.md

本文件给 Codex / AI 助手以及任何接手者快速建立项目心智模型,并说明改动代码时的约束与注意点。

> 阅读顺序建议:本文件 → `README.md`(快速开始)→ `docs/ROADMAP.md`(分阶段路线)→ `DECISIONS.md`(为什么这么选)。

---

## 1. 这是什么

Codeguard 是一个 **AI 代码审查引擎**,以 Agent 为最终核心,双语言架构(Python Agent + Java Gateway)。它的输入是代码变更(git diff),输出是结构化的审查问题(`Issue` 列表),覆盖安全、逻辑、质量等维度。

这是一次 **vibe coding 实践**——刻意按"先跑通 → 看效果 → 小步迭代 → 记录决策"的节奏演进,每个阶段都设计成"可独立跑、可独立讲"的里程碑。

**当前进度:ADR-032 + ADR-038 Phase 1–5 已落地**。证据驱动的多 Agent ReviewCouncil、风险任务链、task-scoped reviewer、风险感知上下文/知识注入，以及策略驱动的证据规划与裁决链均已实现。双语言边界保持不变:Python 智能层 + Java 护栏层。默认审查路径为:

```
git diff → [Summary] → DiffTaskBuilder → RiskTriage → TaskRank → ContextProvider
         → task-scoped Discover × 3 → CouncilCoordinator
         → EvidencePlanner → EvidenceAgent → CouncilJudge → ReviewResult

CouncilJudge -- needs_more_evidence 且未超轮次 --> EvidencePlanner
```

ReviewCouncil 发现者由 `ThreatModelAgent` / `BehaviorAgent` / `MaintainabilityAgent` 方法论分工;最终 category 仍兼容 `security` / `logic` / `quality`。三类发现者各自声明工具 allowlist，并通过 `CandidateIssue` / `EvidenceRequest` / `EvidenceNote(findings)` / `Verdict` / `CouncilTrace` 结构化黑板通信。EvidencePlanner 是 `evidence_requests` 唯一写入者；EvidenceAgent 只执行静态策略；CouncilJudge 做目的感知裁决与全局聚合。旧 Supervisor 图迁移到 `services/agent/legacy/supervisor_graph/`,仅作历史参考,不作为默认路径、feature flag 或 eval profile 回退。

阶段 2(管线化)已完成并存档:**并行三领域审查(security/logic/quality)→ 聚合去重 → 误报过滤**。

风险路由子阶段 Phase 5 已完成：23 个具体 `RiskTag` + `GENERAL_REVIEW`，风险规则只消费
path/diff-text 变化方向，默认任务预算为 100/10；reviewer 范围由 `RiskProfile.tag_scores`
派生，不增加 `assigned_reviewers` 或其他 State 字段。证据策略完整覆盖 24 个标签的
counter/support/severity，候选证据主题从候选语义解析，task RiskTag 只作先验；Phase 5
仍未新增顶层 State、Java 工具或产品 Issue 字段。

---

## 2. 架构

### 目标形态(路线图终点)

```
┌─────────────────┐     HTTP / 工具调用      ┌──────────────────┐
│  Python Agent   │ ──────────────────────> │  Java Gateway    │
│  (审查管线/编排)  │ <────────────────────── │  (AST/调用图/RAG) │
└─────────────────┘     代码上下文工具         └──────────────────┘
```

### 当前实现(风险路由 Phase 5)

Python 智能层 + Java 护栏层。审查统一走多阶段管线,审查员执行方式按是否配置工具服务分流:

```
默认(无工具):git diff → 任务/风险/上下文 → 三路直连发现者 → Planner → Agent(insufficient 安全回退) → Judge → 打印
默认(有工具):配置 CODEGUARD_TOOL_SERVER_URL 后,发现者可走 ReAct；EvidenceAgent 只按已规划策略
              复用/调用 Java 工具(get_file_content / find_sensitive_apis / find_callers / get_code_metrics)获取事实
```

ADR-032 默认阶段:

- **SummaryStage(可选)**:产出变更摘要,作为 ContextBundle 和 ReviewCouncil 的共享背景。由 `CODEGUARD_ENABLE_SUMMARY` 控制(默认开)。
- **ContextProvider**:在 ReviewCouncil 前构造轻量 `ContextBundle`,只产出事实、来源与截断标记,不判断"是不是问题"。
- **ReviewCouncilSubgraph**:三个 task-scoped 发现者 fan-out 产出 `CandidateIssue`;`CouncilCoordinator` 只作显式 fan-in。
- **EvidencePlanner**:解析 candidate evidence tag，按全量静态注册表规划 counter/support/severity 请求；候选主题解析以最多 8 个线程并发，结果按候选稳定顺序进入两遍规划；Judge 回环也必须回到 Planner。
- **EvidenceAgent**:校验请求的 strategy/purpose/target/question/tools/profile allowlist，优先复用 task/context facts，只为缺失事实调用 Gateway；跨请求先按工具名与规范化参数去重，再以最多 8 个线程并发执行唯一工具调用和事实关系分析，最终按请求/事实原顺序组装；失败/空/截断/None 均为 insufficient。
- **CouncilJudge**:外层唯一最终裁决节点；按 request purpose 与 finding strength keep/drop/downgrade/needs_more，再做全局精确/语义聚合，最终仍输出现有 `ReviewResult` / `Issue`。

审查员的"执行方式"抽成可插拔引擎(`pipeline/engines.py`):`DirectEngine`(无工具基准)/ `ToolAgentEngine`(ReAct,基于 langchain v1 `create_agent`)。`ReviewerStage` 按 `tool_client` 是否存在分流(见 ADR-009 / openspec design.md D1、D5)。

**职责边界(阶段 3 钉死,统领后续)**:Python = 智能编排(推理 / 编排 / 对结论加工);Java = 护栏 + 地面真值(安全沙箱 / 重静态计算)。四条不变量:Python 调 Java 单向、Java 不碰 LLM;代码探索只走 Java 沙箱;不确定性只在 Python;Java 不判断"是不是问题"。详见 ADR-009 / design.md D0。

旧 SelfChecker / Challenge 默认运行路径已由 purpose-aware CouncilJudge 取代；legacy stage 只作历史兼容，不参与默认图。

`services/gateway`(Java)本期正式引入:工具服务 + 护栏。**只放"事实与护栏"(工具执行 / 沙箱 / 重计算),绝不在 gateway 里调 LLM 或做"是不是问题"的判断**(那是 Python 的事,见职责边界)。

---

## 3. 目录结构

```
Codeguard/
├── AGENTS.md                      # 本文件
├── README.md                      # 快速开始
├── DECISIONS.md                   # 架构决策记录(ADR)—— 改设计前必读
├── .env.example                   # 环境变量示例(复制为 .env 使用)
├── docs/ROADMAP.md                # 分阶段搭建路线图
└── services/
    ├── agent/                     # Python Agent(智能编排层)
    │   ├── pyproject.toml         # 依赖与打包(打包仅含 src/codeguard_agent)
    │   ├── src/codeguard_agent/
    │   │   ├── __main__.py        # python -m codeguard_agent 入口
    │   │   ├── cli.py             # 命令行:review 子命令、结果打印、退出码、工具会话建/销
    │   │   ├── config.py          # Settings:从环境变量/.env 读配置(含 CODEGUARD_TOOL_SERVER_URL)
    │   │   ├── models/schemas.py  # ★产品输出结构:Severity / Issue / ReviewResult
    │   │   ├── models/council.py  # ★内部结构:CandidateIssue / EvidenceRequest/Finding/Note / Verdict / Trace/Stats
    │   │   ├── git/diff_collector.py  # 调系统 git 采集 diff + parse_changed_files(派生 allowed_files)
    │   │   ├── llm/client.py      # LLM 工厂(openai/Codex/mock)+ 重试 + mock 假数据
    │   │   ├── tools/             # ★阶段3:工具调用(智能层侧)。tool_client(同步 HTTP)+ definitions(LangChain 工具)
    │   │   ├── pipeline/orchestrator.py   # 多阶段管线编排(审查唯一入口)
    │   │   ├── pipeline/risk_rules/       # ★Phase 2:变化特征、23标签规则、注册表/聚合/兜底
    │   │   ├── pipeline/risk_routing.py   # ★从 RiskProfile 派生 reviewer task scope
    │   │   ├── pipeline/evidence_rules/   # ★Phase 5:24 标签三目的静态策略与候选主题分类
    │   │   ├── pipeline/evidence_planner.py # ★dossier 绑定、两遍初轮与回环规划
    │   │   ├── pipeline/evidence_agent.py # ★策略执行、事实复用/工具缓存、finding 安全回退
    │   │   ├── pipeline/council_judge.py  # ★目的感知裁决、去重/语义合并、survivor 映射
    │   │   ├── pipeline/council_metrics.py # ★Phase 5 过程指标唯一计算入口
    │   │   ├── pipeline/engines.py        # ★审查员执行引擎:DirectEngine(直连基准)/ ToolAgentEngine(ReAct)
    │   │   ├── pipeline/stages/           # 各阶段:summary / context_provider / reviewer 等通用 stage
    │   │   ├── pipeline/fp_rules.py       # 误报过滤的确定性规则(纯函数,可单测)
    │   │   └── prompts/                   # threat-model/behavior/maintainability + summary/aggregation/fp prompts
    │   ├── legacy/supervisor_graph/       # 旧 Supervisor 图备份,不作为运行回退
    │   ├── config/false-positive-rules.yaml  # 误报过滤的确定性规则配置(YAML)
    │   ├── tests/                 # pytest:测工程正确性
    │   └── evals/                 # ★审查质量评测框架(量化效果,见 §5)
    └── gateway/                   # ★Java Gateway(护栏 + 地面真值层,阶段3引入)
        ├── pom.xml               # Maven;Javalin + Jackson + SLF4J;fat jar 独立启动
        └── src/main/java/com/codeguard/
            ├── agent/core/       # AgentTool 接口 / ToolResult 信封 / AgentContext
            ├── agent/tools/      # ToolRegistry / FileAccessSandbox(护栏)/ GetFileContentTool / GetRepoMapTool
            ├── agent/repomap/    # ★get_repo_map:TagExtractor 接口(JavaTagExtractor=JavaParser,TagExtractorRegistry 按扩展名路由)+ PageRank + RepoMapRanker + RepoMapRenderer + RepoMapBuilder
            └── toolserver/       # ToolServerApp + Controller(通用 /tools/{name} 分发)+ SessionManager + Main
```

带 ★ 的是改动时最需要小心的核心文件。

---

## 4. 数据流与各模块职责

一次 `python -m codeguard_agent review` 的完整链路:

1. **`cli.py:main`** 解析参数(`--repo` / `--base`),构造 `Settings.from_env()`。
2. **`config.py:Settings.from_env`** 就近加载 `.env`(已显式设置的环境变量优先),读出 provider / model / api_key / structured_method 等。
3. **`git/diff_collector.py:collect_diff`** 调系统 `git diff <base>` 拿 unified diff 文本;空 diff 直接结束。
4. **`llm/client.py:build_llm`** 按 provider 造 LangChain Chat 模型;`provider=mock` 返回 `None`。
5. **工具会话(可选)**:配置 `CODEGUARD_TOOL_SERVER_URL` 且非 mock 时,CLI 为本次 diff 创建 Java 工具会话;否则走无工具直连基准。
6. **`pipeline/orchestrator.py:PipelineOrchestrator.run`** 是审查唯一门面,内部构建 `pipeline/graph.py` 的 ADR-032 LangGraph:
   - `[Summary]` 产出可选变更摘要。
   - `DiffTaskBuilder → RiskTriage → TaskRank` 把 diff 拆成 hunk task、生成风险画像并按预算选择任务。
   - `ContextProvider` 构造只读 `ContextBundle`。
   - `ReviewCouncil` 并行运行 task-scoped 发现者 Agent；没有匹配任务的 reviewer 记录 `no_tasks_routed`。
   - `CouncilCoordinator` 只作三路 fan-in，固定进入 EvidencePlanner。
   - `EvidencePlanner → EvidenceAgent → CouncilJudge` 完成策略规划、受约束取证和裁决；Judge 的 needs_more 回 Planner，最后一轮强制收口。
   - `CouncilRunStats` 从稳定 survivor candidate 映射与结构化 request/finding/verdict/trace 派生，进入 eval/report/archive，不进入产品输出。
7. **`cli.py:_print_result`** 打印;**退出码**:发现任一 `CRITICAL` 返回 1,否则 0(方便接 CI 门禁)。

核心数据单元是 `models/schemas.py` 里的 **`Issue`**:`severity / file / line / type / message / suggestion / confidence`。前五个必需(定位 + 是什么),后两个可选。整个项目所有阶段都围绕它流转——**改它的字段要极其谨慎**(见 ADR-001)。

---

## 5. 怎么跑

> **开发环境**:Python 侧用 conda 环境 `codeguard`。命令前缀统一为
> `conda run -n codeguard --no-capture-output ...`(下方为简洁省略,真实跑请带上)。
> Windows 用 PowerShell;bash 的 `VAR=value cmd` 内联写法不生效(见 §5 末尾)。

### 命令速查

```powershell
# —— Python Agent(services/agent)——
conda run -n codeguard python -m pytest tests/ -q          # 全部单测(工程正确性)
conda run -n codeguard python -m pytest tests/test_xxx.py::test_name   # 跑单个测试
conda run -n codeguard ruff check src/                     # lint
conda run -n codeguard mypy src/                           # 类型检查
conda run -n codeguard python -m evals.runner --mode pipeline --judge --runs 3   # 评测
conda run -n codeguard python -m evals.runner --profile pipeline-file --runs 1   # 按 profile 评测(见 evals/profiles.yaml)

# —— Java Gateway(services/gateway,阶段3工具服务)——
mvn package                # 跑单测 + 出 fat jar
mvn test                   # 只跑单测
java -jar target/codeguard-gateway.jar    # 启动工具服务(默认 9090,CODEGUARD_TOOL_SERVER_PORT 可覆盖)

# —— 真实 ReAct 审查(工具开档:先起 Java 工具服务,再设 URL)——
$env:CODEGUARD_TOOL_SERVER_URL="http://localhost:9090"
conda run -n codeguard python -m codeguard_agent review --repo <repo> --mode pipeline
```

### 命令行审查

```bash
cd services/agent
pip install -e .

# mock 模式:零配置、零成本验证链路
#   PowerShell:  $env:CODEGUARD_PROVIDER="mock"; python -m codeguard_agent review
#   bash:        CODEGUARD_PROVIDER=mock python -m codeguard_agent review

# 真实 LLM:配好 .env(CODEGUARD_PROVIDER / CODEGUARD_API_KEY 等)后
python -m codeguard_agent review --repo . --base HEAD
```

### 单元测试(工程正确性)

```bash
cd services/agent && conda run -n codeguard python -m pytest tests/ -q
```

> 跑单个用例见上方「命令速查」;Java 侧单测随 `mvn package` / `mvn test` 执行。

### 评测框架(审查质量,量化"效果")★

`evals/` 用"带标注的数据集 + 统计指标"量化审查质量,在统一数据集上对照各 profile(无工具 / 文件工具 / repo-map)的增益。详见 `evals/README.md`。

```bash
cd services/agent && pip install -e . pyyaml
python -m evals.runner --runs 3          # 跑评测,3 次统计方差
python -m evals.runner --runs 3 --judge  # 额外开 LLM-as-judge
```

产出 `evals/reports/pipeline.md`,核心指标:Precision / Recall / F1 / 误报率 / 定位准确率 / 级别准确率;**复杂用例行为诊断**(ADR-013):诱饵命中率 / vuln 噪音/条 / 报告膨胀比 / 主项 recall(CRITICAL)/ 次项 recall(WARNING+INFO)/ 裁判↔规则一致率。
加用例只需往 `evals/dataset/vuln`(有漏洞)或 `evals/dataset/clean`(无问题、测误报)丢一个 YAML,无需改代码;**复杂用例**(一份 diff 多个植入问题 + `distractors` 诱饵)指标只有开 `--judge` 才完全可信(规则尺在多问题下偏乐观)。

### 环境变量(完整列表见 `.env.example`)

| 变量 | 默认 | 说明 |
|---|---|---|
| `CODEGUARD_PROVIDER` | `openai` | `openai` / `Codex` / `mock` |
| `CODEGUARD_MODEL` | 按 provider 回退 | 留空自动选默认模型 |
| `CODEGUARD_API_KEY` | 空 | openai/Codex 必填 |
| `CODEGUARD_API_BASE_URL` | 空 | 代理 / 兼容端点(如 DeepSeek)填 |
| `CODEGUARD_STRUCTURED_METHOD` | `function_calling` | 结构化输出方式 |
| `CODEGUARD_DISABLE_THINKING` | `false` | 用 DeepSeek 推理模型时设 `true` |
| `CODEGUARD_MAX_RETRIES` | `3` | LLM 调用重试次数 |
| `CODEGUARD_ENABLE_SUMMARY` | `true` | ADR-032 前置摘要开关;关闭则 ContextProvider / ReviewCouncil 直接基于 diff 运行 |
| `CODEGUARD_REVIEW_ORCHESTRATION` | `adr-032` | 当前唯一运行编排 profile;旧 supervisor 仅在 legacy 目录作参考 |
| `CODEGUARD_MAX_EVIDENCE_ROUNDS` | `2` | Planner → Agent → Judge 证据执行轮次上限；只允许 1 或 2，Judge 补证回 Planner |
| `CODEGUARD_MAX_REVIEW_TASKS` | `100` | Phase 2 总任务预算 |
| `CODEGUARD_MAX_TASKS_PER_FILE` | `10` | Phase 2 单文件任务预算 |
| `CODEGUARD_TRACE_ENABLED` | `false` | 历史本地 HTML Trace；仅在传 `--trace` 或显式设为 true 时运行 |
| `LANGSMITH_TRACING` | `false` | LangSmith 标准开关；设为 true 后由 LangGraph/LangChain 自动追踪 |
| `LANGSMITH_PROJECT` | `codeguard-phase5-test` | LangSmith 测试追踪项目名；需同时设置 `LANGSMITH_API_KEY` |

> **Windows/PowerShell 注意**:bash 的 `VAR=value cmd` 内联写法在 PowerShell 不生效,要先 `$env:VAR="value"` 再跑命令;或直接写 `.env`(推荐)。

---

## 6. 改代码的注意点(重要)

### 6.1 尊重阶段边界——别提前做未来阶段的事

路线图是这个项目的灵魂(`docs/ROADMAP.md`)。三条铁律:

1. **先做减法**:MVP 砍到最小,别顺手加全功能。
2. **先跑通再加深**:每阶段都要能独立跑、看到真实输出再往上叠。
3. **先单语言再双语言**:阶段 1–2 纯 Python;**阶段 3 才碰 Java `gateway`**。

具体禁忌(当前阶段 = 阶段 3,已落地 get_file_content + get_repo_map):
- **一次只加一个工具**:已落地 `get_file_content` + `get_repo_map`。不要顺手把 `get_definition` / AST / 调用图 / RAG / 记忆塞进来——那是后续 change,沿通用协议 + 会话接缝逐个叠加(get_definition 暂缓的边界理由见 ADR-012)。
- 守职责边界(§2 / ADR-009):Java 侧绝不调 LLM、不判断"是不是问题";Python 侧除采集 diff 外不直接读被审仓库文件,一律走 Java 工具沙箱。
- 不要为了"用上工具"在合成评测集上硬跑对照——它喂不了文件工具(ADR-009),量化要等 repo-backed 用例。
- 不切 async(守 ROADMAP "async 留到 chunking" 的岔路口)。
- 加功能前先问:这属于哪个阶段?该放 Python 还是 Java?现在该做吗?

### 6.2 无工具对照基准

原 `--mode single` 的无 Agent 基线(`pipeline/reviewer.py`)已完成"有工具 vs 无工具"对比使命后移除(ADR-002 废弃说明)。当前的对照基准是**管线内的无工具直连引擎**(`DirectEngine`):用 `pipeline-notools` profile 跑出的指标即"管线但不开工具"的基线,与 `pipeline-file` / `pipeline-repomap` 对照量化各工具的增益。加新能力时仍按"同一数据集、只改一个变量(profile)"的方式做对照。

### 6.3 改核心数据结构要慎重

`models/schemas.py` 的 `Issue` 被所有阶段共享。增字段一般安全(给默认值即可);**改名 / 删字段 / 改类型**会波及 prompt、CLI 打印、evals 匹配逻辑,改前先全局搜引用,并在 `DECISIONS.md` 记一条 ADR。`Severity` 是枚举(约束 LLM 输出范围),新增级别要同步更新 `cli.py` 的 `_SEVERITY_ICON`。

### 6.4 LLM / 结构化输出的坑

- **结果可能是 `None`**:`with_structured_output(...).invoke()` 在模型没正确发起工具调用时返回 `None`。审查引擎(`pipeline/engines.py`)已兜底成空结果——**任何新写的、消费 LLM 结构化输出的代码都要做同样的 None 防御。**
- **DeepSeek 等兼容端点**:不支持 OpenAI 的 `json_schema`,必须用 `function_calling`(已是默认);推理模型要 `CODEGUARD_DISABLE_THINKING=true`。flash 类小模型工具调用稳定性弱,评测时漏报偏多属正常。
- **provider=mock 时 `build_llm` 返回 `None`**,靠下游分支识别走假数据——别假设 llm 一定非空。

### 6.5 配置与密钥

- 配置只走 `Settings.from_env()`,**不要在代码里硬编码模型名/密钥/地址**。新增可调项就加一个 `CODEGUARD_*` 环境变量,并同步更新 `.env.example` 和上面的表格。
- `.env` 已被 gitignore,**真实密钥永远不要提交**,也不要写进 `.env.example`。

### 6.6 提示词独立成文件

prompt 放 `prompts/*.txt`,不要写死进代码。改 prompt 不用动代码,且 prompt 本身就是"这个审查员想干什么"的最佳文档。新增审查维度(如逻辑/质量)时,新增对应 `.txt`。

### 6.7 依赖与打包

- 运行时依赖加到 `pyproject.toml` 的 `[project].dependencies`;开发/评测工具加到 `[dependency-groups].dev`(如 `pyyaml`)。
- 打包只含 `src/codeguard_agent`(见 `[tool.hatch.build.targets.wheel]`);`evals/` 和 `tests/` 不随包发布,通过 `python -m evals.runner` / `pytest` 从 `services/agent` 目录运行。
- LLM 相关 import 在 `client.py` 里是**延迟导入**的,保证 mock 模式 / 没装对应 SDK 时也能跑——保持这个习惯。

### 6.8 两类测试别混

- `tests/`(pytest)测**工程正确性**:数据结构、空 diff、mock 流程连通等确定性逻辑。
- `evals/` 测**审查质量**:不确定的 LLM 输出,用统计指标量化,不要用 `assert` 死磕。
- 改了 `reviewer` / `schemas` / prompt 后:先 `pytest` 确认没破坏管线,再视情况跑 `evals` 看质量有没有回退。

### 6.9 提交信息规范(Conventional Commits)

commit message 一律用 `<type>(<scope>): <简短描述>` 格式。**type 必填、小写**,`scope` 可选。

**type 取值**:

| type | 用于 | 示例 |
|---|---|---|
| `feat` | 新功能 / 新阶段能力 | `feat(pipeline): 并行三领域审查员(security/logic/quality)` |
| `fix` | 修 bug | `fix(llm): 兼容 DeepSeek 的 function_calling` |
| `docs` | 文档 / 注释 / ADR / ROADMAP | `docs: 补 ADR-004 级别 rubric 决策` |
| `style` | 不改逻辑的格式调整(空格、换行、引号) | `style: 统一 prompt 缩进` |
| `refactor` | 重构,不改外部行为 | `refactor(pipeline): 抽出 run_domain_reviewer` |
| `test` | 测试 / 评测数据集与脚本 | `test(evals): 扩充 logic/quality 用例` |
| `chore` | 脚手架 / 依赖 / 杂务 | `chore: 初始化项目骨架` |

**写法约定**:

- `scope` 用模块名:`pipeline` / `evals` / `cli` / `prompts` / `schemas` / `llm` / `config` 等。
- 描述用**简洁中文、动词开头、句末不加句号**,首行尽量 ≤ 50 字。
- 需要解释"为什么这么做 / 做了什么权衡"时,空一行写 body(本项目讲究决策留痕,值得写)。
- **不加 `Co-Authored-By` 等 AI 署名尾注**,保持 history 风格统一。
- 一个 commit 只做一件逻辑上内聚的事;跨多个 type 的改动拆成多个 commit。

---

## 7. 每个阶段结束要做的事

- 在 `DECISIONS.md` 追加这一阶段的关键技术选择(选了什么 / 为什么 / 放弃了什么)。
- 写一段复盘:理解了什么、效果如何、踩了什么坑、下一步改什么。
- 跑一次 `evals` 存档当前指标,方便和下阶段对比。

---

_本文件随项目演进更新。改动架构或新增模块时,记得同步这里的目录结构与注意点。_
