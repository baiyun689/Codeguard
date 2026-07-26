"""当前任务的上下文事实筛选与预算辅助函数。"""

from __future__ import annotations

import re

from codeguard_agent.models.council import ContextFact
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.risk.task_prep import _hunk_span


def normalize_path(path: str) -> str:
    """返回用于跨工具事实匹配的规范化路径。"""
    return (path or "").replace("\\", "/").lower()


_METHOD_LINE_SUFFIX = re.compile(r"\[L(\d+)-L(\d+)\]\s*$")
_METHOD_NAME_BEFORE_PARENS = re.compile(r"(\w+)\([^)]*\)\s*$")


def _parse_method_ranges(ast_block: str) -> list[tuple[str, int, int]]:
    """解析 AST 格式化输出中的方法名和行范围。"""
    methods: list[tuple[str, int, int]] = []
    in_method_section = True
    for line in ast_block.splitlines():
        stripped = line.rstrip()
        if stripped in ("  Control flow:", "  Call edges:"):
            in_method_section = False
            continue
        if not in_method_section or not re.match(r"^ {4}\S", line):
            continue
        range_match = _METHOD_LINE_SUFFIX.search(stripped)
        if not range_match:
            continue
        prefix = stripped[: range_match.start()].rstrip()
        name_match = _METHOD_NAME_BEFORE_PARENS.search(prefix)
        if name_match:
            methods.append(
                (name_match.group(1), int(range_match.group(1)), int(range_match.group(2)))
            )
    return methods


def _task_span(task: ReviewTask) -> tuple[int, int] | None:
    span = _hunk_span(task)
    if span is not None:
        return span
    if task.changed_lines:
        return min(task.changed_lines), max(task.changed_lines)
    return None


def resolve_method_name(ast_block: str, task: ReviewTask) -> str | None:
    """兼容旧 AST 事实：解析 task 覆盖范围所属的方法。"""
    span = _task_span(task)
    if span is None:
        return None
    for name, start, end in _parse_method_ranges(ast_block):
        if start <= span[1] and end >= span[0]:
            return name
    return None


_SENSITIVE_ROW = re.compile(r"^\|[^|]*\|[^|]*\|\s*([^:|]+):(\d+)\s*\|")


def sensitive_api_rows_for_task(sensitive_api_text: str, task: ReviewTask) -> list[str]:
    """筛选全局敏感 API 扫描中属于 task 文件和范围的 Gateway Markdown 行。"""
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


def truncate_task_facts(
    facts: list[ContextFact], max_chars: int | None
) -> tuple[list[ContextFact], bool]:
    """按每任务字符预算截断 facts 列表。max_chars 为 None 表示不限制。"""
    if max_chars is None:
        return facts, False

    kept: list[ContextFact] = []
    used = 0
    for fact in facts:
        remaining = max_chars - used
        if remaining <= 0:
            return kept, True
        if len(fact.content) > remaining:
            kept.append(
                ContextFact(
                    source=fact.source,
                    kind=fact.kind,
                    content=fact.content[:remaining] + "...(已截断)",
                    truncated=True,
                )
            )
            return kept, True
        kept.append(fact)
        used += len(fact.content)
    return kept, False
