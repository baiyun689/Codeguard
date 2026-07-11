# CLAUDE.md

本文件给 Claude / AI 助手以及任何接手者快速建立项目心智模型,并说明改动代码时的约束与注意点。

> 阅读顺序建议:本文件 → `README.md`(快速开始)→ `docs/ROADMAP.md`(分阶段路线)→ `DECISIONS.md`(为什么这么选)。

---

## 1. 这是什么

Codeguard 是一个 **AI 代码审查引擎**,以 Agent 为最终核心,双语言架构(Python Agent + Java Gateway)。它的输入是代码变更(git diff),输出是结构化的审查问题(`Issue` 列表),覆盖安全、逻辑、质量等维度。

这是一次 **vibe coding 实践**——刻意按"先跑通 → 看效果 → 小步迭代 → 记录决策"的节奏演进,每个阶段都设计成"可独立跑、可独立讲"的里程碑。

**当前进度:阶段 4 收尾 + 阶段 5 CI 集成先行**。审查核心为 ADR-032 ReviewCouncil 多 Agent 编排；新增 GitHub PR 自动审查 CI 链路。context_provider 新增 `get_diff_ast` 工具(ADR-036)，审查前提取 diff 文件完整 AST 注入共享上下文。

```
START → [summary] → context_provider ─┬─ parse_changed_files
                                       ├─ find_sensitive_apis
                                       ├─ get_diff_ast ★
                                       └─→ ContextBundle
                                             │
          ┌──────────────────────────────────┼──────────────────────────────┐
          ▼                                  ▼                              ▼
 discover_threat_model             discover_behavior           discover_maintainability
          │                                  │                              │
          └──────────────────────────────────┼──────────────────────────────┘
                                             ▼
                                       fan-in (dedup reducer)
                                             │
                                  council_coordinator ←──┐
                                       │                  │
                                  evidence_agent ─────────┘
                                       │
                                  council_judge (7规则+去重+LLM终审)
                                       │
                                      END
```

三个发现者 Agent 各自配备专属工具（`find_sensitive_apis` / `find_callers` / `get_code_metrics`）+ 共享 `get_file_content`，走 ReAct 引擎。`context_provider` 通过 `get_diff_ast` 为所有 Agent 提供共享的代码骨架（类层次/方法签名+可见性+注解/控制流/调用边），减少冗余 `get_file_content` 调用。每个 Agent 的 prompt 包含 ~220 行领域知识图谱（漏洞分类/缺陷模式/判例），合计 ~690 行——拆分是为分摊上下文压力。去重采用"根因标识符主键 + 行号 ±3 兜底"的 5 层体系。裁决节点合并了旧 challenge_agent + self_checker，用异源千问（temperature=0）做语义综合与终审。

旧 supervisor 调度图已迁移到 `services/agent/legacy/supervisor_graph/graph.py`，仅作历史参考。

阶段 2(管线化)已完成并存档；阶段 3(工具调用 Agent)已落地 4 个 Java 工具，能力融入 ADR-032 编排。

---

## 2. 架构

### 目标形态(路线图终点)

```
┌─────────────────┐     HTTP / 工具调用      ┌──────────────────┐
│  Python Agent   │ ──────────────────────> │  Java Gateway    │
│  (审查管线/编排)  │ <────────────────────── │  (AST/调用图/RAG) │
└─────────────────┘     代码上下文工具         └──────────────────┘
```

### 当前实现(阶段 3 第一步)

Python 智能层 + Java 护栏层。审查统一走多阶段管线,审查员执行方式按是否配置工具服务分流:

```
管线(无工具):git diff → [摘要/软分派] → 并行三审查员(直连) → 两段式聚合 → 误报过滤 → 打印
管线(有工具):配置 CODEGUARD_TOOL_SERVER_URL 后,审查员改走 ReAct,
              可调 Java 工具(get_file_content / get_repo_map)获取 diff 之外上下文,其余阶段不变
```

当前审查核心是 **ADR-032 ReviewCouncil** 多 Agent 编排（详见 DECISIONS.md ADR-032/033）：

- **发现者 Agent ×3（并行）**:ThreatModelAgent（安全）/ BehaviorAgent（行为逻辑）/ MaintainabilityAgent（维护质量）。每个 Agent 配备专属工具 + 共享 `get_file_content`，走 ReAct 引擎。每个 prompt ~220 行领域知识图谱（漏洞分类/缺陷模式/判例/判定要点），三个合计 ~690 行——拆分是为分摊上下文压力。重叠不叫重复，叫多角度验证。
- **CouncilCoordinator（确定性路由）**:读结构化字段做路由决策，不调 LLM。
- **EvidenceAgent（智能举证）**:调 Java 工具获取原始事实 → 对每条工具输出调 LLM 分析证据含义，产出结构化 SUPPORTS/CONTRADICTS/INSUFFICIENT 判定 + 推理依据。contradicts 字段已激活。LLM 不可用时回退 raw output 模式。
- **CouncilJudge（证据驱动裁决）**:4 条确定性规则（invalid_file / strong_support fast-track / contradicted / no_evidence）→ 两段式去重（指纹+LLM 语义综合）→ 安全网（根因标识符匹配优先、行号±3 兜底）→ LLM 终审（基于结构化证据摘要做 keep/drop/downgrade/merge，异源千问 temperature=0）。

### CI 集成（阶段 5 先行）

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
      ├─ JobRepository H2 (幂等去重+持久化)    │
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
- **去重体系**:5 层——fan-in reducer（指纹+根因标识符+邻行）→ 规则淘汰（4 条）→ 两段式去重 → 安全网 → LLM 终审。核心设计：行号是 LLM 推算的天然不精确值，降级为兜底弱信号；去重主键改为"同文件+共享关键标识符（方法名/变量名）"。

审查员的"执行方式"抽成可插拔引擎(`pipeline/engines.py`):`DirectEngine`(无工具基准)/ `ToolAgentEngine`(ReAct,基于 langchain v1 `create_agent`)。`ReviewerStage` 按 `tool_client` 是否存在分流(见 ADR-009 / openspec design.md D1、D5)。

**职责边界(阶段 3 钉死,统领后续)**:Python = 智能编排(推理 / 编排 / 对结论加工);Java = 护栏 + 地面真值(安全沙箱 / 重静态计算)。四条不变量:Python 调 Java 单向、Java 不碰 LLM;代码探索只走 Java 沙箱;不确定性只在 Python;Java 不判断"是不是问题"。详见 ADR-009 / design.md D0。

误报过滤两段式:确定性规则(零成本)+ 可选 LLM 验证(默认关,开启时优先异源模型,见 ADR-008)。

`services/gateway`(Java)本期正式引入:工具服务 + 护栏。**只放"事实与护栏"(工具执行 / 沙箱 / 重计算),绝不在 gateway 里调 LLM 或做"是不是问题"的判断**(那是 Python 的事,见职责边界)。

---

## 3. 目录结构

```
Codeguard/
├── CLAUDE.md                      # 本文件
├── README.md                      # 快速开始
├── DECISIONS.md                   # 架构决策记录(ADR)—— 改设计前必读
├── .env.example                   # 环境变量示例(复制为 .env 使用)
├── Dockerfile                     # 生产 Docker 镜像
├── docker-compose.yml             # 生产部署配置
├── docs/ROADMAP.md                # 分阶段搭建路线图
└── services/
    ├── agent/                     # Python Agent(智能编排层)
    │   ├── pyproject.toml         # 依赖与打包(打包仅含 src/codeguard_agent)
    │   ├── src/codeguard_agent/
    │   │   ├── __main__.py        # python -m codeguard_agent 入口
    │   │   ├── cli.py             # 命令行:review 子命令、结果打印、退出码、工具会话建/销
    │   │   ├── config.py          # Settings:从环境变量/.env 读配置(含 CODEGUARD_TOOL_SERVER_URL)
    │   │   ├── models/schemas.py  # ★核心数据结构:Severity / Issue / ReviewResult
    │   │   ├── git/diff_collector.py  # 调系统 git 采集 diff + parse_changed_files(派生 allowed_files)
    │   │   ├── llm/client.py      # LLM 工厂(openai/claude/mock)+ 重试 + mock 假数据
    │   │   │   ├── pipeline/graph.py            # ★ADR-032 审查状态图(~1040行):State+reducer+节点+条件边+去重
    │   │   │   ├── pipeline/orchestrator.py   # PipelineOrchestrator 门面,对外返回 ReviewResult
    │   │   │   ├── pipeline/engines.py        # ★审查员执行引擎:DirectEngine(直连基准)/ToolAgentEngine(ReAct)
    │   │   │   ├── pipeline/fp_rules.py       # 误报过滤的确定性规则(纯函数,可单测)
    │   │   │   ├── pipeline/stages/           # 旧管线阶段(现被 graph 替代):summary/reviewer_stage/aggregation/
    │   │   │   │                              #   fp_filter/context_provider/self_checker
    │   │   │   ├── models/council.py          # ★ADR-032 内部模型:CandidateIssue/EvidenceRequest/Verdict/JudgeDecision
    │   │   │   └── prompts/                   # threat-model.txt(~220行安全知识图谱)/behavior.txt(~240行)/
    │   │   │                                  #   maintainability.txt(~230行)/aggregation-*.txt/fp_verify.txt/
    │   │   │                                  #   旧 security.txt/logic.txt/quality.txt(保留作为知识参考)
    │   ├── config/false-positive-rules.yaml  # 误报过滤的确定性规则配置(YAML)
    │   ├── tests/                 # pytest:测工程正确性
    │   └── evals/                 # ★审查质量评测框架(量化效果,见 §5)
    └── gateway/                   # ★Java Gateway(护栏 + 地面真值层 + CI 入口,阶段3引入)
        ├── pom.xml               # Maven;Javalin + Jackson + SLF4J + Guava + H2;fat jar 独立启动
        ├── start-ci.ps1          # ★本地一键启动 CI Gateway 脚本
        └── src/main/java/com/codeguard/
            ├── agent/core/       # AgentTool 接口 / ToolResult 信封 / AgentContext
            ├── agent/ast/        # ★get_diff_ast:DiffASTResult / DiffASTAnalyzer(JavaParser 完整 AST 提取) / ASTContextFormatter(LLM 文本+两级裁剪)
            ├── agent/tools/      # ToolRegistry / FileAccessSandbox(护栏)/ GetFileContentTool / FindCallersTool / FindSensitiveApisTool / GetCodeMetricsTool / GetDiffASTTool
            ├── legacy/repomap/   # get_repo_map 已下线，实现保留备参考(ADR-020/036)
            ├── ci/               # ★CI 集成(GitHub PR 自动审查):
            │   ├── model/        #   WebhookPayload / ReviewJob 数据模型
            │   ├── webhook/      #   GitHubWebhookController(验签+事件过滤+幂等)
            │   ├── job/          #   JobRepository(H2 持久化) / JobScheduler(有界队列+信号量)
            │   ├── executor/     #   ReviewExecutor(ProcessBuilder+异步IO+超时) / ResultFeedback(Check Runs+行评论)
            │   ├── github/       #   GitHubClient(App JWT+Check Runs API+PR Comments)
            │   └── guard/        #   ReviewGuard(令牌桶限流+大diff降级+重试判定)
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
5. **`pipeline/reviewer.py:review`** 是核心:
   - 空 diff → 直接返回"无需审查"。
   - `llm is None`(mock)→ 返回 `mock_review_result()` 假数据。
   - 否则加载 `prompts/security.txt`,用 `with_structured_output(ReviewResult)` 让模型直接吐结构化结果,经 `invoke_with_retry` 调用。
   - **结果可能为 `None`**(模型没正确发起工具调用时),已兜底成空 `ReviewResult`。
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

### CI 模式（GitHub PR 自动审查）

```powershell
# 本地开发: 一键启动 CI Gateway（需先配 github-app-id.txt + github-app.pem）
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

## Agent skills

### Issue tracker

任务使用 GitHub Issues;外部 Pull Request 不进入 triage 队列。见 `docs/agents/issue-tracker.md`。

### Triage labels

使用默认标签:`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。见 `docs/agents/triage-labels.md`。

### Domain docs

采用 single-context 布局,根目录 `CONTEXT.md` 保存领域术语,`DECISIONS.md` 是现有 ADR 总账。见 `docs/agents/domain.md`。

---

_本文件随项目演进更新。改动架构或新增模块时,记得同步这里的目录结构与注意点。_
