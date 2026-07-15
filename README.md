# Codeguard

> **AI 代码审查引擎** —— 以 Agent 为核心,双语言架构(Java 后端/工具 + Python Agent)。

Codeguard 是一次 **vibe coding 实践** —— 尝试用"边写边迭代、跟着感觉走、借助 AI 协作"的方式,从零搭一个 AI 代码审查 Agent。它分析代码变更(diff),从安全、逻辑、质量等维度审查问题。

当前进度:**ADR-032 + ADR-038 Phase 1–5 · 风险路由与策略证据链**。默认路径为
`summary? → task/risk/context → task-scoped discover × 3 → EvidencePlanner → EvidenceAgent → CouncilJudge → END`，
Judge 需要补证时按结构化目的回到 Planner；旧 Supervisor 图已迁移到
`services/agent/legacy/supervisor_graph/` 作为历史参考,不再作为运行回退。
完整路线图见 [`docs/ROADMAP.md`](docs/ROADMAP.md),关键技术决策见 [`DECISIONS.md`](DECISIONS.md)。

---

## 架构(目标形态)

```
┌─────────────────┐     HTTP / 工具调用      ┌──────────────────┐
│  Python Agent   │ ──────────────────────> │  Java Gateway    │
│  (审查管线/编排)  │ <────────────────────── │  (AST/调用图/RAG) │
└─────────────────┘     代码上下文工具         └──────────────────┘
```

- **Python Agent**(`services/agent`):LLM 审查管线、LangGraph 编排、ReviewCouncil 多 Agent 协作
- **Java Gateway**(`services/gateway`):只提供事实与护栏工具,如文件读取、敏感 API、调用方、代码度量

> 设计原则:Python 负责智能编排和最终判断;Java 只提供事实与沙箱护栏,不调用 LLM、不判断问题。

---

## 快速开始

```bash
cd services/agent

# 1) 安装依赖(推荐用 uv,或用 pip)
pip install -e .

# 2) 默认调真实 OpenAI API(需配密钥)
export CODEGUARD_API_KEY=sk-xxx
python -m codeguard_agent review --repo . --base HEAD

# 3) 不想配密钥?用 mock 模式先验证流水线连通
export CODEGUARD_PROVIDER=mock
python -m codeguard_agent review
```

> 也可把上述变量写进项目根目录的 `.env` 文件(已被 gitignore),程序会自动就近加载,
> 之后直接 `python -m codeguard_agent review` 即可,无需每次 export。已显式设置的环境变量优先于 `.env`。

环境变量见 [`.env.example`](.env.example)。

### Java Gateway 运维端点

Java Gateway 明确按单实例运行，H2 负责 job 恢复，不承诺多实例抢占或一致性。CI 审查采用
SHA 隔离 workspace、最多两次非阻塞重试和 600 秒默认进程超时。Python CLI 的退出码 `1`
表示“审查成功但发现 CRITICAL”，Gateway 会在 `ReviewResult` JSON 合法时把 exit 0/1 都记为成功。

- `GET /health`、`GET /health/live`：进程存活。
- `GET /health/ready`：H2、调度器和 Python 初始化均正常时返回 200，否则返回 503。
- `GET /metrics`：Prometheus 文本指标；repo、PR、SHA 和 jobId 只写日志，不作为 label。

关键配置为 `CODEGUARD_MAX_CONCURRENT_REVIEWS`、`CODEGUARD_REVIEW_TIMEOUT_SECONDS`、
`CODEGUARD_RETRY_DELAY_SECONDS`、`CODEGUARD_SHUTDOWN_GRACE_SECONDS`、
`CODEGUARD_JOB_DB_PATH` 和 `CODEGUARD_WORKSPACE_DIR`，默认值见 `.env.example`。

大 diff 可通过 `CODEGUARD_MAX_REVIEW_TASKS`（总任务数，默认 100）和
`CODEGUARD_MAX_TASKS_PER_FILE`（单文件任务数，默认 10）限制深审范围；限制只作用于
TaskRank，不会删除或改写已生成的风险画像。

---

## 目录结构

```
Codeguard/
├── README.md
├── DECISIONS.md                 # 架构决策记录(ADR)—— "有自己思考"的证据
├── .env.example
├── docs/
│   └── ROADMAP.md               # 分阶段搭建路线图
└── services/
    ├── agent/                   # Python Agent(已实现)
    │   ├── pyproject.toml
    │   ├── src/codeguard_agent/
    │   │   ├── cli.py           # 命令行入口
    │   │   ├── config.py        # 环境变量配置
    │   │   ├── models/schemas.py# 核心数据结构(Issue/ReviewResult)
    │   │   ├── models/council.py# 内部状态(Candidate/EvidenceFinding/Verdict/TraceStats)
    │   │   ├── git/             # diff 采集
    │   │   ├── llm/             # LLM 工厂 + 重试 + mock
    │   │   ├── pipeline/        # 风险路由、EvidencePlanner/Agent、CouncilJudge 与 stage
    │   │   └── prompts/         # 提示词模板
    │   ├── legacy/supervisor_graph/ # 旧 Supervisor 图备份,不作为运行路径
    │   └── tests/
    └── gateway/                 # Java Gateway(事实工具 + 沙箱护栏)
```

---

## 路线图概览

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 立项与边界 | ✅ |
| 1 | 最小可跑闭环(纯 Python) | ✅ |
| 2 | 管线化(多阶段 + 并行审查员) | ✅ |
| 3 | Agent 核心:工具调用(引入 Java) | ✅ |
| 4 | 创新:LangGraph + ReviewCouncil 多 Agent 编排 | 🚧 |
| 5 | 工程化收尾(韧性/可观测/部署) | ⬜ |
