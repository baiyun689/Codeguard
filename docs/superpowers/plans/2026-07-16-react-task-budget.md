# ReAct Task Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 普通 diff 全量审查，只让风险排序前 20 个合格 task 使用 ReAct；大 diff 仅由超过 5000 行触发任务范围降级。

**Architecture:** `large_diff_policy` 唯一决定是否裁剪 task；`risk_routing` 通过一个批量规划接口统一分配 task tier。ReAct 上限放入现有 `ReviewBudget`，经 Settings/CLI 进入既有 ReviewState，不新增顶层 State 字段。

**Tech Stack:** Python 3.11+、Pydantic、LangGraph、pytest、Ruff、mypy。

## Global Constraints

- 大 diff 的唯一判定条件是原始 diff 行数超过 5000。
- 普通 diff 默认选择全部 task；大 diff 保留 20 tasks、3 tasks/file、2000 context chars/task。
- ReAct task 默认上限为 20，环境变量为 `CODEGUARD_MAX_REACT_TASKS`，只接受正整数。
- ReAct 资格保持现有 `max(tag_scores) >= 2`；其余 task 全部 Direct，不跳过。
- 无工具服务时全部 Direct；不新增 ReviewState、ReviewResult、Issue、Java 协议或数据库字段。

---

### Task 1: 模式判定与普通模式全选

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/large_diff_policy.py`
- Modify: `services/agent/tests/test_large_diff_policy.py`
- Modify: `services/agent/tests/test_large_diff_graph.py`

**Interfaces:**
- Consumes: `plan_large_diff(diff_text, tasks, configured_budget) -> LargeDiffPlan`
- Produces: 普通模式的 `effective_budget` 具有 `max_tasks_to_review=None`、`max_tasks_per_file=None`；大 diff 接口不变。

- [x] **Step 1: 写失败测试**

```python
def test_task_count_alone_never_activates_large_diff():
    plan = plan_large_diff("small", [_task(i) for i in range(200)], ReviewBudget())
    assert plan.active is False
    assert plan.effective_budget.max_tasks_to_review is None
    assert plan.effective_budget.max_tasks_per_file is None

def test_normal_graph_selects_more_than_ten_hunks_from_one_file():
    # 11 个同文件 task、diff 不超过 5000 行
    assert len(out["task_selection"].selected_task_ids) == 11
```

- [x] **Step 2: 运行测试确认失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_large_diff_policy.py tests/test_large_diff_graph.py -q`  
Expected: task 数阈值仍激活大 diff，或普通模式仍只选择 10 个同文件 task。

- [x] **Step 3: 实现唯一行数阈值和普通无限选择预算**

```python
active = total_lines > LARGE_DIFF_LINE_THRESHOLD
if not active:
    return LargeDiffPlan(
        False,
        total_lines,
        len(tasks),
        configured_budget.model_copy(
            update={"max_tasks_to_review": None, "max_tasks_per_file": None}
        ),
    )
```

同时删除 `LARGE_DIFF_TASK_THRESHOLD` 及相关文档/测试断言；大 diff 的 `min(configured, 20/3/2000)` 保持不变。

- [x] **Step 4: 重跑目标测试**

Run: 同 Step 2。  
Expected: PASS。

---

### Task 2: 批量 ReAct Tier 规划

**Files:**
- Modify: `services/agent/src/codeguard_agent/models/tasks.py`
- Modify: `services/agent/src/codeguard_agent/pipeline/risk_routing.py`
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py`
- Modify: `services/agent/tests/test_tasks_models.py`
- Modify: `services/agent/tests/test_risk_routing.py`
- Modify: `services/agent/tests/test_graph_orchestration.py`

**Interfaces:**
- Produces: `ReviewBudget.max_react_tasks: StrictInt = 20`
- Consumes: `selected_task_ids: list[str]`、`profiles: Mapping[str, RiskProfile]`、`max_react_tasks: int`、`tools_available: bool`
- Produces: `plan_task_tiers(...) -> dict[str, Literal["react", "direct"]]`

- [x] **Step 1: 写批量规划失败测试**

```python
def test_plan_task_tiers_limits_react_without_dropping_tasks():
    selected = [f"t{i}" for i in range(25)]
    profiles = {task_id: strong_profile(task_id) for task_id in selected}
    tiers = plan_task_tiers(selected, profiles, 20, tools_available=True)
    assert list(tiers.values()).count("react") == 20
    assert list(tiers.values()).count("direct") == 5

def test_plan_task_tiers_without_tools_is_all_direct():
    assert set(plan_task_tiers(selected, profiles, 20, False).values()) == {"direct"}

def test_review_budget_defaults_react_tasks_to_twenty():
    assert ReviewBudget().max_react_tasks == 20
```

另加混合测试，证明 low/general 不消耗配额，后面的强风险仍能获得 ReAct。

- [x] **Step 2: 运行风险路由测试确认接口缺失**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_tasks_models.py tests/test_risk_routing.py -q`  
Expected: import/function missing FAIL。

- [x] **Step 3: 实现预算字段和确定性规划接口**

```python
class ReviewBudget(BaseModel):
    max_react_tasks: StrictInt = Field(default=20, gt=0)

def plan_task_tiers(selected_task_ids, profiles, max_react_tasks, *, tools_available):
    remaining = max_react_tasks if tools_available else 0
    tiers = {}
    for task_id in selected_task_ids:
        eligible = decide_tier(profiles.get(task_id)) == "react"
        use_react = eligible and remaining > 0
        tiers[task_id] = "react" if use_react else "direct"
        remaining -= int(use_react)
    return tiers
```

- [x] **Step 4: 接入 reviewer 节点**

在 `make_reviewer_node._node` 中只派生一次 `tier_by_task`：

```python
tier_by_task = plan_task_tiers(
    selection.selected_task_ids,
    profiles,
    (state.get("review_budget") or ReviewBudget()).max_react_tasks,
    tools_available=tool_client is not None,
)
```

`_invoke_one` 使用 `tier_by_task.get(task_id, "direct")`，不再逐 task 直接调用 `decide_tier`。同一 State 的三路 reviewer 因输入相同而得到相同 tier。

- [x] **Step 5: 添加图级测试并运行**

测试 25 个强风险 task 全部被调用但只有前 20 个 payload tier 为 ReAct；无工具时强风险空结果只调用一次 Direct。  
Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_routing.py tests/test_graph_orchestration.py -q`  
Expected: PASS。

---

### Task 3: 配置接线、文档与交付

**Files:**
- Modify: `services/agent/src/codeguard_agent/config.py`
- Modify: `services/agent/src/codeguard_agent/cli.py`
- Modify: `services/agent/tests/test_config_settings.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `DECISIONS.md`

**Interfaces:**
- Produces: `Settings.max_react_tasks: int = 20` from `CODEGUARD_MAX_REACT_TASKS`

- [x] **Step 1: 写配置失败测试**

```python
def test_react_task_budget_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("CODEGUARD_MAX_REACT_TASKS", raising=False)
    assert Settings.from_env().max_react_tasks == 20
    monkeypatch.setenv("CODEGUARD_MAX_REACT_TASKS", "30")
    assert Settings.from_env().max_react_tasks == 30
```

- [x] **Step 2: 运行配置测试确认失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_config_settings.py -q`  
Expected: missing attribute FAIL。

- [x] **Step 3: 实现配置并接入 CLI**

```python
# Settings.from_env
max_react_tasks = _positive_int_env("CODEGUARD_MAX_REACT_TASKS", 20)

# CLI
ReviewBudget(
    max_tasks_to_review=settings.max_review_tasks,
    max_tasks_per_file=settings.max_tasks_per_file,
    max_react_tasks=settings.max_react_tasks,
)
```

- [x] **Step 4: 更新运维说明和 ADR**

明确普通 diff 全选、大 diff 只看 5000 行、ReAct 默认 20 且其余 Direct；ADR-045 标注该后续修订，不新增新产品字段。

- [x] **Step 5: 全量验证**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
conda run -n codeguard --no-capture-output ruff check src
conda run -n codeguard --no-capture-output mypy src
cd services/gateway
mvn verify
```

Expected: 全部 PASS；`git diff --check` 无错误。

- [ ] **Step 6: 审查并提交**

运行 Standards/Spec 双轴审查，修复发现后提交：

```powershell
git add services/agent .env.example README.md AGENTS.md DECISIONS.md docs/superpowers
git commit -m "feat(pipeline): 限制普通审查ReAct任务数"
```
