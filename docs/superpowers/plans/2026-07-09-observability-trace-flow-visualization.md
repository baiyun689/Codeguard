# Observability Trace Flow Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有事件时间线重构为用户确认的 C 方案“审查叙事笔记”，清晰展示主执行流、三名审查员的 ReAct 步骤、节点输入输出与字段写入。

**Architecture:** Python 侧新增一个纯函数视图模型层，将无损 `TraceReport` 归组为主流程、审查员章节、协调闭环、字段写入索引和完整性摘要；静态 HTML 只负责渲染和交互。原始事件仍完整嵌入并保留为兜底，界面选择步骤时只更新详情并保持中间阅读区滚动位置。

**Tech Stack:** Python 3.11+、Pydantic 2、pytest、纯 HTML/CSS/ES5 JavaScript、LangGraph 1.2+

## Global Constraints

- 默认完整保存，不截断、不脱敏；报告继续是单文件、自包含、离线可用。
- 主视图采用 C 方案：左侧目录、中间纵向执行叙事、右侧详情检查器。
- 中文职责名为主标题，真实节点/工具名称为副标题。
- 节点输入是进入节点前的完整 State；节点输出是直接返回的 patch，不伪装成合并后 State。
- 并行 LangGraph 节点只在 superstep 结束后合并，界面不得伪造每个并行节点各自的合并后状态。
- 选择步骤、切换详情标签和上下步跳转不得重置中间阅读区滚动位置。
- 不增加前端依赖，不修改 `ReviewResult` / `Issue` 产品契约。
- Windows 命令统一使用 `conda run -n codeguard --no-capture-output ...`。

---

### Task 1: 构建可测试的追踪视图模型

**Files:**
- Create: `services/agent/src/codeguard_agent/observability/view_model.py`
- Modify: `services/agent/tests/test_observability.py`

**Interfaces:**
- Consumes: `TraceReport.events`、`TraceReport.summary.node_timeline`
- Produces: `build_trace_view(report: TraceReport) -> dict[str, Any]`
- Produces keys: `main_stages`、`reviewer_sections`、`coordination_steps`、`steps`、`state_writes`、`integrity`
- Step references use event sequence numbers instead of duplicating full input/output payloads.

- [ ] **Step 1: Write failing view-model tests**

Add a fixture with paired node/LLM/tool events and assert:

```python
def test_trace_view_groups_reviewer_react_steps_and_state_writes():
    report = _flow_report_fixture()

    view = build_trace_view(report)

    assert [item["code_name"] for item in view["main_stages"]] == [
        "summary",
        "context_provider",
        "review_council",
        "self_checker",
    ]
    threat = next(
        item
        for item in view["reviewer_sections"]
        if item["key"] == "threat_model"
    )
    assert [view["steps"][step_id]["kind"] for step_id in threat["step_ids"]] == [
        "node",
        "llm",
        "tool_call",
        "tool_result",
        "node",
    ]
    assert view["state_writes"]["candidate_issues"][0]["step_id"]
    assert view["integrity"]["missing_end_count"] == 0
```

Add a missing-end fixture:

```python
def test_trace_view_reports_missing_and_unassociated_events():
    report = _incomplete_flow_report_fixture()

    view = build_trace_view(report)

    assert view["integrity"]["missing_end_count"] == 1
    assert view["integrity"]["unassociated_count"] == 1
    assert view["integrity"]["status"] == "incomplete"
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: collection fails because `codeguard_agent.observability.view_model` does not exist.

- [ ] **Step 3: Implement `build_trace_view`**

Implement these stable seams:

```python
REVIEWERS = {
    "discover_threat_model": ("threat_model", "威胁建模审查员", "ThreatModelAgent"),
    "discover_behavior": ("behavior", "行为审查员", "BehaviorAgent"),
    "discover_maintainability": (
        "maintainability",
        "可维护性审查员",
        "MaintainabilityAgent",
    ),
}


def build_trace_view(report: TraceReport) -> dict[str, Any]:
    events_by_sequence = {event.sequence: event for event in report.events}
    node_steps = _pair_events(report.events, "node_start", "node_end")
    llm_steps = _pair_events(report.events, "llm_start", "llm_end")
    tool_steps = _tool_event_steps(report.events)
    steps = _index_steps(node_steps + llm_steps + tool_steps)
    return {
        "main_stages": _main_stages(node_steps),
        "reviewer_sections": _reviewer_sections(steps),
        "coordination_steps": _coordination_steps(steps),
        "steps": steps,
        "state_writes": _state_writes(steps, events_by_sequence),
        "integrity": _integrity(report.events),
    }
```

Required semantics:

- Pair node and LLM start/end by `run_id`.
- Keep tool call and tool result as separate visible steps, sharing `run_id` as `pair_id`.
- Preserve real `sequence`, `node_path`, `invocation_id`, `duration_ms`, `start_sequence`, `end_sequence`.
- Derive reviewer ownership from the first `node_path` segment.
- Include `prepare` and `collect` node steps; suppress ReAct wrapper nodes named `review`、`model`、`tools` when their LLM/tool children already provide the concrete visible step.
- Aggregate three `discover_*` roots as one `review_council` main stage without inventing child-event causality.
- Add a missing `self_checker` placeholder only when the report lacks it, with `status="missing"`.
- Build `state_writes` only from `node_end.detail.output` mappings, store only source step references, and resolve patch values from raw events in the browser.
- Count unmatched starts, unmatched ends and events with `node_path in {"", "unknown"}`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: all observability tests pass.

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/observability/view_model.py services/agent/tests/test_observability.py
git commit -m "feat(observability): 构建追踪执行流视图模型"
```

---

### Task 2: 将视图模型嵌入自包含报告

**Files:**
- Modify: `services/agent/src/codeguard_agent/observability/dashboard.py`
- Modify: `services/agent/tests/test_observability.py`

**Interfaces:**
- Consumes: `build_trace_view(report)`
- Produces: `_dashboard_payload(report: TraceReport) -> dict[str, Any]`
- Preserves: top-level `run_id`、`events`、`summary` for existing reports/tests
- Adds: top-level `view`

- [ ] **Step 1: Write failing payload compatibility test**

```python
def test_dashboard_payload_keeps_raw_report_and_adds_flow_view():
    report = _flow_report_fixture()

    html = render_dashboard(report)
    payload = _extract_trace_payload(html)

    assert payload["events"] == report.model_dump(mode="json")["events"]
    assert payload["view"]["reviewer_sections"][0]["step_ids"]
    assert payload["view"]["integrity"]["event_count"] == len(report.events)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: FAIL because the payload has no `view`.

- [ ] **Step 3: Add the payload adapter**

```python
from typing import Any

from codeguard_agent.observability.view_model import build_trace_view


def _dashboard_payload(report: TraceReport) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload["view"] = build_trace_view(report)
    return payload
```

Update `_json_for_html_script` to encode `_dashboard_payload(report)` while retaining the existing `<`、`>`、`&` and Unicode separator escaping.

- [ ] **Step 4: Run observability tests**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: all observability tests pass and script-like source still round-trips exactly.

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/observability/dashboard.py services/agent/tests/test_observability.py
git commit -m "feat(observability): 嵌入追踪可视化视图模型"
```

---

### Task 3: 正式实现 C 方案审查叙事界面

**Files:**
- Replace: `services/agent/src/codeguard_agent/observability/dashboard_template.html`
- Modify: `services/agent/tests/test_observability.py`

**Interfaces:**
- Consumes: `DATA.view.steps` and sequence references into `DATA.events`
- Produces three top-level tabs: `flow`、`state`、`raw`
- Produces fixed regions: `trace-outline`、`trace-story`、`trace-inspector`
- Produces `renderPreservingReadingPosition(update)` for local detail updates.

- [ ] **Step 1: Write failing template-contract tests**

```python
def test_dashboard_uses_narrative_layout_and_stable_step_identity():
    template = _dashboard_template()

    assert 'id="trace-outline"' in template
    assert 'id="trace-story"' in template
    assert 'id="trace-inspector"' in template
    assert "中文职责" not in template
    assert "renderMainFlow" in template
    assert "renderReviewerSection" in template
    assert "renderStateEvolution" in template
    assert "renderRawEvents" in template


def test_dashboard_preserves_reading_position_for_local_updates():
    template = _dashboard_template()

    assert "captureReadingPosition" in template
    assert "restoreReadingPosition" in template
    assert "renderPreservingReadingPosition" in template
    assert "selectStep" in template
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: FAIL because the current template is an event timeline.

- [ ] **Step 3: Replace the production template**

Promote only the validated C structure from the prototype:

```html
<main class="workspace">
  <nav id="trace-outline" class="outline"></nav>
  <article id="trace-story" class="story"></article>
  <aside id="trace-inspector" class="inspector"></aside>
</main>
```

The implementation must:

- Render `Summary → ContextProvider → ReviewCouncil → SelfChecker` as the main flow.
- Render each `reviewer_section` as one vertical chapter with sticky title and concrete ReAct steps.
- Render coordination/evidence/judge steps as a final chapter.
- Show sequence, Chinese title, code name, kind, duration and compact input/output counts on every step.
- Use blue/purple/orange/cyan/green/red independently for node/LLM/tool-call/tool-result/output/error.
- Use six inspector tabs: overview/input/output/state changes/internal/raw.
- Resolve full input/output by `start_sequence` and `end_sequence`; never copy or truncate payload data in the view model.
- Show state changes as “输入字段值 → 节点输出 patch”，and explicitly state that parallel nodes merge at superstep boundaries.
- Pair tool call/result through `pair_id` and provide jump controls.
- Render state evolution from `view.state_writes`; clicking a write selects its source step.
- Keep raw events searchable and selectable.
- Display trace integrity and the sensitive-data warning in the header.

Implement local updates with:

```javascript
function renderPreservingReadingPosition(update) {
  var position = captureReadingPosition();
  update();
  renderActiveView();
  restoreReadingPosition(position);
}

function selectStep(stepId) {
  renderPreservingReadingPosition(function () {
    selectedStepId = stepId;
  });
}
```

Switching top-level tabs intentionally starts the new view at its own saved position; selecting a step, changing inspector tabs, or using previous/next must preserve the current story position.

- [ ] **Step 4: Run tests and JavaScript syntax validation**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Then extract the final inline script and run:

```powershell
$content = Get-Content -Raw src\codeguard_agent\observability\dashboard_template.html
$scripts = [regex]::Matches($content, '<script(?:\s[^>]*)?>(.*?)</script>', [Text.RegularExpressions.RegexOptions]::Singleline)
$scripts[$scripts.Count - 1].Groups[1].Value | node --check -
```

Expected: pytest and `node --check` both exit `0`.

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/observability/dashboard_template.html services/agent/tests/test_observability.py
git commit -m "feat(observability): 重构审查叙事追踪界面"
```

---

### Task 4: 端到端验证、文档吸收与原型清理

**Files:**
- Modify: `docs/superpowers/specs/2026-07-09-observability-trace-fidelity-fix-design.md`
- Modify: `CONTEXT.md`
- Delete: `services/agent/src/codeguard_agent/observability/_prototype_trace_flow/`
- Test: `services/agent/tests/test_observability.py`

**Interfaces:**
- Produces: production `trace-*.html` with top-level `view`
- Preserves: one graph execution per review and raw event fidelity.

- [ ] **Step 1: Add end-to-end assertions**

Extend `test_mock_review_with_trace`:

```python
assert report_data["view"]["main_stages"]
assert report_data["view"]["reviewer_sections"]
assert "integrity" in report_data["view"]
assert "trace-story" in content
assert "_prototype_trace_flow" not in content
```

- [ ] **Step 2: Run focused and full verification**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
conda run -n codeguard --no-capture-output ruff check src/codeguard_agent/observability/ tests/test_observability.py
conda run -n codeguard --no-capture-output mypy src/codeguard_agent/observability/
```

Expected: all commands exit `0`.

- [ ] **Step 3: Generate a fresh report**

Run the mock end-to-end test or one configured real review, parse its `trace-data`, and verify:

- `view.main_stages` contains the main flow;
- three reviewer sections exist;
- LLM and tool steps reference real raw event sequences;
- state writes resolve to node output patches;
- no payload is duplicated or truncated by the view model;
- JavaScript passes `node --check`.

- [ ] **Step 4: Absorb the prototype verdict**

Update the design document status and implementation notes, retain the finalized glossary in `CONTEXT.md`, then delete `_prototype_trace_flow/` because its answer has been absorbed into production.

- [ ] **Step 5: Review the branch**

Use `/code-review` against the merge base and resolve all blocking Standards or Spec findings.

- [ ] **Step 6: Commit documentation and cleanup**

```powershell
git add CONTEXT.md docs/superpowers/specs/2026-07-09-observability-trace-fidelity-fix-design.md services/agent/tests/test_observability.py
git add -u services/agent/src/codeguard_agent/observability/_prototype_trace_flow
git commit -m "docs(observability): 固化追踪可视化设计决策"
```

- [ ] **Step 7: Confirm clean scope**

Run:

```powershell
git status --short
git log -8 --oneline
```

Expected: only pre-existing `.superpowers/` remains untracked; implementation and design changes are committed.
