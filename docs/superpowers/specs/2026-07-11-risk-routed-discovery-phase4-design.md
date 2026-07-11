# Phase 4:定向发现链(task 级并发 + 风险分层引擎)设计

**日期**: 2026-07-11
**状态**: 已批准,待实施
**前置阶段**: Phase 1(状态契约与拓扑)、Phase 2(风险标签规则与任务排序)、Phase 3(风险感知 ContextProvider)均已完成
**关联**: ADR-038(风险路由驱动的 ReviewTask 编排)、
[风险路由 ReviewTask 编排设计](./2026-07-10-risk-routed-review-orchestration-design.md)、
[Phase 2 风险标签规则与任务排序设计](./2026-07-10-risk-triage-phase2-design.md)、
[Phase 3 风险感知 ContextProvider 设计](./2026-07-11-risk-aware-context-provider-design.md)
**后续子阶段**(不在本次实施范围,设计另见):
[知识图谱按 RiskTag 拆分注入设计](./2026-07-11-tag-scoped-knowledge-injection-design.md)

---

## 1. 背景与目标

Phase 1-3 已经把 `ReviewTask → RiskProfile → TaskSelection → TaskContextBundle` 这条链路钉死,
每个选中任务都带有细粒度的 `RiskProfile.tag_scores` 和已经按 Level0/Level1 填好的
`TaskContextBundle.facts`。但发现者一侧(ThreatModelAgent / BehaviorAgent / MaintainabilityAgent)
还停在 Phase 2 的粗粒度用法上:

- 一个 reviewer 被路由到的**所有** task,一次性拼成一份 `render_task_scope` 组合文本,
  一次 ReAct/Direct 调用处理完。
- `task_context_bundles`(Phase 3 已填充真实数据)完全没有接入发现者的 prompt——发现者
  仍只读全局 `context_bundle`。
- 不区分 task 的风险强度,一律走同样的执行方式(ReAct 或 Direct,取决于是否配置
  `tool_client`),没有按"这个 task 到底值不值得让模型自主循环探索"做分层。

Phase 4 的目标(呼应 ADR-038 总设计"Phase 4: 完成定向发现链"):

1. 把"一个 reviewer 一次调用处理所有 task"改成"每个 task 独立调用",task 之间**并发**
   执行,不增加总耗时。
2. 按 task 的 `RiskProfile` 强度做**引擎分层**:高风险 task 走 ReAct(可用工具深挖),
   低风险/未分类 task 降级为一次无工具的结构化 LLM 调用——用最小的 token 成本覆盖
   "不太可能有问题"的大多数 task。
3. 让每个 task 的 prompt 真正带上它自己的 `TaskContextBundle.facts`(Phase 3 产出此前
   一直没被消费),而不是共享一份全局上下文。
4. 候选(`CandidateIssue`)的 `task_id` 直接来自发起调用时已知的 task,不再依赖
   Phase 2 遗留的"按 file+line 猜"的 `map_candidate_to_task` 兜底路径(该函数继续保留
   给 `selection is None` 的旧路径用)。

非目标(明确不做,留给后续):

- 不按 RiskTag 收窄 ReAct 工具白名单(`reviewer.tool_allowlist` 本轮保持固定,不做
  tag→tool 映射)。
- 不引入 async;task 级并发复用 Phase 3 已有的 `run_bounded_parallel`(有界线程池)。
- 不改变任何主路由 State 字段(`review_tasks` / `risk_profiles` / `task_selection` /
  `task_context_bundles` 形状不变),tier 决策是节点内部的运行时派生值,不落状态。
- 不改变 `CandidateIssue` / `EvidenceRequest` / `Verdict` / `ReviewResult` 产品契约。
- 知识图谱按 RiskTag 拆分注入(prompt 瘦身)作为紧接的下一个子阶段单独设计和实施,
  不在本次改动范围内。

---

## 2. 设计动机补充:两个关键权衡

### 2.1 并发解决耗时,分层解决成本

task 级并发(reviewer 内部对其路由到的多个 task 并发调用)只缩短墙钟时间,**不减少
总 token 消耗**——该花的调用还是要花。真正带来 token 节省的是分层:ReAct 循环每轮
工具调用都要把完整对话历史和工具返回结果重新过一遍模型,对"大概率没什么好查"的
task,省掉整个循环、只留一次调用,是这套设计里最大的成本杠杆。

### 2.2 分层阈值:score>=2 才进 ReAct,而非 score=3

Phase 2 的打分标准:1 分=结构相关;**2 分=明确涉及控制流/数据流/资源生命周期/一致性**;
3 分=删除保护/暴露入口/危险 sink。`RESOURCE_LIFECYCLE`(资源泄漏)、
`TRANSACTION_ATOMICITY`、`CONCURRENCY_CONSISTENCY` 这类项目历史上反复出现在 eval
fixture 里的典型问题通常只打到 2 分。如果阈值定在"只有 3 分才进 ReAct",会把这几类
恰好最需要工具核实("这个连接/锁到底有没有在别处关闭")的问题降级到无工具的单次
调用,削弱审查深度且切断了唯一的核实手段。

`GENERAL_REVIEW`(规则完全没识别出具体信号)同样降级 Direct——它代表"规则不认识",
继续给工具排查边际收益有限,且 Direct 引擎仍会拿到 `task_context_bundle` 里已有的
Level0 事实,不是空手审查。

风险不对称:task 被误判低危而降级 Direct,是**一次性、不可逆**的漏报(EvidenceAgent/
CouncilJudge 只核实"已经被提出的候选",不会主动发现 Direct 没提出来的问题);反过来
误判高危多跑一轮 ReAct,只是多花钱,不影响正确性。因此阈值选择偏保守(score>=2),
把成本节省让给"确定性最强的边角料"(纯 score=1 弱信号 / GENERAL_REVIEW),不去动
中危区间。

---

## 3. 引擎分层规则

```python
def decide_tier(profile: RiskProfile | None) -> Literal["react", "direct"]:
    if profile is None:
        return "direct"  # 兜底:理论不该发生(Phase1 保证每个 selected task 都有 profile)
    max_score = max(profile.tag_scores.values(), default=0)
    return "react" if max_score >= 2 else "direct"
```

- `GENERAL_REVIEW` 的 `tag_scores` 固定为 `{GENERAL_REVIEW: 1}`(Phase 2 既有行为),
  自然落入 `max_score < 2` 分支,无需特判。
- tier 决策纯函数、可单测,不依赖 LLM,不落 State,只在节点内部运行时使用。

---

## 4. 节点重构

### 4.1 `make_reviewer_node` 外层调度

```
_node(state):
    tasks, profiles, selection = ...(不变)
    routed_ids = routed_task_ids(...)(不变,Phase2 逻辑)

    若 selection is None:
        保留现状——整份 diff 一次调用(测试/无任务模式的兼容路径,不改)

    若 routed_ids 为空:
        保留现状——no_tasks_routed(不改)

    否则,对每个 task_id in routed_ids:
        profile = profiles.get(task_id)
        tier = decide_tier(profile)
        bundle = task_context_bundles.get(task_id)
        payload = 单 task 调用入参(见 4.2)

    用 run_bounded_parallel(routed_ids, lambda tid: subgraph.invoke(payload(tid)), max_workers=8)
    并发执行所有 task 调用

    逐个收集结果:
        - 调用失败(结果为 None)→ 记 CouncilTrace(event="task_review_failed"),该 task 无候选
        - 调用成功 → issue 直接绑定发起调用时的 task_id(不经 map_candidate_to_task)
    汇总 candidates / evidence_requests / trace,MAX_CANDIDATES_PER_AGENT 等截断逻辑不变
```

### 4.2 单 task 调用入参与 `build_reviewer_subgraph` 内部改动

`prepare → review → collect` 三段结构不变,内部实现调整:

- **`prepare`**:不再用 `_build_user_prompt(diff_text, summary)` 拼整份 diff,改为拼单
  task 的内容:
  - `task.patch`(该 task 的 hunk/文件级 fallback 原始 diff)
  - 该 task 的 risk 信息:复用 `risk_routing.py` 里 `render_task_scope` 单任务渲染那段
    逻辑(risk_tags + risk_signals),抽成独立可复用函数(如
    `render_single_task_risk(task, profile)`),避免和 `render_task_scope` 出现两份
    重复实现。
  - `task_context_bundles[task_id]` 渲染:`TaskContextBundle` 新增 `render()` 方法,
    与既有 `ContextBundle.render()` 同构(facts 列表格式化 + 预算截断标记)。
  - 全局 `context_bundle` **不再**注入 reviewer 的 prompt(它仍保留在 State 里供
    `CouncilJudge` / `fp_rules` 等下游节点使用,只是不再是 reviewer 输入源)。
- **`review`**:按调用方传入的 `tier` 选引擎——`tier == "react"` 用现有
  `ToolAgentEngine`(`reviewer.tool_allowlist` 不变,本轮不按 tag 收窄);
  `tier == "direct"` 用 `DirectEngine`(无工具)。子图内部已有的容错(ReAct 撞递归
  上限降级 Direct、无 issue 时降级复审)完全保留,只是每次处理范围从"多 task"变成
  "单 task"。
- **`collect`**:基本不变,仍产出 `issues` / `gathered_context` / `review_summaries`。

### 4.3 候选收集:去掉行号猜测,直接绑定 task_id

外层 `_node` 收集每个 task 调用的结果时,`CandidateIssue.from_issue(..., task_id=task_id)`
直接使用发起该次调用时的 `task_id`——因为这次 prompt 里本来就只包含这一个 task,
不存在"这条 issue 该归属哪个 task"的歧义。

`task_prep.map_candidate_to_task` 函数**保留不删**,继续服务 `selection is None` 的
兼容路径(测试、非任务化调用场景)。

---

## 5. 并发模型

复用 Phase 3 已有的 `pipeline/concurrency.py:run_bounded_parallel`(有界线程池 +
单项失败隔离 + 按输入顺序回收结果),`max_workers=8`,与 Phase 3 ContextProvider
Level1 调用的并发原语保持一致,不新增配置项、不引入 asyncio。

两层并发关系:reviewer 级 fan-out(T/B/M,LangGraph 既有拓扑,3 路)× task 级
fan-out(每路内部再并发,`run_bounded_parallel`,上限 8)。两层都是有界线程池,
不需要跨层全局限流,未触发 ROADMAP 登记的 async 切换时机。

---

## 6. 错误处理

- **单 task 调用失败**:`run_bounded_parallel` 单项异常隔离,该 task 结果为 `None`,
  不影响同 reviewer 下其它 task,也不影响其它 reviewer。记
  `CouncilTrace(node=reviewer.source_agent, event="task_review_failed", detail=task_id)`。
- **子图内部容错不变**:ReAct 撞递归上限降级 Direct、无 issue 时降级复审——逻辑位置
  不变,只是每次处理范围变成单 task。
- **tier 判断兜底**:`risk_profiles` 查不到某 task_id(理论不该发生)→ 按 `direct`
  处理并记 trace,不抛异常。

---

## 7. 测试计划

- `tests/test_graph_orchestration.py`:
  - 验证同一 reviewer 路由到多个 task 时被拆成多次独立子图调用(mock subgraph 计
    调用次数和入参)。
  - 验证 tier 判断:构造 score=1/2/3/GENERAL_REVIEW 的 profile,断言分别选中
    DirectEngine/ToolAgentEngine。
  - 验证候选直接绑定调用时的 task_id,不经过 `map_candidate_to_task`。
  - 验证单 task 失败不影响同 reviewer 其它 task 的候选产出。
  - 验证 `selection is None` 兼容路径行为不变。
- `tests/test_risk_routing.py`(或新增单元测试文件):`render_single_task_risk` /
  `decide_tier` 纯函数单测。
- `tests/test_tasks_models.py` 或新文件:`TaskContextBundle.render()` 单测(空 facts/
  截断标记/多条 facts 拼接)。
- mock CLI 冒烟(`--provider mock`)验证整条链路仍能出 `ReviewResult`,退出码正常。
- `pipeline-notools` / `pipeline-file` mock eval 各跑一次确认无解析层面崩溃(不追求
  真实质量数字,遵循 ADR-004/008/009"测不出就不硬凑")。

---

## 8. 实施台账

实施完成后按以下格式在本节记录事实(不以设计文字替代实施记录):

| 阶段 | 当前状态 | 已落地内容 | State 变更 | 验证证据 | 刻意未做 |
|---|---|---|---|---|---|
| Phase 4 | done | `pipeline/risk_routing.py`: `render_single_task_risk()`(单 task 风险渲染,供发现者 prompt 复用,cc564a5 抽出的 `decide_tier()` 依赖其输出的 profile 语义)、`decide_tier(profile)`(纯函数,按 `RiskProfile` 打分决定 `"react"`/`"direct"` 引擎分层,cc564a58);`models/tasks.py`: `TaskContextBundle.render(budget=4000)`(发现者 prompt 消费的上下文渲染,含空 facts/截断标记/多条 facts 拼接,fb211ad);`pipeline/task_prep.py`: `file_matches_task(file, task)`(候选 issue 与 task 归属一致性校验,公开供收集节点调用,a713d53);`pipeline/graph.py`: `ReviewerState`(TypedDict)新增 `task_risk_context: str`、`tier: str` 两个字段(010fa68),`_prepare()`/`_review()` 子函数据此组装 prompt 并选择引擎(010fa68,c1d3587 补充说明 `tier=="direct"` 时仍走 `_make_engine` 保留可测试入口),`make_reviewer_node()` 整体改为按 `review_tasks` 逐 task 并发派发(`ThreadPoolExecutor`/`run_bounded_parallel` 风格),不再是单次整 diff 调用(65e0aa7,e6c44d7 补 task 级并发 + 真实 `MemorySaver` checkpointer 回归测试,钉住 fan-out 分支必须显式传 `config/thread_id` 的修复) | `ReviewerState` 新增 `task_risk_context: str`、`tier: str`(其余字段不变,无新增主路由 `ReviewState` 顶层字段) | 2026-07-11 本次验证:`python -m pytest tests/ -q` → `418 passed in 8.95s`;`mypy src` → `Success: no issues found in 42 source files`;`ruff check .` 发现 2 处**非本阶段引入**的既有问题(`visualize_graph.py` 4 处 F541 f-string 无占位符,源自 2026-07-07 的 256ceff;`tests/test_graph_orchestration.py:1366` 1 处 F841 未使用局部变量 `out`,随 Task6 测试一并引入但未清理),未在本次任务范围内修复,如实记录不掩盖;CLI mock 冒烟:`CODEGUARD_PROVIDER=mock python -m codeguard_agent review --repo . --base HEAD` 退出码 0(工作区无变更,打印"没有检测到代码变更,无需审查");追加 `--base HEAD~1` 冒烟(有真实 diff)同样退出码 0,打印出 `ReviewResult`("未发现问题");`python -m evals.runner --profile pipeline-notools --runs 1` 与 `--profile pipeline-file --runs 1` 均退出码 0,报告正常写入 `evals/reports/pipeline.md`,无解析崩溃(P/R/F1 均为 mock 数据下的 0,不作为验收依据,遵循 ADR-004/008/009);对应提交:cc564a58、63738a6、fb211ad、a713d53、010fa68、c1d3587、65e0aa7、e6c44d7;全量收尾 review 发现 `_review()` 里"ReAct 空结果降级复审"原为 ReAct/DeepSeek 偶发空响应写的兜底,Phase4 引入 tier 分层后对 `tier=="direct"` 的多数低危 task 也无条件重跑一次,和"分层降本"的设计初衷相悖;已修复为仅 `tier=="react"` 触发(`tier is None` 的旧兼容路径行为不变),`b0ad5a6`,新增 3 条针对三种 tier 取值的回归测试,`python -m pytest tests/ -q` → `421 passed`,ruff/mypy 仍 clean | RiskTag 收窄工具白名单;知识图谱按标签拆分注入(见后续子阶段设计) |
