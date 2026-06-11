# Codeguard

> **AI 代码审查引擎** —— 以 Agent 为核心,双语言架构(Java 后端/工具 + Python Agent)。

Codeguard 是一个借鉴 [DiffGuard] 思路、但目标是"理解并超越它"的个人进阶项目。
它分析代码变更(diff),从安全、逻辑、质量等维度审查问题。

当前进度:**阶段 1 · 最小可跑闭环**(纯 Python,单次 LLM 调用)。
完整路线图见 [`docs/ROADMAP.md`](docs/ROADMAP.md),关键技术决策见 [`DECISIONS.md`](DECISIONS.md)。

---

## 架构(目标形态)

```
┌─────────────────┐     HTTP / 工具调用      ┌──────────────────┐
│  Python Agent   │ ──────────────────────> │  Java Gateway    │
│  (审查管线/编排)  │ <────────────────────── │  (AST/调用图/RAG) │
└─────────────────┘     代码上下文工具         └──────────────────┘
```

- **Python Agent**(`services/agent`):LLM 审查管线、Agent 编排 —— 当前唯一已实现的部分
- **Java Gateway**(`services/gateway`):AST 分析、代码调用图、语义检索工具 —— 阶段 3 才引入,目前为空占位

> 设计原则:**先单语言跑通,再引入双语言。** 阶段 1–2 只有 Python,阶段 3 才加 Java。

---

## 快速开始(阶段 1)

```bash
cd services/agent

# 1) 安装依赖(推荐用 uv,或用 pip)
pip install -e .

# 2) 不配密钥也能跑(mock 模式,验证流水线连通)
python -m codeguard_agent review

# 3) 接入真实 LLM
export CODEGUARD_PROVIDER=claude
export CODEGUARD_API_KEY=sk-xxx
python -m codeguard_agent review --repo . --base HEAD
```

环境变量见 [`.env.example`](.env.example)。

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
    │   │   ├── git/             # diff 采集
    │   │   ├── llm/             # LLM 工厂 + 重试 + mock
    │   │   ├── pipeline/        # 审查管线(阶段1:单次调用)
    │   │   └── prompts/         # 提示词模板
    │   └── tests/
    └── gateway/                 # Java Gateway(阶段3引入,目前占位)
```

---

## 路线图概览

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 立项与边界 | ✅ |
| 1 | 最小可跑闭环(纯 Python) | 🚧 进行中 |
| 2 | 管线化(多阶段 + 并行审查员) | ⬜ |
| 3 | Agent 核心:工具调用(引入 Java) | ⬜ |
| 4 | 创新:LangGraph 重构 + 记忆工程 | ⬜ |
| 5 | 工程化收尾(韧性/可观测/部署) | ⬜ |

[DiffGuard]: https://github.com/
