# 我的 AI 代码审查项目 · 搭建路线图——项目名称暂定：Codeguard

> 定位:**同领域做进阶(AI 代码审查)· 以 Agent 为核心 · 双语言架构(Java 后端/工具 + Python Agent,仿 DiffGuard)**
>
> 这份路线图借鉴 DiffGuard,但目标是**理解并超越它**,而不是复制。每个阶段都设计成"可独立跑、可独立讲"的里程碑。

---

## 核心方法论(决定你是"抄"还是"有自己的思考")

> **读懂 → 盖住 → 重写 → 对比**
>
> 每个阶段:先读 DiffGuard 对应代码搞懂它怎么做 → **合上它的代码** → 自己从零实现一遍 → 再打开对比差异,想清楚"它为什么那样写、我为什么这样写"。

- [ ] 建立 `DECISIONS.md`(架构决策记录 / ADR):每做一个技术选择就写一条,包含**选了什么、为什么、放弃了什么备选**。这份文档本身就是你"有思考"的证据。
- [ ] 每阶段结束写一段"复盘":这一步我理解了什么、和 DiffGuard 的差异、踩了什么坑。

### 三条铁律

1. **先做减法**:MVP 砍到最小,别照搬全功能。
2. **按演化顺序走**:DiffGuard 的复杂度是 143 次提交迭代出来的,按它的演化顺序搭,不要按最终形态搭。
3. **先单语言再双语言**:阶段 1–2 先纯 Python 跑通,阶段 3 才引入 Java。

---

## 阶段 0 · 立项与边界 ⏱️ ~1 周

**目标:把范围砍到最小。**

- [ ] 锁定 MVP 范围:审查 **1 种语言(Java)**、**1 种输入(本地 git diff)**、**1 个维度(安全)**,其余全砍
- [ ] 设计核心数据结构 `Issue`(severity / file / line / type / message / suggestion / confidence)
- [ ] 建空仓库骨架 + `DECISIONS.md` 第一条 + 一页纸"做什么/不做什么/为什么"

📖 **对照阅读**:`README.md`(架构图)、`docker-compose.yml`、`services/agent/src/diffguard_agent/models/schemas.py`

🤔 **你的思考点**:`Issue` 的字段为什么这么设计?`confidence` 字段有什么用?哪些信息是审查结果必须有的、哪些是锦上添花?

🎯 **里程碑产出**:一页设计文档 + 仓库骨架 + 数据结构定义

---

## 阶段 1 · 最小可跑闭环(Walking Skeleton) ⏱️ 1–2 周

**目标:纯 Python,不碰 Java,不碰工具。建立"无 Agent 工作流"基准。**

```
git diff → 一次 LLM 调用 → 返回结构化 issues → 打印
```

- [ ] 用 Python 读取本地 git diff
- [ ] 写第一版 system / user prompt
- [ ] 用 `with_structured_output` 做结构化输出,拿到 `Issue` 列表
- [ ] 命令行打印审查结果
- [ ] 加最简单的重试(`invoke_with_retry`)

📖 **对照阅读**:`github_action_runner.py`、`reviewer.py` 的 `_run_direct`、`llm_utils.py`、`llm/prompts/pipeline/security-*.txt`

🤔 **你的思考点**:这个"无 Agent 版本"能审出哪些问题、审不出哪些?**保留这个基准**,后面要靠它量化"加了 Agent 好了多少"。先读 prompt 再读代码——prompt 最能说明意图。

🎯 **里程碑产出**:一个能跑的命令行工具,输入 diff 输出 issues(= DiffGuard 的"Action 直连模式")

---

## 阶段 2 · 管线化(Pipeline) ⏱️ 1–2 周

**目标:把单次调用拆成多阶段,加并行审查员。仍是"工作流",不是 Agent。**

- [ ] 拆出四阶段:**摘要 → 并行审查 → 聚合去重 → 误报过滤**
- [ ] 实现三个并行领域审查员(安全 / 逻辑 / 质量),用 `asyncio.gather` 并行
- [ ] 实现跨审查员的 issue 去重
- [ ] 实现两段式误报过滤:先正则(零成本)再 LLM 验证
- [ ] 把每阶段做成可组合、可配置(YAML)

📖 **对照阅读**:`pipeline_orchestrator.py`、`pipeline/stages/`(summary / reviewer / aggregation / fp_filter)、`config/false-positive-rules.yaml`

🤔 **你的思考点**:为什么分阶段而不是一个大 prompt?并行三个审查员的收益和坑(同一问题报三次怎么办)?误报过滤为什么先正则再 LLM?

🎯 **里程碑产出**:一条完整的多阶段审查管线 —— 此时你能讲"我做过 LLM 工作流编排"

---

## 阶段 3 · Agent 核心:工具调用 ⏱️ 2–3 周 ⭐ 重头戏

**目标:引入 Java Tool Server,把审查员升级成真正的 Agent。双语言架构登场。一次只加一个工具。**

- [ ] 用 Javalin 起一个最简 Java Tool Server,实现第一个工具 `get_file_content`
- [ ] 把 Python 审查员从 `_run_direct` 改成 ReAct Agent(`create_tool_calling_agent` + `AgentExecutor`)
- [ ] 让 Agent 能自主决定是否读文件、读哪个文件
- [ ] **【关键实验】** 用同一个 PR 跑"阶段 1 无工具版" vs "阶段 3 有工具版",记录质量差异
- [ ] 逐个加重型工具:
  - [ ] `get_method_definition`(Java + JavaParser 做 AST)
  - [ ] `get_call_graph`(自建代码调用图)
  - [ ] `semantic_search`(向量库 RAG)
  - [ ] `get_related_files` / `get_diff_context`

📖 **对照阅读**:`reviewer.py` 的 `_run_with_tools`、`tools/definitions.py`、Java 侧 `agent/tools/`、`toolserver/`、`review/ast/ASTAnalyzer.java`、`review/codegraph/`

🤔 **你的思考点(整个项目最关键的一次)**:亲眼对比"有工具 vs 无工具"的审查质量。这一刻你才会体感到 Agent 不是"更花哨的 LLM 调用",而是"能自主获取上下文"。把这个对比写进复盘 —— 这是面试最有说服力的一段话。

🎯 **里程碑产出**:一个带工具调用 Agent 的代码审查系统 —— 此时你能讲"我做过 Agent"

---

## 阶段 4 · 你的创新:超越 DiffGuard ⏱️ 持续进行 ⭐ 差异化

**目标:做 DiffGuard 没做/做得不够的事。以 Agent 为主,主打下面两个创新点。**

### 创新点 A:用 LangGraph 重构编排(强烈推荐)
- [ ] 把 `AgentExecutor` 换成显式的 LangGraph 状态图
- [ ] 实现可控的多轮审查 / 回溯 / 人在环路(human-in-the-loop)审批
- [ ] 用上 `langgraph-checkpoint` 做有状态、可恢复的审查流程

### 创新点 B:记忆工程(让它越用越懂)
- [ ] 把"开发者采纳/忽略反馈"写回向量库
- [ ] 审查时先检索相似历史 issue,做上下文增强
- [ ] 把"人工写死的误报规则"升级成"自动学习累积"
- [ ] (进阶)按仓库/作者做画像,实现个性化审查

📖 **对照阅读**:`review/coderag/`(现有向量库可复用)、`agent/false_positive_filter.py`(现有静态规则,要改造的对象)

🤔 **你的思考点**:每个创新都要能回答 ——"DiffGuard 为什么没做/做得不够,我做了什么、解决了什么问题"。能答清楚,这就是你的而不是它的。

🎯 **里程碑产出**:有明确差异化的 Agent —— 此时你能讲"我做的 Agent 比开源方案强在哪"

---

## 阶段 5 · 工程化收尾 ⏱️ 1–2 周

**目标:从"能跑"到"生产可用"。后端线加分项。**

- [ ] 韧性治理:熔断 / 限流 / 重试(Resilience4j)
- [ ] 可观测:Prometheus 指标(审查数、issue 数、token、耗时)
- [ ] 缓存:同一 diff 24h 内不重复审查
- [ ] 测试 + 覆盖率门槛 + CI
- [ ] Docker / docker-compose 编排(Gateway + Agent + 向量库/MQ)

📖 **对照阅读**:`platform/resilience/`、`platform/observability/`、`review/ReviewCache.java`、`docker-compose.yml`、`.github/workflows/`

🤔 **你的思考点**:这些"非功能性需求"为什么是"生产可用"的标志?熔断和重试解决的是什么场景?

🎯 **里程碑产出**:一个可部署、可观测、有测试的完整项目

---

## 时间与节奏

| 阶段 | 时长 | 能讲什么 |
|---|---|---|
| 0–1 | 2–3 周 | 跑通第一版无 Agent 工作流 |
| 2 | 1–2 周 | "我做过 LLM 工作流编排" |
| 3 | 2–3 周 | **"我做过 Agent"** |
| 4 | 持续 | **"我做的 Agent 比开源方案强在哪"** |
| 5 | 1–2 周 | "生产可用 + 后端硬工程" |

**整体 8–12 周(业余时间)。关键不是快,是每个阶段都能独立跑、独立讲。** 即使只做到阶段 3,你也已经有一个拿得出手的"带工具调用 Agent 的代码审查系统"。

---

## 进度自检:三句话标准

- 阶段 1–2 结束 → 你能讲 **"LLM 工作流"**
- 阶段 3 结束 → 你能讲 **"我做过 Agent"**
- 阶段 4 结束 → 你能讲 **"我做的 Agent 比开源方案强在哪"**

这三句话的份量差别很大。别在还只做到工作流时,说自己"做过 Agent"。
