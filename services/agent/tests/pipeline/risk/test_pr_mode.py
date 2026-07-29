"""PR 体量分类器单测。"""

from __future__ import annotations

from codeguard_agent.models.tasks import ReviewBudget, ReviewMode
from codeguard_agent.pipeline.risk.task_prep import classify_diff, diff_metrics


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
    def test_metrics_match_the_values_used_for_routing(self):
        diff = _diff_text(files=2, hunks_per_file=2, chars=_SMALL_CHARS)
        assert diff_metrics(diff).model_dump() == {
            "file_count": 2,
            "hunk_count": 2,
            "diff_chars": len(diff),
        }

    def test_single_file_small_diff(self):
        diff = _diff_text(files=1, hunks_per_file=1, chars=_SMALL_CHARS)
        assert classify_diff(diff, _budget()) == ReviewMode.SMALL

    def test_three_files_at_boundary(self):
        diff = _diff_text(files=3, hunks_per_file=1, chars=_SMALL_CHARS)
        assert classify_diff(diff, _budget()) == ReviewMode.SMALL

    def test_four_files_exceeds_small(self):
        diff = _diff_text(files=4, hunks_per_file=1, chars=_SMALL_CHARS)
        assert classify_diff(diff, _budget()) == ReviewMode.MEDIUM

    def test_diff_chars_exceeds_small(self):
        # 9000 chars > small_max_diff_chars=8000
        diff = _diff_text(files=1, hunks_per_file=1, chars=9000)
        assert classify_diff(diff, _budget()) == ReviewMode.MEDIUM

    def test_exceeds_hunks(self):
        # 6 hunks > small_max_hunks=5
        diff = _diff_text(files=1, hunks_per_file=6, chars=_SMALL_CHARS)
        assert classify_diff(diff, _budget()) == ReviewMode.MEDIUM

    def test_custom_thresholds(self):
        diff = _diff_text(files=1, hunks_per_file=1, chars=_SMALL_CHARS)
        # 把 small 阈值降到 500 chars → 1000 chars 变成 medium
        assert classify_diff(diff, _budget(small_max_diff_chars=500)) == ReviewMode.MEDIUM


class TestMediumPR:
    def test_fifteen_files_at_boundary(self):
        diff = _diff_text(files=15, hunks_per_file=1, chars=_MEDIUM_CHARS)
        assert classify_diff(diff, _budget()) == ReviewMode.MEDIUM

    def test_sixteen_files_exceeds_medium(self):
        diff = _diff_text(files=16, hunks_per_file=1, chars=_MEDIUM_CHARS)
        assert classify_diff(diff, _budget()) == ReviewMode.LARGE

    def test_diff_chars_exceeds_medium(self):
        # 65000 chars > medium_max_diff_chars=60000
        diff = _diff_text(files=1, hunks_per_file=1, chars=65000)
        assert classify_diff(diff, _budget()) == ReviewMode.LARGE

    def test_many_files_within_medium_chars(self):
        # 10 files, small diff → medium (exceeds small files but within medium)
        diff = _diff_text(files=10, hunks_per_file=1, chars=_SMALL_CHARS)
        assert classify_diff(diff, _budget()) == ReviewMode.MEDIUM


class TestLargePR:
    def test_many_files(self):
        diff = _diff_text(files=20, hunks_per_file=1, chars=_MEDIUM_CHARS)
        assert classify_diff(diff, _budget()) == ReviewMode.LARGE

    def test_large_diff_chars(self):
        diff = _diff_text(files=1, hunks_per_file=1, chars=70000)
        assert classify_diff(diff, _budget()) == ReviewMode.LARGE

    def test_hunk_count_is_derived_from_diff_not_prebuilt_tasks(self):
        diff = _diff_text(files=3, hunks_per_file=1, chars=_SMALL_CHARS)
        assert classify_diff(diff, _budget()) == ReviewMode.SMALL

    def test_deleted_files_count_toward_pr_size(self):
        diff = "".join(
            f"diff --git a/F{i:02d}.java b/F{i:02d}.java\n"
            "deleted file mode 100644\n"
            f"--- a/F{i:02d}.java\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-class Removed {}\n"
            for i in range(16)
        )
        assert classify_diff(diff, _budget()) == ReviewMode.LARGE

    def test_binary_sections_count_toward_pr_size(self):
        diff = "".join(
            f"diff --git a/F{i:02d}.bin b/F{i:02d}.bin\n"
            f"Binary files a/F{i:02d}.bin and b/F{i:02d}.bin differ\n"
            for i in range(16)
        )
        assert classify_diff(diff, _budget()) == ReviewMode.LARGE
