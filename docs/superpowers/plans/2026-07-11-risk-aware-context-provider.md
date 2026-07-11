# 风险感知 ContextProvider(阶段 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `_context_provider_node` 按每个选中任务的 `RiskProfile.tag_scores`,用纯规则(不调
LLM)把 Level0(全局 AST/敏感 API 结果重新切片)和 Level1(按 tag 定向触发 `find_callers`/
`get_code_metrics`,去重后并发执行)两层事实填进该任务的 `TaskContextBundle.facts`。

**Architecture:** 新增两个纯函数模块——`pipeline/concurrency.py`(通用有界并行派发)和
`pipeline/context_rules.py`(RiskTag→上下文策略映射 + AST/敏感API切片 + 预算截断,均为可单测的
纯函数,不直接碰 `tool_client`)。`graph.py` 的 `_context_provider_node` 只做"编排":调用
`context_rules.plan_context_calls` 拿计划、用 `run_bounded_parallel` 执行 Level1 调用、把结果
装配进 `TaskContextBundle` 并写 `council_trace`。

**Tech Stack:** Python 3.12、pydantic、LangGraph、pytest、`concurrent.futures.ThreadPoolExecutor`。

**参考设计文档:** `docs/superpowers/specs/2026-07-11-risk-aware-context-provider-design.md`

## Global Constraints

- 只消费既有 `ReviewTask → RiskProfile → TaskSelection → TaskContextBundle` 主链，不新增 `ReviewState` 字段，也不改变 `Issue` / `ReviewResult` 契约。
- ContextProvider 只产生带来源的事实；不调 LLM、不生成风险标签、不判断“是不是问题”。
- 不新增 Java Gateway 工具、HTTP 协议或调用图能力；Python 只经现有 ToolClient 调用 Java 工具。
- `GENERAL_REVIEW` 只接收 Level0 事实，绝不触发 Level1；方法无法从 AST 解析时只记录 `no_method_resolved`，不从 patch 正则猜测。
- Level1 单项失败、超时或失败信封均不阻断审查，且失败文本不得进入 `TaskContextBundle.facts`。
- 每个任务的事实内容按 `ReviewBudget.max_context_chars_per_task=4000` 截断；并发上限固定为 8，不增加环境变量、不引入 async。
- 实施遵循 TDD：先观察目标测试失败，再写最小生产代码；每项完成后运行覆盖测试，最终运行 pytest、ruff 和 mypy。

## 文件职责

| 文件 | 职责 |
|---|---|
| `pipeline/concurrency.py` | 有界线程池、单项失败隔离、输入顺序回收；不含业务或工具知识。 |
| `pipeline/context_rules.py` | RiskTag→Level1 规划、Level0 文本切片和预算截断的纯函数；公开稳定 helper，不依赖 `ContextProviderStage` 私有实现。 |
| `models/tasks.py` | 任务级事实预算的默认值。 |
| `pipeline/graph.py` | 调用既有全局 ContextProvider、执行 Level1 计划、装配任务包与 trace。 |
| `tests/test_concurrency.py` | 并发工具的顺序、失败隔离和上限回归。 |
| `tests/test_context_rules.py` | 规划、切片、解析和截断的纯函数回归。 |
| `tests/test_graph_orchestration.py` | Java 工具文本协议到任务包的集成回归。 |

---

## 已批准的方案 A 修订（本节优先于后续同名步骤）

1. **TDD 顺序**：原 Task 7 的集成测试必须在 Task 6 修改 `graph.py` 之前写入并执行，
   作为 Task 6 的 Step 0。测试红灯后才允许编辑图节点；Task 7 只保留全量回归与该测试的
   commit。这样每一段生产实现都有已观察到的失败测试。
2. **公开 helper**：`context_rules.py` 提供 `normalize_path(path: str) -> str`，图节点使用它；
   AST 块拆分实现在 `context_rules.py` 自己的私有 `_split_ast_blocks` 中，不能 import
   `ContextProviderStage` 模块的 `_split_ast_blocks`，也不能从 `graph.py` 调用 `_norm_path`。
3. **Level1 成功判定**：`_execute_level1_call` 返回
   `tuple[Level1Call, str | None, str]`（调用、成功内容、失败原因）。只有
   `response.success is True` 且 `response.as_tool_output().strip()` 非空时返回内容；否则返回
   `None` 内容和错误原因。节点只把成功内容转换为 `ContextFact` 与 `GatheredContext`，并将
   失败原因写进该 task 的 `task_bundle_filled` trace。
4. **真实协议测试**：敏感 API 的测试输入必须匹配 Gateway
   `FindSensitiveApisTool.formatFinding()`：`| 🔴 HIGH | \`Statement.execute\` | A.java:12 | \`sql\` |`。
   AST 测试输入必须匹配 `ASTContextFormatter` 的四空格方法签名与 `[Lstart-Lend]` 范围格式。
5. **收尾文档**：ADR-040 的效果段只记录“pytest 全绿；ruff/mypy clean”，不预填会随测试
   增长变化的用例数；实施日期固定为 `2026-07-11`。本计划不包含远程发布操作，推送需用户
   单独授权。

   `context_rules.py` 中使用的公开/内部 helper 为：

   ```python
   def normalize_path(path: str) -> str:
       return (path or "").replace("\\", "/").lower()


   def _split_ast_blocks(text: str) -> list[str]:
       if not text.strip():
           return []
       return [block.strip() for block in re.split(r"\n(?=AST for:)", text.strip()) if block.strip()]
   ```

   `graph.py` 中替换原 `_execute_level1_call` 的完整实现为：

   ```python
   def _execute_level1_call(
       call: context_rules.Level1Call, tool_client,
   ) -> tuple[context_rules.Level1Call, str | None, str]:
       try:
           response = (
               tool_client.find_callers(call.key)
               if call.level is context_rules.ContextLevel.FIND_CALLERS
               else tool_client.get_code_metrics(call.key)
           )
       except Exception as exc:  # noqa: BLE001
           return call, None, f"{type(exc).__name__}: {exc}"
       if not getattr(response, "success", False):
           return call, None, str(getattr(response, "error", "tool_failed"))
       content = response.as_tool_output().strip()
       return call, (content or None), ""
   ```

   图节点对每个 `run_bounded_parallel` 结果按以下逻辑装配：

   ```python
   for outcome in outcomes:
       if outcome is None:
           continue
       call, content, error = outcome
       if content is None:
           failed_level1[(call.level, call.key)] = error or "tool_failed"
           continue
       level1_content[(call.level, call.key)] = content
       gathered.append(GatheredContext(call.level.value, call.key, content))
   ```

## 前置说明(动手前务必确认)

- 当前分支是 `feat/risk-routed-orchestration-phase1`(风险路由编排整条线共用这一个长期分支,
  全部阶段做完才合并回 `master`)。每个任务提交前先 `git branch --show-current` 确认仍在这
  个分支上,commit 到这个分支,不要切回 `master`。
- 每个任务做完都要跑一次全量 `pytest`(`cd services/agent && conda run -n codeguard
  --no-capture-output python -m pytest tests/ -q`),确认没有破坏既有测试再提交。
- commit message 用 Conventional Commits(中文、无 AI 署名尾注),见 `CLAUDE.md` §6.9。

---

### Task 1: `pipeline/concurrency.py` —— 通用有界并行派发

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/concurrency.py`
- Test: `services/agent/tests/test_concurrency.py`

- [ ] **Step 1: 写失败测试**

创建 `services/agent/tests/test_concurrency.py`:

```python
"""pipeline/concurrency.py 的单测。"""

from __future__ import annotations

from codeguard_agent.pipeline.concurrency import run_bounded_parallel


def test_run_bounded_parallel_returns_results_in_input_order():
    items = [3, 1, 4, 1, 5]
    results = run_bounded_parallel(items, lambda x: x * 10)
    assert results == [30, 10, 40, 10, 50]


def test_run_bounded_parallel_isolates_single_failure():
    def _maybe_fail(x: int) -> int:
        if x == 2:
            raise ValueError("boom")
        return x * 10

    results = run_bounded_parallel([1, 2, 3], _maybe_fail)
    assert results == [10, None, 30]


def test_run_bounded_parallel_empty_items_returns_empty_list():
    assert run_bounded_parallel([], lambda x: x) == []


def test_run_bounded_parallel_caps_workers_to_item_count():
    # max_workers 大于 items 数量时不应报错,ThreadPoolExecutor 内部按 min(max_workers, len(items)) 建池
    results = run_bounded_parallel([1], lambda x: x + 1, max_workers=8)
    assert results == [2]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_concurrency.py -v`
Expected: FAIL,报 `ModuleNotFoundError: No module named 'codeguard_agent.pipeline.concurrency'`

- [ ] **Step 3: 写最小实现**

创建 `services/agent/src/codeguard_agent/pipeline/concurrency.py`:

```python
"""通用有界并行派发(阶段3引入,供 ContextProvider Level1 工具调用复用)。

只做"有界线程池 + 单项失败隔离 + 按输入顺序回收结果"这一件事,不为假设中的 async
迁移预留双接口——若后续阶段的并发形状变成需要全局限流的二维 fan-out,那是 ROADMAP
"chunking 前不切 async"岔路口登记的切换时机,届时另行设计(见
docs/superpowers/specs/2026-07-11-risk-aware-context-provider-design.md §"不是为后续
阶段的 async 迁移预留双接口")。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def run_bounded_parallel(
    items: list[T],
    fn: Callable[[T], R],
    max_workers: int = 8,
) -> list[R | None]:
    """有界线程池并发执行 fn(item),按输入顺序返回结果。

    单项抛异常时该项结果为 None,不影响其它项(与 evidence_agent/reviewer 节点一贯的
    失败隔离风格一致)。
    """
    if not items:
        return []
    results: list[R | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items), 8)) as pool:
        futures = {pool.submit(fn, item): idx for idx, item in enumerate(items)}
        for future in futures:
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception:  # noqa: BLE001 单项失败隔离,不让其它项失败
                results[idx] = None
    return results
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_concurrency.py -v`
Expected: 4 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/concurrency.py services/agent/tests/test_concurrency.py
git commit -m "$(cat <<'EOF'
feat(pipeline): 新增有界并行派发工具 run_bounded_parallel

供阶段3 ContextProvider 的 Level1 工具调用并发派发使用,
有界线程池+单项失败隔离,不预支 async 迁移
EOF
)"
```

---

### Task 2: `ReviewBudget.max_context_chars_per_task` 启用默认值

**Files:**
- Modify: `services/agent/src/codeguard_agent/models/tasks.py:74-80`
- Test: `services/agent/tests/test_tasks_models.py`

- [ ] **Step 1: 写失败测试**

打开 `services/agent/tests/test_tasks_models.py`,在文件末尾追加:

```python
def test_review_budget_defaults_context_chars_per_task_to_4000():
    budget = ReviewBudget()
    assert budget.max_context_chars_per_task == 4000
```

确认文件头部已 `from codeguard_agent.models.tasks import ReviewBudget`(若无,补上该 import)。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_tasks_models.py -v -k context_chars`
Expected: FAIL,`assert None == 4000`

- [ ] **Step 3: 改默认值**

编辑 `services/agent/src/codeguard_agent/models/tasks.py`,把:

```python
    max_context_chars_per_task: StrictInt | None = Field(default=None, gt=0)
```

改为:

```python
    max_context_chars_per_task: StrictInt | None = Field(default=4000, gt=0)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_tasks_models.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/models/tasks.py services/agent/tests/test_tasks_models.py
git commit -m "$(cat <<'EOF'
feat(models): ReviewBudget 每任务上下文字符预算启用默认值4000

阶段3首次真正产出 TaskContextBundle.facts,该字段此前一直是未启用的
占位(default=None);与 context_provider.py 现有全局事实预算 _FACT_BUDGET
保持一致取值
EOF
)"
```

---

### Task 3: `context_rules.py` —— RiskTag→策略映射 + 调用计划(纯规划,不执行)

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/context_rules.py`
- Test: `services/agent/tests/test_context_rules.py`

- [ ] **Step 1: 写失败测试**

创建 `services/agent/tests/test_context_rules.py`:

```python
"""pipeline/context_rules.py 的单测(阶段3)。"""

from __future__ import annotations

from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag
from codeguard_agent.pipeline.context_rules import (
    ContextLevel,
    plan_context_calls,
)


def _profile(task_id: str, *tags: RiskTag) -> RiskProfile:
    return RiskProfile(task_id=task_id, tag_scores={tag: 2 for tag in tags})


def test_general_review_does_not_trigger_level1():
    task = ReviewTask(id="A.java#h0", file="A.java", hunk_header="@@ -1,1 +1,3 @@", patch="+x", changed_lines=[2])
    plan = plan_context_calls(
        [task],
        {"A.java#h0": _profile("A.java#h0", RiskTag.GENERAL_REVIEW)},
        ast_facts_by_file={},
    )
    assert plan.level1_calls == ()
    assert plan.skips == ()


def test_complexity_tag_triggers_code_metrics_keyed_by_file():
    task = ReviewTask(id="A.java#h0", file="A.java", hunk_header="@@ -1,1 +1,3 @@", patch="+x", changed_lines=[2])
    plan = plan_context_calls(
        [task],
        {"A.java#h0": _profile("A.java#h0", RiskTag.COMPLEXITY_CONTROL_FLOW)},
        ast_facts_by_file={},
    )
    assert len(plan.level1_calls) == 1
    call = plan.level1_calls[0]
    assert call.level is ContextLevel.CODE_METRICS
    assert call.key == "A.java"
    assert call.task_ids == ("A.java#h0",)


def test_resource_lifecycle_triggers_find_callers_with_resolved_method():
    ast_block = (
        "AST for: A.java\n"
        "  class: A\n"
        "    public void save(Order order) [L10-L20]\n"
    )
    task = ReviewTask(
        id="A.java#h0", file="A.java", hunk_header="@@ -12,2 +12,2 @@", patch="+x", changed_lines=[12],
    )
    plan = plan_context_calls(
        [task],
        {"A.java#h0": _profile("A.java#h0", RiskTag.RESOURCE_LIFECYCLE)},
        ast_facts_by_file={"a.java": ast_block},
    )
    assert len(plan.level1_calls) == 1
    call = plan.level1_calls[0]
    assert call.level is ContextLevel.FIND_CALLERS
    assert call.key == "A.java#save"


def test_method_unresolved_records_skip_not_call():
    task = ReviewTask(
        id="A.java#h0", file="A.java", hunk_header="@@ -1,1 +1,1 @@", patch="+x", changed_lines=[1],
    )
    plan = plan_context_calls(
        [task],
        {"A.java#h0": _profile("A.java#h0", RiskTag.RESOURCE_LIFECYCLE)},
        ast_facts_by_file={},  # 没有该文件的 AST 切片 → 解析不到方法
    )
    assert plan.level1_calls == ()
    assert len(plan.skips) == 1
    assert plan.skips[0].task_id == "A.java#h0"
    assert plan.skips[0].reason == "no_method_resolved"


def test_same_file_multiple_tasks_dedup_to_one_code_metrics_call():
    task_a = ReviewTask(id="A.java#h0", file="A.java", hunk_header="@@ -1,1 +1,1 @@", patch="+x", changed_lines=[1])
    task_b = ReviewTask(id="A.java#h1", file="A.java", hunk_header="@@ -20,1 +20,1 @@", patch="+y", changed_lines=[20])
    plan = plan_context_calls(
        [task_a, task_b],
        {
            "A.java#h0": _profile("A.java#h0", RiskTag.DUPLICATION_DESIGN),
            "A.java#h1": _profile("A.java#h1", RiskTag.OBSERVABILITY_TESTABILITY),
        },
        ast_facts_by_file={},
    )
    assert len(plan.level1_calls) == 1
    call = plan.level1_calls[0]
    assert call.key == "A.java"
    assert set(call.task_ids) == {"A.java#h0", "A.java#h1"}


def test_task_without_risk_profile_is_skipped_silently():
    task = ReviewTask(id="A.java#h0", file="A.java", hunk_header="@@ -1,1 +1,1 @@", patch="+x", changed_lines=[1])
    plan = plan_context_calls([task], {}, ast_facts_by_file={})
    assert plan.level1_calls == ()
    assert plan.skips == ()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_context_rules.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'codeguard_agent.pipeline.context_rules'`

- [ ] **Step 3: 写实现**

创建 `services/agent/src/codeguard_agent/pipeline/context_rules.py`:

```python
"""阶段3:RiskTag → 上下文策略映射 + 调用计划(纯函数,不碰 tool_client)。

边界(见 docs/superpowers/specs/2026-07-11-risk-aware-context-provider-design.md):
- Level0(零新增调用):AST/敏感 API 全局结果按 task 重新切片,本模块只负责切片,
  不在此文件产生调用计划(Level0 不需要计划,直接切)。
- Level1(定向新增调用):按 RiskTag 触发 find_callers/get_code_metrics,本模块的
  plan_context_calls 只算"要调什么",不实际调用 tool_client。
- GENERAL_REVIEW 不在 TAG_CONTEXT_STRATEGIES 里注册任何策略,天然不触发 Level1。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from codeguard_agent.models.council import ContextFact
from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag
from codeguard_agent.pipeline.task_prep import _hunk_span


class ContextLevel(str, Enum):
    """Level1 定向调用的类型,值即 Java 工具名。"""

    FIND_CALLERS = "find_callers"
    CODE_METRICS = "get_code_metrics"


@dataclass(frozen=True)
class ContextStrategy:
    level: ContextLevel


# RiskTag → Level1 策略。未在此登记的标签(含 GENERAL_REVIEW)不触发 Level1,
# 只拿 Level0 基线切片。
TAG_CONTEXT_STRATEGIES: dict[RiskTag, tuple[ContextStrategy, ...]] = {
    RiskTag.RESOURCE_LIFECYCLE: (ContextStrategy(ContextLevel.FIND_CALLERS),),
    RiskTag.API_CONTRACT: (ContextStrategy(ContextLevel.FIND_CALLERS),),
    RiskTag.TRANSACTION_ATOMICITY: (ContextStrategy(ContextLevel.FIND_CALLERS),),
    RiskTag.CONCURRENCY_CONSISTENCY: (ContextStrategy(ContextLevel.FIND_CALLERS),),
    RiskTag.IDEMPOTENCY_RETRY: (ContextStrategy(ContextLevel.FIND_CALLERS),),
    RiskTag.MESSAGE_DELIVERY: (ContextStrategy(ContextLevel.FIND_CALLERS),),
    RiskTag.CACHE_CONSISTENCY: (ContextStrategy(ContextLevel.FIND_CALLERS),),
    RiskTag.COMPLEXITY_CONTROL_FLOW: (ContextStrategy(ContextLevel.CODE_METRICS),),
    RiskTag.DUPLICATION_DESIGN: (ContextStrategy(ContextLevel.CODE_METRICS),),
    RiskTag.OBSERVABILITY_TESTABILITY: (ContextStrategy(ContextLevel.CODE_METRICS),),
}


@dataclass(frozen=True)
class Level1Call:
    """去重后的一次 Level1 调用计划:同一个 (level, key) 只出现一次。"""

    level: ContextLevel
    key: str  # get_code_metrics 用 "file"; find_callers 用 "file#method"
    task_ids: tuple[str, ...]  # 命中这次调用的所有任务(结果将分发给它们)


@dataclass(frozen=True)
class TaskSkip:
    """无法生成 Level1 调用的任务及原因。"""

    task_id: str
    level: ContextLevel
    reason: str


@dataclass(frozen=True)
class ContextPlan:
    level1_calls: tuple[Level1Call, ...]
    skips: tuple[TaskSkip, ...]


def normalize_path(path: str) -> str:
    return (path or "").replace("\\", "/").lower()


_METHOD_LINE_SUFFIX = re.compile(r"\[L(\d+)-L(\d+)\]\s*$")
_METHOD_NAME_BEFORE_PARENS = re.compile(r"(\w+)\([^)]*\)\s*$")


def _parse_method_ranges(ast_block: str) -> list[tuple[str, int, int]]:
    """从单文件 AST 文本块解析方法签名行,返回 [(方法名, 起始行, 结束行)]。

    只识别形如 `    <签名>(<参数>) [L<start>-L<end>]` 的方法行(4 空格缩进、以
    `[L..-L..]` 结尾)。"Control flow:"/"Call edges:" 小节之后的行不算方法。
    """
    methods: list[tuple[str, int, int]] = []
    in_method_section = True
    for line in ast_block.splitlines():
        stripped = line.rstrip()
        if stripped in ("  Control flow:", "  Call edges:"):
            in_method_section = False
            continue
        if not in_method_section:
            continue
        if not re.match(r"^ {4}\S", line):
            continue
        range_match = _METHOD_LINE_SUFFIX.search(stripped)
        if not range_match:
            continue
        prefix = stripped[: range_match.start()].rstrip()
        name_match = _METHOD_NAME_BEFORE_PARENS.search(prefix)
        if not name_match:
            continue
        methods.append((name_match.group(1), int(range_match.group(1)), int(range_match.group(2))))
    return methods


def _task_span(task: ReviewTask) -> tuple[int, int] | None:
    span = _hunk_span(task)
    if span is not None:
        return span
    if task.changed_lines:
        return (min(task.changed_lines), max(task.changed_lines))
    return None


def resolve_method_name(ast_block: str, task: ReviewTask) -> str | None:
    """从该 task 所属文件的 AST 切片里解析出改动落在哪个方法。

    提不到就返回 None(不做正则猜测兜底,由调用方记 skip)。
    """
    span = _task_span(task)
    if span is None:
        return None
    for name, start, end in _parse_method_ranges(ast_block):
        if start <= span[1] and end >= span[0]:
            return name
    return None


def plan_context_calls(
    tasks: list[ReviewTask],
    risk_profiles: dict[str, RiskProfile],
    ast_facts_by_file: dict[str, str],
) -> ContextPlan:
    """按每个 task 的 RiskProfile.tag_scores 算出去重后的 Level1 调用计划。

    ast_facts_by_file: {规范化文件路径: 该文件的 AST 切片文本},由调用方(节点层)
    从全局 get_diff_ast 结果里按 _split_ast_blocks 切好后传入。
    """
    by_key: dict[tuple[ContextLevel, str], list[str]] = {}
    skips: list[TaskSkip] = []
    for task in tasks:
        profile = risk_profiles.get(task.id)
        if profile is None:
            continue
        levels = {
            strategy.level
            for tag in profile.tag_scores
            for strategy in TAG_CONTEXT_STRATEGIES.get(tag, ())
        }
        for level in levels:
            if level is ContextLevel.CODE_METRICS:
                key = task.file
            else:
                ast_block = ast_facts_by_file.get(normalize_path(task.file))
                method = resolve_method_name(ast_block, task) if ast_block else None
                if method is None:
                    skips.append(TaskSkip(task_id=task.id, level=level, reason="no_method_resolved"))
                    continue
                key = f"{task.file}#{method}"
            by_key.setdefault((level, key), []).append(task.id)

    calls = tuple(
        Level1Call(level=level, key=key, task_ids=tuple(task_ids))
        for (level, key), task_ids in by_key.items()
    )
    return ContextPlan(level1_calls=calls, skips=tuple(skips))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_context_rules.py -v`
Expected: 7 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/context_rules.py services/agent/tests/test_context_rules.py
git commit -m "$(cat <<'EOF'
feat(pipeline): 新增 RiskTag 到 Level1 上下文调用的规划纯函数

context_rules.plan_context_calls 只算"要调什么"(find_callers/get_code_metrics),
不碰 tool_client;按 (level, key) 去重,方法名从 AST 切片解析、
解析不到记 skip 不做正则猜测;GENERAL_REVIEW 未注册策略天然不触发 Level1
EOF
)"
```

---

### Task 4: `context_rules.py` —— Level0 切片(AST 按文件、敏感 API 按文件+行)

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/context_rules.py`
- Test: `services/agent/tests/test_context_rules.py`

- [ ] **Step 1: 写失败测试**

在 `services/agent/tests/test_context_rules.py` 顶部 import 里加入
`ast_block_for_file, sensitive_api_rows_for_task`,文件末尾追加:

```python
from codeguard_agent.pipeline.context_rules import (  # noqa: E402
    ast_block_for_file,
    sensitive_api_rows_for_task,
)


def test_ast_block_for_file_matches_by_normalized_path():
    ast_text = (
        "AST for: A.java\n"
        "  class: A\n"
        "    public void save() [L10-L12]\n"
        "AST for: B.java\n"
        "  class: B\n"
    )
    block = ast_block_for_file(ast_text, "a.java")
    assert block is not None
    assert block.startswith("AST for: A.java")
    assert "B.java" not in block


def test_ast_block_for_file_returns_none_when_no_match():
    assert ast_block_for_file("AST for: A.java\n  class: A\n", "C.java") is None


def test_sensitive_api_rows_for_task_filters_by_file_and_hunk_range():
    sensitive_text = (
        "# 敏感 API 扫描\n"
        "扫描 1 个文件, 跳过 0 个不可解析文件, 发现 2 处敏感 API 调用\n\n"
        "| 危险等级 | API | 文件 | 行号 | 调用参数 |\n"
        "|---------|-----|------|------|----------|\n"
        "| 🔴 HIGH | `Statement.execute` | A.java:12 | `sql` |\n"
        "| 🟡 MEDIUM | `Files.copy` | A.java:99 | `p1, p2` |\n"
    )
    task = ReviewTask(
        id="A.java#h0", file="A.java", hunk_header="@@ -10,5 +10,5 @@", patch="+x", changed_lines=[12],
    )
    rows = sensitive_api_rows_for_task(sensitive_text, task)
    assert len(rows) == 1
    assert "Statement.execute" in rows[0]
    assert "Files.copy" not in "\n".join(rows)


def test_sensitive_api_rows_for_task_accepts_whole_file_for_fallback_task():
    sensitive_text = (
        "| 危险等级 | API | 文件 | 行号 | 调用参数 |\n"
        "|---------|-----|------|------|----------|\n"
        "| 🔴 HIGH | `Statement.execute` | A.java:500 | `sql` |\n"
    )
    task = ReviewTask(id="A.java#file", file="A.java", patch="+x", changed_lines=[])
    rows = sensitive_api_rows_for_task(sensitive_text, task)
    assert len(rows) == 1
```

需要在文件顶部补 `from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag`
(若已存在则跳过,避免重复 import)。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_context_rules.py -v -k "ast_block_for_file or sensitive_api_rows"`
Expected: FAIL,`ImportError: cannot import name 'ast_block_for_file'`

- [ ] **Step 3: 写实现**

在 `services/agent/src/codeguard_agent/pipeline/context_rules.py` 顶部 import 区加入:

```python
_AST_HEADER = re.compile(r"^AST for:\s*(.+)$")
```

在 `normalize_path` 定义之后、`_METHOD_LINE_SUFFIX` 定义之前插入:

```python
def ast_block_for_file(ast_text: str, file: str) -> str | None:
    """从全局 get_diff_ast 结果文本里,按文件路径切出属于该文件的单文件 AST 块。"""
    target = normalize_path(file)
    for block in _split_ast_blocks(ast_text):
        first_line = block.splitlines()[0] if block else ""
        header_match = _AST_HEADER.match(first_line)
        if header_match and normalize_path(header_match.group(1).strip()) == target:
            return block
    return None
```

在文件末尾(`plan_context_calls` 定义之后)追加:

```python
_SENSITIVE_ROW = re.compile(r"^\|[^|]*\|[^|]*\|\s*([^:|]+):(\d+)\s*\|")


def sensitive_api_rows_for_task(sensitive_api_text: str, task: ReviewTask) -> list[str]:
    """从全局 find_sensitive_apis 结果文本里,按文件+行号范围过滤出属于该 task 的命中行。

    有 hunk 的 task 只接受落在其 hunk 覆盖范围内的行;文件级 fallback task(无
    hunk_header)接受该文件的全部命中(它本就代表整个文件级变更,如删除/纯重命名)。
    """
    target = normalize_path(task.file)
    span = _task_span(task)
    rows: list[str] = []
    for line in sensitive_api_text.splitlines():
        match = _SENSITIVE_ROW.match(line)
        if not match:
            continue
        file, line_no = match.group(1).strip(), int(match.group(2))
        if normalize_path(file) != target:
            continue
        if span is not None and not (span[0] <= line_no <= span[1]):
            continue
        rows.append(line)
    return rows
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_context_rules.py -v`
Expected: 11 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/context_rules.py services/agent/tests/test_context_rules.py
git commit -m "$(cat <<'EOF'
feat(pipeline): context_rules 补 Level0 事实切片(AST按文件、敏感API按文件+行)

全局 get_diff_ast/find_sensitive_apis 结果零新增调用,按 task.file +
hunk覆盖范围重新切片;文件级 fallback task 接受整文件命中
EOF
)"
```

---

### Task 5: `context_rules.py` —— 每任务事实预算截断

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/context_rules.py`
- Test: `services/agent/tests/test_context_rules.py`

- [ ] **Step 1: 写失败测试**

在 `services/agent/tests/test_context_rules.py` 追加:

```python
from codeguard_agent.pipeline.context_rules import truncate_task_facts  # noqa: E402


def test_truncate_task_facts_keeps_all_when_under_budget():
    facts = [ContextFact(source="s1", kind="k", content="short")]
    kept, truncated = truncate_task_facts(facts, max_chars=100)
    assert kept == facts
    assert truncated is False


def test_truncate_task_facts_cuts_when_over_budget():
    facts = [
        ContextFact(source="s1", kind="k", content="a" * 60),
        ContextFact(source="s2", kind="k", content="b" * 60),
    ]
    kept, truncated = truncate_task_facts(facts, max_chars=100)
    assert truncated is True
    assert sum(len(f.content) for f in kept) <= 100 + len("...(已截断)")
    assert kept[0].content.startswith("a")


def test_truncate_task_facts_none_budget_means_unbounded():
    facts = [ContextFact(source="s1", kind="k", content="a" * 100000)]
    kept, truncated = truncate_task_facts(facts, max_chars=None)
    assert kept == facts
    assert truncated is False
```

需要在 import 区加 `from codeguard_agent.models.council import ContextFact`(若未 import)。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_context_rules.py -v -k truncate`
Expected: FAIL,`ImportError: cannot import name 'truncate_task_facts'`

- [ ] **Step 3: 写实现**

在 `services/agent/src/codeguard_agent/pipeline/context_rules.py` 末尾追加:

```python
def truncate_task_facts(
    facts: list[ContextFact], max_chars: int | None
) -> tuple[list[ContextFact], bool]:
    """按每任务字符预算截断 facts 列表。max_chars 为 None 表示不限制。"""
    if max_chars is None:
        return facts, False
    kept: list[ContextFact] = []
    used = 0
    truncated = False
    for fact in facts:
        remaining = max_chars - used
        if remaining <= 0:
            truncated = True
            break
        if len(fact.content) > remaining:
            kept.append(
                ContextFact(
                    source=fact.source,
                    kind=fact.kind,
                    content=fact.content[:remaining] + "...(已截断)",
                    truncated=True,
                )
            )
            truncated = True
            break
        kept.append(fact)
        used += len(fact.content)
    return kept, truncated
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_context_rules.py -v`
Expected: 14 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/context_rules.py services/agent/tests/test_context_rules.py
git commit -m "$(cat <<'EOF'
feat(pipeline): context_rules 补每任务上下文预算截断

复用 ReviewBudget.max_context_chars_per_task,超预算的 fact 就地截断
并标记 truncated,None 预算表示不限制
EOF
)"
```

---

### Task 6: 重写 `_context_provider_node`,接入 Level0/Level1 与并发派发

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py:352-374`(现有 `_context_provider_node`)

- [ ] **Step 1: 加 import**

在 `services/agent/src/codeguard_agent/pipeline/graph.py` 顶部 import 区(紧跟现有
`from codeguard_agent.pipeline import task_prep` 之后)加入:

```python
from codeguard_agent.pipeline import context_rules
from codeguard_agent.pipeline.concurrency import run_bounded_parallel
```

- [ ] **Step 2: 替换 `_context_provider_node` 实现**

把 `services/agent/src/codeguard_agent/pipeline/graph.py` 现有的:

```python
def _context_provider_node(tool_client):
    def _node(state: ReviewState) -> dict:
        ctx = _state_to_context(state, tool_client=tool_client)
        ContextProviderStage().execute(ctx)
        selection = state.get("task_selection")
        selected_ids = selection.selected_task_ids if selection is not None else []
        # Phase 1：为每个选中任务建立空 TaskContextBundle，确立所有权；
        # 按 RiskTag 定向填充留到 Phase 3。
        task_bundles = {tid: TaskContextBundle(task_id=tid) for tid in selected_ids}
        return {
            "context_bundle": ctx.context_bundle,
            "gathered_context": list(ctx.gathered_context),
            "task_context_bundles": task_bundles,
            "council_trace": [
                CouncilTrace(
                    node="context_provider",
                    event="bundle_created",
                    detail=f"facts={len(ctx.context_bundle.facts)} task_bundles={len(task_bundles)}",
                )
            ],
        }

    return _node
```

整段替换为:

```python
def _execute_level1_call(
    call: context_rules.Level1Call, tool_client,
) -> tuple[context_rules.Level1Call, str | None, str]:
    try:
        response = (
            tool_client.find_callers(call.key)
            if call.level is context_rules.ContextLevel.FIND_CALLERS
            else tool_client.get_code_metrics(call.key)
        )
    except Exception as exc:  # noqa: BLE001
        return call, None, f"{type(exc).__name__}: {exc}"
    if not getattr(response, "success", False):
        return call, None, str(getattr(response, "error", "tool_failed"))
    content = response.as_tool_output().strip()
    return call, (content or None), ""


def _context_provider_node(tool_client):
    """Phase 3:按每个选中任务的 RiskProfile.tag_scores 定向填充 TaskContextBundle。

    Level0(零新增调用):全局 get_diff_ast/find_sensitive_apis 结果重新切片。
    Level1(定向新增调用):按 RiskTag 触发 find_callers/get_code_metrics,
    context_rules.plan_context_calls 计划、run_bounded_parallel 并发执行。
    """

    def _node(state: ReviewState) -> dict:
        ctx = _state_to_context(state, tool_client=tool_client)
        ContextProviderStage().execute(ctx)
        bundle = ctx.context_bundle

        selection = state.get("task_selection")
        selected_ids = set(selection.selected_task_ids) if selection is not None else set()
        all_tasks: list[ReviewTask] = state.get("review_tasks") or []
        tasks = [t for t in all_tasks if t.id in selected_ids]
        risk_profiles: dict[str, RiskProfile] = state.get("risk_profiles") or {}
        budget = state.get("review_budget") or ReviewBudget()

        ast_text = "\n".join(
            fact.content for fact in bundle.facts if fact.source == "tool:get_diff_ast"
        )
        sensitive_text = "\n".join(
            fact.content for fact in bundle.facts if fact.source == "tool:find_sensitive_apis"
        )

        ast_blocks: dict[str, str] = {}
        for task in tasks:
            key = context_rules.normalize_path(task.file)
            if key in ast_blocks:
                continue
            block = context_rules.ast_block_for_file(ast_text, task.file)
            if block is not None:
                ast_blocks[key] = block

        plan = context_rules.plan_context_calls(tasks, risk_profiles, ast_blocks)

        level1_content: dict[tuple[context_rules.ContextLevel, str], str] = {}
        failed_level1: dict[tuple[context_rules.ContextLevel, str], str] = {}
        gathered = list(ctx.gathered_context)
        if tool_client is not None and plan.level1_calls:
            results = run_bounded_parallel(
                list(plan.level1_calls),
                lambda call: _execute_level1_call(call, tool_client),
                max_workers=8,
            )
            for outcome in results:
                if outcome is None:
                    continue
                call, content, error = outcome
                if content is None:
                    failed_level1[(call.level, call.key)] = error or "tool_failed"
                    continue
                level1_content[(call.level, call.key)] = content
                gathered.append(GatheredContext(call.level.value, call.key, content))

        task_bundles: dict[str, TaskContextBundle] = {}
        trace: list[CouncilTrace] = [
            CouncilTrace(
                node="context_provider",
                event="bundle_created",
                detail=f"facts={len(bundle.facts)} tasks={len(tasks)}",
            )
        ]
        for task in tasks:
            facts: list = []
            ast_block = ast_blocks.get(context_rules.normalize_path(task.file))
            if ast_block:
                facts.append(
                    ContextFact(source="tool:get_diff_ast", kind="ast_structure", content=ast_block)
                )
            sensitive_rows = context_rules.sensitive_api_rows_for_task(sensitive_text, task)
            if sensitive_rows:
                facts.append(
                    ContextFact(
                        source="tool:find_sensitive_apis",
                        kind="sensitive_api",
                        content="\n".join(sensitive_rows),
                    )
                )
            level1_labels: list[str] = []
            for call in plan.level1_calls:
                if task.id not in call.task_ids:
                    continue
                content = level1_content.get((call.level, call.key))
                if content is None:
                    continue
                facts.append(
                    ContextFact(source=f"tool:{call.level.value}", kind=call.level.value, content=content)
                )
                level1_labels.append(f"{call.level.value}({call.key})")

            facts, truncated = context_rules.truncate_task_facts(
                facts, budget.max_context_chars_per_task
            )
            task_bundles[task.id] = TaskContextBundle(task_id=task.id, facts=facts, truncated=truncated)

            skip_reasons = [s.reason for s in plan.skips if s.task_id == task.id]
            failure_reasons = [
                failed_level1[(call.level, call.key)]
                for call in plan.level1_calls
                if task.id in call.task_ids and (call.level, call.key) in failed_level1
            ]
            trace.append(
                CouncilTrace(
                    node="context_provider",
                    event="task_bundle_filled",
                    detail=(
                        f"task={task.id} facts={len(facts)} level1={level1_labels} "
                        f"skips={skip_reasons} failed={failure_reasons} truncated={truncated}"
                    ),
                )
            )

        return {
            "context_bundle": bundle,
            "gathered_context": gathered,
            "task_context_bundles": task_bundles,
            "council_trace": trace,
        }

    return _node
```

- [ ] **Step 3: 运行既有编排测试确认没有破坏**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py -v`
Expected: 全部 PASS(这一步先只验证没有回归,新增覆盖在 Task 7 补)

- [ ] **Step 4: 运行全量测试**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/graph.py
git commit -m "$(cat <<'EOF'
feat(graph): _context_provider_node 按RiskTag定向填充TaskContextBundle

接入 context_rules.plan_context_calls 的 Level0/Level1 分层与
run_bounded_parallel 并发派发;每个task落一条council_trace,
不再产出空TaskContextBundle
EOF
)"
```

---

### Task 7: 图集成测试 —— Level0/Level1 事实真正落进 TaskContextBundle

**Files:**
- Modify: `services/agent/tests/test_graph_orchestration.py`

- [ ] **Step 1: 扩展 `_MockToolClient` 支持 `get_diff_ast`**

在 `services/agent/tests/test_graph_orchestration.py` 的 `_MockToolClient` 类
(`find_sensitive_apis` 方法之后)插入:

```python
    def get_diff_ast(self, diff_text: str = "") -> _MockToolResponse:
        self.calls.append(("get_diff_ast", {"query": diff_text}))
        return _MockToolResponse(
            True,
            result=(
                "AST for: A.java\n"
                "  class: A\n"
                "    public void save(Order order) [L12-L18]\n"
            ),
        )
```

- [ ] **Step 2: 写失败测试**

在文件末尾追加:

```python
def test_context_provider_node_fills_level0_and_level1_facts_per_task():
    diff = (
        "diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n"
        "@@ -12,2 +12,2 @@\n+order.save();\n"
    )
    task = G.ReviewTask(
        id="A.java#h0", file="A.java", hunk_header="@@ -12,2 +12,2 @@", patch="+x", changed_lines=[12],
    )
    profile = G.RiskProfile(
        task_id="A.java#h0",
        tag_scores={G.RiskTag.RESOURCE_LIFECYCLE: 2},
    )
    tool_client = _MockToolClient()
    node = G._context_provider_node(tool_client)

    out = node(
        {
            "diff_text": diff,
            "review_tasks": [task],
            "risk_profiles": {"A.java#h0": profile},
            "task_selection": G.TaskSelection(selected_task_ids=["A.java#h0"]),
            "review_budget": G.ReviewBudget(),
        }
    )

    bundle = out["task_context_bundles"]["A.java#h0"]
    sources = {fact.source for fact in bundle.facts}
    assert "tool:get_diff_ast" in sources
    assert "tool:find_callers" in sources
    assert any(("find_callers", {"query": "A.java#save"}) == call for call in tool_client.calls)
    detail_events = [t.detail for t in out["council_trace"] if t.event == "task_bundle_filled"]
    assert any("task=A.java#h0" in d for d in detail_events)


def test_context_provider_node_records_skip_when_method_unresolved():
    diff = (
        "diff --git a/B.java b/B.java\n--- a/B.java\n+++ b/B.java\n"
        "@@ -1,1 +1,1 @@\n+int x=1;\n"
    )
    task = G.ReviewTask(
        id="B.java#h0", file="B.java", hunk_header="@@ -1,1 +1,1 @@", patch="+x", changed_lines=[1],
    )
    profile = G.RiskProfile(task_id="B.java#h0", tag_scores={G.RiskTag.API_CONTRACT: 2})
    tool_client = _MockToolClient()
    node = G._context_provider_node(tool_client)

    out = node(
        {
            "diff_text": diff,
            "review_tasks": [task],
            "risk_profiles": {"B.java#h0": profile},
            "task_selection": G.TaskSelection(selected_task_ids=["B.java#h0"]),
            "review_budget": G.ReviewBudget(),
        }
    )

    assert not any(call[0] == "find_callers" for call in tool_client.calls)
    detail_events = [t.detail for t in out["council_trace"] if t.event == "task_bundle_filled"]
    assert any("no_method_resolved" in d for d in detail_events)


def test_context_provider_node_general_review_gets_no_level1_call():
    diff = (
        "diff --git a/C.java b/C.java\n--- a/C.java\n+++ b/C.java\n"
        "@@ -1,1 +1,1 @@\n+int x=1;\n"
    )
    task = G.ReviewTask(
        id="C.java#h0", file="C.java", hunk_header="@@ -1,1 +1,1 @@", patch="+x", changed_lines=[1],
    )
    profile = G.RiskProfile(task_id="C.java#h0", tag_scores={G.RiskTag.GENERAL_REVIEW: 1})
    tool_client = _MockToolClient()
    node = G._context_provider_node(tool_client)

    out = node(
        {
            "diff_text": diff,
            "review_tasks": [task],
            "risk_profiles": {"C.java#h0": profile},
            "task_selection": G.TaskSelection(selected_task_ids=["C.java#h0"]),
            "review_budget": G.ReviewBudget(),
        }
    )

    assert not any(call[0] in ("find_callers", "get_code_metrics") for call in tool_client.calls)


def test_context_provider_node_does_not_store_failed_level1_response_as_fact():
    class _FailingCallersClient(_MockToolClient):
        def find_callers(self, query: str = "") -> _MockToolResponse:
            self.calls.append(("find_callers", {"query": query}))
            return _MockToolResponse(False, error="gateway timeout")

    task = G.ReviewTask(
        id="A.java#h0", file="A.java", hunk_header="@@ -12,1 +12,1 @@", patch="+x", changed_lines=[12],
    )
    out = G._context_provider_node(_FailingCallersClient())({
        "diff_text": "diff --git a/A.java b/A.java\n+++ b/A.java\n@@ -12 +12 @@\n+x\n",
        "review_tasks": [task],
        "risk_profiles": {"A.java#h0": G.RiskProfile(
            task_id="A.java#h0", tag_scores={RiskTag.RESOURCE_LIFECYCLE: 2},
        )},
        "task_selection": G.TaskSelection(selected_task_ids=["A.java#h0"]),
        "review_budget": G.ReviewBudget(),
    })

    facts = out["task_context_bundles"]["A.java#h0"].facts
    assert all("gateway timeout" not in fact.content for fact in facts)
    assert any("gateway timeout" in trace.detail for trace in out["council_trace"])
```

在 `tests/test_graph_orchestration.py` 顶部直接加入
`from codeguard_agent.models.tasks import RiskTag`；其余模型继续使用已存在的
`from codeguard_agent.pipeline import graph as G` 访问。

- [ ] **Step 3: 运行测试确认新测试先失败(或验证 import 问题)**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py -v -k context_provider_node`
Expected: Task 6 的 Step 0 已先执行本组测试并观察到失败；图节点实现后，四个测试全部 PASS。

- [ ] **Step 4: 跑全量测试**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add services/agent/tests/test_graph_orchestration.py
git commit -m "$(cat <<'EOF'
test(graph): 补 _context_provider_node 的 Level0/Level1 集成测试

覆盖:方法名可解析时真正调 find_callers、解析不到时记 skip、
GENERAL_REVIEW 不触发任何 Level1 调用
EOF
)"
```

---

### Task 8: 静态检查 + ADR 留痕 + 收尾

**Files:**
- Modify: `DECISIONS.md`(追加 ADR)
- Modify: `docs/ROADMAP.md`(勾选阶段3对应项,若适用)

- [ ] **Step 1: 跑 ruff 和 mypy**

Run:
```bash
cd services/agent
conda run -n codeguard --no-capture-output ruff check src/ tests/
conda run -n codeguard --no-capture-output mypy src/
```
Expected: `All checks passed!` / `Success: no issues found in N source files`
如有报错按提示修,修完重新跑本步直到干净。

- [ ] **Step 2: 跑全量 pytest 做最终确认**

Run: `cd services/agent && conda run -n codeguard --no-capture-output python -m pytest tests/ -q`
Expected: 全部 PASS,记下总用例数(用于 ADR)

- [ ] **Step 3: 在 `DECISIONS.md` 追加 ADR-040**

在 `DECISIONS.md` 文件末尾(ADR-039 之后)追加:

```markdown

## ADR-040: Phase 3 风险感知 ContextProvider——Level0 切片 + Level1 定向工具调用

**日期**: 2026-07-11
**状态**: 已实现
**关联设计**: `docs/superpowers/specs/2026-07-11-risk-aware-context-provider-design.md`

### 决策

1. `TaskContextBundle.facts` 不再恒为空。按每个选中任务的 `RiskProfile.tag_scores`
   分两层填充:Level0(零新增网络调用,全局 `get_diff_ast`/`find_sensitive_apis`
   结果按 `task.file` + hunk 覆盖范围重新切片)+ Level1(按 `RiskTag` 定向触发
   `find_callers`/`get_code_metrics`,同 `(level, key)` 去重后经
   `run_bounded_parallel`(有界线程池,上限 8)并发执行)。
2. 不引入新 Java 工具,不做 diff 内部调用图缝合增强——完整调用图需要
   SymbolSolver/classpath,ADR-012 已放弃过这条路线,阶段3不重新踩。
3. `find_callers` 所需方法名从该 task 的 AST 切片解析(`context_rules.
   resolve_method_name`);解析不到直接跳过并记 `council_trace`,不做正则猜测兜底。
4. `ReviewBudget.max_context_chars_per_task` 默认值从 `None` 改为 `4000`,
   首次真正启用每任务上下文预算截断。
5. `task_context_bundles` 本阶段仍未被发现者 Agent 消费(接入 prompt 是阶段4的事),
   验证范围只做工程正确性(pytest),不跑真实 eval 质量对比。

### 效果

工程正确性:全量 pytest 通过，ruff/mypy clean。`task_context_bundles`
每个选中任务现在带有真实的 AST/敏感 API/调用方/复杂度事实(按标签定向),
为阶段4"发现者 Agent 消费任务级上下文"打好数据基础。

### 放弃的备选

- 用 SymbolSolver/classpath 做完整调用图缝合:成本与 ADR-012 已放弃的
  `get_method_definition`/`get_call_graph` 同一量级,不在本阶段引入。
- 方法名解析不到时用正则从 patch 文本猜测:放弃,不做不确定性兜底,
  保持"提不到就跳过"的确定性边界。
- 阶段3就把 `task_context_bundles` 接入发现者 prompt:放弃,尊重 ADR-038
  的阶段边界,留给阶段4。

**日期**: 2026-07-11
```

- [ ] **Step 4: 提交**

```bash
git add DECISIONS.md
git commit -m "$(cat <<'EOF'
docs: 记录 ADR-040 阶段3风险感知ContextProvider决策

Level0零调用切片 + Level1按tag定向调用 + 并发派发 + 每任务预算截断,
补充效果与放弃的备选(完整调用图/正则猜测方法名/提前接入prompt)
EOF
)"
```

## Self-Review 备忘(执行者交付前自查)

- 每个 Task 是否都独立可跑、可提交,没有跨 Task 的隐藏依赖?(有——Task 3-5 都依赖
  Task 3 建的 `context_rules.py` 文件本身,属预期的同文件累积编辑,不是跨 Task 耦合)
- `ContextLevel`/`Level1Call`/`ContextPlan`/`TaskSkip` 等类型名在 Task 3-7 全程一致,
  未出现改名不同步。
- 是否有未填项?本计划全部步骤含完整代码,无占位符。
- 是否覆盖设计文档 §3-§7 的每一条?Level0(Task 4)、Level1(Task 3)、并发(Task 1、6)、
  预算(Task 2、5)、trace(Task 6、7)、验证范围只做工程正确性(Task 7 无 eval 步骤)——
  均已覆盖。
