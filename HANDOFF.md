# 交接清单（2026-06-13）

> 当前进度快照。下次接手从「下次从哪开始」一节读起即可。

## 阶段 3 第一步(工具调用 Agent · 双语言登场)—— 已完成 ✅

目标:把审查员从"单次直连"升级成**会用工具的 ReAct Agent**,引入 Java 护栏层,只落地第一个工具 `get_file_content`,跑通整条双语言链路。openspec change:`phase3-tool-calling-agent`(proposal/design/specs/tasks 齐备并 validate)。

| 模块 | 内容 | 状态 |
|---|---|---|
| Java 工具服务 | Javalin Tool Server:`AgentTool`/`ToolResult`/`AgentContext` + `ToolRegistry` + 通用分发 `POST /api/v1/tools/{name}` + `/health`;Maven fat jar 独立启动 | ✅ 11 单测 + 端点 smoke 全过 |
| 工具会话层 | `ToolSessionManager`(create/destroy + `X-Session-Id` + TTL);为后续按 project 共享重资源预留挂载点(本期不填充) | ✅ |
| get_file_content + 护栏 | `FileAccessSandbox`:防穿越 + 限 diff 范围 + 大小上限;四类拒绝结构化返回 | ✅ |
| Python 工具客户端 | 同步 `ToolClient`(httpx)+ `get_file_content` 工具定义(LangChain `StructuredTool`) | ✅ |
| ReAct 引擎与分流 | `ToolAgentEngine`(v1 `create_agent` + `response_format`)vs `DirectEngine`(直连基准);`ReviewerStage` 按 `tool_client` 分流 | ✅ |
| 管线接线 | diff→改动文件集合纯函数;`PipelineContext` 增 repo_path/allowed_files/tool_client;CLI 建/销会话 | ✅ |
| 配置 | `CODEGUARD_TOOL_SERVER_URL` + `.env.example` | ✅ |
| 评测两档开关 | runner `--tools` harness 就位 | ✅(见下方限制) |

## 这轮关键结论(都已写进 ADR-009 / design.md)

- **职责边界钉死(统领后续所有阶段)**:Python 编排/推理,Java 护栏/地面真值;四条不变量见 ADR-009 / design.md D0。后续加任何功能(工具/记忆/换编排)都能机械归位。
- **工具价值已端到端定性坐实**:构造"被改方法调用了 diff 外定义的 `sanitize`"的用例,真实 DeepSeek 下**两个审查员自主调用 `get_file_content` 读整文件并据其实现推理**(Java 日志可证)。这就是阶段 3 的核心命题。
- **量化对照本期测不出,如实记(不硬凑)**:评测数据集是合成 diff、磁盘无对应文件,工具档下 `get_file_content` 必"文件不存在"——对照是结构性无效,故不跑会误导的数字(同 ADR-004/008 原则)。
- **实现期修正**:ReAct 框架按环境实装(langchain 1.3)从 0.3 的 `AgentExecutor` 改用 v1 `create_agent`,反而更优(内置结构化收口 + 对齐阶段4 LangGraph)。详见 design.md D5。

## 复盘(理解了什么 / 踩了什么坑 / 下一步)

- **理解**:Agent ≠ 更强的 prompt,而是"能自主获取上下文"。亲眼看到审查员为搞清 `sanitize` 到底做了什么而去读整文件,这一刻 Agent 的价值才具体。双语言的合理性也清楚了:Python 适合编排不确定性,Java 适合做确定性护栏与重计算。
- **坑**:① 实装 langchain 是 v1,0.3 的 agent API 已移除——规划阶段对依赖版本的假设要在落地时校验。② 后台进程端口复用:`kill %1` 跨 Bash 调用失效,旧服务占着 9090 导致"改了代码却没生效",靠按端口 kill 才定位(教训:验证前先确认跑的是新构建)。③ 评测数据集与新能力错配——合成 diff 喂不了文件工具。
- **下一步**:补 **repo-backed 评测用例**(量化工具增益的前提);再沿通用协议 + 会话接缝逐个加重型工具(method/call-graph/RAG)。

## 阶段 2(管线化)—— 已完成 ✅(存档)

并行审查 → 聚合去重 → 误报过滤。指标(`evals/reports/pipeline.md`,默认管线 = 误报验证关):
Precision ≈ 0.40 / Recall ≈ 0.97 / clean 误报率 ≈ 0.67(3 跑);开异源 FP 验证可达 P 0.459 / 误报率 0.417。
关键决策见 ADR-005~008(评测台校准、多标答、prompt 判例、两段式误报过滤)。

## 跑测速查

```powershell
# Java 工具服务:打包 + 单测 + 启动
cd services/gateway; mvn package          # 跑 11 个单测 + 出 fat jar
java -jar target/codeguard-gateway.jar    # 默认端口 9090(CODEGUARD_TOOL_SERVER_PORT 可覆盖)

# Python 单测(工程正确性,应 58 passed)
cd services/agent; conda run -n codeguard --no-capture-output python -m pytest tests/ -q

# 真实 ReAct 审查(先起工具服务,再设 URL)
$env:CODEGUARD_TOOL_SERVER_URL="http://localhost:9090"
conda run -n codeguard --no-capture-output python -m codeguard_agent review --repo <repo> --mode pipeline

# 评测(默认管线);--judge 开裁判;--tools 工具开档(需起服务 + 配 URL;合成数据集下工具读不到文件)
conda run -n codeguard --no-capture-output python -m evals.runner --mode pipeline --judge --runs 3
```

> 裁判 / FP 验证用独立模型:`.env` 配 `CODEGUARD_JUDGE_*`(本机用通义千问 qwen,推理模型需 `CODEGUARD_JUDGE_DISABLE_THINKING=true`)。

## 👉 下次从哪开始

1. **补 repo-backed 评测用例**(最该先做):带真实多文件仓库的用例,才能量化"工具开 vs 关"的真实增益——本期已定性证明工具有效,量化就差这个数据集。
2. **逐个加重型工具**:`get_method_definition`(JavaParser AST)→ `get_call_graph` → `semantic_search`(RAG),沿通用协议 + 会话接缝叠加;届时按需在会话层填"按 project 共享重资源"。
3. 工具利用率/耗时纳入评测报告。

## 衍生待办(散落在各 ADR)

- 评测报告头部仍写死"阶段1 baseline"(实为 pipeline),待修(`evals/report.py`)。
- 级别准确率长期 ~0.6,模型系统性高判 severity(ADR-004 老账,待数据集扩量后复查)。
- `.env.example` 已补 `CODEGUARD_TOOL_SERVER_URL`;`CODEGUARD_JUDGE_*` 仍未进示例,需要时补。
