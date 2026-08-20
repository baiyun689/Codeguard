"""审查报告渲染(render_review_report)与 diff 代码片段提取(extract_code_snippet)的工程正确性测试。

报告是轻量美化版 ReviewResult(无证据链),供 CLI `--report` 使用;
CI(GitHub App)链路不调用本模块。均为确定性纯函数,适合 pytest 死磕。
"""

from __future__ import annotations

from datetime import datetime

from codeguard_agent.models.schemas import Issue, ReviewResult, Severity
from codeguard_agent.report import (
    extract_code_snippet,
    render_review_report,
    report_filename,
)


def _mk_issue(
    severity: Severity,
    type_: str,
    file: str,
    line: int,
    message: str = "问题描述",
    suggestion: str = "修复建议",
    confidence: float = 0.9,
) -> Issue:
    return Issue(
        severity=severity,
        file=file,
        line=line,
        type=type_,
        message=message,
        suggestion=suggestion,
        confidence=confidence,
    )


def _render(result: ReviewResult, *, diff_text: str = "") -> str:
    return render_review_report(
        result,
        repo="my-repo",
        base="HEAD",
        model="deepseek-v4-flash",
        duration_s=152.3,
        diff_text=diff_text,
    )


# ── 渲染 ────────────────────────────────────────────────────────────────


def test_空结果_总览全零且提示未发现问题():
    text = _render(ReviewResult(summary="", issues=[]))
    assert "| **0** | **0** | **0** |" in text
    assert "✅ 未发现问题。" in text
    assert "## 🔴 CRITICAL" not in text


def test_按严重级分组_全局编号连续_空组不渲染():
    result = ReviewResult(
        summary="有高危问题",
        issues=[
            _mk_issue(Severity.INFO, "命名", "Shell.java", 175),
            _mk_issue(Severity.CRITICAL, "注入", "Commandline.java", 518),
            _mk_issue(Severity.WARNING, "泄漏", "Utils.java", 216),
            _mk_issue(Severity.CRITICAL, "空指针", "Shell.java", 127),
        ],
    )
    text = _render(result)
    assert text.index("## 🔴 CRITICAL") < text.index("## 🟡 WARNING") < text.index("## 🔵 INFO")
    assert "### 1. 注入 · `Commandline.java:518`" in text
    assert "### 2. 空指针 · `Shell.java:127`" in text   # 组内保持原顺序
    assert "### 3. 泄漏 · `Utils.java:216`" in text      # 全局编号连续
    assert "### 4. 命名 · `Shell.java:175`" in text


def test_元信息与结论_耗时取整秒():
    result = ReviewResult(summary="建议修复后合并", issues=[])
    text = _render(result)
    assert "**仓库** `my-repo`" in text
    assert "**基准** `HEAD`" in text
    assert "**模型** `deepseek-v4-flash`" in text
    assert "**耗时** `152s`" in text
    assert "**审查结论**:建议修复后合并" in text


def test_建议为空时省略建议行():
    issue = _mk_issue(Severity.WARNING, "泄漏", "Utils.java", 216, suggestion="")
    text = _render(ReviewResult(summary="", issues=[issue]))
    assert "**问题**:问题描述" in text
    assert "**建议**" not in text


def test_行号为零时位置只显示文件():
    issue = _mk_issue(Severity.INFO, "命名", "Shell.java", 0)
    text = _render(ReviewResult(summary="", issues=[issue]))
    assert "### 1. 命名 · `Shell.java`\n" in text
    assert "`Shell.java:0`" not in text


def test_置信度保留两位小数():
    issue = _mk_issue(Severity.WARNING, "泄漏", "Utils.java", 216, confidence=0.9)
    text = _render(ReviewResult(summary="", issues=[issue]))
    assert "> 🎯 置信度 **0.90**" in text


def test_渲染_代码片段嵌入java代码块():
    diff = (
        "diff --git a/src/Commandline.java b/src/Commandline.java\n"
        "--- a/src/Commandline.java\n"
        "+++ b/src/Commandline.java\n"
        "@@ -1,6 +1,7 @@\n"
        " lineA1\n"
        " lineA2\n"
        " lineA3\n"
        " lineA4\n"
        "+exec(cmd);\n"
        " lineA5\n"
        " lineA6\n"
        " lineA7\n"
    )
    issue = _mk_issue(Severity.CRITICAL, "注入", "Commandline.java", 5)
    text = _render(ReviewResult(summary="", issues=[issue]), diff_text=diff)
    assert "```java\n" in text
    assert "exec(cmd);  // ← 第5行" in text


# ── 片段提取 ────────────────────────────────────────────────────────────

_DIFF = (
    "diff --git a/src/Commandline.java b/src/Commandline.java\n"
    "--- a/src/Commandline.java\n"
    "+++ b/src/Commandline.java\n"
    "@@ -1,6 +1,7 @@\n"
    " lineA1\n"
    " lineA2\n"
    " lineA3\n"
    " lineA4\n"
    "+exec(cmd);\n"
    " lineA5\n"
    " lineA6\n"
    " lineA7\n"
)


def test_片段_命中变更行_去diff前缀并标记行号():
    snippet = extract_code_snippet(_DIFF, "Commandline.java", 5)
    assert "+exec(cmd);" not in snippet.splitlines()
    assert "exec(cmd);  // ← 第5行" in snippet
    assert snippet.startswith("lineA1")          # 窗口内上下文原样保留


def test_片段_行号不在diff返回空串():
    assert extract_code_snippet(_DIFF, "Commandline.java", 999) == ""


def test_片段_行号为零返回空串():
    assert extract_code_snippet(_DIFF, "Commandline.java", 0) == ""


def test_片段_命中上下文行_标记上下文():
    snippet = extract_code_snippet(_DIFF, "Commandline.java", 1)
    assert "lineA1  // ← 第1行(上下文)" in snippet


def test_片段_多hunk命中正确hunk():
    diff = (
        "diff --git a/src/Commandline.java b/src/Commandline.java\n"
        "--- a/src/Commandline.java\n"
        "+++ b/src/Commandline.java\n"
        "@@ -1,2 +1,2 @@\n"
        " hunkOneA\n"
        " hunkOneB\n"
        "@@ -10,2 +10,3 @@\n"
        " hunkTwoA\n"
        " hunkTwoB\n"
        "+hunkTwoNew\n"
    )
    snippet = extract_code_snippet(diff, "Commandline.java", 12)
    assert "hunkTwoNew  // ← 第12行" in snippet
    assert "hunkOneA" not in snippet


def test_片段_窗口截断加省略号():
    diff = (
        "diff --git a/src/Commandline.java b/src/Commandline.java\n"
        "--- a/src/Commandline.java\n"
        "+++ b/src/Commandline.java\n"
        "@@ -1,10 +1,10 @@\n"
        + "".join(f" line{i:02d}\n" for i in range(1, 11))
    )
    snippet = extract_code_snippet(diff, "Commandline.java", 5, window=2)
    lines = snippet.splitlines()
    assert lines[0] == "..."
    assert lines[-1] == "..."
    assert "line05  // ← 第5行(上下文)" in snippet
    assert "line01" not in snippet                # 窗口外行被截掉
    assert "line10" not in snippet


def test_片段_按basename匹配文件路径():
    # LLM 报的文件路径可能是完整路径,而 diff 里是仓库相对路径——basename 匹配兜住。
    snippet = extract_code_snippet(_DIFF, "src/main/java/Commandline.java", 5)
    assert "exec(cmd);  // ← 第5行" in snippet


def test_片段_文件不在diff返回空串():
    assert extract_code_snippet(_DIFF, "Other.java", 5) == ""


# ── 文件名 ──────────────────────────────────────────────────────────────


def test_报告文件名_时间戳格式():
    assert (
        report_filename(datetime(2026, 8, 20, 15, 30, 45))
        == "review-report-20260820-153045.md"
    )
