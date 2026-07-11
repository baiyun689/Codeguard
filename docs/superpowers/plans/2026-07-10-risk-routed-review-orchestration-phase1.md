# 风险路由驱动的 ReviewTask 编排 — Phase 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 冻结 `ReviewTask → RiskProfile → TaskSelection → TaskContextBundle → CandidateIssue(task_id)` 状态契约与最终拓扑，让每条候选问题都可回溯到一个任务，且首次 EvidenceAgent 必经一次。

**Architecture:** 在 `context_provider` 之前插入 `DiffTaskBuilder → RiskTriage → TaskRank` 三个薄节点（hunk 级任务、空风险画像、默认全选）；`CandidateIssue.task_id` 设为字面必填，收集节点用 basename+行号确定性映射到任务，无法映射的候选被拒绝并留 trace；移除 `council_route` 条件路由，三路 fan-in 后固定进一次 EvidenceAgent 再进 CouncilJudge，Judge 仅在 needs_more 且轮次未超时回环。产品输出 `ReviewResult`/`Issue` 不变。

**Tech Stack:** Python 3.12、Pydantic v2、LangGraph、pytest。开发命令统一带 `conda run -n codeguard --no-capture-output` 前缀，工作目录 `services/agent`。

---

## 约束与不变量（每个任务都要守）

- 事实类字段单一所有者，下游不得回写上游（见 spec §3.3）。
- 所有跨节点关联用稳定 ID（`task_id` / `candidate_id`）。
- Java Gateway 只提供事实与护栏，不判断"是不是问题"。
- 最终 `Issue` 契约不变（`models/schemas.py`），中间态只进 state/trace/eval。
- 每个任务结束前必须 `pytest tests/ -q` 全绿再提交。

## 文件结构（本计划涉及的文件与职责）

- **创建** `services/agent/src/codeguard_agent/models/tasks.py` — 任务化与风险路由的新模型（`ReviewTask` / `RiskTag` / `RiskSignal` / `RiskProfile` / `ReviewBudget` / `SkippedTask` / `TaskSelection` / `TaskContextBundle`）。
- **创建** `services/agent/src/codeguard_agent/pipeline/task_prep.py` — 任务准备的纯函数（`build_tasks` / `triage_tasks` / `rank_tasks` / `map_candidate_to_task`），不触碰 LLM、不读仓库文件。
- **修改** `services/agent/src/codeguard_agent/models/council.py` — `CandidateIssue` 增必填 `task_id`，`from_issue` 增 `task_id` 参数。
- **修改** `services/agent/src/codeguard_agent/pipeline/graph.py` — 新增 3 个节点 + 5 个 state 字段、`context_provider` 产出 `task_context_bundles`、`make_reviewer_node` 回填 task_id、移除 `council_route`、重连证据边。
- **创建** `services/agent/tests/test_tasks_models.py` — 新模型协议测试。
- **创建** `services/agent/tests/test_task_prep.py` — 任务准备纯函数测试。
- **修改** `services/agent/tests/test_council_models.py` — `from_issue` 增 task_id、`model_dump` 键集合加 `task_id`。
- **修改** `services/agent/tests/test_graph_orchestration.py` — 24 处 `CandidateIssue(...)` 加 task_id、删除 4 个 `_route_after_coordinator` 测试、修正 fan-in/limits/mock 用例的 diff 与假引擎、新增拓扑测试。

---

## Task 1: 新增任务化与风险路由模型

**Files:**
- Create: `services/agent/src/codeguard_agent/models/tasks.py`
- Test: `services/agent/tests/test_tasks_models.py`

- [ ] **Step 1: 写失败测试**

创建 `services/agent/tests/test_tasks_models.py`:

```python
"""风险路由任务模型协议测试（Phase 1）。"""

from __future__ import annotations

from codeguard_agent.models.council import ContextFact
from codeguard_agent.models.tasks import (
    ReviewBudget,
    ReviewTask,
    RiskProfile,
    RiskSignal,
    RiskTag,
    SkippedTask,
    TaskContextBundle,
    TaskSelection,
)


def test_review_task_minimal_fields():
    task = ReviewTask(id="A.java#h0", file="A.java", patch="@@ -1 +1 @@\n+x")
    assert task.hunk_header == ""
    assert task.changed_lines == []


def test_risk_profile_defaults_empty():
    profile = RiskProfile(task_id="A.java#h0")
    assert profile.tag_scores == {}
    assert profile.signals == []


def test_risk_signal_carries_source_and_reason():
    sig = RiskSignal(tag=RiskTag.AUTHORIZATION, score=3, source="rule:auth", reason="Controller 无权限注解")
    assert sig.line is None
    assert sig.tag == RiskTag.AUTHORIZATION


def test_review_budget_defaults_to_no_limit():
    budget = ReviewBudget()
    assert budget.max_tasks_to_review is None
    assert budget.max_final_issues is None


def test_task_selection_records_skips():
    sel = TaskSelection(
        selected_task_ids=["A.java#h0"],
        skipped_tasks=[SkippedTask(task_id="B.java#h0", reason="低价值文件")],
    )
    assert sel.selected_task_ids == ["A.java#h0"]
    assert sel.skipped_tasks[0].risk_score == 0


def test_task_context_bundle_does_not_duplicate_task_facts():
    bundle = TaskContextBundle(
        task_id="A.java#h0",
        facts=[ContextFact(source="diff", kind="hunk", content="x")],
    )
    keys = set(bundle.model_dump())
    assert keys == {"task_id", "facts", "truncated"}
    assert "file" not in keys
    assert "patch" not in keys


def test_profile_has_no_total_score_field():
    # total_score 是 TaskRank 的派生计算，不得成为第二份可变事实（spec §3.2）。
    assert "total_score" not in RiskProfile.model_fields
```

- [ ] **Step 2: 运行测试确认失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_tasks_models.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'codeguard_agent.models.tasks'`

- [ ] **Step 3: 写最小实现**

创建 `services/agent/src/codeguard_agent/models/tasks.py`:

```python
"""风险路由任务链的内部状态模型（Phase 1）。

这些模型只用于图 State、trace 和 eval 诊断，不进入 ReviewResult 产品输出。
事实源单一所有者原则见 spec §3.3：TaskContextBundle 不复制 file/patch/RiskTag，
RiskProfile 不保存 total_score（分数是 TaskRank 的派生计算）。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from codeguard_agent.models.council import ContextFact


class RiskTag(str, Enum):
    """路由信号标签——只说明"应从哪些角度审"，不代表"这里已有问题"。"""

    AUTHORIZATION = "AUTHORIZATION"
    INPUT_VALIDATION = "INPUT_VALIDATION"
    SQL_DATA_ACCESS = "SQL_DATA_ACCESS"
    TRANSACTION = "TRANSACTION"
    IDEMPOTENCY = "IDEMPOTENCY"
    CACHE_CONSISTENCY = "CACHE_CONSISTENCY"
    MESSAGE_QUEUE = "MESSAGE_QUEUE"
    ERROR_HANDLING = "ERROR_HANDLING"
    NULL_SAFETY = "NULL_SAFETY"
    CONFIG_SECURITY = "CONFIG_SECURITY"
    MAINTAINABILITY = "MAINTAINABILITY"


class ReviewTask(BaseModel):
    """最小调度单位：一个 hunk 或一个文件级 fallback 片段。"""

    id: str
    file: str
    hunk_header: str = ""
    patch: str
    changed_lines: list[int] = Field(default_factory=list)


class RiskSignal(BaseModel):
    """单条风险信号：说明某个 RiskTag 来自哪里、为什么。"""

    tag: RiskTag
    score: int
    source: str
    reason: str
    line: int | None = None


class RiskProfile(BaseModel):
    """一个任务的风险画像。不保存 total_score（派生计算）。"""

    task_id: str
    tag_scores: dict[RiskTag, int] = Field(default_factory=dict)
    signals: list[RiskSignal] = Field(default_factory=list)


class ReviewBudget(BaseModel):
    """预算入口。None 表示当前策略不施加该项限制；Phase 1 基线为全选。"""

    max_tasks_to_review: int | None = None
    max_tasks_per_file: int | None = None
    max_context_chars_per_task: int | None = None
    max_final_issues: int | None = None


class SkippedTask(BaseModel):
    """TaskRank 跳过的任务及原因。"""

    task_id: str
    reason: str
    risk_score: int = 0


class TaskSelection(BaseModel):
    """TaskRank 的唯一选择决策。"""

    selected_task_ids: list[str]
    skipped_tasks: list[SkippedTask] = Field(default_factory=list)


class TaskContextBundle(BaseModel):
    """按任务构建的上下文包。不复制 file/patch/RiskTag（通过 task_id 关联读取）。"""

    task_id: str
    facts: list[ContextFact] = Field(default_factory=list)
    truncated: bool = False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_tasks_models.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/models/tasks.py services/agent/tests/test_tasks_models.py
git commit -m "feat(models): 新增风险路由任务链模型(ReviewTask/RiskProfile/TaskSelection 等)"
```

---

## Task 2: 任务准备纯函数（build/triage/rank/map）

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/task_prep.py`
- Test: `services/agent/tests/test_task_prep.py`

- [ ] **Step 1: 写失败测试**

创建 `services/agent/tests/test_task_prep.py`:

```python
"""任务准备纯函数测试（Phase 1）。"""

from __future__ import annotations

from codeguard_agent.models.tasks import ReviewBudget, ReviewTask
from codeguard_agent.pipeline.task_prep import (
    build_tasks,
    map_candidate_to_task,
    rank_tasks,
    triage_tasks,
)

_TWO_HUNK_DIFF = (
    "diff --git a/A.java b/A.java\n"
    "--- a/A.java\n"
    "+++ b/A.java\n"
    "@@ -1,2 +1,3 @@ class A\n"
    " int a=0;\n"
    "+int b=1;\n"
    " int c=2;\n"
    "@@ -10,1 +11,2 @@ void f()\n"
    " call();\n"
    "+guard();\n"
)


def test_build_tasks_one_task_per_hunk():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    assert [t.id for t in tasks] == ["A.java#h0", "A.java#h1"]
    assert all(t.file == "A.java" for t in tasks)


def test_build_tasks_records_added_line_numbers():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    # hunk0 新起点 1：上下文行 a=0(1)、新增 b=1(2)、上下文 c=2(3) → 新增行号 [2]
    assert tasks[0].changed_lines == [2]
    # hunk1 新起点 11：上下文 call(11)、新增 guard(12) → [12]
    assert tasks[1].changed_lines == [12]


def test_build_tasks_falls_back_to_file_level_when_no_hunk():
    # 无 @@ hunk 头（例如纯 rename/二进制）→ 文件级 fallback task
    diff = "diff --git a/B.java b/B.java\nrename from B.java\nrename to B.java\n+++ b/B.java\n"
    tasks = build_tasks(diff)
    assert len(tasks) == 1
    assert tasks[0].id == "B.java#file"
    assert tasks[0].changed_lines == []


def test_triage_tasks_returns_empty_profile_per_task():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    profiles = triage_tasks(tasks)
    assert set(profiles) == {"A.java#h0", "A.java#h1"}
    assert profiles["A.java#h0"].tag_scores == {}


def test_rank_tasks_selects_all_by_default():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    profiles = triage_tasks(tasks)
    sel = rank_tasks(tasks, profiles, ReviewBudget())
    assert sel.selected_task_ids == ["A.java#h0", "A.java#h1"]
    assert sel.skipped_tasks == []


def test_map_candidate_uses_changed_line_first():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    # 行 12 命中 hunk1 的 changed_lines
    assert map_candidate_to_task("A.java", 12, tasks) == "A.java#h1"


def test_map_candidate_falls_back_to_first_file_task():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    # 行 999 不在任何 changed_lines → 落到该文件第一个 task
    assert map_candidate_to_task("A.java", 999, tasks) == "A.java#h0"


def test_map_candidate_matches_by_basename():
    tasks = [ReviewTask(id="src/A.java#h0", file="src/A.java", patch="")]
    # LLM 常只给 basename
    assert map_candidate_to_task("A.java", 0, tasks) == "src/A.java#h0"


def test_map_candidate_returns_none_when_file_absent():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    assert map_candidate_to_task("Ghost.java", 1, tasks) is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_task_prep.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'codeguard_agent.pipeline.task_prep'`

- [ ] **Step 3: 写最小实现**

创建 `services/agent/src/codeguard_agent/pipeline/task_prep.py`:

```python
"""任务准备纯函数（Phase 1 薄实现）。

职责边界（spec §4.2/§4.3/§4.4）：
- build_tasks：解析 unified diff → 每 hunk 一个 ReviewTask，无 hunk 退化为文件级 fallback。
  不判断风险、不读仓库文件、不调 LLM。
- triage_tasks：Phase 1 为每个任务产出空 RiskProfile（规则留到 Phase 2）。
- rank_tasks：Phase 1 默认全选（预算生效留到 Phase 2）。
- map_candidate_to_task：候选(file, line) → task_id 的确定性映射，无法映射返回 None。
"""

from __future__ import annotations

import re

from codeguard_agent.git.diff_collector import split_diff_by_file
from codeguard_agent.models.tasks import (
    ReviewBudget,
    ReviewTask,
    RiskProfile,
    TaskSelection,
)

# @@ -oldStart[,oldLen] +newStart[,newLen] @@ [section heading]
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _basename(path: str) -> str:
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()


def _split_hunks(section: str) -> list[tuple[str, str, int]]:
    """把单文件 diff 片段切成 [(header_line, hunk_body, new_start_line)]。"""
    hunks: list[tuple[str, str, int]] = []
    current: list[str] | None = None
    header = ""
    new_start = 0
    for line in section.splitlines():
        m = _HUNK_HEADER.match(line)
        if m:
            if current is not None:
                hunks.append((header, "\n".join(current), new_start))
            header = line
            new_start = int(m.group(1))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        hunks.append((header, "\n".join(current), new_start))
    return hunks


def _changed_lines(hunk_body: str, new_start: int) -> list[int]:
    """返回 hunk 中新增行('+')在新文件中的行号。"""
    changed: list[int] = []
    line_no = new_start
    for line in hunk_body.splitlines():
        if line.startswith("@@") or line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            changed.append(line_no)
            line_no += 1
        elif line.startswith("-"):
            continue  # 删除行不占新文件行号
        else:
            line_no += 1  # 上下文行
    return changed


def build_tasks(diff_text: str) -> list[ReviewTask]:
    """解析 unified diff → ReviewTask 列表（每 hunk 一个，无 hunk 退化为文件级）。"""
    tasks: list[ReviewTask] = []
    for file, section in split_diff_by_file(diff_text).items():
        hunks = _split_hunks(section)
        if not hunks:
            tasks.append(
                ReviewTask(id=f"{file}#file", file=file, patch=section, changed_lines=[])
            )
            continue
        for i, (header, body, new_start) in enumerate(hunks):
            tasks.append(
                ReviewTask(
                    id=f"{file}#h{i}",
                    file=file,
                    hunk_header=header,
                    patch=body,
                    changed_lines=_changed_lines(body, new_start),
                )
            )
    return tasks


def triage_tasks(tasks: list[ReviewTask]) -> dict[str, RiskProfile]:
    """Phase 1：每个任务产出空 RiskProfile。"""
    return {t.id: RiskProfile(task_id=t.id) for t in tasks}


def rank_tasks(
    tasks: list[ReviewTask],
    profiles: dict[str, RiskProfile],
    budget: ReviewBudget,
) -> TaskSelection:
    """Phase 1：默认全选，不施加预算限制。"""
    return TaskSelection(selected_task_ids=[t.id for t in tasks], skipped_tasks=[])


def map_candidate_to_task(file: str, line: int, tasks: list[ReviewTask]) -> str | None:
    """候选(file, line) → task_id。命中 changed_lines 优先，否则落到该文件首个 task。

    文件不在任何任务中 → None（调用方应拒绝该候选并留 trace）。
    """
    target = _basename(file)
    file_tasks = [t for t in tasks if _basename(t.file) == target]
    if not file_tasks:
        return None
    for t in file_tasks:
        if line in t.changed_lines:
            return t.id
    return file_tasks[0].id
```

- [ ] **Step 4: 运行测试确认通过**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_task_prep.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/task_prep.py services/agent/tests/test_task_prep.py
git commit -m "feat(pipeline): 新增任务准备纯函数(hunk 解析/空画像/全选/候选映射)"
```

---

## Task 3: 图中接入 DiffTaskBuilder → RiskTriage → TaskRank 三节点

此任务只接入准备链与新 state 字段，**不改动候选流转**（`task_id` 仍未必填）。图跑通后现有候选相关测试应保持通过。

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py`
- Test: `services/agent/tests/test_graph_orchestration.py`

- [ ] **Step 1: 写失败测试**

在 `services/agent/tests/test_graph_orchestration.py` 末尾追加：

```python
def test_build_graph_has_task_prep_nodes():
    graph = G.build_review_graph(enable_summary=True, llm=None)
    names = set(graph.get_graph().nodes)
    assert {"diff_task_builder", "risk_triage", "task_rank"} <= names


def test_task_prep_nodes_populate_state():
    from codeguard_agent.pipeline import task_prep

    tasks = task_prep.build_tasks(_DIFF)
    assert [t.id for t in tasks] == ["A.java#h0"]
    profiles = task_prep.triage_tasks(tasks)
    sel = task_prep.rank_tasks(tasks, profiles, G.ReviewBudget())
    assert sel.selected_task_ids == ["A.java#h0"]


def test_review_state_has_task_chain_fields():
    ann = G.ReviewState.__annotations__
    for field in (
        "review_budget",
        "review_tasks",
        "risk_profiles",
        "task_selection",
        "task_context_bundles",
    ):
        assert field in ann
```

- [ ] **Step 2: 运行测试确认失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py::test_build_graph_has_task_prep_nodes tests/test_graph_orchestration.py::test_review_state_has_task_chain_fields -q`
Expected: FAIL（节点不存在 / state 字段缺失）

- [ ] **Step 3a: graph.py 顶部增加导入**

在 `graph.py` 的 import 区（约 41 行 `from codeguard_agent.models.schemas import ...` 之后）加入：

```python
from codeguard_agent.models.tasks import (
    ReviewBudget,
    ReviewTask,
    RiskProfile,
    TaskContextBundle,
    TaskSelection,
)
from codeguard_agent.pipeline import task_prep
```

- [ ] **Step 3b: ReviewState 增加任务链字段**

在 `graph.py` 的 `class ReviewState(TypedDict, total=False):` 中，`diff_summary: str` 之后插入：

```python
    review_budget: ReviewBudget
    review_tasks: list[ReviewTask]
    risk_profiles: dict[str, RiskProfile]
    task_selection: TaskSelection
    task_context_bundles: dict[str, TaskContextBundle]
```

- [ ] **Step 3c: 新增三个准备节点**

在 `graph.py` 的 `_context_provider_node` 定义**之前**插入：

```python
def _diff_task_builder_node():
    """DiffTaskBuilder：解析 diff → ReviewTask。不判断风险、不读仓库、不调 LLM。"""

    def _node(state: ReviewState) -> dict:
        tasks = task_prep.build_tasks(state.get("diff_text", ""))
        return {
            "review_tasks": tasks,
            "council_trace": [
                CouncilTrace(
                    node="diff_task_builder",
                    event="tasks_built",
                    detail=f"tasks={len(tasks)}",
                )
            ],
        }

    return _node


def _risk_triage_node():
    """RiskTriage：为每个任务产出 RiskProfile（Phase 1 为空画像）。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        profiles = task_prep.triage_tasks(tasks)
        return {
            "risk_profiles": profiles,
            "council_trace": [
                CouncilTrace(
                    node="risk_triage",
                    event="profiled",
                    detail=f"profiles={len(profiles)}",
                )
            ],
        }

    return _node


def _task_rank_node():
    """TaskRank：根据画像与预算选择进入深审的任务（Phase 1 全选）。"""

    def _node(state: ReviewState) -> dict:
        tasks = state.get("review_tasks") or []
        profiles = state.get("risk_profiles") or {}
        budget = state.get("review_budget") or ReviewBudget()
        selection = task_prep.rank_tasks(tasks, profiles, budget)
        return {
            "task_selection": selection,
            "council_trace": [
                CouncilTrace(
                    node="task_rank",
                    event="selected",
                    detail=f"selected={len(selection.selected_task_ids)} skipped={len(selection.skipped_tasks)}",
                )
            ],
        }

    return _node
```

- [ ] **Step 3d: context_provider 节点产出 task_context_bundles**

把 `graph.py` 的 `_context_provider_node` 内 `_node` 的 `return {...}` 替换为（在原有键基础上增加 `task_context_bundles`）：

```python
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

- [ ] **Step 3e: 在 build_review_graph 中注册节点并重连前置边**

在 `build_review_graph` 中，`g.add_node("context_provider", ...)` 那行**之后**加入三节点注册：

```python
    g.add_node("diff_task_builder", _diff_task_builder_node())
    g.add_node("risk_triage", _risk_triage_node())
    g.add_node("task_rank", _task_rank_node())
```

再把原来的入口边块：

```python
    if enable_summary:
        g.add_node("summary", _summary_node(llm, tool_client))
        g.add_edge(START, "summary")
        g.add_edge("summary", "context_provider")
    else:
        g.add_edge(START, "context_provider")
```

替换为（插入准备链，`context_provider` 前移到 `task_rank` 之后）：

```python
    if enable_summary:
        g.add_node("summary", _summary_node(llm, tool_client))
        g.add_edge(START, "summary")
        g.add_edge("summary", "diff_task_builder")
    else:
        g.add_edge(START, "diff_task_builder")
    g.add_edge("diff_task_builder", "risk_triage")
    g.add_edge("risk_triage", "task_rank")
    g.add_edge("task_rank", "context_provider")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py -q`
Expected: PASS（新增 3 个测试通过；现有测试仍全绿——候选流转未改动）

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/graph.py services/agent/tests/test_graph_orchestration.py
git commit -m "feat(pipeline): 图接入 DiffTaskBuilder/RiskTriage/TaskRank 准备链"
```

---

## Task 4: CandidateIssue.task_id 字面必填 + 收集节点确定性映射

这是"严格 task_id"的原子改动：模型字段 + `from_issue` 签名 + 收集节点回填 + 全部测试构造同步更新。完成后每条候选都有 task_id，无法映射的候选被拒绝并留 trace。

**Files:**
- Modify: `services/agent/src/codeguard_agent/models/council.py`
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py:make_reviewer_node`
- Modify: `services/agent/tests/test_council_models.py`
- Modify: `services/agent/tests/test_graph_orchestration.py`

- [ ] **Step 1: 写失败测试（council 模型契约）**

在 `services/agent/tests/test_council_models.py` 顶部 import 后追加新测试：

```python
def test_candidate_requires_task_id():
    issue = Issue(
        severity=Severity.WARNING, file="A.java", line=1, type="t",
        message="m", confidence=0.9,
    )
    candidate = CandidateIssue.from_issue(
        issue, source_agent="threat_model", index=1, task_id="A.java#h0"
    )
    assert candidate.task_id == "A.java#h0"
```

并把 `test_candidate_contains_only_the_candidate_claim` 中 `from_issue(...)` 调用与断言集合更新为：

```python
    candidate = CandidateIssue.from_issue(
        issue, source_agent="threat_model", index=1, task_id="src/UserService.java#h0"
    )

    assert set(candidate.model_dump()) == {
        "id",
        "task_id",
        "source_agent",
        "file",
        "line",
        "type",
        "severity_proposal",
        "claim",
        "suggestion",
        "confidence",
    }
```

同时把该文件另外两处 `from_issue(...)`（`test_build_evidence_requests_dispatches_tools_by_source_agent`、`test_build_evidence_requests_skips_located_high_confidence_candidate`）都补上 `task_id="A.java#h0"` 参数。

- [ ] **Step 2: 运行测试确认失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_council_models.py -q`
Expected: FAIL（`from_issue()` 收到未知参数 `task_id` / `CandidateIssue` 无 `task_id` 字段）

- [ ] **Step 3a: council.py 增字段与参数**

在 `models/council.py` 的 `class CandidateIssue(BaseModel):` 中，`id: str` 之后插入：

```python
    task_id: str
```

把 `from_issue` 的签名与实现改为：

```python
    @classmethod
    def from_issue(
        cls,
        issue: Issue,
        *,
        index: int,
        source_agent: str,
        task_id: str,
    ) -> "CandidateIssue":
        """把现有 reviewer 输出转换为内部候选结构。task_id 必填（spec §3.2）。"""
        cid = f"{source_agent}-{index}-{issue.file}:{issue.line}:{issue.type}"
        return cls(
            id=cid,
            task_id=task_id,
            source_agent=source_agent,
            file=issue.file,
            line=issue.line,
            type=issue.type,
            severity_proposal=issue.severity,
            claim=issue.message,
            suggestion=issue.suggestion,
            confidence=issue.confidence,
        )
```

- [ ] **Step 3b: make_reviewer_node 回填 task_id 并拒绝无法映射的候选**

在 `graph.py` 的 `make_reviewer_node._node` 中，把原来构造 `candidates` 的代码块：

```python
        issues = list(result.get("issues") or [])
        kept_issues = issues[:MAX_CANDIDATES_PER_AGENT]
        truncated_candidates = max(0, len(issues) - len(kept_issues))
        candidates = [
            CandidateIssue.from_issue(
                issue,
                source_agent=reviewer.source_agent,
                index=i + 1,
            )
            for i, issue in enumerate(kept_issues)
        ]
```

替换为：

```python
        issues = list(result.get("issues") or [])
        kept_issues = issues[:MAX_CANDIDATES_PER_AGENT]
        truncated_candidates = max(0, len(issues) - len(kept_issues))
        tasks = state.get("review_tasks") or []
        candidates: list[CandidateIssue] = []
        rejected: list[str] = []
        for i, issue in enumerate(kept_issues):
            task_id = task_prep.map_candidate_to_task(issue.file, issue.line, tasks)
            if task_id is None:
                rejected.append(f"{issue.file}:{issue.line}")
                continue
            candidates.append(
                CandidateIssue.from_issue(
                    issue,
                    source_agent=reviewer.source_agent,
                    index=i + 1,
                    task_id=task_id,
                )
            )
```

然后在该 `_node` 的 `out` 字典的 `council_trace` 列表中，追加一条拒绝记录。把：

```python
            "council_trace": list(result.get("council_trace") or [])
            + [
                CouncilTrace(
                    node=reviewer.source_agent,
                    event="candidates_created",
                    detail=f"count={len(candidates)} truncated={truncated_candidates}",
                )
            ],
```

替换为：

```python
            "council_trace": list(result.get("council_trace") or [])
            + [
                CouncilTrace(
                    node=reviewer.source_agent,
                    event="candidates_created",
                    detail=f"count={len(candidates)} truncated={truncated_candidates} rejected={len(rejected)}",
                )
            ]
            + (
                [
                    CouncilTrace(
                        node=reviewer.source_agent,
                        event="candidate_rejected_unmapped",
                        detail="; ".join(rejected),
                    )
                ]
                if rejected
                else []
            ),
```

- [ ] **Step 3c: 更新 test_graph_orchestration.py 的 24 处直接构造与 from_issue 辅助**

`_candidate()` 辅助（约 95 行）改为传 task_id：

```python
def _candidate(*, confidence=0.9):
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=1,
        type="t",
        message="m",
        confidence=confidence,
    )
    return G.CandidateIssue.from_issue(
        issue,
        source_agent="threat_model",
        index=1,
        task_id="A.java#h0",
    )
```

其余所有 `G.CandidateIssue(...)` 直接构造（`test_council_judge_*`、`test_evidence_agent_*` 等，共约 23 处）均加入 `task_id="<file>#h0"` 参数——用该构造里的 `file=` 值生成，例如 `file="A.java"` → `task_id="A.java#h0"`、`file="UserService.java"` → `task_id="UserService.java#h0"`、`file="Ghost.java"` → `task_id="Ghost.java#h0"`、`file=""`（`test_council_judge_rule_invalid_file`）→ `task_id="#file"`。task_id 具体值不影响这些单测断言，只需存在且非缺省。

> 执行提示：`grep -n "CandidateIssue(" tests/test_graph_orchestration.py` 定位每一处，逐个在 `id=...` 后补 `task_id=...`。

- [ ] **Step 3d: 修正 fan-in 假引擎（改用 diff 内文件 + 分散行号）**

`_FakeEngine.review`（约 731 行）当前用 `file=f"{reviewer_name}.java"`、`line=1`，会因文件不在 `_DIFF`（A.java）而被全部拒绝；改为发到 diff 内文件 A.java，并用分散行号避免邻行合并：

```python
_FAKE_LINE = {"ThreatModelAgent": 1, "BehaviorAgent": 5, "MaintainabilityAgent": 9}


class _FakeEngine:
    def review(self, llm, *, system_prompt, user_prompt, reviewer_name, max_retries, structured_method, enable_hitl=False):
        line = _FAKE_LINE.get(reviewer_name, 1)
        issue = Issue(
            severity=Severity.WARNING,
            file="A.java",
            line=line,
            type=reviewer_name,
            message="m",
        )
        gc = [GatheredContext("get_file_content", f"{reviewer_name}.java", "x")]
        return ReviewOutcome(ReviewResult(summary=f"sum-{reviewer_name}", issues=[issue]), gc)
```

`test_graph_fanin_three_discoverers` 的断言保持不变（三种 type 各一条、trace 3 条、candidate_count 3）。

- [ ] **Step 3e: 修正 limits 假引擎与其 diff（多文件 diff 使 24 候选可映射）**

`test_candidate_and_evidence_request_limits_are_enforced` 里，把 `_many_candidates_from_issue` 的签名补上 `task_id`，并让 orchestrator 跑一个包含全部 24 个文件的 diff。改动如下：

在 `_many_candidates_from_issue` 定义处：

```python
    def _many_candidates_from_issue(issue, *, source_agent, index, task_id):
        return original_from_issue(
            issue,
            source_agent=source_agent,
            index=index,
            task_id=task_id,
        )
```

`_ManyIssueEngine.review` 保持发 `file=f"{reviewer_name}-{i}.java"`、`line=i+1`（8 条）。把该测试中 `result = orch.run(_FakeLLM(), _DIFF, metadata_sink=meta)` 改为使用覆盖全部文件的 diff：

```python
    files = [
        f"{name}-{i}.java"
        for name in ("ThreatModelAgent", "BehaviorAgent", "MaintainabilityAgent")
        for i in range(8)
    ]
    limits_diff = "".join(
        f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -1 +1,2 @@\n+int x=1;\n"
        for f in files
    )
    result = orch.run(_FakeLLM(), limits_diff, metadata_sink=meta)
```

断言保持不变（24 issues、candidate_count 24、evidence_request_count 20、truncated_candidates 0）。每个文件独立成任务，行号不同、文件不同 → 无邻行/指纹合并，24 条全存活。

- [ ] **Step 3f: 修正 mock 端到端用例的 diff（用 Demo.java）**

`mock_review_result()` 发到 `example/Demo.java:42`，需让 diff 包含该文件。在 `test_graph_orchestration.py` 中 `_DIFF` 定义附近新增：

```python
_MOCK_DIFF = (
    "diff --git a/example/Demo.java b/example/Demo.java\n"
    "--- a/example/Demo.java\n"
    "+++ b/example/Demo.java\n"
    "@@ -1 +1,2 @@\n"
    "+int x=1;\n"
)
```

把以下三个用例中传给 `run(...)` 的 `_DIFF` 改为 `_MOCK_DIFF`（它们都走 mock/llm=None 路径，候选来自 mock_review_result）：
- `test_adr032_mock_end_to_end`（断言 `len(result.issues) == 1` 不变）
- `test_orchestrator_with_memory_checkpointer_produces_same_result`
- `test_hitl_is_ignored_in_adr032_default_path`

> `test_orchestrator_initial_state_omits_empty_runtime_outputs` 与 `test_run_empty_diff_short_circuits` 使用 `_Graph` 桩或空 diff，不经真实建图，保持用 `_DIFF`/空串不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_council_models.py tests/test_graph_orchestration.py -q`
Expected: PASS（全绿；候选均带 task_id，fan-in/limits/mock 用例在新映射下通过）

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/models/council.py services/agent/src/codeguard_agent/pipeline/graph.py services/agent/tests/test_council_models.py services/agent/tests/test_graph_orchestration.py
git commit -m "feat(pipeline): CandidateIssue.task_id 字面必填 + 收集节点确定性映射与拒绝"
```

---

## Task 5: 移除 council_route，固定三路 fan-in → EvidenceAgent → CouncilJudge

拓扑收敛为：`context_provider → discover_*(×3) → council_coordinator(fan-in 一次) → evidence_agent(必经一次) → council_judge → [evidence_agent(needs_more 且轮次未超) | END]`。删除 `council_route` state 与协调器条件路由。

> **观测层作用域说明**：`observability/collector.py`、`observability/view_model.py` 通过 `.get("council_route")` 防御式读取该字段。移除后这些读取恒为空、路由计数走 `route_decision` 事件分支，**不会崩溃**（`test_observability.py` 自造合成事件，不依赖图产出 council_route，保持通过）。这些恒空读取属遗留死路径，清理留到 Phase 6（Trace/Eval 闭环），本任务不改观测层，守住 Phase 1 作用域。

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py`
- Modify: `services/agent/tests/test_graph_orchestration.py`

- [ ] **Step 1: 写失败测试（新拓扑不变量）**

在 `test_graph_orchestration.py` 追加：

```python
def test_review_state_excludes_council_route():
    assert "council_route" not in G.ReviewState.__annotations__


def test_coordinator_always_edges_to_evidence_agent():
    graph = G.build_review_graph(enable_summary=False, llm=None)
    edges = graph.get_graph().edges
    pairs = {(e.source, e.target) for e in edges}
    # coordinator → evidence_agent 是无条件边
    assert ("council_coordinator", "evidence_agent") in pairs
    # evidence_agent → council_judge 是无条件边
    assert ("evidence_agent", "council_judge") in pairs
    # 旧的 evidence → coordinator 回环已移除
    assert ("evidence_agent", "council_coordinator") not in pairs


def test_evidence_agent_runs_once_before_judge(monkeypatch):
    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _FakeEngine())
    orch = PipelineOrchestrator(enable_summary=False)
    meta: dict = {}
    orch.run(_FakeLLM(), _DIFF, metadata_sink=meta)
    # 首次 Evidence 必经一次（即便无 evidence_requests 也 no-op 跑一轮）
    assert meta["council"]["evidence_rounds"] >= 1
```

同时**删除**以下 4 个测试（它们断言即将移除的 `_route_after_coordinator`）：
- `test_coordinator_skips_evidence_and_judge_when_no_candidates`
- `test_coordinator_routes_to_evidence_on_first_round`
- `test_coordinator_routes_to_council_judge_when_no_evidence_needed`
- `test_coordinator_after_evidence_round_goes_to_council_judge`

`test_route_after_council_judge_*` 三个测试保留（Judge 回环规则不变）。

- [ ] **Step 2: 运行测试确认失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py::test_review_state_excludes_council_route tests/test_graph_orchestration.py::test_coordinator_always_edges_to_evidence_agent -q`
Expected: FAIL（`council_route` 仍在 state；仍是条件边）

- [ ] **Step 3a: ReviewState 移除 council_route**

删除 `graph.py` 的 `class ReviewState` 中这一行：

```python
    council_route: str
```

- [ ] **Step 3b: 简化 coordinator 节点（不再计算路由）**

把 `_coordinator_node` 整体替换为：

```python
def _coordinator_node():
    """三路发现者的显式 fan-in barrier：只在三路结束后运行一次。

    只记录本轮候选/证据请求批次统计，固定转入 EvidenceAgent；
    不承担"是否跳过首次补证"的路由决策，也不解析自然语言（spec §4.7）。
    """

    def _node(state: ReviewState) -> dict:
        candidates = state.get("candidate_issues") or []
        pending = state.get("evidence_requests") or []
        return {
            "council_trace": [
                CouncilTrace(
                    node="council_coordinator",
                    event="fan_in",
                    detail=f"candidates={len(candidates)} evidence_requests={len(pending)}",
                )
            ],
        }

    return _node
```

- [ ] **Step 3c: 删除已废弃的路由辅助**

删除 `graph.py` 中的 `_route_after_coordinator` 函数与 `_conditional_route` 函数（整段两个函数定义）。`_route_after_council_judge` 保留不动。

- [ ] **Step 3d: 重连证据边**

在 `build_review_graph` 中，把协调器之后的边定义：

```python
    g.add_conditional_edges(
        "council_coordinator",
        _conditional_route,
        {
            "evidence_agent": "evidence_agent",
            "council_judge": "council_judge",
        },
    )
    g.add_edge("evidence_agent", "council_coordinator")
    g.add_conditional_edges(
        "council_judge",
        _route_after_council_judge,
        {
            "evidence_agent": "evidence_agent",
            "END": END,
        },
    )
```

替换为：

```python
    # 三路 fan-in 后固定进一次 EvidenceAgent，再进 CouncilJudge。
    g.add_edge("council_coordinator", "evidence_agent")
    g.add_edge("evidence_agent", "council_judge")
    # Judge 仅在 needs_more 且轮次未超时回环补证，否则 END。
    g.add_conditional_edges(
        "council_judge",
        _route_after_council_judge,
        {
            "evidence_agent": "evidence_agent",
            "END": END,
        },
    )
```

- [ ] **Step 3e: 更新 build_review_graph 顶部拓扑注释**

把 `build_review_graph` docstring 里的"目标拓扑"改为反映新链路：

```python
    """编译 ADR-032 审查状态图（风险路由 Phase 1）。

    目标拓扑:
        summary? → diff_task_builder → risk_triage → task_rank → context_provider
          → discover_*(×3) → council_coordinator(fan-in 一次)
          → evidence_agent(必经一次) → council_judge
          → [evidence_agent(needs_more 且轮次未超) | END]
    """
```

- [ ] **Step 4: 运行测试确认通过**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/ -q`
Expected: PASS（全套测试全绿）

- [ ] **Step 5: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/graph.py services/agent/tests/test_graph_orchestration.py
git commit -m "refactor(pipeline): 移除 council_route，固定 fan-in→Evidence→Judge 主路由"
```

---

## Task 6: 回归验证与实施台账更新

**Files:**
- Modify: `docs/superpowers/specs/2026-07-10-risk-routed-review-orchestration-design.md`

- [ ] **Step 1: 全量单测 + lint + 类型检查**

Run:
```bash
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
conda run -n codeguard ruff check src/
conda run -n codeguard mypy src/
```
Expected: 单测全绿；ruff 无新增告警；mypy 无新增错误（若 mypy 对 `dict[RiskTag, int]` 报既有风格问题，与本次改动无关的既有告警可忽略，但不得引入新错误）。

- [ ] **Step 2: mock 冒烟（人工确认链路仍打通）**

Run:
```bash
$env:CODEGUARD_PROVIDER="mock"; conda run -n codeguard --no-capture-output python -m codeguard_agent review --repo . --base HEAD
```
Expected: 正常打印 mock 审查结果，退出码 0/1（取决于是否有 CRITICAL），无异常栈。

- [ ] **Step 3: 更新设计文档实施台账**

把 spec `§5 实施台账` 表中 Phase 1 行由：

```
| Phase 1 | planned | 无 | 未开始 | 无 | 见本阶段非目标 |
```

改为（填入真实落地内容与验证证据；`<commit>` 用本计划各任务的提交短哈希）：

```
| Phase 1 | done | models/tasks.py(ReviewTask/RiskProfile/ReviewBudget/TaskSelection/TaskContextBundle)、pipeline/task_prep.py(build/triage/rank/map)、graph.py 新增 diff_task_builder/risk_triage/task_rank 节点、CandidateIssue.task_id 必填、context_provider 产出 task_context_bundles、移除 council_route | 新增 review_budget/review_tasks/risk_profiles/task_selection/task_context_bundles，删除 council_route | tests/test_tasks_models.py、tests/test_task_prep.py、tests/test_graph_orchestration.py(拓扑+映射+fan-in-once)全绿；commit <commit> | 风险规则/预算/定向上下文/定向发现（Phase 2-6） |
```

- [ ] **Step 4: 提交**

```bash
git add docs/superpowers/specs/2026-07-10-risk-routed-review-orchestration-design.md
git commit -m "docs(orchestration): 更新风险路由 Phase 1 实施台账"
```

---

## 完成条件（对齐 spec §5 Phase 1 完成条件）

- [ ] 图结构测试证明 `council_coordinator` 在三路发现结束后只运行一次，`evidence_agent` 首次必经（`test_coordinator_always_edges_to_evidence_agent` + `test_evidence_agent_runs_once_before_judge`）。
- [ ] 所有候选均有 `task_id`，无法映射者被显式拒绝并留 `candidate_rejected_unmapped` trace（`make_reviewer_node` + `test_candidate_requires_task_id`）。
- [ ] 新旧 mock 路径仍能返回 `ReviewResult`（`test_adr032_mock_end_to_end`）。
- [ ] `ReviewResult`/`Issue` 产品契约未变（`models/schemas.py` 未改动）。
- [ ] 实施台账 Phase 1 行标记 `done` 并附验证证据与 commit。
