# Codeguard CI 集成设计

**日期**: 2026-07-06
**状态**: 设计完成，待实现
**阶段**: 阶段 5 · 工程化收尾（提前启动子集）

---

## 概述

将 Codeguard 从命令行工具升级为 GitHub CI 常驻服务。开发者提交 PR 时，GitHub Webhook 自动触发审查，结果通过 Check Runs 做门禁（阻断合并）+ 行级评论贴高危问题。

部署形态：docker-compose 单机，Java Gateway + Python Agent 合并在一个容器内。

---

## 架构总览

```
开发者 Push PR
      │
      ▼
GitHub Webhook ──→ POST /webhooks/github (:9090)
                          │
                    ┌─────▼──────┐
                    │  Java Gateway (Javalin)
                    │
                    │  1. 验签 + 事件过滤 + 幂等去重
                    │  2. async job → H2 持久化
                    │  3. ProcessBuilder → python CLI
                    │  4. 解析结果
                    │  5. GitHub API → Check Runs + PR comments
                    │
                    │  /tools/* (不变)
                    └─────────────┘
                          │
                    ProcessBuilder exec
                          │
                    ┌─────▼──────┐
                    │  Python Agent
                    │  codeguard_agent review
                    │  --repo <clone_path>
                    │  --base origin/<base_ref>
                    │  --mode pipeline
                    └─────────────┘
```

### 关键决策：合并容器

Gateway 容器需要调用 Python Agent CLI，但两个独立容器无法跨容器 `ProcessBuilder`。给 Agent 加 HTTP 包装违背"Java 管基础设施"的架构原则。最终选择：**一个容器 + 双运行时（JDK 21 + Python 3.12）**。这是此项目"Agent + Gateway 紧耦合"的自然形态。

---

## 模块 1：Webhook 接入层

### 端点

`POST /webhooks/github`

### 三层过滤

1. **验签**: `X-Hub-Signature-256` HMAC-SHA256，secret 来自 `CODEGUARD_WEBHOOK_SECRET`。`constantTimeEquals` 防时序攻击。不通过 → 401
2. **事件过滤**: `X-GitHub-Event: pull_request`。只处理 `opened / reopened / synchronize`，其余事件 200 空响应
3. **幂等**: `(repo, pr_number, head_sha)` 查 H2，已存在且 status 非 failed → 200 返回已有状态

### Payload 提取

从 webhook body 只取 6 字段：`repo_full_name`、`clone_url`、`pr_number`、`head_sha`、`base_ref`、`installation_id`

### 认证

使用 **GitHub App**（非 PAT），因为 Check Runs API 要求 App 安装令牌。Token 1 小时自动轮换。

---

## 模块 2：异步 Job 系统

### 双层并发控制

- **层 1 — 有界队列**: `ArrayBlockingQueue<Runnable>(10)`，满 → 503
- **层 2 — 全局信号量**: `Semaphore(2)`，`CODEGUARD_MAX_CONCURRENT_REVIEWS` 控制

### Job 生命周期

```
pending → running → done
              │
              └→ retrying(30s后) → running (最多2次)
                        │
                        └→ failed
```

### 持久化：H2

```sql
CREATE TABLE review_jobs (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    repo          VARCHAR(255) NOT NULL,
    pr_number     INT NOT NULL,
    head_sha      VARCHAR(40) NOT NULL,
    base_ref      VARCHAR(255),
    clone_url     VARCHAR(512),
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',
    result_json   CLOB,
    retry_count   INT DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (repo, pr_number, head_sha)
);
```

### 启动恢复

Gateway 启动时扫描 `status IN ('pending','running','retrying')` 的 job → 重置为 `pending` → 重新入队。H2 是数据源，内存 BlockingQueue 只是调度器——重启不丢任务。

### 为什么不用 Redis / MQ

单 Gateway 实例 + H2 已提供持久化队列语义。多实例（水平扩展）时需要 Redis / MQ，那是产品化阶段的事。

---

## 模块 3：审查执行器

### 流程

1. **git clone/fetch**（2min 超时，不可重试）
2. **构建命令**: `python -m codeguard_agent review --repo <workdir> --base origin/<base_ref> --mode pipeline --format json`
3. **ProcessBuilder 执行**（10min 硬超时，可重试 1 次）：环境变量透传所有 `CODEGUARD_*`；stdout 流式读，stderr 记录日志不中断
4. **结果解析**: stdout 为空 → failed（可重试）；JSON 解析失败 → failed（不可重试）

### 与现有架构的关系

- Python Agent **不加 HTTP 包装**，Gateway 通过 `ProcessBuilder` 直接调 CLI
- 环境变量含 `CODEGUARD_TOOL_SERVER_URL=http://localhost:9090`，Agent CLI 通过此 URL 调 Gateway 的 `/tools/*` 获取文件上下文
- 同一个 Gateway 既接收 webhook，又给 Agent 提供工具服务

### 后续 RAG 扩展预留

嵌入模型将作为 Gateway 的一个 tool (`POST /tools/embed`) 提供，Java 启动时加载 ONNX 模型。Agent 通过现有工具协议调用，和 `find_sensitive_apis` / `get_file_content` 模式一致。嵌入是确定性计算（同文本同向量），不违反"Java 不碰 LLM"原则。ProcessBuilder 方案无需因 RAG 而改为 HTTP 服务。

---

## 模块 4：结果反馈

### Check Runs（门禁 + 总览）

- 审查开始时创建 Check Run (`status=in_progress`)
- 完成时更新: `conclusion=success|neutral|failure`
- 规则: CRITICAL → `failure`（阻止合并）；只有 WARNING/INFO → `neutral`；零 issue → `success`
- `output.summary`: Markdown 表格列出全部 issue
- `output.annotations`: 最多 50 条（GitHub API 硬上限）；超限截断 + 标注
- 超大 diff 降级时 `conclusion=neutral`，summary 注明跳过原因

### 行级评论（仅高危）

- 筛选: `severity=CRITICAL AND confidence >= 0.7`
- 频控: 同一 PR 最多 10 条，取 confidence 最高的
- 去重: 同 `path+line` 已有同类评论则 skip（防 rerun 刷屏）

### GitHub App 权限

```yaml
permissions:
  checks: write
  pull_requests: write
  contents: read
  metadata: read
```

---

## 模块 5：防护层

### 接口限流

Guava `RateLimiter` 令牌桶，`CODEGUARD_WEBHOOK_RATE_LIMIT`（默认 30 次/小时）。拒绝返回 429 + `Retry-After`。

### LLM 下游管控

不另做独立限流器。通过 `CODEGUARD_MAX_CONCURRENT_REVIEWS` 间接管控：最多 N 个 Python Agent 同时跑，每个内部 ReAct 已有重试与退避。后续需要更细粒度时再加。

### 超时三层

| 阶段 | 超时 | 超时后 |
|------|------|--------|
| git clone/fetch | 2min | failed，不可重试 |
| Python 审查进程 | 10min | failed，可重试 1 次 |
| 单次 job 总耗时 | 12min | failed |

### 大 diff 降级

`diff > CODEGUARD_MAX_DIFF_LINES`（默认 5000）→ 不审查，返回一个 WARNING issue 说明跳过原因。Job 标记 `done`（非 failed），Check Run `neutral`，不阻止合并。

> **后续计划**: 做大 diff chunking（切割+跨 chunk 去重）。待 chunking 落地后替换此降级逻辑。

### 失败重试

- **可重试**: 网络超时、LLM 瞬态错误、进程超时 → 最多 2 次，间隔 30s
- **不可重试**: 验签失败、clone 失败、JSON 解析失败、超大 diff → 直接 failed
- 最终 failed → PR 底部贴通用评论说明原因

---

## 模块 6：部署

### docker-compose

单容器（Java 21 + Python 3.12 双运行时）：

```yaml
services:
  codeguard:
    build: .
    ports:
      - "9090:9090"
    environment:
      - CODEGUARD_WEBHOOK_SECRET=${CODEGUARD_WEBHOOK_SECRET}
      - CODEGUARD_GITHUB_APP_ID=${CODEGUARD_GITHUB_APP_ID}
      - CODEGUARD_GITHUB_PRIVATE_KEY=${CODEGUARD_GITHUB_PRIVATE_KEY}
      - CODEGUARD_PROVIDER=${CODEGUARD_PROVIDER}
      - CODEGUARD_API_KEY=${CODEGUARD_API_KEY}
      - CODEGUARD_MODEL=${CODEGUARD_MODEL}
      - CODEGUARD_TOOL_SERVER_URL=http://localhost:9090
      - CODEGUARD_MAX_CONCURRENT_REVIEWS=2
      - CODEGUARD_WEBHOOK_RATE_LIMIT=30
      - CODEGUARD_MAX_DIFF_LINES=5000
    volumes:
      - gateway-data:/app/data
      - job-workspaces:/tmp/codeguard-jobs
    restart: unless-stopped

volumes:
  gateway-data:
  job-workspaces:
```

### Dockerfile

```dockerfile
FROM eclipse-temurin:21-jre
RUN apt-get update && apt-get install -y python3.12 python3-pip && rm -rf /var/lib/apt/lists/*
COPY gateway/target/codeguard-gateway.jar /app/
COPY agent/ /app/agent/
WORKDIR /app/agent
RUN pip install --no-cache-dir -e .
WORKDIR /app
EXPOSE 9090
ENTRYPOINT ["java", "-jar", "codeguard-gateway.jar"]
```

### 公网暴露

- **快速验证**: ngrok (`ngrok http 9090`)
- **长期使用**: frp / CloudFlare Tunnel / 直接公网 IP + 防火墙只放通 GitHub webhook IP 段

---

## 新增环境变量汇总

| 变量 | 默认 | 说明 |
|------|------|------|
| `CODEGUARD_WEBHOOK_SECRET` | - | GitHub webhook 共享密钥（必填） |
| `CODEGUARD_GITHUB_APP_ID` | - | GitHub App ID（必填） |
| `CODEGUARD_GITHUB_PRIVATE_KEY` | - | GitHub App 私钥 PEM（必填） |
| `CODEGUARD_MAX_CONCURRENT_REVIEWS` | 2 | 全局审查并发上限 |
| `CODEGUARD_WEBHOOK_RATE_LIMIT` | 30 | 每小时最多 webhook 次数 |
| `CODEGUARD_MAX_DIFF_LINES` | 5000 | 跳过审查的 diff 行数阈值 |

---

## 数据库演进路径

| 阶段 | 数据库 | 适用规模 |
|------|--------|----------|
| 当前 | H2 文件模式 | 单实例、小团队、<10 仓库 |
| 多仓库高并发 | MySQL | 多实例 GW、月审百+ PR |
| 水平扩展 | MySQL + Redis 队列 | 多 pod 抢 job |

JDBC 抽象，升级只需改连接 URL + 驱动。

---

## 不在此次 CI 范围内（留到后续）

- 大 diff chunking 切割 + 跨 chunk 去重（专题设计）
- RAG / 嵌入模型（嵌入放 Gateway tool 方案已预留）
- 审查结果 Dashboard / 历史查询
- 多 Git 平台适配（GitLab / Gitee）
- 多用户 / 多租户 / 团队管理
- 自适应限流 / 熔断 / Prometheus 指标
