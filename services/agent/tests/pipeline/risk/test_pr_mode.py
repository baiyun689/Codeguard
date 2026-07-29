"""PR 体量分类器单测。"""

from __future__ import annotations

from codeguard_agent.models.tasks import ReviewBudget, ReviewMode, ReviewTask
from codeguard_agent.pipeline.risk.task_prep import classify_pr_mode


_SMALL_CHARS = 1000
_MEDIUM_CHARS = 30000


def _diff_text(files: int, hunks_per_file: int = 1, chars: int = _SMALL_CHARS) -> str:
    """生成指定大小的合成 diff 文本。"""
    per_file = max(1, files)
    per_hunk = max(1, hunks_per_file * per_file)
    hunk_chars = max(1, chars // per_hunk)
    line = "x" * min(hunk_chars - 30, 80)  # 留空间给 diff header
    sections: list[str] = []
    for i in range(files):
        f = f"F{i:02d}.java"
        header = f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n"
        hunks = "\n".join(
            f"@@ -1,1 +1,{len(line) // 2 + 1} @@\n+{line}"
            for _ in range(max(1, hunks_per_file // max(1, files)))
        )
        sections.append(header + hunks)
    text = "\n".join(sections)
    # 填充或截断到目标长度
    if len(text) < chars:
        text += "\n" + " " * (chars - len(text))
    return text[:chars]


def _task(file: str, hunk: bool = True) -> ReviewTask:
    return ReviewTask(
        id=f"{file}#h0" if hunk else f"{file}#file",
        file=file,
        hunk_header="@@ -1,3 +1,5 @@" if hunk else "",
        patch="+x\n",
        changed_lines=[1],
    )


def _tasks(count: int, hunk: bool = True) -> list[ReviewTask]:
    return [_task(f"F{i:02d}.java", hunk=hunk) for i in range(count)]


def _budget(**overrides) -> ReviewBudget:
    kwargs = {
        "small_max_files": 3,
        "small_max_hunks": 5,
        "small_max_diff_chars": 8000,
        "medium_max_files": 15,
        "medium_max_diff_chars": 60000,
        **overrides,
    }
    return ReviewBudget(**kwargs)


class TestSmallPR:
    def test_single_file_small_diff(self):
        tasks = _tasks(1)
        diff = _diff_text(files=1, hunks_per_file=1, chars=_SMALL_CHARS)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.SMALL

    def test_three_files_at_boundary(self):
        tasks = _tasks(3)
        diff = _diff_text(files=3, hunks_per_file=1, chars=_SMALL_CHARS)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.SMALL

    def test_four_files_exceeds_small(self):
        tasks = _tasks(4)
        diff = _diff_text(files=4, hunks_per_file=1, chars=_SMALL_CHARS)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.MEDIUM

    def test_diff_chars_exceeds_small(self):
        # 9000 chars > small_max_diff_chars=8000
        tasks = _tasks(1)
        diff = _diff_text(files=1, hunks_per_file=1, chars=9000)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.MEDIUM

    def test_exceeds_hunks(self):
        # 6 hunks > small_max_hunks=5
        tasks = _tasks(6)
        diff = _diff_text(files=1, hunks_per_file=6, chars=_SMALL_CHARS)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.MEDIUM

    def test_custom_thresholds(self):
        tasks = _tasks(1)
        diff = _diff_text(files=1, hunks_per_file=1, chars=_SMALL_CHARS)
        # 把 small 阈值降到 500 chars → 1000 chars 变成 medium
        assert classify_pr_mode(diff, tasks, _budget(small_max_diff_chars=500)) == ReviewMode.MEDIUM


class TestMediumPR:
    def test_fifteen_files_at_boundary(self):
        tasks = _tasks(15)
        diff = _diff_text(files=15, hunks_per_file=1, chars=_MEDIUM_CHARS)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.MEDIUM

    def test_sixteen_files_exceeds_medium(self):
        tasks = _tasks(16)
        diff = _diff_text(files=16, hunks_per_file=1, chars=_MEDIUM_CHARS)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.LARGE

    def test_diff_chars_exceeds_medium(self):
        # 65000 chars > medium_max_diff_chars=60000
        tasks = _tasks(1)
        diff = _diff_text(files=1, hunks_per_file=1, chars=65000)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.LARGE

    def test_many_files_within_medium_chars(self):
        # 10 files, small diff → medium (exceeds small files but within medium)
        tasks = _tasks(10)
        diff = _diff_text(files=10, hunks_per_file=1, chars=_SMALL_CHARS)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.MEDIUM


class TestLargePR:
    def test_many_files(self):
        tasks = _tasks(20)
        diff = _diff_text(files=20, hunks_per_file=1, chars=_MEDIUM_CHARS)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.LARGE

    def test_large_diff_chars(self):
        tasks = _tasks(1)
        diff = _diff_text(files=1, hunks_per_file=1, chars=70000)
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.LARGE

    def test_file_level_fallback_tasks_not_counted_as_hunks(self):
        # 文件级 fallback task（无 hunk_header）不算入 hunk_count
        tasks = [
            _task("Deleted.java", hunk=False),
            _task("F00.java", hunk=True),
            _task("F01.java", hunk=True),
        ]
        diff = _diff_text(files=3, hunks_per_file=1, chars=_SMALL_CHARS)
        # 2 files with hunk, 2 hunks, small chars → small
        assert classify_pr_mode(diff, tasks, _budget()) == ReviewMode.SMALL
