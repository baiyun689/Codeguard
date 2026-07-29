# AGENTS.md

本文件给 Codex / AI 助手以及任何接手者快速建立项目心智模型,并说明改动代码时的约束与注意点。

> 阅读顺序建议:本文件 → `README.md`(公开使用、部署与开发说明)。

---

## 1. 这是什么

Codeguard 是一个 **AI 代码审查引擎**,以 Agent 为最终核心,双语言架构(Python Agent + Java Gateway)。它的输入是代码变更(git diff),输出是结构化的审查问题(`Issue` 列表),覆盖安全、逻辑、质量等维度。

默认审查使用证据驱动的多 Agent ReviewCouncil、风险任务链、task-scoped reviewer、风险感知上下文/知识注入，以及策略驱动的证据规划与裁决链。双语言边界保持不变:Python 智能层 + Java 护栏层。默认审查路径为:

```
git diff → DiffTaskBuilder → RiskTriage → TaskRank → ReviewCoverage → [Summary] → ContextProvider
         → task-scoped Discover × 3 → CouncilCoordinator → ConcernAnalyzer
         → ClaimEvidencePlanner → EvidenceAgent → ImpactAssessor → CouncilJudge → ReviewResult
```

Java Gateway 的单实例 CI 执行底座一次执行返回结构化 outcome，调度器负责
H2 状态、非阻塞重试、恢复、反馈与停机；workspace 按完整 SHA 隔离，并提供 readiness 与 Prometheus。
Compose 的 `observability` profile 提供 Prometheus、预置告警规则和自动配置的 Grafana
看板，覆盖审查吞吐/耗时、AST 工具调用及 LLM provider 重试、fallback 和熔断状态。

ReviewCouncil 发现者由 `ThreatModelAgent` / `BehaviorAgent` / `MaintainabilityAgent` 方法论分工;最终 category 仍兼容 `security` / `logic` / `quality`。三类发现者各自声明工具 allowlist，并通过 `CandidateIssue` / `CandidateConcern` / `EvidenceGoal` / `EvidenceRequest` / `EvidenceNote(findings)` / `Verdict` / `CouncilTrace` 结构化黑板通信。三路发现者只通过 ID reducer 汇集 raw candidates；CouncilCoordinator 在 fan-in 后复用候选 RiskTag 解析、按完整路径和局部位置构块，并以最多 8 个并行结构化 LLM 调用进行保守归并。非法、低置信或失败结果一律保留候选。ConcernAnalyzer 为已分组和未分组成员建立无损 concern/claim 映射；ClaimEvidencePlanner 按 root cause、trigger、impact 等事实类型规划并保留成员对齐字段。旧 Supervisor 图迁移到 `services/agent/legacy/supervisor_graph/`,仅作历史参考,不作为默认路径、feature flag 或 eval profile 回退。

风险路由包含 24 个具体 `RiskTag` + `GENERAL_REVIEW`，风险规则只消费
path/diff-text 变化方向；普通 diff 默认全选 task，旧 100/10 配置只作为大 diff 的更严格上限。
`RiskProfile` 先派生为 `TaskRiskPrior`，再由 `ReviewCoveragePlanner` 组合基础覆盖、风险增强
和 ReAct assignment 预算；Risk 只能增加/升级 Reviewer，不能排除基础覆盖。内部 State 保存
`risk_priors` 和 `review_coverage_plan`，不增加产品输出字段。证据策略完整覆盖 25 个标签的
counter/support/severity，候选证据主题从候选语义解析，task RiskTag 只作先验。知识注入固定包含
reviewer BASE 方法论，并由 risk prior、patch 与 symbol context 共同选择有预算上限的专项主题。

---

## 2. 架构

### 组件边界

```
┌─────────────────┐     HTTP / 工具调用      ┌──────────────────┐
│  Python Agent   │ ──────────────────────> │  Java Gateway    │
│  (审查管线/编排)  │ <────────────────────── │  (AST/调用图/RAG) │
└─────────────────┘     代码上下文工具         └──────────────────┘
```

### 默认审查流

Python 智能层 + Java 护栏层。审查统一走多阶段管线,审查员执行方式按是否配置工具服务分流:

```
默认(无工具):git diff → 任务/风险/覆盖计划/上下文 → 三路直连发现者 → Planner → Agent(insufficient 安全回退) → Judge → 打印
默认(有工具):配置 CODEGUARD_TOOL_SERVER_URL 后,Tool Server 按 revision 构建完整 Java ProjectSnapshot；
              ContextProvider 注入 symbol_context，三路发现者分别使用 inspect_security_path /
              inspect_change_impact / inspect_structure，EvidenceAgent 按语义证据能力复用同一图谱事实
```

默认节点:

- **SummaryStage(可选)**:在 TaskRank 后对选中任务范围产出变更摘要,作为 ContextBundle 和 ReviewCouncil 的共享背景。由 `CODEGUARD_ENABLE_SUMMARY` 控制(默认开)。
- **ContextProvider**:在 ReviewCouncil 前构造轻量 `ContextBundle`,只产出事实、来源与截断标记,不判断"是不是问题"。
- **大 diff 降级**:仅在超过 5000 行时，Python 确定性收紧为最多 20 个任务、每文件 3 个、每任务上下文 2000 字符；普通 diff 全选 task，并只让风险排序前 `CODEGUARD_MAX_REACT_TASKS`（默认 20）个合格 task 使用 ReAct，其余 Direct。Summary/AST/发现者在大 diff 时只看选中范围，结果摘要披露部分覆盖。Java 不重复判断。
- **ReviewCouncilSubgraph**:三个 task-scoped 发现者 fan-out 产出 raw `CandidateIssue`;system prompt 定义稳定的上下文语义与工具门槛，user prompt 动态携带本 task 的 patch、风险画像、预取事实、缺失/失败状态和 BASE+专项 knowledge bundle。`CouncilCoordinator` 在显式 fan-in 后批量解析 RiskTag、构建局部候选块并保守归并。
- **发现者工具协调**:`pipeline/discovery_tools.py` 在单次 review 的单个 reviewer node 内按规范化工具参数执行 single-flight/cache；不同 task 首次复用完整结果，同一 ReAct 对话重复调用只返回短标记，最终 gathered context 也按相同 canonical key 去重，三个发现者之间及跨 review 不共享。只有未被大 diff 策略截断的完整新增文件 patch 才可代替 `get_file_content`。
- **ConcernAnalyzer / ClaimEvidencePlanner**:把所有候选（包括未分组成员）映射为 concern，结构化保留 root cause、trigger、observable consequence、fix location/action 和候选来源。随后按 claim/fact type 规划 support、counter、impact 请求；goal/concern/claim/fact 对齐字段贯穿请求与 finding，动态策略仍转换为 Gateway 真实工具名。
- **EvidenceAgent**:校验请求的 strategy/purpose/target/question/tools/profile allowlist，优先复用 task/context facts（完整新增文件的 task patch 直接满足当前文件内容请求），只为缺失事实调用 Gateway；跨请求先按工具名与规范化参数去重并并发执行唯一工具调用，再以一个 `EvidenceRequest` 一次结构化 LLM 调用批量判断其全部局部事实，按 `evidence_id` 严格对齐并恢复原顺序；未知/重复/遗漏 ID、失败/空/截断/None 均安全降级为 insufficient。Trace 独立记录发现者和 Evidence 的每次工具输入、输出、耗时、复用与失败，不依赖上下文去重结果。
- **ImpactAssessor / CouncilJudge**:Judge 先对已正确绑定的 support/counter finding 执行确定性证据门槛；ImpactAssessor 只从当前 concern 的 impact findings 归纳可引用因子，SeverityResolver 依据多标签 rubric 和固定 CRITICAL predicate 确定级别。RiskTag 只选择相关因子/predicate，不提供默认级别；缺失或失败安全降级且绝不产生 CRITICAL。Judge 不补证、不按标签直定级，也不接受 LLM 直接选择危险等级。

审查员的"执行方式"抽成可插拔引擎(`pipeline/engines.py`):`DirectEngine`(无工具基准)/ `ToolAgentEngine`(ReAct,基于 langchain v1 `create_agent`)。`ReviewerStage` 按 `tool_client` 是否存在分流。

**职责边界**:Python = 智能编排(推理 / 编排 / 对结论加工);Java = 护栏 + 地面真值(安全沙箱 / 重静态计算)。四条不变量:Python 调 Java 单向、Java 不碰 LLM;代码探索只走 Java 沙箱;不确定性只在 Python;Java 不判断"是不是问题"。

旧 SelfChecker / Challenge 默认运行路径已由 purpose-aware CouncilJudge 取代；其
stage、prompt 和测试已移出 Python 包并归档到 `services/agent/legacy/`。

`services/gateway`(Java)提供工具服务 + 护栏。**只放"事实与护栏"(工具执行 / 沙箱 / 重计算),绝不在 gateway 里调 LLM 或做"是不是问题"的判断**(那是 Python 的事,见职责边界)。

---

## 3. 目录结构

```
Codeguard/
├── AGENTS.md                      # 本文件
├── README.md                      # 快速开始
├── .env.example                   # 环境变量示例(复制为 .env 使用)
├── docker-compose.yml             # 单实例 Compose 部署
├── Dockerfile                     # Python Agent + Java Gateway 镜像
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
    │   │   ├── tools/             # ★工具调用(智能层侧)。tool_client(同步 HTTP)+ definitions(LangChain 工具)
    │   │   ├── pipeline/orchestrator.py   # 多阶段管线编排(审查唯一入口)
    │   │   ├── pipeline/risk/             # ★任务拆分、风险规则、路由与排序
    │   │   ├── pipeline/context/          # ★图谱符号上下文与事实预算
    │   │   ├── pipeline/reviewers/        # ★三路发现者、工具协调与 prompt 构造
    │   │   ├── pipeline/evidence/         # ★证据策略、规划、执行与能力映射
    │   │   ├── pipeline/council/          # ★候选归并、裁决与过程指标
    │   │   ├── pipeline/summary/          # 可选变更摘要阶段
    │   │   ├── pipeline/engines.py        # ★审查员执行引擎:DirectEngine(直连基准)/ ToolAgentEngine(ReAct)
    │   │   └── prompts/                   # 三路发现、证据、裁决、摘要与 RiskTag 知识
    │   ├── legacy/                # 不打包、不参与默认 pytest 的历史实现
    │   │   ├── supervisor_graph/  # 旧 Supervisor 图
    │   │   ├── runtime_archive/   # 旧 stages/prompts/fp rules
    │   │   └── tests/             # 对应历史测试
    │   ├── tests/                 # pytest:测工程正确性
    │   └── evals/                 # ★质量评测:60例真实仓库、四档消融与人工盲审(见 §5)
    └── gateway/                   # ★Java Gateway(护栏 + 地面真值层)
        ├── pom.xml                # Maven 四模块 parent
        ├── shared/                # 指标、健康检查和共享配置
        ├── tool-server/           # ★沙箱、ProjectSnapshot/ProjectCodeGraph、语义工具
        ├── ci-webhook/            # CI 执行、job 调度、GitHub webhook 与 fat jar
        ├── llm-proxy/             # OpenAI 兼容代理、路由、熔断与 fallback
        └── legacy/                # .java.legacy 历史归档,不参与构建或项目图
            ├── pre-modular-gateway/       # Gateway 拆模块前的根 src
            ├── pre-codegraph-tool-server/ # 项目图前的逐次 AST/扫描工具
            └── repomap/                    # 已下线 repo-map 实现
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
   - `DiffTaskBuilder → RiskTriage → TaskRank → ReviewCoverage` 把 diff 拆成 hunk task、生成风险画像、按预算选择任务并生成 Reviewer 覆盖计划。
   - `[Summary]` 对 TaskRank 选中范围产出可选变更摘要。
   - `ContextProvider` 构造只读 `ContextBundle`。
   - `ReviewCouncil` 并行运行 task-scoped 发现者 Agent；没有匹配任务的 reviewer 记录 `no_tasks_routed`。
   - `CouncilCoordinator` 完成三路 fan-in、候选 RiskTag 批量解析和保守归并；`ConcernAnalyzer` 随后为全部成员建立无损 claim/concern 映射。
   - `ClaimEvidencePlanner → EvidenceAgent → ImpactAssessor → CouncilJudge` 完成 claim-driven 规划、受约束取证、证据门槛、影响因子归纳和确定性定级。
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
conda run -n codeguard python -m evals.runner --profile eval-codeguard-full --judge --runs 3  # 单 profile
conda run -n codeguard python -m evals.interview_eval run --workspace <dir> --cache <dir> --runs 3 --judge  # 面试版四档

# —— Java Gateway(services/gateway 工具服务)——
mvn package                # 跑单测 + 出 fat jar
mvn test                   # 只跑单测
java -jar ci-webhook/target/codeguard-gateway.jar  # 同 JVM 启动 CI(8080)/工具(9090)/LLM Proxy(9091)

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

`evals/` 用"精确版本的真实仓库数据 + 自动暂定评分 + 双人人工盲审"量化审查质量。冻结的 `interview-v1` 含 60 例(Vul4J 25、GitBug-Java 35)，四档只改变编排/图谱/举证能力。详见 `evals/INTERVIEW_EVAL.md`。

```bash
cd services/agent && pip install -e . pyyaml
python -m evals.interview_eval run --workspace ../../.eval-work/interview-v1 --cache ../../.eval-cache/interview-v1 --runs 3 --judge
```

产出四档原始归档、`provisional-report.md` 和来源盲化任务池。当前 DeepSeek 同源 Judge 只形成 `automatic-provisional`；两位 reviewer 一致裁决（分歧由第三人仲裁）后，离线生成 `human-adjudicated-final`。额外合理发现会成为所有 profile 的共享补充标答。核心指标包括 Precision/Recall/F1、case-cluster bootstrap 95% CI、稳定/最差轮 Recall、检出集合 Jaccard、clean 误报和 P95 时延。

### 环境变量(完整列表见 `.env.example`)

| 变量 | 默认 | 说明 |
|---|---|---|
| `CODEGUARD_PROVIDER` | `openai` | `openai` / `claude` / `mock` |
| `CODEGUARD_MODEL` | 按 provider 回退 | 留空自动选默认模型 |
| `CODEGUARD_API_KEY` | 空(Compose 必填) | openai/claude 必填 |
| `CODEGUARD_IMAGE_TAG` | `latest` | Compose 部署使用的 `ghcr.io/baiyun689/codeguard` 镜像标签 |
| `CODEGUARD_HOST_PORT` | `9090` | Compose 发布到宿主机的 Webhook 端口；映射到容器内 CI 服务 8080 |
| `CODEGUARD_WEBHOOK_SECRET` | 空(Compose 必填) | GitHub App webhook HMAC 验签密钥 |
| `CODEGUARD_GITHUB_APP_ID` | 空(Compose 必填) | 用于 installation 认证和结果回写的 GitHub App ID |
| `CODEGUARD_GITHUB_PRIVATE_KEY_FILE` | `./secrets/github-app.pem` | GitHub App 私钥的宿主机路径；Compose 以只读 secret 挂载 |
| `CODEGUARD_GITHUB_TOKEN` | 空 | 私有仓库 clone 使用的只读 token；公开仓库无需设置 |
| `CODEGUARD_WEBHOOK_RATE_LIMIT` | `0.5` | 单实例每秒 webhook 许可数；`0` 表示不限制 |
| `CODEGUARD_API_BASE_URL` | 空 | 代理 / 兼容端点(如 DeepSeek)填 |
| `CODEGUARD_STRUCTURED_METHOD` | `function_calling` | 结构化输出方式 |
| `CODEGUARD_DISABLE_THINKING` | `false` | 用 DeepSeek 推理模型时设 `true` |
| `CODEGUARD_MAX_RETRIES` | `3` | LLM 调用重试次数 |
| `CODEGUARD_ENABLE_SUMMARY` | `true` | ADR-032 选中范围摘要开关;关闭则 TaskRank 后直接进入 ContextProvider |
| `CODEGUARD_MAX_REVIEW_TASKS` | `100` | 仅作为大 diff 的更严格总任务上限 |
| `CODEGUARD_MAX_TASKS_PER_FILE` | `10` | 仅作为大 diff 的更严格单文件上限 |
| `CODEGUARD_MAX_REACT_TASKS` | `20` | 普通/大 diff 选中范围内允许使用 ReAct 的 task 上限；其余 Direct |
| `CODEGUARD_TRACE_ENABLED` | `false` | 历史本地 HTML Trace；仅在传 `--trace` 或显式设为 true 时运行 |
| `LANGSMITH_TRACING` | `false` | LangSmith 标准开关；设为 true 后由 LangGraph/LangChain 自动追踪 |
| `LANGSMITH_PROJECT` | `codeguard` | LangSmith 追踪项目名；需同时设置 `LANGSMITH_API_KEY` |
| `CODEGUARD_MAX_CONCURRENT_REVIEWS` | `2` | Java CI 单实例最大并发审查数 |
| `CODEGUARD_REVIEW_TIMEOUT_SECONDS` | `600` | Python 审查子进程超时 |
| `CODEGUARD_RETRY_DELAY_SECONDS` | `30` | 可重试失败的非阻塞延迟 |
| `CODEGUARD_SHUTDOWN_GRACE_SECONDS` | `30` | 停机等待活动审查的最长时间 |
| `CODEGUARD_JOB_DB_PATH` | `./data/codeguard-jobs` | H2 job 数据库路径 |
| `CODEGUARD_WORKSPACE_DIR` | 系统临时目录 | SHA 隔离 workspace 根目录 |
| `CODEGUARD_GRAPH_CACHE_MAX_SNAPSHOTS` | `4` | 完整项目快照缓存上限 |
| `CODEGUARD_GRAPH_CACHE_TTL_MINUTES` | `30` | 项目快照访问后过期分钟数 |
| `CODEGUARD_GRAPH_BUILD_TIMEOUT_SECONDS` | `120` | 全项目 AST/语义图构建超时 |

> **Windows/PowerShell 注意**:bash 的 `VAR=value cmd` 内联写法在 PowerShell 不生效,要先 `$env:VAR="value"` 再跑命令;或直接写 `.env`(推荐)。

---

## 6. 改代码的注意点(重要)

### 6.1 守住组件职责

- Java 侧绝不调 LLM、不判断"是不是问题";Python 侧除采集 diff 外不直接读被审仓库文件,一律走 Java 工具沙箱。
- 工具能力沿通用协议与会话边界逐个增加，不在无关改动中顺手扩展 AST、调用图、RAG 或记忆能力。
- 新能力保持可独立验证，并用相同数据集、只改一个变量的 profile 做效果对照。

### 6.2 无工具对照基准

原 `--mode single` 的无 Agent 基线(`pipeline/reviewer.py`)已完成"有工具 vs 无工具"对比使命后移除(ADR-002 废弃说明)。当前的对照基准是**管线内的无工具直连引擎**(`DirectEngine`):用 `pipeline-notools` profile 跑出的指标即"管线但不开工具"的基线,与 `pipeline-file` / `pipeline-repomap` 对照量化各工具的增益。加新能力时仍按"同一数据集、只改一个变量(profile)"的方式做对照。

### 6.3 改核心数据结构要慎重

`models/schemas.py` 的 `Issue` 被所有阶段共享。增字段一般安全(给默认值即可);**改名 / 删字段 / 改类型**会波及 prompt、CLI 打印、evals 匹配逻辑,改前先全局搜引用。`Severity` 是枚举(约束 LLM 输出范围),新增级别要同步更新 `cli.py` 的 `_SEVERITY_ICON`。

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

## 7. 完成改动前

- 运行与改动范围相符的确定性测试和静态检查。
- 涉及审查质量时运行对应 eval profile，并保存所需结果。
- 架构或模块变化要同步仍被版本控制跟踪的公开说明。

---

_本文件随项目演进更新。改动架构或新增模块时,记得同步这里的目录结构与注意点。_
