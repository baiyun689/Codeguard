# Phase 4 定向发现链(task 级并发 + 风险分层引擎) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让三路发现者(ThreatModelAgent/BehaviorAgent/MaintainabilityAgent)对它们路由到的每个 `ReviewTask` 独立、并发地发起审查调用,并按该 task 的 `RiskProfile` 强度在 ReAct(可用工具)和 Direct(单次无工具调用)之间分层,同时把 Phase 3 已产出但从未被消费的 `task_context_bundles` 真正接入发现者 prompt。

**Architecture:** 在 `pipeline/graph.py` 的 `make_reviewer_node` 里,把"整批路由 task 拼成一份 scoped diff、一次调用"改成"对每个路由到的 task 独立调用 `build_reviewer_subgraph` 生成的子图,用 `run_bounded_parallel` 并发派发"。子图内部的 `prepare/review/collect` 三段结构不变,`prepare` 改为拼单 task 的 patch + risk 信息 + task context,`review` 按调用方传入的 `tier` 字段选择 `DirectEngine` 或 `ToolAgentEngine`。候选的 `task_id` 直接来自发起调用时已知的 task,不再依赖按 file/line 猜测的 `map_candidate_to_task`(该函数保留给 `task_selection is None` 的兼容路径)。`selection is None` 时的整份 diff 兼容路径完全不变。

**Tech Stack:** Python 3 / Pydantic / LangGraph / pytest / 项目既有 `run_bounded_parallel`(`concurrent.futures.ThreadPoolExecutor`)。

---

## 前置说明

- 本计划实现的是 `docs/superpowers/specs/2026-07-11-risk-routed-discovery-phase4-design.md`。实施前建议先读一遍该设计文档。
- 本仓库开发环境用 conda 环境 `codeguard`,所有 Python 命令前缀为 `conda run -n codeguard --no-capture-output ...`(以下步骤为简洁省略,真实执行请带上)。工作目录为 `services/agent`。
- 每个 Task 结束后运行一次相关测试确认不回归,再提交(Conventional Commits,`type(scope): 描述`,不加 `Co-Authored-By`,见 `CLAUDE.md` §6.9)。
- 涉及的核心文件:
  - 修改:`src/codeguard_agent/models/tasks.py`(新增 `TaskContextBundle.render()`)
  - 修改:`src/codeguard_agent/pipeline/risk_routing.py`(新增 `decide_tier` / `render_single_task_risk`,重构 `render_task_scope` 复用后者)
  - 修改:`src/codeguard_agent/pipeline/task_prep.py`(新增公开的 `file_matches_task`)
  - 修改:`src/codeguard_agent/pipeline/graph.py`(`ReviewerState` 新增字段;`build_reviewer_subgraph` 的 `_prepare`/`_review`;`make_reviewer_node` 的 `_node` 重构)
  - 修改/新增测试:`tests/test_risk_routing.py`、`tests/test_tasks_models.py`(或等价文件)、`tests/test_graph_orchestration.py`

---

## Task 1: `decide_tier` 纯函数(引擎分层规则)

**Files:**
- Modify: `src/codeguard_agent/pipeline/risk_routing.py`
- Test: `tests/test_risk_routing.py`

- [ ] **Step 1: 写失败的测试**

在 `tests/test_risk_routing.py` 末尾追加(先确认文件顶部已 `from codeguard_agent.pipeline.risk_routing import decide_tier`,若无则加上;同时需要 `from codeguard_agent.models.tasks import RiskProfile, RiskTag`):

```python
def test_decide_tier_react_when_any_tag_score_at_least_two():
    profile = RiskProfile(
        task_id="t1",
        tag_scores={RiskTag.RESOURCE_LIFECYCLE: 2},
    )
    assert decide_tier(profile) == "react"


def test_decide_tier_direct_when_only_weak_signal():
    profile = RiskProfile(
        task_id="t1",
        tag_scores={RiskTag.SQL_DATA_ACCESS: 1},
    )
    assert decide_tier(profile) == "direct"


def test_decide_tier_direct_for_general_review():
    profile = RiskProfile(
        task_id="t1",
        tag_scores={RiskTag.GENERAL_REVIEW: 1},
    )
    assert decide_tier(profile) == "direct"


def test_decide_tier_react_when_strong_signal_present():
    profile = RiskProfile(
        task_id="t1",
        tag_scores={RiskTag.AUTHORIZATION: 3},
    )
    assert decide_tier(profile) == "react"


def test_decide_tier_direct_when_profile_missing():
    assert decide_tier(None) == "direct"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_risk_routing.py -k decide_tier -v`
Expected: FAIL,报 `ImportError` 或 `AttributeError: module has no attribute 'decide_tier'`

- [ ] **Step 3: 实现 `decide_tier`**

在 `src/codeguard_agent/pipeline/risk_routing.py` 顶部导入区加 `from typing import Literal`,并在文件末尾追加:

```python
def decide_tier(profile: RiskProfile | None) -> Literal["react", "direct"]:
    """按 task 的 RiskProfile 强度决定发现引擎:score>=2(含强信号)进 ReAct,
    否则(纯弱信号或 GENERAL_REVIEW)降级为无工具单次调用。

    分层理由见 spec:score=2 已涵盖控制流/数据流/资源生命周期/一致性类问题
    (如 RESOURCE_LIFECYCLE/TRANSACTION_ATOMICITY),这类问题往往需要工具核实,
    阈值定得比"只有 score=3"更保守，避免因分层误伤这类中危问题。
    """
    if profile is None:
        return "direct"
    max_score = max(profile.tag_scores.values(), default=0)
    return "react" if max_score >= 2 else "direct"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_risk_routing.py -k decide_tier -v`
Expected: PASS(5 passed)

- [ ] **Step 5: 提交**

```bash
git add src/codeguard_agent/pipeline/risk_routing.py tests/test_risk_routing.py
git commit -m "feat(risk_routing): 新增 task 风险分层引擎决策 decide_tier"
```

---

## Task 2: 抽出 `render_single_task_risk`,供单 task 调用复用

**Files:**
- Modify: `src/codeguard_agent/pipeline/risk_routing.py`
- Test: `tests/test_risk_routing.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_risk_routing.py`:

```python
def test_render_single_task_risk_includes_tags_and_signals():
    task = G_ReviewTask = __import__(
        "codeguard_agent.models.tasks", fromlist=["ReviewTask"]
    ).ReviewTask(id="A.java#h0", file="A.java", patch="+x")
    profile = RiskProfile(
        task_id="A.java#h0",
        tag_scores={RiskTag.AUTHORIZATION: 3},
        signals=[
            __import__(
                "codeguard_agent.models.tasks", fromlist=["RiskSignal"]
            ).RiskSignal(
                tag=RiskTag.AUTHORIZATION,
                score=3,
                source="text:deleted:authorization_guard_removed",
                reason="删除 @PreAuthorize",
            )
        ],
    )
    rendered = render_single_task_risk(task, profile)
    assert "AUTHORIZATION" in rendered
    assert "删除 @PreAuthorize" in rendered
    assert "+x" in rendered


def test_render_single_task_risk_omits_zero_score_tags():
    from codeguard_agent.models.tasks import ReviewTask, RiskSignal

    task = ReviewTask(id="A.java#h0", file="A.java", patch="+x")
    profile = RiskProfile(
        task_id="A.java#h0",
        tag_scores={RiskTag.AUTHORIZATION: 3, RiskTag.PERFORMANCE: 0},
        signals=[
            RiskSignal(
                tag=RiskTag.AUTHORIZATION,
                score=3,
                source="text:deleted:authorization_guard_removed",
                reason="删除 @PreAuthorize",
            )
        ],
    )
    rendered = render_single_task_risk(task, profile)
    assert "PERFORMANCE" not in rendered
```

(上面第一个测试用 `__import__` 只是为了不重复改 import 头;实现时请直接在文件顶部
补 `from codeguard_agent.models.tasks import ReviewTask, RiskSignal`,并把两个测试
都改成直接用 `ReviewTask` / `RiskSignal`,不要保留 `__import__` 写法——这里只是
说明必须覆盖的断言点。)

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_risk_routing.py -k render_single_task_risk -v`
Expected: FAIL,`AttributeError: module has no attribute 'render_single_task_risk'`

- [ ] **Step 3: 从 `render_task_scope` 里抽出单任务渲染逻辑**

打开 `src/codeguard_agent/pipeline/risk_routing.py`,把 `render_task_scope` 函数体里
`for task_id in routed_task_ids(...)` 循环内部构造 `<task>` 块的逻辑抽成新函数,
放在 `render_task_scope` 定义之前:

```python
def render_single_task_risk(task: ReviewTask, profile: RiskProfile) -> str:
    """渲染单个 task 的风险标签块(<task><risk_tags><risk_signals><patch>),
    供 Phase4 单 task 调用和 render_task_scope 共用,避免两处重复实现。"""
    tags = sorted(tag.value for tag, score in profile.tag_scores.items() if score > 0)
    signals = [
        f"{signal.source}:{signal.reason}"
        for signal in profile.signals
        if signal.tag in profile.tag_scores and profile.tag_scores[signal.tag] > 0
    ]
    parts = [
        f'<task id="{task.id}" file="{task.file}">',
        f"<risk_tags>{','.join(tags)}</risk_tags>",
        f"<risk_signals>{'; '.join(signals)}</risk_signals>",
        "<patch>",
        task.patch,
        "</patch>",
        "</task>",
    ]
    return "\n".join(parts)
```

然后把 `render_task_scope` 里对应的循环体改为调用它:

```python
def render_task_scope(
    reviewer_source_agent: str,
    tasks: list[ReviewTask],
    profiles: Mapping[str, RiskProfile],
    selection: TaskSelection,
) -> str:
    """Render only this reviewer's selected tasks and their evidence."""
    reviewer = _canonical_reviewer(reviewer_source_agent)
    task_by_id = {task.id: task for task in tasks}
    parts = [f'<review_scope reviewer="{_REVIEWER_NAMES.get(reviewer, reviewer)}">']
    for task_id in routed_task_ids(reviewer_source_agent, tasks, profiles, selection):
        task = task_by_id[task_id]
        profile = profiles[task_id]
        parts.append(render_single_task_risk(task, profile))
    parts.append("</review_scope>")
    return "\n".join(parts)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_risk_routing.py -v`
Expected: PASS(全部,含既有的 `render_task_scope` 测试——它的输出内容不变,只是
实现路径变了,断言应仍然全过)

- [ ] **Step 5: 提交**

```bash
git add src/codeguard_agent/pipeline/risk_routing.py tests/test_risk_routing.py
git commit -m "refactor(risk_routing): 抽出 render_single_task_risk 供单task调用复用"
```

---

## Task 3: `TaskContextBundle.render()`

**Files:**
- Modify: `src/codeguard_agent/models/tasks.py`
- Test: 新建或追加 `tests/test_tasks_models.py`

- [ ] **Step 1: 写失败的测试**

在 `tests/test_tasks_models.py` 追加(若文件不存在则新建,并在顶部加
`from codeguard_agent.models.tasks import TaskContextBundle` 及
`from codeguard_agent.models.council import ContextFact`):

```python
def test_task_context_bundle_render_empty_facts():
    bundle = TaskContextBundle(task_id="A.java#h0")
    assert bundle.render() == "(无任务上下文事实)"


def test_task_context_bundle_render_lists_facts_with_truncation_flag():
    bundle = TaskContextBundle(
        task_id="A.java#h0",
        facts=[
            ContextFact(source="diff", kind="sensitive_api", content="Runtime.exec"),
            ContextFact(
                source="tool:find_callers", kind="callers", content="X.java:10",
                truncated=True,
            ),
        ],
    )
    rendered = bundle.render()
    assert "Runtime.exec" in rendered
    assert "(已截断)" in rendered


def test_task_context_bundle_render_respects_budget():
    bundle = TaskContextBundle(
        task_id="A.java#h0",
        facts=[ContextFact(source="diff", kind="x", content="A" * 100)],
    )
    rendered = bundle.render(budget=10)
    assert len(rendered) <= 10 + len("\n...(TaskContextBundle 已达预算上限,后续省略)")
    assert rendered.endswith("...(TaskContextBundle 已达预算上限,后续省略)")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_tasks_models.py -k render -v`
Expected: FAIL,`AttributeError: 'TaskContextBundle' object has no attribute 'render'`

- [ ] **Step 3: 实现 `render()`**

在 `src/codeguard_agent/models/tasks.py` 的 `TaskContextBundle` 类里追加方法
(与 `models/council.py` 的 `ContextBundle.render()` 同构):

```python
class TaskContextBundle(BaseModel):
    """按任务构建的上下文包。不复制 file/patch/RiskTag（通过 task_id 关联读取）。"""

    task_id: str
    facts: list[ContextFact] = Field(default_factory=list)
    truncated: bool = False

    def render(self, budget: int = 4000) -> str:
        """渲染为 prompt 可读文本，并按字符预算截断。"""
        if not self.facts:
            return "(无任务上下文事实)"
        lines = ["任务上下文事实:"]
        for fact in self.facts:
            flag = " (已截断)" if fact.truncated else ""
            lines.append(f"- [{fact.source}/{fact.kind}]{flag} {fact.content}")
        text = "\n".join(lines).strip()
        if len(text) <= budget:
            return text
        return text[:budget] + "\n...(TaskContextBundle 已达预算上限,后续省略)"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_tasks_models.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/codeguard_agent/models/tasks.py tests/test_tasks_models.py
git commit -m "feat(models): TaskContextBundle 新增 render() 供发现者 prompt 消费"
```

---

## Task 4: `task_prep.file_matches_task`(公开的文件一致性校验)

**Files:**
- Modify: `src/codeguard_agent/pipeline/task_prep.py`
- Test: `tests/test_task_prep.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_task_prep.py`(确认顶部已 import `ReviewTask`,若无则加):

```python
def test_file_matches_task_true_for_exact_path():
    task = ReviewTask(id="a#h0", file="src/main/java/A.java", patch="")
    assert file_matches_task("src/main/java/A.java", task) is True


def test_file_matches_task_true_for_basename_fallback():
    task = ReviewTask(id="a#h0", file="src/main/java/A.java", patch="")
    assert file_matches_task("A.java", task) is True


def test_file_matches_task_false_for_different_file():
    task = ReviewTask(id="a#h0", file="src/main/java/A.java", patch="")
    assert file_matches_task("B.java", task) is False
```

(需要在文件顶部加 `from codeguard_agent.pipeline.task_prep import file_matches_task`,
若测试文件是通过 `from codeguard_agent.pipeline.task_prep import *` 或模块别名导入,
按现有文件的既有导入风格调整。)

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_task_prep.py -k file_matches_task -v`
Expected: FAIL,`ImportError`

- [ ] **Step 3: 实现 `file_matches_task`**

在 `src/codeguard_agent/pipeline/task_prep.py` 里,紧跟 `_basename` 函数之后新增
(注意这是新增的**公开**函数,复用已有的 `_norm`/`_basename` 私有 helper):

```python
def file_matches_task(file: str, task: ReviewTask) -> bool:
    """候选文件是否属于该 task 的文件（全路径精确匹配优先，退化到 basename）。

    Phase4 单 task 调用不再做行号级映射（prompt 本来就只含这一个 task），但仍需要
    这道最基本的一致性校验，防止模型报告了完全无关的文件却被直接绑定到该 task。
    """
    return _norm(file) == _norm(task.file) or _basename(file) == _basename(task.file)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_task_prep.py -v`
Expected: PASS(全部)

- [ ] **Step 5: 提交**

```bash
git add src/codeguard_agent/pipeline/task_prep.py tests/test_task_prep.py
git commit -m "feat(task_prep): 新增公开的 file_matches_task 一致性校验"
```

---

## Task 5: `ReviewerState` 新增字段 + `_prepare`/`_review` 改为单 task 输入

**Files:**
- Modify: `src/codeguard_agent/pipeline/graph.py`
- Test: `tests/test_graph_orchestration.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_graph_orchestration.py`(紧挨着现有的
`test_reviewer_prompt_contains_summary_once` 之后即可):

```python
def test_reviewer_prepare_injects_task_risk_context_instead_of_global_bundle(monkeypatch):
    captured = {}

    class _CapturingEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            captured["user_prompt"] = user_prompt
            return ReviewOutcome(ReviewResult(summary="s"))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _CapturingEngine())
    sub = G.build_reviewer_subgraph(G.DEFAULT_REVIEWERS[0], llm=_FakeLLM())
    sub.invoke(
        {
            "diff_text": "+risky",
            "task_risk_context": "<task id=\"A.java#h0\">RISK_BLOCK</task>",
            "tier": "direct",
            "context_bundle": G.ContextBundle(facts=[
                G.ContextFact(source="diff", kind="x", content="SHOULD_NOT_APPEAR"),
            ]),
        }
    )
    assert "RISK_BLOCK" in captured["user_prompt"]
    assert "SHOULD_NOT_APPEAR" not in captured["user_prompt"]


def test_reviewer_prepare_falls_back_to_global_bundle_without_task_risk_context(monkeypatch):
    captured = {}

    class _CapturingEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            captured["user_prompt"] = user_prompt
            return ReviewOutcome(ReviewResult(summary="s"))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _CapturingEngine())
    sub = G.build_reviewer_subgraph(G.DEFAULT_REVIEWERS[0], llm=_FakeLLM())
    sub.invoke(
        {
            "diff_text": "+risky",
            "context_bundle": G.ContextBundle(facts=[
                G.ContextFact(source="diff", kind="x", content="LEGACY_PATH"),
            ]),
        }
    )
    assert "LEGACY_PATH" in captured["user_prompt"]


def test_reviewer_review_uses_direct_engine_when_tier_is_direct(monkeypatch):
    calls = []

    class _ShouldNotBeCalledToolEngine:
        def __init__(self, *a, **k):
            calls.append("tool_agent")

    monkeypatch.setattr(G, "ToolAgentEngine", _ShouldNotBeCalledToolEngine)
    sub = G.build_reviewer_subgraph(
        G.DEFAULT_REVIEWERS[0], llm=_FakeLLM(), tool_client=object()
    )
    sub.invoke({"diff_text": "+x", "tier": "direct"})
    assert "tool_agent" not in calls
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_graph_orchestration.py -k "task_risk_context or tier_is_direct" -v`
Expected: FAIL——`task_risk_context`/`tier` 未被消费,第一个测试会因为 `SHOULD_NOT_APPEAR`
仍出现在 user_prompt 里而失败;第三个测试会因为 `tool_client` 非空时仍构造了
`ToolAgentEngine` 而失败。

- [ ] **Step 3: 修改 `ReviewerState` 与 `_prepare`/`_review`**

在 `src/codeguard_agent/pipeline/graph.py` 的 `ReviewerState` 定义(约第 229 行)里
新增两个字段:

```python
class ReviewerState(TypedDict, total=False):
    """单个发现者 Agent 子图状态。"""

    diff_text: str
    enabled_tools: Any
    max_retries: int
    structured_method: str
    diff_summary: str
    react_recursion_limit: int
    context_bundle: ContextBundle
    task_risk_context: str
    tier: str

    issues: list
    gathered_context: list
    review_summaries: list
    council_trace: Annotated[list[CouncilTrace], operator.add]

    user_prompt: str
    outcome: Any
```

把 `build_reviewer_subgraph` 里的 `_prepare` 改为:

```python
    def _prepare(state: ReviewerState) -> dict:
        if llm is None:
            return {}
        user = _build_user_prompt(
            state["diff_text"], summary=state.get("diff_summary", "")
        )
        task_risk_context = state.get("task_risk_context")
        if task_risk_context:
            user += "\n\n" + task_risk_context
        else:
            bundle = state.get("context_bundle")
            if bundle is not None:
                user += "\n\n<shared_context>\n" + bundle.render() + "\n</shared_context>"
        return {"user_prompt": user}
```

把 `_review` 里选择引擎的部分(原来直接 `engine = _make_engine(state, tool_client=tool_client)`)
改为:

```python
    def _review(state: ReviewerState) -> dict:
        if llm is None:
            if reviewer.source_agent == "threat_model":
                return {"outcome": ReviewOutcome(mock_review_result())}
            return {"outcome": ReviewOutcome(ReviewResult(summary=""))}
        tier = state.get("tier")
        engine = (
            DirectEngine()
            if tier == "direct"
            else _make_engine(state, tool_client=tool_client)
        )
        try:
            outcome = engine.review(
                llm,
                system_prompt=_load_prompt(reviewer.prompt_file),
                user_prompt=state.get("user_prompt", ""),
                reviewer_name=reviewer.name,
                max_retries=state.get("max_retries", 3),
                structured_method=state.get("structured_method", "function_calling"),
                enable_hitl=False,
            )
        except Exception as exc:  # noqa: BLE001 单发现者失败不拖垮 council
            from langgraph.errors import GraphRecursionError

            if isinstance(exc, GraphRecursionError):
                logger.warning("[%s] 发现者撞递归上限,降级直连: %s", reviewer.name, exc)
                outcome = _direct_fallback(state)
            else:
                logger.warning("[%s] 发现者失败,跳过: %s", reviewer.name, exc)
                return {
                    "outcome": ReviewOutcome(ReviewResult(summary="")),
                    "council_trace": [
                        CouncilTrace(node=reviewer.source_agent, event="discover_failed", detail=str(exc))
                    ],
                }

        if not outcome.result.issues:
            logger.warning(
                "[%s] ReAct 未产出 issue,降级直连复审以保住该域覆盖", reviewer.name
            )
            outcome = _direct_fallback(state)
        return {"outcome": outcome}
```

(`tier` 缺省时——即现有 `selection is None` 兼容路径——`state.get("tier")` 为 `None`,
落进 `else` 分支走 `_make_engine`,行为和改动前完全一致,不影响旧路径。)

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_graph_orchestration.py -v`
Expected: PASS(全部,包括本 Task 新增的 3 个和此前所有既有测试——这一步只改了
`_prepare`/`_review` 内部逻辑,`selection is None` 路径和现有 `test_reviewer_*`
系列测试的输入都不带 `task_risk_context`/`tier`,行为应保持不变)

- [ ] **Step 5: 提交**

```bash
git add src/codeguard_agent/pipeline/graph.py tests/test_graph_orchestration.py
git commit -m "feat(graph): reviewer 子图按 task_risk_context/tier 组装 prompt 与选择引擎"
```

---

## Task 6: `make_reviewer_node` 改造为单 task 并发派发

**Files:**
- Modify: `src/codeguard_agent/pipeline/graph.py`
- Test: `tests/test_graph_orchestration.py`(新增 + 改写两个既有测试)

- [ ] **Step 1: 改写两个语义已失效的既有测试**

`test_make_reviewer_node_rejects_unselected_task` 和
`test_make_reviewer_node_passes_only_routed_scope_and_rejects_unrouted_candidate`
依赖"整批 task 一次调用 + 按 file/line 猜归属"的旧机制,在单 task 独立调用架构下
这两种"猜错归属"的场景不再会发生(每次调用天然只对应一个 task)。把它们替换成
验证新的 `candidate_rejected_task_mismatch` 校验:

用下面的内容**替换**`test_make_reviewer_node_rejects_unselected_task`(保留函数名前的
其它测试不动):

```python
def test_make_reviewer_node_only_invokes_routed_and_selected_tasks(monkeypatch):
    """收集节点：未被 TaskRank 选中/未路由到本 reviewer 的 task 根本不会被调用。"""
    invoked_task_files = []

    class _RecordingEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            invoked_task_files.append(user_prompt)
            return ReviewOutcome(ReviewResult(summary="s"))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _RecordingEngine())
    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[0], llm=_FakeLLM())
    out = node({
        "diff_text": "+x\n+y",
        "review_tasks": [
            G.ReviewTask(id="A.java#h0", file="A.java", patch="+x", changed_lines=[1]),
            G.ReviewTask(id="B.java#h0", file="B.java", patch="+y", changed_lines=[1]),
        ],
        # 只选中 B.java#h0；A.java#h0 即使命中 ThreatModelAgent 的标签也不该被调用
        "task_selection": G.TaskSelection(selected_task_ids=["B.java#h0"]),
        "risk_profiles": {
            "B.java#h0": G.RiskProfile(
                task_id="B.java#h0",
                tag_scores={RiskTag.GENERAL_REVIEW: 1},
                signals=[
                    RiskSignal(
                        tag=RiskTag.GENERAL_REVIEW, score=1,
                        source="fallback:unclassified", reason="fallback",
                    )
                ],
            )
        },
    })
    assert len(invoked_task_files) == 1
    assert "+y" in invoked_task_files[0]
    assert "+x" not in invoked_task_files[0]
```

用下面的内容**替换**`test_make_reviewer_node_passes_only_routed_scope_and_rejects_unrouted_candidate`:

```python
def test_make_reviewer_node_rejects_candidate_with_mismatched_file(monkeypatch):
    """收集节点：某 task 调用返回的 issue.file 和被调用 task 的 file 对不上 → 拒绝。"""

    class _WrongFileEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            issue = Issue(
                severity=Severity.WARNING, file="Unrelated.java", line=1,
                type="t", message="m",
            )
            return ReviewOutcome(ReviewResult(summary="s", issues=[issue]))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _WrongFileEngine())
    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[1], llm=_FakeLLM())
    task = G.ReviewTask(id="A.java#h0", file="A.java", patch="+sql", changed_lines=[1])
    out = node({
        "diff_text": "+sql",
        "review_tasks": [task],
        "risk_profiles": {
            task.id: G.RiskProfile(
                task_id=task.id,
                tag_scores={RiskTag.SQL_DATA_ACCESS: 1},
                signals=[
                    RiskSignal(
                        tag=RiskTag.SQL_DATA_ACCESS, score=1,
                        source="text:added:sql_data_access", reason="query",
                    )
                ],
            )
        },
        "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
    })
    assert out["candidate_issues"] == []
    events = {t.event for t in out["council_trace"]}
    assert "candidate_rejected_task_mismatch" in events
```

再追加一个验证并发派发和 tier 分层的新测试:

```python
def test_make_reviewer_node_invokes_tasks_concurrently_with_correct_tier(monkeypatch):
    seen_tiers = {}

    class _Engine:
        def __init__(self, tier):
            self._tier = tier

        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            return ReviewOutcome(ReviewResult(summary="s"))

    def _fake_subgraph_invoke(payload):
        seen_tiers[payload["diff_text"]] = payload.get("tier")
        return {"issues": [], "council_trace": []}

    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[1], llm=_FakeLLM())
    # 直接monkeypatch build_reviewer_subgraph返回的子图invoke，验证tier透传即可，
    # 不需要真的跑一次LLM调用。
    import types

    fake_subgraph = types.SimpleNamespace(invoke=_fake_subgraph_invoke)
    monkeypatch.setattr(
        G, "build_reviewer_subgraph", lambda *a, **k: fake_subgraph
    )
    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[1], llm=_FakeLLM())
    tasks = [
        G.ReviewTask(id="A.java#h0", file="A.java", patch="+strong", changed_lines=[1]),
        G.ReviewTask(id="B.java#h0", file="B.java", patch="+weak", changed_lines=[1]),
    ]
    node({
        "diff_text": "+strong\n+weak",
        "review_tasks": tasks,
        "risk_profiles": {
            "A.java#h0": G.RiskProfile(
                task_id="A.java#h0", tag_scores={RiskTag.CONCURRENCY_CONSISTENCY: 2},
            ),
            "B.java#h0": G.RiskProfile(
                task_id="B.java#h0", tag_scores={RiskTag.SQL_DATA_ACCESS: 1},
            ),
        },
        "task_selection": G.TaskSelection(
            selected_task_ids=["A.java#h0", "B.java#h0"]
        ),
    })
    assert seen_tiers["+strong"] == "react"
    assert seen_tiers["+weak"] == "direct"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_graph_orchestration.py -k "only_invokes_routed_and_selected or rejects_candidate_with_mismatched_file or invokes_tasks_concurrently" -v`
Expected: FAIL(新逻辑还没写)

- [ ] **Step 3: 重写 `make_reviewer_node` 的 `_node`**

打开 `src/codeguard_agent/pipeline/graph.py`,找到 `make_reviewer_node` 函数
(约第 598 行)。把 `_node` 里"若 `routed_ids` 非空"之后、直到 `return` 之前的
整段(原来的"拼 scoped_diff → 单次 subgraph.invoke → map_candidate_to_task 逐条
校验"逻辑)替换成:

```python
    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        profiles = state.get("risk_profiles") or {}
        selection = state.get("task_selection")
        routed_ids = (
            set(routed_task_ids(reviewer.source_agent, tasks, profiles, selection))
            if selection is not None
            else None
        )
        if routed_ids is not None and not routed_ids:
            return {
                "candidate_issues": [],
                "evidence_requests": [],
                "truncated_candidates": 0,
                "truncated_evidence_requests": 0,
                "council_trace": [
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event="no_tasks_routed",
                        detail="selected tasks do not match reviewer risk tags",
                    )
                ],
            }

        effective_tools = (
            state.get("enabled_tools")
            if state.get("enabled_tools") is not None
            else reviewer.tool_allowlist
        )

        if selection is None:
            # 兼容路径：无任务化 State（测试 / 非任务化调用场景）——整份 diff 一次调用，
            # 沿用 map_candidate_to_task 的按 file/line 猜测归属。
            result = subgraph.invoke(
                {
                    "diff_text": state.get("diff_text", ""),
                    "enabled_tools": effective_tools,
                    "max_retries": state.get("max_retries", 3),
                    "structured_method": state.get("structured_method", "function_calling"),
                    "diff_summary": state.get("diff_summary", ""),
                    "react_recursion_limit": state.get("react_recursion_limit", 24),
                    "context_bundle": state.get("context_bundle"),
                }
            )
            issues = list(result.get("issues") or [])
            kept_issues = issues[:MAX_CANDIDATES_PER_AGENT]
            truncated_candidates = max(0, len(issues) - len(kept_issues))
            candidates: list[CandidateIssue] = []
            rejected_unmapped: list[str] = []
            accepted_count = 0
            for issue in kept_issues:
                task_id = task_prep.map_candidate_to_task(issue.file, issue.line, tasks)
                if task_id is None:
                    rejected_unmapped.append(f"{issue.file}:{issue.line}")
                    continue
                accepted_count += 1
                candidates.append(
                    CandidateIssue.from_issue(
                        issue, source_agent=reviewer.source_agent,
                        index=accepted_count, task_id=task_id,
                    )
                )
            truncated_evidence_requests = 0
            requests: list[EvidenceRequest] = []
            for candidate in candidates:
                candidate_requests = build_evidence_requests(candidate)
                requests.extend(candidate_requests[:MAX_EVIDENCE_REQUESTS_PER_CANDIDATE])
                truncated_evidence_requests += max(
                    0, len(candidate_requests) - MAX_EVIDENCE_REQUESTS_PER_CANDIDATE,
                )
            trace: list[CouncilTrace] = list(result.get("council_trace") or [])
            trace.append(
                CouncilTrace(
                    node=reviewer.source_agent,
                    event="candidates_created",
                    detail=(
                        f"count={len(candidates)} truncated={truncated_candidates} "
                        f"rejected_unmapped={len(rejected_unmapped)}"
                    ),
                )
            )
            if rejected_unmapped:
                trace.append(
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event="candidate_rejected_unmapped",
                        detail="; ".join(rejected_unmapped),
                    )
                )
            return {
                "candidate_issues": candidates,
                "evidence_requests": requests[:MAX_TOTAL_EVIDENCE_REQUESTS],
                "truncated_candidates": truncated_candidates,
                "truncated_evidence_requests": truncated_evidence_requests,
                "council_trace": trace,
            }

        # Phase4：每个路由到的 task 独立调用，task 间并发派发。
        task_by_id = {t.id: t for t in tasks}
        task_context_bundles = state.get("task_context_bundles") or {}
        ordered_ids = list(routed_task_ids(reviewer.source_agent, tasks, profiles, selection))

        def _invoke_one(task_id: str) -> dict:
            task = task_by_id[task_id]
            profile = profiles.get(task_id)
            tier = decide_tier(profile)
            risk_text = render_single_task_risk(task, profile) if profile is not None else ""
            bundle = task_context_bundles.get(task_id)
            bundle_text = bundle.render() if bundle is not None else ""
            task_risk_context = "\n\n".join(p for p in (risk_text, bundle_text) if p)
            return subgraph.invoke(
                {
                    "diff_text": task.patch,
                    "enabled_tools": effective_tools,
                    "max_retries": state.get("max_retries", 3),
                    "structured_method": state.get("structured_method", "function_calling"),
                    "diff_summary": state.get("diff_summary", ""),
                    "react_recursion_limit": state.get("react_recursion_limit", 24),
                    "task_risk_context": task_risk_context,
                    "tier": tier,
                }
            )

        task_results = run_bounded_parallel(ordered_ids, _invoke_one, max_workers=8)

        per_task_issues: list[tuple[str, Any]] = []
        trace: list[CouncilTrace] = []
        for task_id, result in zip(ordered_ids, task_results):
            if result is None:
                trace.append(
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event="task_review_failed",
                        detail=task_id,
                    )
                )
                continue
            for issue in result.get("issues") or []:
                per_task_issues.append((task_id, issue))
            trace.extend(result.get("council_trace") or [])

        kept_pairs = per_task_issues[:MAX_CANDIDATES_PER_AGENT]
        truncated_candidates = max(0, len(per_task_issues) - len(kept_pairs))

        candidates: list[CandidateIssue] = []
        rejected_mismatched: list[str] = []
        accepted_count = 0
        for task_id, issue in kept_pairs:
            task = task_by_id[task_id]
            if not task_prep.file_matches_task(issue.file, task):
                rejected_mismatched.append(f"{issue.file}:{issue.line} -> {task_id}")
                continue
            accepted_count += 1
            candidates.append(
                CandidateIssue.from_issue(
                    issue, source_agent=reviewer.source_agent,
                    index=accepted_count, task_id=task_id,
                )
            )

        truncated_evidence_requests = 0
        requests: list[EvidenceRequest] = []
        for candidate in candidates:
            candidate_requests = build_evidence_requests(candidate)
            requests.extend(candidate_requests[:MAX_EVIDENCE_REQUESTS_PER_CANDIDATE])
            truncated_evidence_requests += max(
                0, len(candidate_requests) - MAX_EVIDENCE_REQUESTS_PER_CANDIDATE,
            )

        trace.append(
            CouncilTrace(
                node=reviewer.source_agent,
                event="candidates_created",
                detail=(
                    f"count={len(candidates)} truncated={truncated_candidates} "
                    f"rejected_task_mismatch={len(rejected_mismatched)}"
                ),
            )
        )
        if rejected_mismatched:
            trace.append(
                CouncilTrace(
                    node=reviewer.source_agent,
                    event="candidate_rejected_task_mismatch",
                    detail="; ".join(rejected_mismatched),
                )
            )

        return {
            "candidate_issues": candidates,
            "evidence_requests": requests[:MAX_TOTAL_EVIDENCE_REQUESTS],
            "truncated_candidates": truncated_candidates,
            "truncated_evidence_requests": truncated_evidence_requests,
            "council_trace": trace,
        }
```

同时在文件顶部导入区加入本次新增的两个函数:

```python
from codeguard_agent.pipeline.risk_routing import (
    decide_tier,
    render_single_task_risk,
    render_task_scope,
    routed_task_ids,
)
```

(`render_task_scope` 在新架构下不再被 `make_reviewer_node` 调用,但保留导入——它
仍可能被其它测试或未来兼容路径引用;若 ruff 报 unused-import,直接删掉该行即可,
以 lint 结果为准。)

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_graph_orchestration.py -v`
Expected: PASS(全部——包括本 Task 新增/改写的测试,以及所有此前未触及的既有测试,
如 `test_make_reviewer_node_skips_reviewer_without_routed_tasks` /
`test_make_reviewer_node_rejects_unmapped_candidate`)

- [ ] **Step 5: 全量测试 + lint + mypy**

Run:
```bash
python -m pytest tests/ -q
ruff check src/
mypy src/
```
Expected: 全部通过(pytest all passed;ruff/mypy clean)

- [ ] **Step 6: 提交**

```bash
git add src/codeguard_agent/pipeline/graph.py tests/test_graph_orchestration.py
git commit -m "feat(graph): reviewer 节点改为按 task 并发独立调用 + 引擎分层"
```

---

## Task 7: mock CLI 冒烟 + evals 解析层验证

**Files:**
- 不新增文件,仅运行既有命令验证。

- [ ] **Step 1: mock 模式冒烟**

Run:
```bash
python -m codeguard_agent review --repo . --base HEAD
```
(先确保 `CODEGUARD_PROVIDER=mock`,或在 `.env` 里已配置)
Expected: 退出码 0,能打印出 `ReviewResult`(或"无需审查",取决于当前工作区是否有 diff)

- [ ] **Step 2: eval 解析层验证(不追求质量数字)**

Run:
```bash
python -m evals.runner --profile pipeline-notools --runs 1
python -m evals.runner --profile pipeline-file --runs 1
```
Expected: 两次都能正常跑完并产出报告,不出现解析异常/崩溃。P/R/F1 数字不作为
本阶段验收依据(mock/合成数据集下的数字，遵循 ADR-004/008/009 一贯的"测不出就不
硬凑"原则)。

- [ ] **Step 3: 更新 Phase4 设计文档的实施台账**

打开 `docs/superpowers/specs/2026-07-11-risk-routed-discovery-phase4-design.md`
第 8 节"实施台账",把 Phase4 那一行的"当前状态"改为 `done`,"已落地内容"填入
实际改动的文件和函数,"验证证据"填入本次全量测试的通过数字(如
`全量 pytest N passed;ruff/mypy clean`)和对应 commit hash,"刻意未做"保持
"RiskTag 收窄工具白名单;知识图谱按标签拆分注入(见后续子阶段设计)"。

- [ ] **Step 4: 提交台账更新**

```bash
git add docs/superpowers/specs/2026-07-11-risk-routed-discovery-phase4-design.md
git commit -m "docs(orchestration): 记录 Phase4 定向发现链落地"
```
