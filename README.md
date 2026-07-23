# Codeguard

简体中文 | [English](README.en.md)

基于风险感知多 Agent 分析的 AI Pull Request 审查系统。

Codeguard 接收 GitHub Pull Request 事件，由 Python 审查委员会分析本次代码变更，再通过 GitHub Check Run 和 PR 评论输出结构化问题。Java Gateway 负责 Webhook 校验、任务持久化、独立工作区、重试与代码访问护栏。

## 功能特性

- 从安全、行为正确性和可维护性三个维度审查 Pull Request。
- 先按风险对变更片段进行路由，再由任务级专业审查员分析。
- 向审查员显式提供当前任务的摘要、风险画像、AST、敏感 API、调用方和代码指标，并标明来源、范围、截断及不可用原因。
- 在单个审查员范围内合并并发和重复工具调用，避免重复文件读取和重复上下文注入；不同审查员保持隔离。
- 在最终裁决前规划并收集支持证据、反证和严重性证据。
- 通过 GitHub Check Run、Diff Annotation 和高置信度严重问题评论反馈结果。
- 校验 Webhook 签名，并按仓库、PR 和 Commit SHA 对任务去重。
- 使用 H2 持久化任务，进程重启后可恢复未完成任务。
- 提供存活、就绪和 Prometheus 指标端点。
- 通过 Docker Compose 在同一容器中运行 Python Agent 与 Java Gateway。

## 工作原理

```text
GitHub pull_request Webhook
        |
        v
Java Gateway
  校验签名 -> 持久化/去重 -> 调度 -> 准备 SHA 独立工作区
        |
        v
Python Agent
  Diff 任务 -> 风险路由 -> 专业审查 -> 证据收集 -> 委员会裁决
        |
        v
GitHub Check Run、Diff Annotation 和 PR 评论
```

Python Agent 负责审查推理与编排。Java Gateway 负责确定性事实和运行护栏；Java 侧不调用 LLM，也不判断候选问题是否成立。

发现阶段的 system prompt 只定义稳定的上下文语义和工具调用门槛；每个 task 的实际 patch、风险画像、预取事实、缺失状态及标签知识通过 user 消息动态注入。上下文已经回答候选所需事实时，发现者必须略过工具。单个发现者的并发 task 共享一次审查内的工具结果，但不会与另外两个发现者或下一次审查共享。

三路发现者只按 ID 汇集原始候选。CouncilCoordinator 在 fan-in 后批量解析候选 RiskTag，按完整 Git 路径和局部位置构建连通候选块，并以最多 8 个并行结构化 LLM 调用执行保守归并。只有高置信且同时满足同根因、同影响和单一修复条件的分组才会删除重复候选；非法、低置信或失败结果一律完整保留。EvidencePlanner 直接复用归并阶段已解析的 RiskTag。

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

Gateway 在容器内始终监听 `9090`。如需修改宿主机端口，只设置 `CODEGUARD_HOST_PORT`：

```dotenv
CODEGUARD_HOST_PORT=8080
```

Gateway 的映射端口提供明文 HTTP，不直接提供 TLS。生产环境必须由反向代理终止 HTTPS，并将 `/webhooks/github` 转发到该宿主机端口；公开 Webhook 地址应为 `https://your-host.example/webhooks/github`。不要将 GitHub Webhook 直接指向映射端口。

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

默认服务商为 OpenAI：

```dotenv
CODEGUARD_PROVIDER=openai
CODEGUARD_MODEL=gpt-4o-mini
CODEGUARD_API_KEY=replace-with-your-key
```

使用 OpenAI 兼容接口时，还需设置：

```dotenv
CODEGUARD_API_BASE_URL=https://provider.example/v1
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

## 配置项

部署配置：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `CODEGUARD_IMAGE_TAG` | `latest` | `ghcr.io/baiyun689/codeguard` 下的镜像标签 |
| `CODEGUARD_HOST_PORT` | `9090` | 映射到容器固定端口 `9090` 的宿主机端口 |
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

Compose 会设置打包部署所需的容器内部路径和端口。除非维护自定义部署，否则不要修改 `CODEGUARD_TOOL_SERVER_PORT`、`CODEGUARD_TOOL_SERVER_URL`、`CODEGUARD_JOB_DB_PATH` 或 `CODEGUARD_WORKSPACE_DIR`。

## 运维与可观测性

Codeguard 当前只支持单 Gateway 实例。H2 持久化和调度器可以在该实例内恢复任务，但尚未实现多实例选主、分布式锁或共享工作区协调。不要将 Compose 服务扩容到一个以上副本。

运维端点：

| 端点 | 含义 |
|---|---|
| `GET /health` | 兼容健康检查端点，报告进程存活状态 |
| `GET /health/live` | 存活探针 |
| `GET /health/ready` | 检查 H2、调度器和 Python 初始化状态；不可用时返回 `503` |
| `GET /metrics` | Prometheus 文本格式指标 |

Compose 将 H2 数据库持久化到 `gateway-data`，将按 SHA 隔离的临时审查工作区保存到 `job-workspaces`。使用 `docker compose down` 停止服务；只有在明确需要删除持久化任务状态和工作区时才添加 `--volumes`。

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
mvn --batch-mode verify
```

容器构建：

```bash
docker build -t codeguard:local .
```

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
