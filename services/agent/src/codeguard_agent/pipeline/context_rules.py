"""阶段3：RiskTag → 上下文策略映射 + 调用计划（纯函数，不碰 tool_client）。

Level0 的既有事实切片和每任务预算由后续任务实现；本模块当前只规划按 RiskTag
定向调用的 Level1 工具。GENERAL_REVIEW 未注册策略，因此天然不触发 Level1。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag
from codeguard_agent.pipeline.task_prep import _hunk_span


class ContextLevel(str, Enum):
    """Level1 定向调用类型；值即 Java 工具名。"""

    FIND_CALLERS = "find_callers"
    CODE_METRICS = "get_code_metrics"


@dataclass(frozen=True)
class ContextStrategy:
    """一个 RiskTag 对应的 Level1 调用策略。"""

    level: ContextLevel


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
    """去重后的一次 Level1 调用计划。"""

    level: ContextLevel
    key: str
    task_ids: tuple[str, ...]


@dataclass(frozen=True)
class TaskSkip:
    """无法生成 Level1 调用的任务及原因。"""

    task_id: str
    level: ContextLevel
    reason: str


@dataclass(frozen=True)
class ContextPlan:
    """任务的去重 Level1 调用计划和跳过记录。"""

    level1_calls: tuple[Level1Call, ...]
    skips: tuple[TaskSkip, ...]


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
    """解析 task 覆盖范围所属的方法；解析不到不从 patch 猜测。"""
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
    """按任务风险画像生成去重后的 Level1 调用计划，不执行调用。"""
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
                    skips.append(TaskSkip(task.id, level, "no_method_resolved"))
                    continue
                key = f"{task.file}#{method}"
            by_key.setdefault((level, key), []).append(task.id)

    calls = tuple(
        Level1Call(level, key, tuple(task_ids))
        for (level, key), task_ids in by_key.items()
    )
    return ContextPlan(level1_calls=calls, skips=tuple(skips))
