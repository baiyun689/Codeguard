# CLAUDE.md

本文件给 Claude / AI 助手以及任何接手者快速建立项目心智模型,并说明改动代码时的约束与注意点。

> 阅读顺序建议:本文件 → `README.md`(公开使用、部署与开发说明)。

---

## 1. 这是什么

Codeguard 是一个 **AI 代码审查引擎**,以 Agent 为最终核心,双语言架构(Python Agent + Java Gateway)。它的输入是代码变更(git diff),输出是结构化的审查问题(`Issue` 列表),覆盖安全、逻辑、质量等维度。

审查核心为证据驱动的 ReviewCouncil 多 Agent 编排；GitHub PR 自动审查链路由 Java Gateway 接收 webhook、调度 Python Agent，并把结果回写到 GitHub。

```
START → classify_mode ─┬─ small  → direct_review
 (PR 规模路由)          └─ medium → file_task_builder → risk_triage → task_rank
                        └─ large  → diff_task_builder ─┘     │
                                                             ├─ summary(可选) → context_provider
                                                             └─ (无摘要时直达) →──┘
                                                                                  │
                          ┌───────────────────────────────────────────────────────┤
                          ▼                        ▼                              ▼
                 discover_threat_model    discover_behavior            discover_maintainability
                          │                        │                              │
                          └────────────────────────┼──────────────────────────────┘
                                                   ▼
                                           council_coordinator ★
                                           (fan-in + RiskTag 解析
                                            + 保守语义归并 + 去重)
                                                   │
                    ┌──────────────────────────────┴──────────────────────────────┐
                    ▼ (evidence_mode=off)                                          ▼ (evidence_mode=full)
            direct_judge ★                                                evidence_verifier ★
            (消融档:跳过取证,                                              (账本健康检查 → 图护栏 →
             候选无证据直接终审)                                             guard 扫描 → 异常重放,零 LLM)
                                                                                       │
                                                                                       ▼
                                                                               council_judge ★
                                                                               (批量 EvidenceJudge:支持/反驳/
                                                                                去留/定级,合同校验 fail-closed
                                                                                → 组内合并)
                    └───────────────────────────────┬────────────────────────────────┘
                                                    ▼
                                                   END
```

管线入口 `classify_mode` 按 PR 体量做纯确定性路由（只统计文件数/hunk 数/字符数，不调 LLM、不建任务）：small（≤3 文件、≤5 hunk、≤8000 字符）走 `direct_review` 单次 LLM 直接审查完整 diff；medium（≤15 文件、≤60000 字符）走 `file_task_builder` 按文件拆分；large 走 `diff_task_builder` 按 hunk 拆分 + 预算控制。后两者经 `risk_triage` 标定每项任务的风险标签，由 `task_rank` 排序限流后，可选经 `summary` 产出变更摘要作为背景。`context_provider` 通过 `resolve_change_context` 为后续所有 Agent 预取共享上下文（类层次/方法签名+可见性+注解/控制流/调用边/敏感 API），减少各发现者冗余的工具调用。

三个发现者 Agent（ThreatModel / Behavior / Maintainability）并行运行，各自配备专属语义图工具 + 共享 `get_file_content`，走 ReAct 引擎：威胁建模用 `inspect_security_path`、行为审查用 `inspect_change_impact`、可维护性用 `inspect_structure`。每个 Agent 的 prompt = 45 行 base 领域知识 + 按 RiskTag 注入的知识文件（`prompts/knowledge/`，37 个），拆分是为分摊上下文压力，重叠是多角度验证。

三路输出在 `council_coordinator` 处 fan-in：解析 RiskTag → 按文件路径和局部位置构建连通候选块 → 最多 8 个并行 LLM 调用做保守语义归并。只有高置信且同时满足同根因、同影响和单一修复条件的分组才会去重；非法、低置信或失败结果一律完整保留。证据采用 **Evidence Ledger**（取代 ADR-046 的 evidence_chain 重放验证）：patch(P01)、预取上下文(Cxx)、真实工具结果(Txx)由运行时代码捕获为内容寻址 Artifact，审查员只输出短编号引用（`evidence_refs`），不生产任何证据文本；`evidence_verifier` 零 LLM 证明 Artifact 真实可用（健康检查 + 图护栏 + guard 扫描 + 异常重放白名单）；`council_judge` 用批量 EvidenceJudge 一次完成支持/反驳/去留/定级，输出经确定性合同校验、失败 fail-closed。`evidence_mode=off` 时跳过取证，候选由 `direct_judge` 直接终审（无证据链消融基线档）。

旧 supervisor 调度图及 SelfChecker/FP/聚合 stages 的归档目录
`services/agent/legacy/` 已于 2026-08 删除（git 历史可回溯），不再随 Python wheel 打包。

---

## 2. 架构

### 组件边界

```
                    ┌──────────────────────────────────────────┐
                    │         Java Gateway（单 JVM 三服务）      │
                    │                                           │
  ┌──────────┐  LLM │  LLM Proxy (:9091)    ──→ DeepSeek/Claude │
  │ Python   │ ────→│  · OpenAI 兼容 API     · 多提供商路由      │
  │ Agent    │      │  · 限流 + 熔断 + 重试  · 降级链透明切换    │
  │          │ 工具 │                                           │
  │ (审查编排)│ ────→│  Tool Server (:9090)  ──→ 磁盘/AST/调用图  │
  │          │      │  · 文件沙箱 + 护栏    · Agent 工具服务      │
  └──────────┘      │                                           │
                    │  CI Webhook (:8080)   ←── GitHub Webhook  │
                    │  · 验签 + 幂等调度    → ProcessBuilder     │
                    │  · Job 持久化 + 重试  → Check Runs 回写   │
                    └──────────────────────────────────────────┘
```

### 默认审查流

Python 智能层 + Java 护栏层。审查统一走多阶段管线,审查员执行方式按是否配置工具服务分流:

```
管线(无工具):git diff → [摘要] → 并行三审查员(直连) → 归并 → 裁决(门控+终审) → 打印
管线(有工具):配置 CODEGUARD_TOOL_SERVER_URL 后,审查员改走 ReAct,
              可调 Java 工具(get_file_content / inspect_security_path / inspect_change_impact / inspect_structure)
              获取 diff 之外上下文

LLM 调用路径:Python → LLM Proxy(:9091) → 按 model 路由 → DeepSeek/Claude/千问
              (或直连:Python → CODEGUARD_API_BASE_URL → LLM 提供商)
```

当前审查核心是 ReviewCouncil 多 Agent 编排：

- **发现者 Agent ×3（并行）**:ThreatModelAgent（安全）/ BehaviorAgent（行为逻辑）/ MaintainabilityAgent（维护质量）。每个 Agent 配备专属语义图工具（`inspect_security_path` / `inspect_change_impact` / `inspect_structure`）+ 共享 `get_file_content`，走 ReAct 引擎。prompt = base 领域知识 + 按 RiskTag 注入的 `prompts/knowledge/` 知识文件——拆分是为分摊上下文压力。重叠不叫重复，叫多角度验证。每个候选输出带 `evidence_refs` 证据编号引用——审查员只从运行时捕获的 `<evidence_catalog>`（patch=P01 / 预取上下文=Cxx / 工具结果=Txx）里挑编号，不得重新填写工具参数、代码片段或工具原文；工具返回会回显编号（[证据编号 T0n]），离开发现子图即绑定为内容寻址 artifact ID。
- **CouncilCoordinator（fan-in 归并）**:三路发现者输出的显式汇聚屏障——批量解析 RiskTag → 按同文件/邻行构建连通候选块 → 每块并行 LLM 保守语义归并（同根因+同影响+单一修复，最多 8 路）→ 产出严格等价逻辑组；非法/低置信/失败结果一律完整保留。
- **EvidenceVerifier（确定性验证，零 LLM、正常路径零重放）**:只证明 Artifact 真实、可用、属于候选范围——① Artifact 健康检查（patch 摘要一致；图响应 subject/scope/status/coverage 护栏；TEST 关系不能证明生产可达）；② guard 注解扫描器（确定性：@PreAuthorize/@Transactional 命中直接产出 direct 反证，Judge 前淘汰）；③ 引用范围核对（跨 task/缺失/失败 Artifact 留痕）。仅异常 Artifact（失败/未知/revision 不一致/响应不可解析）进入重放队列，受 `enabled_evidence_tools` 白名单约束，重放失败只产生限制、不作反证。
- **CouncilJudge（批量证据裁决）**:每批 ≤8 候选、最多 4 批并行，一次完成支持/反驳/去留/定级（`EvidenceJudgeBatch`）——keep 必须引用 ≥1 支持事实、引用 ID 必须属于候选可见范围、supporting/counter 不得重叠、维护性候选不得 CRITICAL、LOCATION 角色不能单独支持 keep；输出合同违约重试/二分拆批，单候选最终失败 fail-closed 不输出。之后组内合并（严格等价组收敛，组内形状不一致安全拆回）。`evidence_mode=off` 时走 `direct_judge` 消融档：无证据输入、跳过取证，输出 keep/drop/severity 同构。

### CI 集成

Codeguard 支持 GitHub PR 自动审查——PR 创建/更新时自动触发，结果回写到 Check Runs 和行级评论。

```
GitHub PR opened/synchronize/reopened
      │
      ▼
  Webhook (POST /webhooks/github)
      │
      ▼
  Java Gateway CI 链路 ──────────────────────┐
      │                                       │
      ├─ WebhookVerifier (HMAC-SHA256 验签)    │
      ├─ GitHubWebhookController (事件过滤)    │
      ├─ JobRepository MySQL (幂等去重+持久化)    │
      ├─ JobScheduler (有界队列+并发信号量)    │
      ├─ ReviewExecutor (git clone+Python CLI) │
      ├─ ResultFeedback (Check Runs+行评论)    │
      └─ ReviewGuard (令牌桶限流+降级+重试)    │
                                              │
  Python Agent (ProcessBuilder 调用) ◄────────┘
```

**启动方式**:
```powershell
# 本地开发: 一键脚本
cd services/gateway
.\start-ci.ps1

# 生产部署: Docker Compose
docker compose up -d
```

**前提条件**: GitHub App（Checks R&W + PR R&W + Contents R），SSH 反向隧道或公网暴露。
- **去重体系**:分层归并——发现者侧按候选 id 去重（fan-in reducer）+ 任务错配/噪音候选拒绝 → `council_coordinator` 内规范化排序、同文件邻行连通分块 → 每块并行 LLM 保守归并（同根因+同影响+单一修复，非破坏性逻辑组）→ 终审后按严格等价组收敛最终 Issue。核心设计：行号是 LLM 推算的天然不精确值，只作分块窗口的兜底弱信号，归并语义由 LLM 判断。

审查员的"执行方式"抽成可插拔引擎(`pipeline/engines.py`):`DirectEngine`(无工具基准)/ `ToolAgentEngine`(ReAct,基于 langchain v1 `create_agent`)。`ReviewerStage` 按 `tool_client` 是否存在分流。

**职责边界**:Python = 智能编排(推理 / 编排 / 对结论加工);Java = 三大独立服务:
- **LLM Proxy (:9091)**:OpenAI 兼容代理,统一管理多提供商路由、熔断/限流/重试、API key。Python 侧无提供商密钥,只看到一个本地端点。
- **Tool Server (:9090)**:Agent 工具服务 + 文件访问沙箱 + AST 分析 + 调用链 + 危险 API 扫描。
- **CI Webhook (:8080)**:GitHub PR 自动审查链路——验签、幂等调度、git clone、Python 子进程调用、Check Runs 回写。
四条不变量:Python 调 Java 单向;代码探索只走 Java 沙箱;不确定性只在 Python;Java 不判断"是不是问题"(LLM Proxy 仅协议转发,不做语义判断)。

误报过滤由裁决阶段承担:Verifier 的 guard 反证与引用范围核对(零成本淘汰)+ 批量 EvidenceJudge 终审(keep/drop 裁决、合同校验 fail-closed;评判环节遵循异源 + temperature=0,见 ADR-008)。

`services/gateway`(Java)提供工具服务 + 护栏。**只放"事实与护栏"(工具执行 / 沙箱 / 重计算),绝不在 gateway 里调 LLM 或做"是不是问题"的判断**(那是 Python 的事,见职责边界)。

---

## 3. 目录结构

```
Codeguard/
├── CLAUDE.md                      # 本文件
├── README.md                      # 快速开始
├── .env.example                   # 环境变量示例(复制为 .env 使用)
├── Dockerfile                     # 生产 Docker 镜像
├── docker-compose.yml             # 生产部署配置
└── services/
    ├── agent/                     # Python Agent(智能编排层)
    │   ├── pyproject.toml         # 依赖与打包(打包仅含 src/codeguard_agent)
    │   ├── src/codeguard_agent/
    │   │   ├── __main__.py        # python -m codeguard_agent 入口
    │   │   ├── cli.py             # 命令行:review 子命令、结果打印、退出码、工具会话建/销
    │   │   ├── config.py          # Settings:从环境变量/.env 读配置(含 CODEGUARD_TOOL_SERVER_URL)
    │   │   ├── models/            # ★数据结构:schemas.py(Issue/Severity/ReviewResult/
    │   │   │   └──               #   DiscoveredIssue/evidence_refs) evidence.py(证据账本:
    │   │   │                     #     Artifact/Catalog/Ref/Verifier/Judge 模型)
    │   │   │                     #   tasks.py(任务/PR 规模路由阈值) council.py(候选/
    │   │   │                     #     裁决/统计) knowledge.py
    │   │   ├── git/diff_collector.py  # 调系统 git 采集 diff + parse_changed_files(派生 allowed_files)
    │   │   ├── llm/client.py      # LLM 工厂(openai/claude/mock)+ 重试 + mock 假数据
    │   │   ├── observability/     # HTML Trace:审查过程视图/统计(collector/dashboard/view_model)
    │   │   ├── pipeline/graph.py          # ★ReviewCouncil 状态图、节点与条件边(含 classify_mode 规模路由)
    │   │   ├── pipeline/orchestrator.py   # PipelineOrchestrator 门面
    │   │   ├── pipeline/engines.py        # DirectEngine / ToolAgentEngine
    │   │   ├── pipeline/risk/             # 任务、风险规则、路由与排序(classify_diff/diff_metrics)
    │   │   ├── pipeline/context/          # 图谱符号上下文与事实预算
    │   │   ├── pipeline/reviewers/        # 三路发现者与工具协调
    │   │   ├── pipeline/evidence/         # 证据账本(ledger 注册/绑定/目录渲染,
    │   │   │                             #   verifier 健康检查/图护栏/异常重放,
    │   │   │                             #   graph_response 图摘要与护栏, guard_scan 注解扫描,
    │   │   │                             #   tags 标签开关, planner, rules/classify+terms)
    │   │   ├── pipeline/council/          # 裁决与指标(verdict 门控+终审+组内合并, dedup 归并, metrics)
    │   │   ├── pipeline/knowledge/        # RiskTag 知识目录与选择器
    │   │   ├── pipeline/summary/          # 可选变更摘要阶段
    │   │   └── prompts/                   # 发现者 base×3、discovery-evidence-contract(证据引用契约)、
    │   │                                 #   evidence-judge(批量终审)、direct-judge(消融档)、
    │   │                                 #   candidate-dedup-*(归并)、summary-*(摘要)、eval-direct-reviewer、
    │   │                                 #   discovery-context-contract + knowledge/(按 RiskTag 注入)
    │   ├── tests/                 # pytest:测工程正确性
    │   └── evals/                 # ★审查质量评测框架(量化效果,见 §5;profiles.yaml 定义被测档位)
    └── gateway/                   # ★Java Gateway——四模块 Maven 多模块项目
        ├── pom.xml               # 父 POM(管理 shared/tool-server/ci-webhook/llm-proxy 四个子模块)
        ├── start-ci.ps1          # ★本地一键启动 CI Gateway 脚本
        ├── shared/               # 共用基础设施
        │   └── src/.../common/   #   GatewayMetrics / OperationalController
        ├── tool-server/          # Agent 工具服务(:9090)
        │   └── src/.../
        │       ├── toolserver/   #   ToolServerApp / ToolServerController / ToolSessionManager / GatewaySettings
        │       └── agent/
        │           ├── core/     #   AgentTool 接口 / ToolResult 信封 / AgentContext
        │           ├── graph/    # ★ ProjectSnapshot / ProjectCodeGraph / JavaParser 符号解析
        │           └── tools/    #   沙箱与语义图工具
        ├── ci-webhook/           # GitHub PR 自动审查链路(:8080)
        │   └── src/.../
        │       ├── Main.java     # ★ 统一入口(同时启动三个服务)
        │       └── ci/
        │           ├── model/    #   WebhookPayload / ReviewJob 数据模型
        │           ├── webhook/  #   WebhookVerifier / GitHubWebhookController(验签+事件过滤+幂等)
        │           ├── job/      #   JobRepository(MySQL 持久化) / JobScheduler(有界队列+信号量)
        │           ├── executor/ #   ReviewExecutor(ProcessBuilder+异步IO+超时) / ResultFeedback(Check Runs+行评论)
        │           ├── github/   #   GitHubClient(App JWT+Check Runs API+PR Comments)
        │           └── guard/    #   ReviewGuard(令牌桶限流)
        ├── llm-proxy/            # ★ LLM 代理网关(:9091)——OpenAI 兼容端点，多提供商路由 + Resilience4j 韧性
        │   └── src/.../proxy/
        │       ├── ProxyServer.java
        │       ├── handler/      #   ChatCompletionsHandler(验证→路由→降级链→协议转换)
        │       ├── router/       #   ProviderRouter(model 名→adapter 链)
        │       ├── adapter/      #   DeepSeekAdapter / ClaudeAdapter / QwenAdapter(协议转换+透传)
        │       ├── model/        #   OpenAiChatRequest / OpenAiChatResponse(Jackson record)
        │       ├── config/       #   ProxyConfig(YAML 加载+环境变量替换)
        │       └── resilience/   #   ResilienceService(限流→熔断 per-provider→重试 指数退避+jitter)
```

带 ★ 的是改动时最需要小心的核心文件。

---

## 4. 数据流与各模块职责

一次 `python -m codeguard_agent review` 的完整链路:

1. **`cli.py:main`** 解析参数(`--repo` / `--base`),构造 `Settings.from_env()`。
2. **`config.py:Settings.from_env`** 就近加载 `.env`(已显式设置的环境变量优先),读出 provider / model / api_key / structured_method / tool_server_url 等。
3. **`git/diff_collector.py:collect_diff`** 调系统 `git diff <base>` 拿 unified diff 文本;空 diff 直接结束。
4. **`llm/client.py:build_llm`** 按 provider 造 LangChain Chat 模型;`provider=mock` 返回 `None`(下游走假数据)。
5. **`pipeline/graph.py` 的 ReviewCouncil 状态图**是核心:
   - `classify_mode` 按 diff 体量路由(small→`direct_review` 单次直审 / medium→`file_task_builder` 按文件 / large→`diff_task_builder` 按 hunk)。
   - medium/large 走完整管线:`risk_triage` 标风险标签 → `task_rank` 排序限流 → 可选 `summary` → `context_provider` 预取符号上下文 → 三路发现者并行(ReAct,输出带 `evidence_refs` 证据编号引用)→ `council_coordinator` 归并 → `evidence_verifier` 证据验证(Artifact 健康检查/图护栏/异常重放,零 LLM)→ `council_judge` 批量 EvidenceJudge 裁决出 `Issue`(`evidence_mode=off` 时经 `direct_judge` 直接终审)。
   - `llm is None`(mock)→ 各阶段返回 mock 假数据(如 `direct_review` 返回 `mock_review_result()`)。
   - 工具服务不可用时显式降级(ReAct 退直连 / 空证据不炸管线)。
6. **`cli.py:_print_result`** 打印;**退出码**:发现任一 `CRITICAL` 返回 1,否则 0(方便接 CI 门禁)。

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
conda run -n codeguard python -m evals.runner --profile eval-codeguard-full --runs 1   # 按 profile 评测(见 evals/profiles.yaml)
conda run -n codeguard python -m evals.runner --tools --runs 1   # 或 --tools 合成 ad-hoc 档(管线 + 工具开/关)
conda run -n codeguard python -m evals.runner --judge --runs 3   # 开 LLM 裁判做案例级语义配对

# —— Java Gateway(services/gateway 工具服务)——
mvn package                # 跑单测 + 出 fat jar
mvn test                   # 只跑单测
java -jar target/codeguard-gateway.jar    # 启动工具服务(默认 9090,CODEGUARD_TOOL_SERVER_PORT 可覆盖)

# —— 真实 ReAct 审查(工具开档:先起 Java 工具服务,再设 URL)——
$env:CODEGUARD_TOOL_SERVER_URL="http://localhost:9090"
conda run -n codeguard python -m codeguard_agent review --repo <repo> --trace
```

### CI 模式（GitHub PR 自动审查）

```powershell
# 本地开发:先 mvn package；脚本只补载 .env 中未显式设置的 CODEGUARD_* 并启动已有 jar
cd services/gateway
.\start-ci.ps1

# 生产部署: Docker Compose
docker compose up -d
```

> CI 模式下，Gateway 监听 `/webhooks/github`，收到 PR 事件后自动 clone、审查、回写 Check Runs。
> 启动前需注册 GitHub App（Checks R&W + Pull requests R&W + Contents R），配置 webhook URL 指向 Gateway 公网地址。

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
| `CODEGUARD_PROVIDER` | `openai` | `openai` / `claude` / `mock` |
| `CODEGUARD_MODEL` | 按 provider 回退 | 留空自动选默认模型 |
| `CODEGUARD_API_KEY` | 空 | openai/claude 必填 |
| `CODEGUARD_API_BASE_URL` | 空 | 代理 / 兼容端点(如 DeepSeek)填 |
| `CODEGUARD_STRUCTURED_METHOD` | `function_calling` | 结构化输出方式 |
| `CODEGUARD_DISABLE_THINKING` | `false` | 用 DeepSeek 推理模型时设 `true` |
| `CODEGUARD_MAX_RETRIES` | `3` | LLM 调用重试次数 |
| `CODEGUARD_ENABLE_SUMMARY` | `true` | 前置摘要/软分派阶段开关;关闭则审查员吃整份 diff(仅 pipeline) |
| `CODEGUARD_EVIDENCE_MODE` | `full` | 证据开关;`off` 跳过取证,候选由 DirectJudge 直接终审(无证据链消融基线档) |

> **Windows/PowerShell 注意**:bash 的 `VAR=value cmd` 内联写法在 PowerShell 不生效,要先 `$env:VAR="value"` 再跑命令;或直接写 `.env`(推荐)。

---

## 6. 改代码的注意点(重要)

### 6.1 守住组件职责

- Java 侧绝不调 LLM、不判断"是不是问题";Python 侧除采集 diff 外不直接读被审仓库文件,一律走 Java 工具沙箱。
- 工具能力沿通用协议与会话边界逐个增加，不在无关改动中顺手扩展 AST、调用图、RAG 或记忆能力。
- 新能力保持可独立验证，并用相同数据集、只改一个变量的 profile 做效果对照。

### 6.2 无工具对照基准

原 `--mode single` 的无 Agent 基线(`pipeline/reviewer.py`)已完成"有工具 vs 无工具"对比使命后移除(ADR-002 废弃说明)。当前的对照基准是**管线内的无工具直连引擎**(`DirectEngine`):`pipeline-notools` profile 跑出的指标即"管线但不开工具"的基线,与 `pipeline-file`(仅文件工具) / `pipeline-codegraph`(语义图工具) / `pipeline-fpverify`(误报复核) 对照量化各能力的增益;消融对照档在 `eval-*` 四个 profile(见 evals/profiles.yaml)。加新能力时仍按"同一数据集、只改一个变量(profile)"的方式做对照。

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

## Agent skills

### Issue tracker

任务使用 GitHub Issues;外部 Pull Request 不进入 triage 队列。

### Triage labels

使用默认标签:`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。

### Domain context

领域行为以当前代码、测试和仍被版本控制跟踪的公开说明为准。

---

_本文件随项目演进更新。改动架构或新增模块时,记得同步这里的目录结构与注意点。_
