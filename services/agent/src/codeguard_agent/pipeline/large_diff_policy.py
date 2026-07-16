"""大 diff 的确定性审查范围策略。只控制成本与覆盖范围，不判断代码问题。"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from codeguard_agent.models.tasks import ReviewBudget, ReviewTask, TaskSelection

LARGE_DIFF_LINE_THRESHOLD = 5000
LARGE_DIFF_TASK_THRESHOLD = 50
LARGE_MAX_TASKS = 20
LARGE_MAX_TASKS_PER_FILE = 3
LARGE_MAX_CONTEXT_CHARS = 2000
LARGE_SELECTED_DIFF_CHARS = 60_000
LARGE_TASK_PATCH_CHARS = 12_000


def _bounded(configured: int | None, ceiling: int) -> int:
    return ceiling if configured is None else min(configured, ceiling)


@dataclass(frozen=True)
class LargeDiffPlan:
    """一次审查的不可变范围决策；各图节点可从原始 State 重复确定性派生。"""

    active: bool
    total_lines: int
    total_tasks: int
    effective_budget: ReviewBudget

    def selected_diff(
        self,
        tasks: list[ReviewTask],
        selection: TaskSelection,
    ) -> str:
        """重建只包含选中 task 的最小 unified diff，并在大 diff 模式下限制字符数。"""
        selected_ids = set(selection.selected_task_ids)
        grouped: OrderedDict[str, list[ReviewTask]] = OrderedDict()
        for task in tasks:
            if task.id in selected_ids:
                grouped.setdefault(task.file, []).append(task)

        sections: list[str] = []
        for file, file_tasks in grouped.items():
            full_sections = [
                task.patch for task in file_tasks if task.patch.startswith("diff --git ")
            ]
            if full_sections:
                sections.extend(full_sections)
                continue
            hunks = "\n".join(task.patch for task in file_tasks)
            sections.append(
                f"diff --git a/{file} b/{file}\n"
                f"--- a/{file}\n"
                f"+++ b/{file}\n"
                f"{hunks}"
            )

        rendered = "\n".join(sections)
        if not self.active or len(rendered) <= LARGE_SELECTED_DIFF_CHARS:
            return rendered
        marker = "\n...(大 diff 选中范围已截断)"
        return rendered[: LARGE_SELECTED_DIFF_CHARS - len(marker)] + marker

    def scoped_patch(self, patch: str) -> str:
        if not self.active or len(patch) <= LARGE_TASK_PATCH_CHARS:
            return patch
        marker = "...(大 diff 单任务 patch 已截断)"
        return patch[: LARGE_TASK_PATCH_CHARS - len(marker)] + marker

    def coverage_notice(self, selection: TaskSelection) -> str:
        if not self.active:
            return ""
        selected = len(selection.selected_task_ids)
        skipped = max(0, self.total_tasks - selected)
        return (
            f"大变更降级审查：共 {self.total_tasks} 个任务，本次按风险审查 {selected} 个，"
            f"跳过 {skipped} 个；结果不代表完整覆盖，建议拆分 PR。"
        )


def plan_large_diff(
    diff_text: str,
    tasks: list[ReviewTask],
    configured_budget: ReviewBudget,
) -> LargeDiffPlan:
    total_lines = 0 if not diff_text else diff_text.count("\n") + 1
    active = total_lines > LARGE_DIFF_LINE_THRESHOLD or len(tasks) > LARGE_DIFF_TASK_THRESHOLD
    if not active:
        return LargeDiffPlan(False, total_lines, len(tasks), configured_budget)

    return LargeDiffPlan(
        active=True,
        total_lines=total_lines,
        total_tasks=len(tasks),
        effective_budget=ReviewBudget(
            max_tasks_to_review=_bounded(configured_budget.max_tasks_to_review, LARGE_MAX_TASKS),
            max_tasks_per_file=_bounded(
                configured_budget.max_tasks_per_file, LARGE_MAX_TASKS_PER_FILE
            ),
            max_context_chars_per_task=_bounded(
                configured_budget.max_context_chars_per_task, LARGE_MAX_CONTEXT_CHARS
            ),
            max_final_issues=configured_budget.max_final_issues,
        ),
    )
