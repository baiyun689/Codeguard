# CLAUDE.md

本文件给 Claude / AI 助手以及任何接手者快速建立项目心智模型,并说明改动代码时的约束与注意点。

> 阅读顺序建议:本文件 → `README.md`(快速开始)→ `docs/ROADMAP.md`(分阶段路线)→ `DECISIONS.md`(为什么这么选)。

---

## 1. 这是什么

Codeguard 是一个 **AI 代码审查引擎**,以 Agent 为最终核心,双语言架构(Python Agent + Java Gateway)。它的输入是代码变更(git diff),输出是结构化的审查问题(`Issue` 列表),覆盖安全、逻辑、质量等维度。

这是一次 **vibe coding 实践**——刻意按"先跑通 → 看效果 → 小步迭代 → 记录决策"的节奏演进,每个阶段都设计成"可独立跑、可独立讲"的里程碑。

**当前进度:阶段 1 · 最小可跑闭环**。纯 Python,单次 LLM 调用,只做安全维度,只吃本地 git diff。没有 Agent、没有工具调用、没有 Java——**这是故意的**(见 §6)。

---

## 2. 架构

### 目标形态(路线图终点)

```
┌─────────────────┐     HTTP / 工具调用      ┌──────────────────┐
│  Python Agent   │ ──────────────────────> │  Java Gateway    │
│  (审查管线/编排)  │ <────────────────────── │  (AST/调用图/RAG) │
└─────────────────┘     代码上下文工具         └──────────────────┘
```

### 当前实现(阶段 1)

只有 Python Agent 这一侧,且只是一条直线:

```
git diff  →  一次 LLM 调用(结构化输出)  →  ReviewResult  →  终端打印
```

`services/gateway`(Java)目前是**空占位**,阶段 3 才引入。**现在不要往 gateway 写任何东西。**

---

## 3. 目录结构

```
Codeguard/
├── CLAUDE.md                      # 本文件
├── README.md                      # 快速开始
├── DECISIONS.md                   # 架构决策记录(ADR)—— 改设计前必读
├── .env.example                   # 环境变量示例(复制为 .env 使用)
├── docs/ROADMAP.md                # 分阶段搭建路线图
└── services/
    ├── agent/                     # Python Agent(唯一已实现的部分)
    │   ├── pyproject.toml         # 依赖与打包(打包仅含 src/codeguard_agent)
    │   ├── src/codeguard_agent/
    │   │   ├── __main__.py        # python -m codeguard_agent 入口
    │   │   ├── cli.py             # 命令行:review 子命令、结果打印、退出码
    │   │   ├── config.py          # Settings:从环境变量/.env 读配置
    │   │   ├── models/schemas.py  # ★核心数据结构:Severity / Issue / ReviewResult
    │   │   ├── git/diff_collector.py  # 调系统 git 采集 diff
    │   │   ├── llm/client.py      # LLM 工厂(openai/claude/mock)+ 重试 + mock 假数据
    │   │   ├── pipeline/reviewer.py   # ★审查逻辑(阶段1:单次调用)
    │   │   └── prompts/security.txt   # 安全审查的 system prompt
    │   ├── tests/                 # pytest:测工程正确性
    │   └── evals/                 # ★审查质量评测框架(量化效果,见 §5)
    └── gateway/                   # Java Gateway(阶段3引入,目前空占位)
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
cd services/agent && pytest
```

### 评测框架(审查质量,量化"效果")★

`evals/` 用"带标注的数据集 + 统计指标"量化审查质量,这是阶段 1 的 baseline,也是阶段 3 证明 Agent 价值的对照基准(见 ADR-002)。详见 `evals/README.md`。

```bash
cd services/agent && pip install -e . pyyaml
python -m evals.runner --runs 3          # 跑 baseline,3 次统计方差
python -m evals.runner --runs 3 --judge  # 额外开 LLM-as-judge
```

产出 `evals/reports/baseline.md`,核心指标:Precision / Recall / F1 / 误报率 / 定位准确率 / 级别准确率。
加用例只需往 `evals/dataset/vuln`(有漏洞)或 `evals/dataset/clean`(无问题、测误报)丢一个 YAML,无需改代码。

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

> **Windows/PowerShell 注意**:bash 的 `VAR=value cmd` 内联写法在 PowerShell 不生效,要先 `$env:VAR="value"` 再跑命令;或直接写 `.env`(推荐)。

---

## 6. 改代码的注意点(重要)

### 6.1 尊重阶段边界——别提前做未来阶段的事

路线图是这个项目的灵魂(`docs/ROADMAP.md`)。三条铁律:

1. **先做减法**:MVP 砍到最小,别顺手加全功能。
2. **先跑通再加深**:每阶段都要能独立跑、看到真实输出再往上叠。
3. **先单语言再双语言**:阶段 1–2 纯 Python;**阶段 3 才碰 Java `gateway`**。

具体禁忌(当前阶段):
- 不要在 `reviewer.py` 里引入 Agent / 工具调用 / 多轮循环——那是阶段 3。
- 不要往 `services/gateway` 写代码。
- 不要把单次调用拆成多阶段管线——那是阶段 2。
- 加功能前先问:这属于哪个阶段?现在该做吗?

### 6.2 保护好 baseline

`reviewer.py` 当前的"单次直接调用"版本是**有意保留的无 Agent 基准**(ADR-002)。阶段 3 加 Agent 后要用 `evals/` 同一套数据集跑对比。**改 `reviewer.py` 时不要把这个基准实现删掉**;要做 Agent 版,新增一个实现(如 `pipeline/agent_reviewer.py`)并行存在,而不是原地替换。

### 6.3 改核心数据结构要慎重

`models/schemas.py` 的 `Issue` 被所有阶段共享。增字段一般安全(给默认值即可);**改名 / 删字段 / 改类型**会波及 prompt、CLI 打印、evals 匹配逻辑,改前先全局搜引用,并在 `DECISIONS.md` 记一条 ADR。`Severity` 是枚举(约束 LLM 输出范围),新增级别要同步更新 `cli.py` 的 `_SEVERITY_ICON`。

### 6.4 LLM / 结构化输出的坑

- **结果可能是 `None`**:`with_structured_output(...).invoke()` 在模型没正确发起工具调用时返回 `None`。`reviewer.review` 已兜底成空结果——**任何新写的、消费 LLM 结构化输出的代码都要做同样的 None 防御。**
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

---

## 7. 每个阶段结束要做的事

- 在 `DECISIONS.md` 追加这一阶段的关键技术选择(选了什么 / 为什么 / 放弃了什么)。
- 写一段复盘:理解了什么、效果如何、踩了什么坑、下一步改什么。
- 跑一次 `evals` 存档当前 baseline 指标,方便和下阶段对比。

---

_本文件随项目演进更新。改动架构或新增模块时,记得同步这里的目录结构与注意点。_
