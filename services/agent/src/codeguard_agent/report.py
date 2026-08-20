"""审查报告渲染(Markdown)与 diff 代码片段提取。

轻量报告:只美化 ReviewResult(severity 统计 + 按严重级分组的问题列表 +
代码片段),不含证据链;供 CLI `--report` 使用。GitHub App 模式的 CI 链路
(Gateway 调 `--format json`)不调用本模块,天然不生成报告。

均为确定性纯函数,无 IO/网络,可独立单测。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath

from codeguard_agent.git.diff_collector import split_diff_by_file
from codeguard_agent.models.schemas import Issue, ReviewResult, Severity

_SEVERITY_ICON = {
    Severity.CRITICAL: "🔴",
    Severity.WARNING: "🟡",
    Severity.INFO: "🔵",
}
_SECTION_ORDER = (Severity.CRITICAL, Severity.WARNING, Severity.INFO)

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def report_filename(timestamp: datetime) -> str:
    """报告文件名(Windows 友好时间戳,与 Trace Dashboard 同格式)。"""
    return f"review-report-{timestamp.strftime('%Y%m%d-%H%M%S')}.md"


def render_review_report(
    result: ReviewResult,
    *,
    repo: str,
    base: str,
    model: str,
    duration_s: float,
    diff_text: str,
) -> str:
    """把 ReviewResult 渲染为 Markdown 报告字符串。

    结构:标题 + 元信息 → 总览统计表 + 审查结论 → 按严重级分组的问题列表
    (CRITICAL→WARNING→INFO,组内保持原顺序,全局编号连续)。每条问题含
    问题/建议(可空省略)/代码片段(diff 可提取时)/置信度。
    """
    lines: list[str] = [
        "# 🔍 Codeguard 审查报告",
        "",
        f"> **仓库** `{repo}` · **基准** `{base}` · **模型** `{model}`"
        f" · **耗时** `{duration_s:.0f}s`",
        "",
        "---",
        "",
        "## 📊 总览",
        "",
        "| 🔴 CRITICAL | 🟡 WARNING | 🔵 INFO |",
        "| :---: | :---: | :---: |",
    ]
    counts = {severity: 0 for severity in _SECTION_ORDER}
    for issue in result.issues:
        counts[issue.severity] += 1
    lines.append(
        "| **{critical}** | **{warning}** | **{info}** |".format(
            critical=counts[Severity.CRITICAL],
            warning=counts[Severity.WARNING],
            info=counts[Severity.INFO],
        )
    )
    lines.append("")
    if result.summary:
        lines.append(f"**审查结论**:{result.summary}")
        lines.append("")

    if not result.issues:
        lines.append("✅ 未发现问题。")
        lines.append("")
        return "\n".join(lines)

    grouped: dict[Severity, list[Issue]] = {severity: [] for severity in _SECTION_ORDER}
    for issue in result.issues:
        grouped[issue.severity].append(issue)

    number = 0
    for severity in _SECTION_ORDER:
        if not grouped[severity]:
            continue
        lines.append(f"## {_SEVERITY_ICON[severity]} {severity.value}")
        lines.append("")
        for issue in grouped[severity]:
            number += 1
            location = f"{issue.file}:{issue.line}" if issue.line > 0 else issue.file
            lines.append(f"### {number}. {issue.type} · `{location}`")
            lines.append("")
            lines.append(f"**问题**:{issue.message}")
            lines.append("")
            if issue.suggestion:
                lines.append(f"**建议**:{issue.suggestion}")
                lines.append("")
            snippet = extract_code_snippet(diff_text, issue.file, issue.line)
            if snippet:
                lines.append("```java")
                lines.append(snippet)
                lines.append("```")
                lines.append("")
            lines.append(f"> 🎯 置信度 **{issue.confidence:.2f}**")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def extract_code_snippet(
    diff_text: str, file: str, line: int, *, window: int = 4
) -> str:
    """从 unified diff 提取 file:line 附近的代码片段(纯代码行,无 diff 前缀)。

    - line<=0 或 file 为空或行号不在任何 hunk 的新侧 → 返回空串;
    - 目标行是本次变更行(+)→ 追加 `// ← 第N行` 标记;是上下文行 → 标记 `(上下文)`;
    - 窗口越界用独立一行 `...` 省略;
    - 文件按 basename 匹配(兼容 LLM 报的部分路径),同名文件取第一段。
    """
    if line <= 0 or not file:
        return ""
    target_name = PurePosixPath(file).name
    section = ""
    for path, block in split_diff_by_file(diff_text).items():
        if PurePosixPath(path).name == target_name:
            section = block
            break
    if not section:
        return ""

    # 解析 hunk:跟踪新侧行号,按 hunk 分组收集 (新行号, 去前缀文本, 是否变更行)。
    hunks: list[list[tuple[int, str, bool]]] = []
    current: list[tuple[int, str, bool]] = []
    new_line = 0
    for raw in section.splitlines():
        if raw.startswith("@@"):
            current = []
            hunks.append(current)
            match = _HUNK_HEADER.match(raw)
            new_line = int(match.group(1)) if match else 0
            continue
        if new_line <= 0:
            continue
        prefix = raw[:1]
        if prefix == " ":
            current.append((new_line, raw[1:], False))
            new_line += 1
        elif prefix == "+":
            current.append((new_line, raw[1:], True))
            new_line += 1
        # '-':新侧无此行号,跳过;'\\' 等其余行忽略。

    # 窗口只在目标行所在 hunk 内裁剪——跨 hunk 的"上下文"在文件里相距甚远,无意义。
    target_hunk: list[tuple[int, str, bool]] = []
    index = -1
    for hunk in hunks:
        for idx, (ln, _text, _changed) in enumerate(hunk):
            if ln == line:
                target_hunk = hunk
                index = idx
                break
        if index >= 0:
            break
    if index < 0:
        return ""

    start = max(0, index - window)
    end = min(len(target_hunk), index + window + 1)
    rendered: list[str] = []
    if start > 0:
        rendered.append("...")
    for idx in range(start, end):
        ln, text, changed = target_hunk[idx]
        if idx == index:
            marker = f"  // ← 第{ln}行" if changed else f"  // ← 第{ln}行(上下文)"
            rendered.append(text + marker)
        else:
            rendered.append(text)
    if end < len(target_hunk):
        rendered.append("...")
    return "\n".join(rendered)
