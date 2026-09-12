# Codeguard

简体中文 | [English](README.en.md)

AI Pull Request 代码审查系统，结合项目级代码事实分析发现代码变更中的具体问题，并通过 GitHub Checks 和 PR 评论反馈结果。

Codeguard 由 Python Agent 和 Java Gateway 组成，提供受控审查编排、代码事实分析、证据验证和 GitHub 集成能力。

默认采用变更驱动的有界审查流程。

## 功能特性

- 围绕变更上下文进行源码和项目关系分析，输出可定位、可追溯的问题报告。
- 先准备变更相关上下文，再按需深入调查相关源码和关系。
- 基于 Java AST、符号和调用图的代码事实工具。
- Evidence Ledger、确定性证据验证和批量结果裁决。
- 多模型路由、限流、熔断、重试和故障降级。
- GitHub Webhook、Check Run、行级标注和 PR 评论集成。
- 任务持久化、Prometheus 指标和 Docker Compose 部署。

## 工作原理

Codeguard 以 PR 变更为中心组织审查。系统先将 Diff 按文件或 hunk 切分为任务，再把变更行定位到对应的 Java symbol，并为每个审查组准备有限的源码和关系上下文。

```text
PR Diff → 任务切分 → 符号定位 → 受控调查
        → 证据验证 → 结果裁决 → 结果合并 → 审查报告
```

受控 Reviewer 在隔离的变更上下文中阅读代码，并根据证据缺口按需读取 symbol 源码或查询项目关系。工具调用受到 symbol 来源、查询范围、预算和超时限制；返回内容进入证据账本，经过验证和裁决后生成最终报告。信息不足时保留未完成状态，不将未查询到关系解释为不存在。

### 整体架构

```mermaid
flowchart LR
    Input[代码变更<br/>本地项目 / GitHub PR]
    Gateway[Java Gateway]
    Webhook[CI Webhook<br/>接入与调度]
    Agent[Python Agent<br/>审查编排与推理]
    Proxy[LLM Proxy<br/>模型路由]
    Tools[Tool Server<br/>代码事实与沙盒]
    Output[审查结果<br/>报告 / Check Run / 评论]

    Input --> Gateway
    Gateway --> Webhook
    Webhook --> Agent
    Agent <--> Proxy
    Agent <--> Tools
    Agent --> Output
```

模块职责：

- **CI Webhook**：接收 GitHub 事件，创建和调度审查任务。
- **Python Agent**：组织审查任务，执行变更驱动的有界审查，并完成证据验证与结果裁决。
- **LLM Proxy**：统一管理模型访问和提供商路由。
- **Tool Server**：在沙盒内提供文件、符号、AST 和调用关系等代码事实。

### Agent 审查工作流

PR 规模只分两档：`NORMAL`（文件数 ≤15 且 diff ≤60,000 字符）按文件构建任务；超过任一阈值则进入 `LARGE`，按 hunk 构建任务。hunk 数仅作统计，不再划分第三档。

下图展示从 PR Diff 到最终报告的完整过程，并展开 `controlled_review` 内部的调查循环。Direct 与 Full 是任务类别：先处理 Direct 任务并暂存结果，再处理选中的 Full 任务；没有对应任务的阶段直接跳过，不调用模型。

```mermaid
flowchart TD
    Diff[PR Diff] --> Tasks[按规模构建文件或 hunk 任务]
    Tasks --> Route[DirectGate：区分低风险与 Full 任务]
    Route --> Direct[低风险任务：独立直审并暂存结果]
    Direct --> Select[选择 Full 任务并记录覆盖限制]
    Select --> Symbols[解析变更位置，获取真实符号]

    subgraph Review[受控审查 · controlled_review]
        Groups[按声明分组，隔离本组 diff] --> Context[预取源码与有限一跳关系]
        Context --> Decide[模型评估已有证据]
        Decide -->|缺少决定性事实| Tools[执行 1～2 个工具查询]
        Tools -->|返回证据、新增符号与剩余预算| Decide
        Decide -->|可以结束| Outcome[提交候选或无发现、未完成状态]
        Decide -->|工具或轮次上限、无进展| Conclude[最多一次无工具收口]
        Conclude --> Outcome
        Outcome --> Bind[校验本组定位，绑定证据账本]
    end

    Symbols --> Groups
    Bind --> Coordinator[汇总与归并候选]
    Coordinator --> Verify[Verifier：确定性证据校验]
    Verify --> Judge[Judge：判断缺陷成立与定级]
    Judge --> Merge[根因合并，并合入 Direct 结果]
    Direct -. 暂存的独立结果 .-> Merge
    Merge --> Result[最终审查报告与完成状态]
```

| 节点 | 职责 |
|---|---|
| `classify_mode` | 按 diff 规模决定文件或 hunk 粒度，不调用模型。 |
| `file_task_builder` / `diff_task_builder` | 构建文件任务 / hunk 任务，保存变更行与删除锚点。 |
| `task_route` | DirectGate 确定性区分低风险文档/注释任务与 Full 任务。 |
| `direct_task_review` | 仅处理 Direct 任务；节点内完成直审、定位和裁决，暂存独立结果。 |
| `task_selection` | 选择 Full 任务并记录大 diff 截断，不调用风险分类模型。 |
| `symbol_resolution` | 通过 Gateway 批量解析变更位置，获得真实符号和导航上下文。 |
| `controlled_review` | 按变更声明分组、预取上下文，运行有界 ReAct；节点内完成候选定位与账本绑定。 |
| `council_coordinator` | 汇总和归并候选，准备后续验证的候选集合。 |
| `evidence_verifier` | 零 LLM 校验证据内容、revision、符号范围及图谱合同；仅对可恢复失败重放。 |
| `council_judge` | 按批判断候选是否由有效证据支持，给出去留和定级；不补查工具。 |
| `causal_merge` | 对保留候选做根因分析与保守合并，合入 Direct 结果。 |

图中的分组、预取、定位和绑定都由运行时完成，不是额外的模型规划节点。每组独立运行 ReAct：第一轮可以直接提交结果，也可以继续查询；提交结果后本组结束。工具或轮次上限、无进展触发有界收口，超时和协议失败保留未完成或失败状态，不视为审查通过。虚线表示 Direct 结果在最终合并时使用，不表示并行子图。

| 工具 | 使用者与用途 |
|---|---|
| `resolve_change_context` | 运行时专用，将变更位置解析成符号；不向 Reviewer 暴露。 |
| `read_symbol` | Reviewer 读取已知符号的有界源码，可分页。 |
| `query_relations` | Reviewer 查询 callers、callees、field_readers、field_writers、implementations、overrides、parents、children、type_users、type_references、entrypoints，沿返回的真实 ID 深入。 |

最终报告提供根因与代码来源，内部证据编号由运行时管理。图谱展示静态事实，不保证动态调用关系完备；预算或证据不足会留下未完成状态。

### 审查提示词

主提示词位于 [change-review.txt](services/agent/src/codeguard_agent/prompts/controlled/change-review.txt)，按角色与目标、输入与证据边界、审查与决策、工具使用、结束条件、输出合同组织。

[controlled 提示词目录](services/agent/src/codeguard_agent/prompts/controlled)中的辅助文件分别负责任务范围、预算通知、探索/结论阶段及错误反馈。结构化字段以运行时绑定的 schema 为准，调用预算、超时和终止约束由代码执行。提示词分文件组织不增加工作流节点或模型调用。

## 使用 Docker Compose 快速开始

准备条件：

- Docker Engine 和 Docker Compose v2
- 已安装到待审查仓库的 GitHub App
- GitHub 能访问的公网 HTTPS Webhook 地址
- 所配置 LLM 服务商的 API Key

克隆仓库、创建部署配置和密钥目录：

```bash
git clone https://github.com/baiyun689/codeguard.git
cd codeguard
cp .env.example .env
mkdir -p secrets
```

PowerShell：

```powershell
git clone https://github.com/baiyun689/codeguard.git
Set-Location codeguard
Copy-Item .env.example .env
New-Item -ItemType Directory -Force secrets | Out-Null
```

编辑 `.env`，至少填写：

```dotenv
CODEGUARD_WEBHOOK_SECRET=replace-with-a-long-random-secret
CODEGUARD_GITHUB_APP_ID=123456
CODEGUARD_API_KEY=replace-with-your-provider-key
CODEGUARD_GITHUB_PRIVATE_KEY_FILE=./secrets/github-app.pem
```

将 GitHub 下载的 App 私钥保存为 `./secrets/github-app.pem`。Compose 会以只读方式挂载该文件，并自动设置容器内的 `CODEGUARD_GITHUB_PRIVATE_KEY_FILE`。

启动稳定版本。默认镜像为 `ghcr.io/baiyun689/codeguard:latest`：

```bash
docker compose up -d
```

在 Bash 中运行持续发布的 `edge` 镜像：

```bash
CODEGUARD_IMAGE_TAG=edge docker compose up -d
```

PowerShell：

```powershell
$env:CODEGUARD_IMAGE_TAG = "edge"
docker compose up -d
```

从当前源码构建并启动，而不是拉取已发布镜像：

```bash
docker compose up -d --build
```

启动完整可观测性栈：

```bash
docker compose --profile observability up -d --build
```

启动后访问 `http://localhost:3000`。Grafana 会自动加载 Prometheus 数据源和
**Codeguard · Review & LLM Operations** 看板；匿名用户可只读查看，管理员默认账号为
`admin / codeguard`，公开部署前应通过 `GRAFANA_ADMIN_USER` 和
`GRAFANA_ADMIN_PASSWORD` 修改，并设置 `GRAFANA_ANONYMOUS_ENABLED=false`。
Prometheus 位于 `http://localhost:9093`。

看板覆盖以下运行指标：

- 审查管线：活动任务、成功率、吞吐和 P95 耗时；
- AST / Evidence 工具：按工具与结果统计调用速率；
- LLM 韧性：按 Provider 呈现调用量、P95 耗时、重试、fallback 和熔断器状态。

`ops/prometheus/alerts.yml` 预置服务不可用、审查失败率、慢审查、工具错误率、
LLM 失败率和熔断器开路告警规则。Prometheus 默认保留 15 天数据；Grafana 与
Prometheus 数据均使用命名卷持久化。

### 本地 Web 审查界面

启动 `codeguard` 服务后，可访问 `http://localhost:8501` 打开本地审查界面。
在界面中填写宿主机上的 Git 项目根目录，选择 Diff 基线后即可开始审查。界面默认使用完整的 `controlled` 受控审查管线；代码图谱、Evidence Ledger、Judge
和语义合并不会被拆成相互独立的开关；报告和 Agent Trace
作为展示选项提供。

```powershell
docker compose up -d --build codeguard
# 浏览器打开 http://localhost:8501
```

在 `.env` 中设置项目父目录，Docker 会将它挂载到 UI：

```dotenv
CODEGUARD_PROJECTS_DIR=E:/workspace/review-projects
```

例如填写 `E:\\workspace\\demo-project`。项目最好保留 Git 历史，
这样可以从下拉框选择 `HEAD`、`main` 或其他本地分支作为 Diff 基线。

容器内用于接收 GitHub Webhook 的 CI 服务固定监听 `8080`；Tool Server 与 LLM Proxy
分别监听内部端口 `9090` 和 `9091`。Webhook 通过 `CODEGUARD_HOST_PORT` 发布；
Tool Server 仅绑定宿主机回环地址，通过 `CODEGUARD_TOOL_HOST_PORT` 提供给本机
Python Agent：

```dotenv
CODEGUARD_HOST_PORT=8080
CODEGUARD_TOOL_HOST_PORT=9092
# 随机长字符串；本机 Agent 连接 Tool Server 时也必须使用同一值。
CODEGUARD_TOOL_SERVER_TOKEN=请自行生成随机值
```

Tool Server 不应暴露到公网；即使仅绑定回环地址，所有工具请求仍必须携带
`X-Codeguard-Tool-Token`。Gateway 的 Webhook 映射端口提供明文 HTTP，不直接提供
TLS。生产环境必须由反向代理终止 HTTPS，并将 `/webhooks/github` 转发到该宿主机端口；
公开 Webhook 地址应为 `https://your-host.example/webhooks/github`。不要将 GitHub
Webhook 直接指向映射端口。

## 项目展示

下面展示一次本地审查和一次 GitHub App 审查的主要产物。

### 本地审查界面

通过 Web UI 填写待审查项目根目录、选择 Diff 基线，并选择是否生成 Markdown 报告和 Agent Trace。

![本地审查界面](docs/showcase/local-review-ui.png)

### Agent Trace

Trace 展示实际执行的任务路由、SymbolResolution、变更分组、源码预取和 Reviewer 的查询/结论，以及
证据验证、Judge 和语义合并，并可展开查看工具调用、预算、证据引用与节点输入输出。

![Agent Trace 执行流](docs/showcase/agent-trace.png)

### Markdown 审查报告

本地审查完成后可以生成 Markdown 报告，按严重级别、问题类型、文件和行号汇总最终保留的问题。

示例报告：[review-report-example.md](docs/showcase/review-report-example.md)

### GitHub App 审查结果

GitHub App 接收 Pull Request Webhook 后，会将审查结果回写到 Check Run，并在变更文件对应位置发布行内评论；无法精确定位的问题会进入汇总结果。

![GitHub App 审查结果](docs/showcase/github-app-review.png)

## 配置 GitHub App

1. 在 GitHub 打开 **Settings > Developer settings > GitHub Apps > New GitHub App**。
2. 将 Webhook URL 设置为 `https://your-host.example/webhooks/github`。
3. 创建 Webhook Secret，并将相同值写入 `CODEGUARD_WEBHOOK_SECRET`。
4. 设置以下 Repository permissions：
   - **Checks：** Read and write
   - **Contents：** Read-only
   - **Pull requests：** Read and write
   - **Metadata：** Read-only（GitHub 会自动授予）
5. 在 Webhook events 中订阅 **Pull request**。Codeguard 会处理 `opened`、`reopened` 和 `synchronize` 事件。
6. 创建 App，将 **App ID** 写入 `CODEGUARD_GITHUB_APP_ID`，生成私钥并保存到 `./secrets/github-app.pem`。
7. 将 App 安装到需要 Codeguard 审查的组织或仓库。

公开仓库不需要额外 Token 即可克隆。私有仓库需要在 `.env` 中设置具有仓库内容读取权限的 `CODEGUARD_GITHUB_TOKEN`；当前克隆流程不会自动复用 GitHub App installation token。

Webhook 端点必须能通过 HTTPS 被 GitHub 访问。如果 Codeguard 位于反向代理后，请将 `/webhooks/github` 转发到 `CODEGUARD_HOST_PORT` 指定的宿主机端口。

## 配置 LLM

### LLM Gateway 模式（推荐）

所有 LLM 调用经本地 LLM Proxy 统一路由，Python Agent 无需持有提供商密钥：

```dotenv
CODEGUARD_PROVIDER=openai
CODEGUARD_API_BASE_URL=http://localhost:9091/v1
CODEGUARD_API_KEY=dummy           # Gateway localhost 不验证，但 ChatOpenAI 要求非空
CODEGUARD_MODEL=deepseek-chat     # 或其他 model 名，Gateway 按 model 路由
```

LLM Proxy 通过 `llm-proxy-config.yml` 配置多提供商路由和韧性策略。Compose 部署时该配置已内置。

### 直连模式

绕过 Gateway，Python Agent 直连 LLM 提供商：

```dotenv
CODEGUARD_PROVIDER=openai
CODEGUARD_MODEL=gpt-4o-mini
CODEGUARD_API_KEY=replace-with-your-key
```

使用 OpenAI 兼容接口时，还需设置：

```dotenv
CODEGUARD_API_BASE_URL=https://api.deepseek.com
CODEGUARD_STRUCTURED_METHOD=function_calling
```

设置 `CODEGUARD_PROVIDER=claude` 可使用 Anthropic。设置 `CODEGUARD_PROVIDER=mock` 可在不调用真实模型的情况下验证管线，仅适合开发检查，不应作为生产审查模式。

全部模型、审查预算和运行参数参见 [`.env.example`](.env.example)。

## 验证部署

检查容器状态和日志：

```bash
docker compose ps
docker compose logs -f codeguard
```

使用默认宿主机端口检查就绪状态：

```bash
curl --fail http://localhost:9090/health/ready
```

随后可在 GitHub App 设置页发送测试 Delivery，或在已安装 App 的仓库中创建、更新 Pull Request。有效的 `pull_request` 事件会被异步接收，审查结束后将生成 Codeguard Check Run。

## 本地 CLI 使用

Python Agent 可直接审查本地 Git Diff，无需接入 GitHub：

```bash
cd services/agent
python -m venv .venv
source .venv/bin/activate
pip install -e .
export CODEGUARD_API_KEY=replace-with-your-key
python -m codeguard_agent review --repo /path/to/repository --base HEAD
```

PowerShell：

```powershell
Set-Location services/agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
$env:CODEGUARD_API_KEY = "replace-with-your-key"
python -m codeguard_agent review --repo C:\path\to\repository --base HEAD
```

设置 `CODEGUARD_PROVIDER=mock` 可进行零成本管线冒烟测试。如果本地 Agent 需要通过独立运行的 Gateway 获取仓库上下文工具，请设置 `CODEGUARD_TOOL_SERVER_URL=http://localhost:9090`。

加 `--report` 参数可在审查结束后于 `<repo>/reports/` 生成带时间戳的 Markdown 报告（severity 统计 + 按严重级分组的问题列表 + 根因、来源位置和 diff 代码片段）。GitHub App 的 CI 模式结果走 Check Runs，不生成本地报告。

## 配置项

部署配置：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `CODEGUARD_IMAGE_TAG` | `latest` | `ghcr.io/baiyun689/codeguard` 下的镜像标签 |
| `CODEGUARD_HOST_PORT` | `9090` | 映射到容器 CI Webhook 端口 `8080` 的宿主机端口 |
| `CODEGUARD_TOOL_HOST_PORT` | `9092` | 仅绑定 `127.0.0.1`、映射到容器 Tool Server 端口 `9090` 的宿主机端口 |
| `CODEGUARD_TOOL_SERVER_TOKEN` | 必填 | Python Agent 与 Tool Server 的内部共享 Token |
| `CODEGUARD_TOOL_ALLOWED_ROOTS` | Compose 固定 | 可创建工具会话的 Git 工作区父目录，逗号分隔 |
| `CODEGUARD_WEBHOOK_SECRET` | 必填 | 校验 GitHub Webhook 签名的 Secret |
| `CODEGUARD_GITHUB_APP_ID` | 必填 | 用于 installation 认证的 GitHub App ID |
| `CODEGUARD_GITHUB_PRIVATE_KEY_FILE` | `./secrets/github-app.pem` | Compose 挂载的 App 私钥宿主机路径 |
| `CODEGUARD_GITHUB_TOKEN` | 空 | 克隆私有仓库所需的仓库读取 Token |
| `CODEGUARD_PROVIDER` | `openai` | LLM 服务商：`openai`、`claude` 或 `mock` |
| `CODEGUARD_MODEL` | 服务商默认值 | 模型名称 |
| `CODEGUARD_API_KEY` | Compose 中必填 | LLM 服务商 API Key |
| `CODEGUARD_API_BASE_URL` | 空 | 可选的兼容 API 地址 |
| `CODEGUARD_MAX_CONCURRENT_REVIEWS` | `2` | 当前实例允许并发执行的最大审查数 |
| `CODEGUARD_REVIEW_TIMEOUT_SECONDS` | `600` | Python 审查进程超时时间 |
| `CODEGUARD_RETRY_DELAY_SECONDS` | `30` | 可重试任务重新调度前的等待时间 |
| `CODEGUARD_SHUTDOWN_GRACE_SECONDS` | `30` | 停机时等待活动任务结束的最长时间 |
| `CODEGUARD_WEBHOOK_RATE_LIMIT` | `0.5` | 每秒接收的 Webhook 请求数；`0` 表示关闭限流 |
| `CODEGUARD_GRAPH_CACHE_MAX_SNAPSHOTS` | `4` | 跨会话保留的完整项目快照上限 |
| `CODEGUARD_GRAPH_CACHE_TTL_MINUTES` | `30` | 项目快照访问后过期时间 |
| `CODEGUARD_GRAPH_BUILD_TIMEOUT_SECONDS` | `120` | 全项目 AST 与语义图构建超时 |
| `CODEGUARD_DISCOVERY_MODE` | `controlled` | 受控审查模式：`controlled`（变更驱动有界审查）或 `direct`（无工具对照） |
| `CODEGUARD_CONTROLLED_MAX_PATH_DEPTH` | `3` | controlled 路径最大深度（最大 3） |
| `CODEGUARD_CONTROLLED_EXECUTE_CONCURRENCY` | `3` | controlled 同一 task 内独立证据步骤的最大并发数；`1` 为串行 |
| `CODEGUARD_CONTROLLED_SUBTASK_MAX_TOOL_CALLS` | `10` | 单个调查子任务的工具调用上限（可配置） |
| `CODEGUARD_CONTROLLED_SUBTASK_MAX_ROUNDS` | `6` | 单个调查子任务的 React 轮数上限（可配置） |
| `CODEGUARD_CONTROLLED_SUBTASK_TIMEOUT_SECONDS` | `120` | 单个调查子任务的执行超时预算 |
| `CODEGUARD_CONTROLLED_TASK_MAX_TOOL_CALLS` | `32` | 单 task 所有调查子任务共享的工具调用上限（可配置） |
| `CODEGUARD_CONTROLLED_MAX_SUBTASKS_PER_TASK` | `8` | 单 task 的调查子任务上限 |
| `CODEGUARD_TOOL_SERVER_PROJECT_ROOT` | 空 | 宿主 Agent 连接 Docker Gateway 时的容器项目根路径（Compose 通常为 `/workspace/projects`） |

Compose 会设置打包部署所需的容器内部路径和端口，并在未显式设置时将
`CODEGUARD_API_BASE_URL` 指向容器内的 LLM Proxy。除非维护自定义部署，否则不要修改
`CODEGUARD_CI_PORT`、`CODEGUARD_TOOL_SERVER_PORT`、`CODEGUARD_TOOL_SERVER_URL`、
`CODEGUARD_LLM_PROXY_PORT`、`CODEGUARD_JOB_DB_URL` 或 `CODEGUARD_WORKSPACE_DIR`。

## 运维与可观测性

Codeguard 当前只支持单 Gateway 实例。MySQL 持久化和调度器可以在该实例内恢复任务，但尚未实现多实例选主、分布式锁或共享工作区协调。不要将 Compose 服务扩容到一个以上副本。

Java Gateway 三个服务各监听独立端口：

| 服务 | 默认端口 | 用途 |
|---|---|---|
| CI Webhook | 8080 | GitHub Webhook 接入 + 审查调度 |
| Tool Server | 9090 | Agent 工具服务 + 文件沙箱 |
| LLM Proxy | 9091 | OpenAI 兼容 LLM 代理 |

运维端点（所有服务均提供 `/health` 和 `/health/live`）：

| 端点 | 含义 |
|---|---|
| `GET /health` | 兼容健康检查端点，报告进程存活状态 |
| `GET /health/live` | 存活探针 |
| `GET /health/ready` | CI 服务：检查 MySQL（连接 ping）、调度器和 Python 初始化状态；不可用时返回 `503` |
| `GET /metrics` | Prometheus 文本格式指标（CI、Tool Server 和 LLM Proxy 均提供） |

Compose 将 MySQL 任务数据持久化到 `mysql-data` 卷（独立 MySQL 容器），将按 SHA 隔离的临时审查工作区保存到 `job-workspaces`。使用 `docker compose down` 停止服务；只有在明确需要删除持久化任务状态和工作区时才添加 `--volumes`。

镜像发布规则：

- 推送到 `master` 时发布 `edge`
- `v1.2.3` 等语义化版本标签发布稳定镜像
- 最新语义化版本同时发布为 `latest`

GHCR 首次发布镜像后，仓库所有者可能需要在 GitHub Package 设置中手动将可见性改为 **Public**，否则未认证用户无法通过 `docker compose up -d` 拉取镜像。

## 开发

Python 检查：

```bash
cd services/agent
uv sync --group dev
uv run pytest tests/ -q
uv run ruff check src/
uv run mypy src/
```

Java 检查：

```bash
cd services/gateway
mvn --batch-mode verify     # 构建全部四个子模块：shared、tool-server、ci-webhook、llm-proxy
```

容器构建：

```bash
docker build -t codeguard:local .
```

真实质量评测使用 `selected-20-v2` 当前启用的 15 个精选真实 Java 仓库；正式标答以各 case 的 `expected` 为准，另有
`planted-bugs.diff` 生成的 hunk 诊断记录用于辅助分析。评测 profile 覆盖 direct、代码图谱和
变更驱动的有界发现，具体 Recall、Precision、F1 与稳定性结果以评测报告为准。评测框架、profile 定义与报告见
[`services/agent/evals/README.md`](services/agent/evals/README.md)。

## 参与贡献

欢迎提交 Issue 和 Pull Request。代码改动应保持聚焦、添加确定性测试，并在提交前运行相应的 Python、Java 和容器检查。

Commit Message 使用 Conventional Commits：

```text
<type>(<scope>): <description>
<type>: <description>
```

`scope` 可选。常用类型包括 `feat`、`fix`、`docs`、`refactor`、`test` 和 `chore`。

## 许可证

Codeguard 使用 [MIT License](LICENSE)。
