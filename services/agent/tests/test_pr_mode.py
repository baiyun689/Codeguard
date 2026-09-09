"""两档 PR 粒度的边界、覆盖与真实图路由回归。"""

import pytest

from codeguard_agent.models.tasks import ReviewBudget, ReviewMode, ReviewRouteThresholds
from codeguard_agent.pipeline.tasks.task_builder import classify_diff, diff_metrics
from codeguard_agent.pipeline.orchestration.graph import _classify_mode_node


def _diff(files=1, hunks=1, chars=None):
    text = "".join(
        f"diff --git a/F{i}.java b/F{i}.java\n--- a/F{i}.java\n+++ b/F{i}.java\n"
        + "".join(
            f"@@ -{j + 1},1 +{j + 1},1 @@\n-old();\n+newCall();\n" for j in range(hunks)
        )
        for i in range(files)
    )
    if chars is not None:
        assert chars >= len(text)
        text += " " * (chars - len(text))
    return text


@pytest.mark.parametrize(
    "files,chars,expected",
    [
        (1, 1000, ReviewMode.NORMAL),
        (4, 9000, ReviewMode.NORMAL),
        (15, 60000, ReviewMode.NORMAL),
        (16, 60000, ReviewMode.LARGE),
        (1, 60001, ReviewMode.LARGE),
    ],
)
def test_default_boundaries(files, chars, expected):
    diff = _diff(files=files, chars=chars)
    state = _classify_mode_node()({"diff_text": diff})
    assert classify_diff(diff, ReviewBudget()) == expected
    assert state["review_mode"] == expected.value
    route = state["review_route"]
    assert route.initial_mode == route.effective_mode == expected
    assert route.selected_node == (
        "file_task_builder" if expected is ReviewMode.NORMAL else "diff_task_builder"
    )
    assert route.metrics.diff_chars == chars
    assert route.thresholds.model_dump() == {
        "normal_max_files": 15,
        "normal_max_diff_chars": 60000,
    }


def test_many_hunks_do_not_create_a_third_tier():
    diff = _diff(files=1, hunks=30)
    assert diff_metrics(diff).hunk_count == 30
    assert classify_diff(diff, ReviewBudget()) is ReviewMode.NORMAL


def test_custom_thresholds_drive_both_classification_and_trace():
    budget = ReviewBudget(normal_max_files=2, normal_max_diff_chars=1000)
    for diff in (_diff(files=3), _diff(chars=1001)):
        state = _classify_mode_node()({"diff_text": diff, "review_budget": budget})
        assert state["review_mode"] == "large"
        assert state["review_route"].thresholds.normal_max_files == 2
        assert state["review_route"].thresholds.normal_max_diff_chars == 1000


@pytest.mark.parametrize("binary", [False, True])
def test_deleted_and_binary_files_count_toward_size(binary):
    diff = "".join(
        f"diff --git a/F{i}.java b/F{i}.java\n"
        + (
            f"Binary files a/F{i}.java and b/F{i}.java differ\n"
            if binary
            else f"deleted file mode 100644\n--- a/F{i}.java\n+++ /dev/null\n@@ -1 +0,0 @@\n-class Removed {{}}\n"
        )
        for i in range(16)
    )
    assert diff_metrics(diff).file_count == 16
    assert classify_diff(diff, ReviewBudget()) is ReviewMode.LARGE


def test_empty_diff_and_two_mode_contract():
    assert classify_diff("", ReviewBudget()) is ReviewMode.NORMAL
    assert set(ReviewMode) == {ReviewMode.NORMAL, ReviewMode.LARGE}
    assert set(ReviewRouteThresholds.model_fields) == {
        "normal_max_files",
        "normal_max_diff_chars",
    }
    assert not any(
        key.startswith(("small_", "medium_")) for key in ReviewBudget.model_fields
    )
