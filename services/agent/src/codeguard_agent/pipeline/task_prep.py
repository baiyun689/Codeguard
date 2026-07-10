"""任务准备纯函数（Phase 1 薄实现）。

职责边界（spec §4.2/§4.3/§4.4）：
- build_tasks：解析 unified diff → 每 hunk 一个 ReviewTask；无 hunk（含删除文件、
  纯重命名）退化为文件级 fallback task。不判断风险、不读仓库文件、不调 LLM。
- triage_tasks：Phase 1 为每个任务产出空 RiskProfile（规则留到 Phase 2）。
- rank_tasks：Phase 1 默认全选（预算生效留到 Phase 2）。
- map_candidate_to_task：候选(file, line) → task_id 的确定性映射。必须能绑定到具体
  changed 区域（命中 changed line、落在 hunk 覆盖范围、或该文件的明确文件级 fallback），
  否则返回 None（spec §3.2「无法映射的候选不得进入共享黑板」/§4.9「无法绑定 changed
  line → drop」）。绝不把无法绑定的候选硬塞给"第一个" task。
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
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _norm(path: str) -> str:
    """整条路径归一化（正斜杠 + 小写），用于全路径精确匹配。"""
    return (path or "").replace("\\", "/").lower()


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]


def _iter_diff_blocks(diff_text: str) -> list[list[str]]:
    """按 `diff --git ` 边界把 diff 切成块（每块是行列表）。"""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def _old_path(block: list[str]) -> str | None:
    """从 diff 块提取旧文件路径：优先 `--- a/<path>`，退化到 `diff --git a/<path> b/`。"""
    for line in block:
        if line.startswith("--- a/"):
            return line[len("--- a/"):].split("\t", 1)[0].strip()
    m = re.match(r"^diff --git a/(.+?) b/", block[0]) if block else None
    return m.group(1).strip() if m else None


def _fallback_targets(diff_text: str) -> dict[str, str]:
    """扫描 split_diff_by_file 会漏掉的块，返回需要文件级 fallback 的 {path: section}。

    覆盖两类 split_diff_by_file 刻意跳过（无 `+++ b/`）的变更：
    - 删除文件（`+++ /dev/null` / `deleted file mode`）→ 取旧路径。
    - 纯重命名（有 `rename to` 且无 `+++ b/`，即无内容变更）→ 取新路径。
    删除鉴权/校验/事务代码时，reviewer 仍能把候选绑定到该文件（spec §4.2）。
    """
    targets: dict[str, str] = {}
    for block in _iter_diff_blocks(diff_text):
        is_deletion = any(
            line == "+++ /dev/null" or line.startswith("deleted file mode")
            for line in block
        )
        has_plus_header = any(line.startswith("+++ b/") for line in block)
        rename_to = next(
            (line[len("rename to "):].strip() for line in block if line.startswith("rename to ")),
            None,
        )
        if is_deletion:
            path = _old_path(block)
            if path:
                targets[path] = "\n".join(block)
        elif rename_to and not has_plus_header:
            targets[rename_to] = "\n".join(block)
    return targets


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


def _hunk_span(task: ReviewTask) -> tuple[int, int] | None:
    """从 task.hunk_header 解析该 hunk 覆盖的新文件行范围 [start, end]。

    无 hunk_header（文件级 fallback task）返回 None。
    """
    m = _HUNK_HEADER.match(task.hunk_header or "")
    if not m:
        return None
    start = int(m.group(1))
    length = int(m.group(2)) if m.group(2) else 1
    return (start, start + max(length, 1) - 1)


def build_tasks(diff_text: str) -> list[ReviewTask]:
    """解析 unified diff → ReviewTask 列表。

    - 有内容变更的文件：每 hunk 一个 task；无 hunk 时退化为该文件的文件级 fallback。
    - 删除文件 / 纯重命名：split_diff_by_file 会漏掉，补一个文件级 fallback task。
    """
    tasks: list[ReviewTask] = []
    seen_files: set[str] = set()
    for file, section in split_diff_by_file(diff_text).items():
        seen_files.add(file)
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
    for path, section in _fallback_targets(diff_text).items():
        if path in seen_files:
            continue
        tasks.append(
            ReviewTask(id=f"{path}#file", file=path, patch=section, changed_lines=[])
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
    """候选(file, line) → task_id。无法绑定到具体 changed 区域时返回 None。

    文件匹配：全路径精确匹配优先（消解同 basename 不同目录的歧义，如
    src/Foo.java vs test/Foo.java）；无全路径命中再退化到 basename 匹配。

    行绑定（按精确度递减）：
      1. 命中某 hunk 的 changed_lines → 该 hunk 的 task。
      2. 落在某 hunk 覆盖的新文件行范围内（含上下文行）→ 该 hunk 的 task。
      3. 该文件存在文件级 fallback task（删除/纯重命名/无 hunk 文件，无行信息）→ 它。
      4. 以上都不满足（行落在所有 hunk 之外）→ None，交由调用方拒绝并留 trace。

    绝不把无法绑定的候选归属到"第一个"task——那会让风险/上下文/证据错挂到无关任务。
    """
    exact = [t for t in tasks if _norm(t.file) == _norm(file)]
    file_tasks = exact or [t for t in tasks if _basename(t.file) == _basename(file)]
    if not file_tasks:
        return None
    for t in file_tasks:
        if line in t.changed_lines:
            return t.id
    for t in file_tasks:
        span = _hunk_span(t)
        if span is not None and span[0] <= line <= span[1]:
            return t.id
    fallback = next((t for t in file_tasks if t.id.endswith("#file")), None)
    if fallback is not None:
        return fallback.id
    return None
