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


def _norm(path: str) -> str:
    """整条路径归一化（正斜杠 + 小写），用于全路径精确匹配。"""
    return (path or "").replace("\\", "/").lower()


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]


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
        if (
            line.startswith("@@")
            or line.startswith("+++")
            or line.startswith("---")
            or line.startswith("\\ ")  # `\ No newline at end of file` 非文件行，不占行号
        ):
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

    文件匹配：全路径精确匹配优先（消解同 basename 不同目录的歧义，如
    src/Foo.java vs test/Foo.java）；无全路径命中再退化到 basename 匹配，容忍
    LLM 只给文件名的情况。文件不在任何任务中 → None（调用方应拒绝该候选并留 trace）。
    """
    exact = [t for t in tasks if _norm(t.file) == _norm(file)]
    file_tasks = exact or [t for t in tasks if _basename(t.file) == _basename(file)]
    if not file_tasks:
        return None
    for t in file_tasks:
        if line in t.changed_lines:
            return t.id
    return file_tasks[0].id
