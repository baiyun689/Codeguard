# Codeguard · 搭建路线图

> 定位:**一次 vibe coding 实践 —— 从零搭一个 AI 代码审查 Agent · 以 Agent 为核心 · 双语言架构(Java 后端/工具 + Python Agent)**
>
> vibe coding 不等于乱写。这里的玩法是:**先让它跑起来,再看效果、再迭代**;跟着感觉走,但每个阶段都设计成"可独立跑、可独立讲"的里程碑,踩着确定的脚印往前。

---

## 核心方法论(vibe coding 怎么才不跑偏)

> **跑通 → 看效果 → 小步迭代 → 记录决策**
>
> 每个阶段都先用最糙的方式把链路打通(哪怕假数据),亲眼看到输出后再决定下一步改什么。不追求一次到位,追求"每一步都有反馈"。

- [ ] 维护 `DECISIONS.md`(架构决策记录 / ADR):每做一个技术选择就写一条 —— **选了什么、为什么、放弃了什么备选**。vibe coding 最容易"凭感觉一路改",这份文档让感觉留痕、可回溯。
- [ ] 每阶段结束写一段"复盘":这一步我理解了什么、效果如何、踩了什么坑、下一步想改什么。

### 三条铁律

1. **先做减法**:MVP 砍到最小,别一上来追全功能。
2. **先跑通再加深**:每个阶段都必须能独立跑起来,看到真实输出再往上叠。
3. **先单语言再双语言**:阶段 1–2 先纯 Python 跑通,阶段 3 才引入 Java。

---

## 阶段 0 · 立项与边界 ⏱️ ~1 周

**目标:把范围砍到最小。**

- [ ] 锁定 MVP 范围:审查 **1 种语言(Java)**、**1 种输入(本地 git diff)**、**1 个维度(安全)**,其余全砍
- [ ] 设计核心数据结构 `Issue`(severity / file / line / type / message / suggestion / confidence)
- [ ] 建空仓库骨架 + `DECISIONS.md` 第一条 + 一页纸"做什么/不做什么/为什么"

🤔 **思考点**:`Issue` 的字段为什么这么设计?`confidence` 字段有什么用?哪些信息是审查结果必须有的、哪些是锦上添花?

🎯 **里程碑产出**:一页设计文档 + 仓库骨架 + 数据结构定义 ✅(已完成)

---

## 阶段 1 · 最小可跑闭环(Walking Skeleton) ⏱️ 1–2 周

**目标:纯 Python,不碰 Java,不碰工具。先建立一个"无 Agent 工作流"基准。**

```
git diff → 一次 LLM 调用 → 返回结构化 issues → 打印
```

- [ ] 用 Python 读取本地 git diff
- [ ] 写第一版 system / user prompt
- [ ] 用 `with_structured_output` 做结构化输出,拿到 `Issue` 列表
- [ ] 命令行打印审查结果
- [ ] 加最简单的重试

🤔 **思考点**:这个"无 Agent 版本"能审出哪些问题、审不出哪些?**保留这个基准**,后面加了工具调用 Agent 后,要靠它量化"到底好了多少"。先把 prompt 写清楚——prompt 最能体现你想让它干什么。

🎯 **里程碑产出**:一个能跑的命令行工具,输入 diff 输出 issues ✅(已完成)

---

## 阶段 2 · 管线化(Pipeline) ⏱️ 1–2 周

**目标:把单次调用拆成多阶段,加并行审查员。仍是"工作流",不是 Agent。**

- [x] 拆出四阶段:**摘要 → 并行审查 → 聚合去重 → 误报过滤**(四段补齐:摘要/软分派已落地,可开关)
- [x] 实现三个并行领域审查员(安全 / 逻辑 / 质量),用**线程池**并行(I/O 密集,线程足矣,暂不切 async)
- [x] 实现跨审查员的 issue 去重(纯规则、确定性);并升级为**两段式聚合**(规则去重 + LLM 语义综合,治理"行号漂移导致去重失效")
- [x] 实现两段式误报过滤:先正则(零成本)再 LLM 验证(默认关)
- [ ] 把每阶段做成可组合、可配置(YAML)(误报规则已 YAML 化)

🤔 **思考点**:为什么分阶段而不是一个大 prompt?并行三个审查员的收益和坑(同一问题报三次怎么办)?误报过滤为什么先正则再 LLM?

🎯 **里程碑产出**:一条完整的多阶段审查管线 —— 此时你能讲"我做过 LLM 工作流编排"

> 🔭 **后续岔路口(标记,暂不决策)**:管线阶段当前是**同步**实现——阶段 2 的并行审查员
> 用线程池在单个 ReviewerStage 内 fan-out 即可,不需要 async。
> 等后面做**长 diff 分割(chunking)**时,会出现"跨 chunk 并行"的需求,那时再定要不要切 async:
> - 若并行仍简单(只有审查员 *或* 只有 chunk),线程池继续够用;
> - 若变成 chunks × reviewers 的二维 fan-out 且需全局限流,async(Semaphore + gather)更干净。
> sync→async 是机械 retrofit,且 chunking 本就要大改 orchestrator,届时一起换。
> ⚠️ 提醒:chunking(token 预算打包 + hunk 切分 + 重写 @@ 头)极易过度工程,
> 做最小版即可(超阈值按文件切 + 跨 chunk 去重),别一上来就上复杂方案。

---

## 阶段 3 · Agent 核心:工具调用 ⏱️ 2–3 周 ⭐ 重头戏

**目标:引入 Java Tool Server,把审查员升级成真正的 Agent。双语言架构登场。一次只加一个工具。**

- [x] 用 Javalin 起一个最简 Java Tool Server,实现第一个工具 `get_file_content`(+ 文件访问护栏 + 会话层)
- [x] 把 Python 审查员从"单次直接调用"改成 ReAct Agent(实装为 langchain v1 `create_agent`;与直连基准按配置分流,见 ADR-009)
- [x] 让 Agent 能自主决定是否读文件、读哪个文件(已端到端定性坐实:审查员自主调 `get_file_content` 读整文件推理)
- [~] **【关键实验】** "无工具 vs 有工具"对照:harness(`--tools`)已就位;**定性已证有效**,量化待 repo-backed 评测用例(现合成数据集喂不了文件工具,ADR-009)
- [~] **【关键实验·编排治理】** 同一多维度 fixture 上量化"前置软路由 + 两段式聚合 + prompt 赛道纪律"的效果:before=18 条/合并 0(规则去重因行号漂移失效);after 目标 ~7~9 且不丢任何一类真问题。before/after 数字以 repo-backed 实跑为准(见 openspec `improve-reviewer-orchestration`、HANDOFF.md)
- [x] `get_repo_map`(借鉴 aider repo map:JavaParser 抽 tag + 自实现 diff-种子 PageRank + 签名级预算渲染),解决"该读哪个 diff 外文件";同时放宽沙箱到"repo 根内源码"使 `get_file_content` 能读 diff 外定义文件(ADR-012)
- [ ] 逐个加重型工具(沿通用 `/tools/{name}` 协议 + 会话接缝叠加):
  - [ ] `get_method_definition`(Java + JavaParser 做 AST)
  - [ ] `get_call_graph`(自建代码调用图)
  - [ ] `semantic_search`(向量库 RAG)
  - [ ] `get_related_files` / `get_diff_context`

🤔 **思考点(整个项目最关键的一次)**:亲眼对比"有工具 vs 无工具"的审查质量。这一刻你才会体感到 Agent 不是"更花哨的 LLM 调用",而是"能自主获取上下文"。把这个对比写进复盘 —— 这是最有说服力的一段经历。

🎯 **里程碑产出**:一个带工具调用 Agent 的代码审查系统 —— 此时你能讲"我做过 Agent"

---

## 阶段 4 · 你的创新点 ⏱️ 持续进行 ⭐ 差异化

**目标:做出市面常见方案没做/做得不够的事。以 Agent 为主,主打下面两个创新点。**

### 创新点 A:用 LangGraph 重构编排(强烈推荐)
- [x] 把默认审查路径切到显式 LangGraph 状态图:ADR-032 ReviewCouncil 外层拓扑已落地
- [x] 落地 ReviewCouncil 内部 Agent 职责:已从 security / logic / quality 过渡到 ThreatModel / Behavior / Maintainability 方法论分工
- [x] 落地每个发现者 Agent 的第一版工具边界:ThreatModel / Behavior / Maintainability 已声明固定 allowlist;后续继续补齐 budget / 失败策略 / 禁止行为 / trace
- [ ] 强化 CouncilCoordinator 的确定性调度:基于结构化字段控制 Evidence / Challenge / 轮次 / fast path,不回到自然语言关键词判断
- [ ] 增强 EvidenceAgent 的证据路由:把 `related_snippet` / `caller_path` / `sensitive_sink` / `metric_context` / `open_question` 稳定映射到现有工具与证据状态
- [ ] 把 SelfChecker 从旧 aggregation + fp_filter 包装升级为真正裁决节点:去重、证据一致性、challenge 处理、级别校准
- [ ] 增加 ReviewCouncil 过程 trace 与 eval 指标:Agent 触发次数、补证次数、challenge 推翻率、证据覆盖率、候选丢弃原因等
- [ ] 重新设计可控的多轮审查 / 回溯 / 人在环路(human-in-the-loop)审批
- [ ] 用 `langgraph-checkpoint` 做有状态、可恢复的审查流程

### 创新点 B:记忆工程(让它越用越懂)
- [ ] 把"开发者采纳/忽略反馈"写回向量库
- [ ] 审查时先检索相似历史 issue,做上下文增强
- [ ] 把"人工写死的误报规则"升级成"自动学习累积"
- [ ] (进阶)按仓库/作者做画像,实现个性化审查

### 风险路由编排子路线（ADR-038）

这条子路线不新增一套并行状态，而是沿现有 `ReviewTask → RiskProfile → TaskSelection →
TaskContextBundle` 接缝逐阶段增强:

| 子阶段 | 状态 | 已确定边界 |
|---|---|---|
| Phase 1 | ✅ | 冻结五个任务链 State 字段、hunk/fallback task、严格候选映射和 Evidence 首次必经拓扑 |
| Phase 2 | ✅ | 23 个具体 RiskTag + `GENERAL_REVIEW`、path/text 方向规则、TaskRank 默认 100/10 预算、RiskTag 到三路 reviewer 的 task scope 路由 |
| Phase 3 | ✅ | ContextProvider 按 RiskTag 构建上下文，AST 只作为事实来源，不产出风险标签 |
| Phase 4 | ⬜ | 在已有 task scope 上增强三路审查员的风险感知提示和工具策略 |
| Phase 5 | ⬜ | Evidence/Judge 消费 task、risk、context、evidence，保持既有候选和裁决契约 |
| Phase 6 | ⬜ | Trace Dashboard 与 eval 展示命中、路由、预算跳过和证据闭环 |

🤔 **思考点**:每个创新都要能回答 ——"常见方案为什么没做/做得不够,我做了什么、解决了什么问题"。能答清楚,这就是你的东西。

🎯 **里程碑产出**:有明确差异化的 Agent —— 此时你能讲"我做的 Agent 强在哪"

---

## 阶段 5 · 工程化收尾 ⏱️ 1–2 周

**目标:从"能跑"到"生产可用"。后端线加分项。**

- [ ] 韧性治理:熔断 / 限流 / 重试(Resilience4j)
- [ ] 可观测:Prometheus 指标(审查数、issue 数、token、耗时)
- [ ] 缓存:同一 diff 短期内不重复审查
- [ ] 测试 + 覆盖率门槛 + CI
- [ ] Docker / docker-compose 编排(Gateway + Agent + 向量库/MQ)

🤔 **思考点**:这些"非功能性需求"为什么是"生产可用"的标志?熔断和重试解决的是什么场景?

🎯 **里程碑产出**:一个可部署、可观测、有测试的完整项目

---

## 时间与节奏

| 阶段 | 时长 | 能讲什么 |
|---|---|---|
| 0–1 | 2–3 周 | 跑通第一版无 Agent 工作流 |
| 2 | 1–2 周 | "我做过 LLM 工作流编排" |
| 3 | 2–3 周 | **"我做过 Agent"** |
| 4 | 持续 | **"我做的 Agent 强在哪"** |
| 5 | 1–2 周 | "生产可用 + 后端硬工程" |

**整体 8–12 周(业余时间)。关键不是快,是每个阶段都能独立跑、独立讲。** 即使只做到阶段 3,你也已经有一个拿得出手的"带工具调用 Agent 的代码审查系统"。

---

## 进度自检:三句话标准

- 阶段 1–2 结束 → 你能讲 **"LLM 工作流"**
- 阶段 3 结束 → 你能讲 **"我做过 Agent"**
- 阶段 4 结束 → 你能讲 **"我做的 Agent 强在哪"**

这三句话的份量差别很大。别在还只做到工作流时,说自己"做过 Agent"。
