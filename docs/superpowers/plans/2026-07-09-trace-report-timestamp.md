# Trace Report Timestamp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Trace HTML 文件名和页面头部展示同一个本地生成时间。

**Architecture:** 复用 `TraceReport.timestamp`，由 Dashboard 文件写入函数将 ISO 时间格式化为文件名片段；模板直接读取嵌入数据中的 timestamp。缺失或非法时间仅在文件名处回退当前本地时间。

**Tech Stack:** Python 3.11+、pytest、纯 HTML/JavaScript

## Global Constraints

- 文件名格式为 `trace-YYYYMMDD-HHMMSS-<run-id前8位>.html`。
- 页面显示 `TraceReport.timestamp` 原值。
- 不改变 Trace 数据结构与输出目录。

---

### Task 1: 报告时间戳

**Files:**
- Modify: `services/agent/src/codeguard_agent/observability/dashboard.py`
- Modify: `services/agent/src/codeguard_agent/observability/dashboard_template.html`
- Modify: `services/agent/tests/test_observability.py`

**Interfaces:**
- Consumes: `TraceReport.timestamp`
- Produces: `_filename_timestamp(timestamp: str) -> str`
- Produces: 带生成时间指标的静态 HTML

- [x] **Step 1: Write failing tests**

```python
def test_render_dashboard_file_includes_timestamp(tmp_path):
    report = TraceReport(
        run_id="abc12345",
        timestamp="2026-07-09T20:30:45",
    )
    path = render_dashboard_file(report, str(tmp_path), report.run_id)
    assert path.name == "trace-20260709-203045-abc12345.html"


def test_template_displays_report_timestamp():
    assert "生成时间" in _dashboard_template()
    assert "DATA.timestamp" in _dashboard_template()
```

- [x] **Step 2: Verify RED**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_observability.py -q
```

Expected: filename and template assertions fail.

- [x] **Step 3: Implement**

```python
def _filename_timestamp(timestamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        parsed = datetime.now()
    return parsed.strftime("%Y%m%d-%H%M%S")
```

Use it in `render_dashboard_file` and add a header metric using `DATA.timestamp || "unknown"`.

- [x] **Step 4: Verify**

Run focused tests, full tests, ruff, mypy, and `node --check` on the inline JavaScript.

- [x] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/observability/dashboard.py services/agent/src/codeguard_agent/observability/dashboard_template.html services/agent/tests/test_observability.py docs/superpowers/plans/2026-07-09-trace-report-timestamp.md
git commit -m "feat(observability): 为追踪报告增加生成时间"
```
