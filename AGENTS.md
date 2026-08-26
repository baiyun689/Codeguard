# AGENTS.md

本文件给 Codex / AI 助手以及任何接手者快速建立项目心智模型,并说明改动代码时的约束与注意点。

> 阅读顺序建议:本文件 → `README.md`(公开使用、部署与开发说明)。

---

## 1. 这是什么

Codeguard 是一个 **AI 代码审查引擎**,以 Agent 为最终核心,双语言架构(Python Agent + Java Gateway)。它的输入是代码变更(git diff),输出是结构化的审查问题(`Issue` 列表),覆盖安全、逻辑、质量等维度。

默认审查使用证据驱动的多 Agent ReviewCouncil、task 级 DirectGate、OCR 式 PlanUnit、task-scoped reviewer、按需知识注入，以及策略驱动的证据规划与裁决链。双语言边界保持不变:Python 智能层 + Java 护栏层。默认审查路径为:

```
git diff → PRModeClassifier → FileTaskBuilder/HunkTaskBuilder
         → TaskRoute(DirectGate)
         ├─ direct task → DirectTaskReview
         └─ full task → TaskSelection → PlanUnit(按文件复用)
              → Plan → ReviewPlan → [Summary] → SymbolResolution → task-scoped Discover
              → CandidateLocator → CouncilCoordinator → EvidenceVerifier(账本验证,零 LLM)
              → CouncilJudge(批量 EvidenceJudge)→ ReviewResult
```

Java Gateway 的单实例 CI 执行底座一次执行返回结构化 outcome，调度器负责
H2 状态、非阻塞重试、恢复、反馈与停机；workspace 按完整 SHA 隔离，并提供 readiness 与 Prometheus。
Compose 的 `observability` profile 提供 Prometheus、预置告警规则和自动配置的 Grafana
看板，覆盖审查吞吐/耗时、AST 工具调用及 LLM provider 重试、fallback 和熔断状态。

ReviewCouncil 发现者由 `ThreatModelAgent` / `BehaviorAgent` / `MaintainabilityAgent` 方法论分工;最终 category 仍兼容 `security` / `logic` / `quality`。三类发现者各自声明工具 allowlist，并通过 `CandidateIssue` / `EvidenceRef` / `Verdict` / `CouncilTrace` 结构化黑板通信。三路发现者只通过 ID reducer 汇集 raw candidates；CouncilCoordinator 在 fan-in 后按完整路径和局部位置构块，并以最多 8 个并行结构化 LLM 调用进行保守归并。非法、低置信或失败结果一律保留候选。

证据采用 **Evidence Ledger**（取代 ADR-046 的 evidence_chain 重放验证与更早的多阶段 Concern/Strategist/Researcher/ImpactAssessor，后者已废弃勿恢复）：patch（P01）、预取上下文（Cxx）、真实工具结果（Txx）由运行时代码捕获为内容寻址 Artifact，审查员只输出短编号引用（`evidence_refs`），不生产任何证据文本。EvidenceVerifier 全部确定性、零 LLM、正常路径零重放——Artifact 健康检查（patch 摘要一致、图响应 schema/outcome/scope/coverage 护栏）、guard 注解扫描（按发现者分工：@PreAuthorize 族/@Transactional 确定性反证）、引用范围核对；`indeterminate` 形成不可引用的 EvidenceGap，仅执行失败、空响应、解析失败和 revision 不一致进入重放，重放结果必须重新校验。CouncilJudge 用批量 EvidenceJudge（每批 ≤8 候选）一次完成支持/反驳/去留/定级，输出经确定性合同校验（keep 必须引用支持事实、ID 必须可见、维护性候选不得 CRITICAL、LOCATION 不能单独支持），违规重试/二分拆批，单候选最终失败 fail-closed 不输出。旧 Supervisor 图迁移到 `services/agent/legacy/supervisor_graph/`,仅作历史参考,不作为默认路径、feature flag 或 eval profile 回退。

Reviewer 分派和知识注入完全由 Full task 的 Plan 决定；Plan 失败时只注入 BASE，并沿用基础 Reviewer 覆盖策略。
任务选择只消费 DirectGate、diff 规模和确定性任务上限，不依赖额外的风险分类模型。内部 State
保存 `task_routes`、`plan_units`、`task_plans` 和 `review_assignments`，不增加产品输出字段。

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
默认(无工具):git diff → task 构建/DirectGate → Plan → Reviewer 直连 → 证据账本验证 → 批量 Judge → 打印
默认(有工具):配置 CODEGUARD_TOOL_SERVER_URL 后,Tool Server 按 revision 构建完整 Java ProjectSnapshot；
              Full task 经 Plan 选择 Reviewer，SymbolResolution 注入稳定 symbol context，三路发现者分别使用
              inspect_security_path / inspect_change_impact / inspect_structure，工具结果由运行时捕获为 Txx Artifact 进账
```

默认节点:

- **Summary 阶段(可选)**:在 TaskRank 后对选中任务范围产出变更摘要,作为 ReviewCouncil 的导航背景。由 `CODEGUARD_ENABLE_SUMMARY` 控制(默认开)。摘要不进入 Evidence Ledger,也不作为候选成立依据。
- **PR 规模与 Task 路由**:`PRModeClassifier` 只按 diff 体量选择 task 粒度：SMALL 整个 diff 一个 task，MEDIUM 按文件建 task，LARGE 按 hunk 建 task。TaskBuilder 之后由确定性 DirectGate 逐 task 决定 direct/full；低风险文档/注释任务走 Direct，其余默认 Full。SMALL 不再绕过统一管线。LARGE 同一文件的 Full hunk 共享一个 PlanUnit，Plan 只调用一次；HTML Trace 展示 TaskRoute、DirectTaskReview 和 Plan。
- **SymbolResolution**:在 ReviewCouncil 前把 Full task 的变更文件与行号批量解析为强类型 `TaskSymbolContext`。它只提供稳定 `symbol_id`、声明范围、注解、局部控制流和来源集合，供领域工具、Evidence Ledger 与 guard 扫描使用；不负责摘要、知识选择、Reviewer 分派、深层图谱查询或问题判断。
- **大 diff 降级**:仅在超过 5000 行时，Python 确定性收紧为最多 20 个任务、每文件 3 个、每任务上下文 2000 字符；普通 diff 全选 Full task。Plan 不引入新的总 Token 预算，成本由 task 粒度、同文件 Plan 复用、并发限制、超时和现有重试控制。Java 不重复判断。
- **Plan 与 ReviewCouncilSubgraph**:Full task 按 PlanUnit 并发执行结构化 Plan；Plan 显式选择 `ThreatModelAgent` / `BehaviorAgent` / `MaintainabilityAgent`、审查重点和知识主题，不选择工具。三个 task-scoped 发现者 fan-out 产出 raw `CandidateIssue`;Reviewer 固定持有 `inspect_security_path` / `inspect_change_impact` / `inspect_structure`，这些专属工具负责发现隐藏的跨文件安全、行为和结构问题。user prompt 携带 Plan 重点、预取事实和 Plan 选中的 BASE+专项 knowledge bundle。`CouncilCoordinator` 在显式 fan-in 后构建局部候选块并保守归并。
- **安全路径查询边界**:`inspect_security_path` 只沿已解析调用关系执行最多三层传播；未解析调用仅在目标名称直接命中敏感 sink 时进入 `unresolved_relationships`。普通未解析调用不作为安全事实且不输出关系明细，但会汇总进 `unresolved_count` 并保持 `partial`，避免通用解析噪声膨胀响应，也不把无法遍历的下游误判为完整缺席。
- **图谱响应合同**:schema v2 只输出当前 `source_scope` 的 canonical `symbols`、`relationships`、`unresolved_relationships`；每项 `source_set` 必须与 scope 一致，不再双写 MAIN/TEST/GENERATED 专用数组。带旧 scope 数组的 Gateway 响应视为协议不兼容。
- **CandidateLocator(节点内定位护栏)**:Full 与 Direct 的 `DiscoveredIssue` 在绑定稳定候选 ID 前统一校验 `location_snippet`。只允许当前 task 新增行中的 1～5 行连续原文；唯一匹配可修正 Reviewer 行号，合法原行号可兜底，其余按 task 每批最多 8 条调用 LLM 重新提取片段并确定性复验。最终失败保留为 `line=0` 文件级候选，并向 Judge 暴露 `candidate_location_unresolved` 限制；该步骤不新增 LangGraph 节点，定位片段也不进入产品输出或证据账本。
- **发现者工具协调**:`pipeline/execution/discovery.py` 在单次 review 的单个 reviewer node 内按规范化工具参数执行 single-flight/cache；不同 task 首次复用完整结果，同一 ReAct 对话重复调用只返回短标记，最终 gathered context 也按相同 canonical key 去重，三个发现者之间及跨 review 不共享。只有未被大 diff 策略截断的完整新增文件 patch 才可代替 `get_file_content`。
- **工具响应投影**:Gateway 原始响应只进入 Evidence Artifact；三个 `inspect_*` 图谱工具通过确定性 `PayloadProjection` 向 Reviewer/Judge 提供 schema v2 摘要，`get_file_content` 仍提供完整代码。工具轨迹通道只流转 `ToolTraceRef`，不再把 `DiscoveryToolRecord.output/resolved_output` 写入 State；Evidence Ledger 的 Artifact 仍作为证据状态保留。HTML Trace 按 `payload_hash` 单份保存原文，事件通过 `call_id/artifact_id` 引用。
- **EvidenceVerifier(证据账本验证,零 LLM)**:审查员只从运行时捕获的 `<evidence_catalog>` 里选编号(`evidence_refs` 最多 3 条，patch=P01 自动绑定)，离开发现子图即绑定为内容寻址 artifact ID——LLM 无法伪造、改写或重新填写证据。Verifier 只证明 Artifact 真实、可用、属于候选范围：patch 摘要一致、图响应 schema/outcome/scope/coverage 护栏（`MAIN/TEST/GENERATED` 分类，生产查询不消费 TEST 关系，测试事实不能证明生产可达/影响/severity）、guard 注解扫描确定性反证、引用范围核对；`found + partial` 只保留正向事实，`indeterminate + partial` 进入 EvidenceGap，只有可恢复执行异常进入重放且重放后重新执行相同校验。
- **CouncilJudge(批量证据裁决)**:每批 ≤8 候选、最多 4 批并行，一次完成支持/反驳/去留/定级。Patch 可以证明局部代码机制，但不能自动证明跨文件调用、生产可达性或外部契约；定位事实（LOCATION）不能单独证明缺陷成立；未找到保护不等于证明保护不存在。输出经确定性合同校验（keep 必须引用 ≥1 支持事实、引用 ID 必须属于候选可见范围、supporting/counter 不得重叠、维护性候选不得 CRITICAL），违规重试/二分拆批，单候选最终失败 fail-closed 不输出。Judge 不补证、不按标签直定级，也不接受 LLM 直接选择危险等级。

审查员的"执行方式"抽成可插拔引擎(`pipeline/execution/engines.py`):`DirectEngine`(无工具基准)/ `ToolAgentEngine`(ReAct,基于 langchain v1 `create_agent`)。`ReviewerStage` 按 `tool_client` 是否存在分流。

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
    │   │   ├── config.py          # Settings:从环境变量/.env 读配置(含 Tool Server URL/Token)
    │   │   ├── models/schemas.py  # ★产品输出结构:Severity / Issue / ReviewResult / DiscoveredIssue(evidence_refs)
    │   │   ├── models/state.py    # ★LangGraph 顶层 ReviewState/ReviewerState 与 reducer
    │   │   ├── models/evidence.py # ★证据账本:Artifact/Catalog/Ref/Verifier/Judge 模型
    │   │   ├── models/council.py  # ★内部结构:CandidateIssue / Verdict / Trace/Stats
    │   │   ├── git/diff_collector.py  # 调系统 git 采集 diff + parse_changed_files(派生 allowed_files)
    │   │   ├── llm/client.py      # LLM 工厂(openai/Codex/mock)+ 重试 + mock 假数据
    │   │   ├── tools/             # ★工具调用(智能层侧)。tool_client(同步 HTTP)+ definitions(LangChain 工具)
    │   │   ├── pipeline/orchestration/    # ★LangGraph 图构建与管线入口
    │   │   │   ├── graph.py
    │   │   │   └── orchestrator.py
    │   │   ├── pipeline/tasks/            # ★任务拆分、DirectGate 与规模路由
    │   │   │   ├── task_builder.py
    │   │   │   └── scope.py
    │   │   ├── pipeline/symbols/          # ★Full task 变更位置到稳定项目符号的解析
    │   │   ├── pipeline/reviewers/        # ★三路发现者、工具协调与 prompt 构造
    │   │   ├── pipeline/planning/         # ★OCR 式 PlanUnit、Reviewer 与知识主题规划
    │   │   ├── pipeline/location/         # ★候选新增行定位校验与批量重定位
    │   │   ├── pipeline/evidence/         # ★证据账本:注册/绑定/目录渲染、健康检查/图护栏/异常重放、guard 扫描
    │   │   ├── pipeline/council/          # ★候选归并、裁决与过程指标
    │   │   ├── pipeline/summary/          # 可选变更摘要阶段
    │   │   ├── pipeline/execution/        # ★运行时执行、工具发现与并发控制
    │   │   │   ├── engines.py
    │   │   │   ├── discovery.py
    │   │   │   └── concurrency.py
    │   │   └── prompts/                   # Plan、三路发现、证据、裁决、摘要与知识主题
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
6. **`pipeline/orchestration/orchestrator.py:PipelineOrchestrator.run`** 是审查唯一门面,内部构建 `pipeline/orchestration/graph.py` 的 ADR-032 LangGraph:
   - `PRModeClassifier` 先按规模选择 whole-diff、file task 或 hunk task；所有规模都进入统一 task 管线。
   - TaskBuilder 后执行确定性 `TaskRoute(DirectGate)`；Direct task 独立直审，Full task 进入 `TaskSelection → Plan → ReviewPlan`。
   - `Plan` 按 PlanUnit 并发生成 Reviewer、审查重点和知识主题；LARGE 模式同文件 hunk 复用文件级 Plan。
   - `[Summary]` 对 TaskRank 选中范围产出可选变更摘要。
   - `SymbolResolution` 把选中 Full task 的变更行解析为只读 `TaskSymbolContext`。
   - `ReviewCouncil` 并行运行 task-scoped 发现者 Agent；没有匹配任务的 reviewer 记录 `no_tasks_routed`。发现结果在绑定候选 ID 前经过统一新增行定位校验，必要时按 task 批量重定位。
   - `CouncilCoordinator` 完成三路 fan-in 和保守归并。
   - `EvidenceVerifier → CouncilJudge` 完成证据账本验证(健康检查/图护栏/异常重放,零 LLM)与批量证据裁决(支持/反驳/去留/定级,合同校验 fail-closed)。
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
conda run -n codeguard python -m evals.runner --profile eval-codeguard-full --judge --runs 3  # 单 profile(完整档)
# 消融对照:分别用 eval-direct-diff / eval-council-diff / eval-council-codegraph 换掉上面 profile 名

# —— Java Gateway(services/gateway 工具服务)——
mvn package                # 跑单测 + 出 fat jar
mvn test                   # 只跑单测
java -jar ci-webhook/target/codeguard-gateway.jar  # 同 JVM 启动 CI(8080)/工具(9090)/LLM Proxy(9091)

# —— 真实 ReAct 审查(工具开档:先起 Java 工具服务,再设 URL)——
$env:CODEGUARD_TOOL_SERVER_URL="http://localhost:9090"
conda run -n codeguard python -m codeguard_agent review --repo <repo> --trace
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

`evals/` 用"带标注的真实仓库数据集 + 统计指标"量化审查质量。`selected-20-v2` 评测集(20 个真实 Java 仓库、115 个植入缺陷,含 Vul4J 真实 CVE)按 profile 对照:直接只看 diff / ReviewCouncil / Council+代码图谱 / 完整举证四档,只改变编排/图谱/举证能力,数据集与指标零改动。报告与 profile 定义见 `evals/README.md` 与 `evals/profiles.yaml`。

```bash
cd services/agent && pip install -e . pyyaml
python -m evals.runner --profile eval-codeguard-full --runs 1   # 完整档单次
```

核心指标包括 Precision/Recall/F1、稳定/最差轮 Recall、检出集合 Jaccard、clean 误报与报告膨胀比。命中匹配走确定性口径(`evals/recall_analyzer.py`:file+行号±10 硬命中 / 描述 token 重叠≥2 软命中)。

### 环境变量(完整列表见 `.env.example`)

| 变量 | 默认 | 说明 |
|---|---|---|
| `CODEGUARD_PROVIDER` | `openai` | `openai` / `claude` / `mock` |
| `CODEGUARD_MODEL` | 按 provider 回退 | 留空自动选默认模型 |
| `CODEGUARD_API_KEY` | 空(Compose 必填) | openai/claude 必填 |
| `CODEGUARD_IMAGE_TAG` | `latest` | Compose 部署使用的 `ghcr.io/baiyun689/codeguard` 镜像标签 |
| `CODEGUARD_HOST_PORT` | `9090` | Compose 发布到宿主机的 Webhook 端口；映射到容器内 CI 服务 8080 |
| `CODEGUARD_TOOL_HOST_PORT` | `9092` | Compose 仅绑定 `127.0.0.1` 的 Tool Server 宿主机端口；映射到容器内 9090 |
| `CODEGUARD_TOOL_SERVER_TOKEN` | 必填 | Agent 与 Tool Server 的内部请求 Token；缺失时 Gateway 拒绝启动 |
| `CODEGUARD_TOOL_ALLOWED_ROOTS` | Compose 固定 | 允许 Tool Server 创建 Git 会话的工作区父目录列表 |
| `CODEGUARD_WEBHOOK_SECRET` | 空(Compose 必填) | GitHub App webhook HMAC 验签密钥 |
| `CODEGUARD_GITHUB_APP_ID` | 空(Compose 必填) | 用于 installation 认证和结果回写的 GitHub App ID |
| `CODEGUARD_GITHUB_PRIVATE_KEY_FILE` | `./secrets/github-app.pem` | GitHub App 私钥的宿主机路径；Compose 以只读 secret 挂载 |
| `CODEGUARD_GITHUB_TOKEN` | 空 | 私有仓库 clone 使用的只读 token；公开仓库无需设置 |
| `CODEGUARD_WEBHOOK_RATE_LIMIT` | `0.5` | 单实例每秒 webhook 许可数；`0` 表示不限制 |
| `CODEGUARD_API_BASE_URL` | 空 | 代理 / 兼容端点(如 DeepSeek)填 |
| `CODEGUARD_STRUCTURED_METHOD` | `function_calling` | 结构化输出方式 |
| `CODEGUARD_DISABLE_THINKING` | `false` | 用 DeepSeek 推理模型时设 `true` |
| `CODEGUARD_MAX_RETRIES` | `3` | LLM 调用重试次数 |
| `CODEGUARD_ENABLE_SUMMARY` | `true` | ADR-032 选中范围摘要开关;关闭则 ReviewPlan 后直接进入 SymbolResolution |
| `CODEGUARD_EVIDENCE_MODE` | `full` | 证据开关;`off` 跳过取证,候选由 DirectJudge 直接终审(无证据链消融基线档) |
| `CODEGUARD_MAX_REVIEW_TASKS` | `100` | 仅作为大 diff 的更严格总任务上限 |
| `CODEGUARD_MAX_TASKS_PER_FILE` | `10` | 仅作为大 diff 的更严格单文件上限 |
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

- **结果可能是 `None`**:`with_structured_output(...).invoke()` 在模型没正确发起工具调用时返回 `None`。审查引擎(`pipeline/execution/engines.py`)已兜底成空结果——**任何新写的、消费 LLM 结构化输出的代码都要做同样的 None 防御。**
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
