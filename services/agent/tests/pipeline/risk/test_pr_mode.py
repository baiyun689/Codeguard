"""PR 体量分类器单测。"""

from __future__ import annotations

import pytest

from codeguard_agent.models.tasks import ReviewBudget, ReviewMode, ReviewTask
from codeguard_agent.pipeline.risk.task_prep import classify_pr_mode


def _task(file: str, changed_lines: list[int], hunk: bool = True) -> ReviewTask:
    return ReviewTask(
        id=f"{file}#h0" if hunk else f"{file}#file",
        file=file,
        hunk_header="@@ -1,3 +1,5 @@" if hunk else "",
        patch="+added_line\n context\n",
        changed_lines=changed_lines,
    )


def _budget(**overrides) -> ReviewBudget:
    kwargs = {
        "small_max_files": 3,
        "small_max_changed_lines": 200,
        "small_max_hunks": 5,
        "medium_max_files": 15,
        "medium_max_changed_lines": 2000,
        "medium_file_changed_lines_fallback": 500,
        **overrides,
    }
    return ReviewBudget(**kwargs)


class TestSmallPR:
    def test_single_file_few_lines(self):
        tasks = [_task("A.java", [10, 11])]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.SMALL

    def test_three_files_at_boundary(self):
        tasks = [
            _task("A.java", [1]),
            _task("B.java", [2]),
            _task("C.java", [3]),
        ]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.SMALL

    def test_four_files_exceeds_small(self):
        tasks = [_task(f"{c}.java", [1]) for c in "ABCD"]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.MEDIUM

    def test_exceeds_changed_lines(self):
        # 201 changed lines > small_max_changed_lines=200
        tasks = [_task("A.java", list(range(201)))]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.MEDIUM

    def test_exceeds_hunks(self):
        # 6 hunks > small_max_hunks=5
        tasks = [_task("A.java", [i], hunk=True) for i in range(6)]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.MEDIUM

    def test_custom_thresholds(self):
        tasks = [_task("A.java", list(range(10)))]
        # 把 small 阈值降到 5 行 → 10 行变成 medium
        assert classify_pr_mode(tasks, _budget(small_max_changed_lines=5)) == ReviewMode.MEDIUM


class TestMediumPR:
    def test_fifteen_files_at_boundary(self):
        tasks = [_task(f"{i}.java", [1]) for i in range(15)]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.MEDIUM

    def test_sixteen_files_exceeds_medium(self):
        tasks = [_task(f"{i}.java", [1]) for i in range(16)]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.LARGE

    def test_exceeds_medium_lines(self):
        tasks = [_task("A.java", list(range(2001)))]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.LARGE

    def test_many_files_few_lines_each(self):
        # 10 files, 10 lines each = 100 lines total → medium
        tasks = [_task(f"{i}.java", list(range(10 * i, 10 * (i + 1)))) for i in range(10)]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.MEDIUM


class TestLargePR:
    def test_many_files(self):
        tasks = [_task(f"{i}.java", [1]) for i in range(20)]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.LARGE

    def test_many_lines(self):
        tasks = [_task("A.java", list(range(3000)))]
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.LARGE

    def test_file_level_fallback_tasks_count_correctly(self):
        # 文件级 fallback task（无 hunk_header）不算入 hunk_count
        tasks = [
            _task("Deleted.java", [], hunk=False),  # 删除文件 fallback
            _task("A.java", [1, 2], hunk=True),
            _task("B.java", [3], hunk=True),
        ]
        # 2 files, 3 lines, 2 hunks → small
        assert classify_pr_mode(tasks, _budget()) == ReviewMode.SMALL
