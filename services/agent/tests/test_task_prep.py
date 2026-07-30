"""Task construction, triage and prior-based ranking contracts."""

from codeguard_agent.models.tasks import (
    ReviewBudget,
    ReviewTask,
    RiskCoverage,
    RiskHypothesis,
    RiskTag,
    TaskRiskPrior,
)
from codeguard_agent.pipeline.risk.task_prep import (
    _changed_lines,
    _is_production_path,
    build_tasks,
    file_matches_task,
    is_noise_issue,
    rank_tasks,
    triage_tasks,
)

_TWO_HUNK_DIFF = (
    "diff --git a/A.java b/A.java\n"
    "--- a/A.java\n"
    "+++ b/A.java\n"
    "@@ -1,2 +1,3 @@ class A\n"
    " int a=0;\n"
    "+int b=1;\n"
    " int c=2;\n"
    "@@ -10,1 +11,2 @@ void f()\n"
    " call();\n"
    "+guard();\n"
)


def _task(task_id: str, file: str) -> ReviewTask:
    return ReviewTask(id=task_id, file=file, patch="+changed")


def _prior(
    task_id: str,
    tag: RiskTag,
    priority: int = 1,
    *,
    deleted: bool = False,
) -> TaskRiskPrior:
    return TaskRiskPrior(
        task_id=task_id,
        coverage=RiskCoverage.CONFIDENT,
        hypotheses=(
            RiskHypothesis(
                tag=tag,
                match_confidence={1: 0.45, 2: 0.70, 3: 0.85}[priority],
                review_priority=priority,
                source_kind="diff_text",
                source=(
                    "text:deleted:test" if deleted else "text:added:test"
                ),
                reason="test",
            ),
        ),
    )


def test_build_tasks_one_task_per_hunk_and_records_lines():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    assert [task.id for task in tasks] == ["A.java#h0", "A.java#h1"]
    assert [task.changed_lines for task in tasks] == [[2], [12]]


def test_build_tasks_falls_back_to_file_level_when_no_hunk():
    diff = (
        "diff --git a/B.java b/B.java\n"
        "rename from B.java\nrename to B.java\n+++ b/B.java\n"
    )
    tasks = build_tasks(diff)
    assert [(task.id, task.changed_lines) for task in tasks] == [
        ("B.java#file", [])
    ]


def test_triage_tasks_returns_unclassified_prior_without_fake_general_tag():
    priors = triage_tasks(build_tasks(_TWO_HUNK_DIFF)).priors
    assert set(priors) == {"A.java#h0", "A.java#h1"}
    assert priors["A.java#h0"].coverage is RiskCoverage.UNCLASSIFIED
    assert priors["A.java#h0"].hypotheses == ()


def test_rank_tasks_selects_all_without_oversized_budget():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    selection = rank_tasks(
        tasks,
        triage_tasks(tasks).priors,
        ReviewBudget(max_tasks_to_review=None, max_tasks_per_file=None),
    )
    assert selection.selected_task_ids == ["A.java#h0", "A.java#h1"]
    assert selection.skipped_tasks == []


def test_rank_tasks_uses_priority_and_records_skipped_priority():
    tasks = [_task(f"A.java#h{i}", "src/main/A.java") for i in range(3)]
    priors = {
        tasks[0].id: _prior(tasks[0].id, RiskTag.PERFORMANCE, 1),
        tasks[1].id: _prior(tasks[1].id, RiskTag.INJECTION, 3),
        tasks[2].id: _prior(
            tasks[2].id,
            RiskTag.TRANSACTION_ATOMICITY,
            2,
        ),
    }
    selection = rank_tasks(
        tasks,
        priors,
        ReviewBudget(max_tasks_to_review=2, max_tasks_per_file=None),
    )
    assert selection.selected_task_ids == ["A.java#h1", "A.java#h2"]
    assert [
        (item.task_id, item.reason, item.review_priority)
        for item in selection.skipped_tasks
    ] == [("A.java#h0", "total_limit", 1)]


def test_rank_tasks_applies_per_file_budget():
    tasks = [
        _task("A.java#h0", "src/main/A.java"),
        _task("A.java#h1", "src/main/A.java"),
        _task("B.java#h0", "src/main/B.java"),
    ]
    priors = {
        task.id: _prior(task.id, RiskTag.PERFORMANCE)
        for task in tasks
    }
    selection = rank_tasks(
        tasks,
        priors,
        ReviewBudget(max_tasks_to_review=None, max_tasks_per_file=1),
    )
    assert selection.selected_task_ids == ["A.java#h0", "B.java#h0"]
    assert [
        (item.task_id, item.reason) for item in selection.skipped_tasks
    ] == [("A.java#h1", "per_file_limit")]


def test_rank_tasks_prefers_production_then_stable_task_id_on_tie():
    tasks = [
        _task("z#h0", "src/main/Z.java"),
        _task("a#h0", "src/main/A.java"),
        _task("test#h0", "src/test/T.java"),
    ]
    priors = {
        task.id: _prior(task.id, RiskTag.PERFORMANCE, 2)
        for task in tasks
    }
    selection = rank_tasks(
        tasks,
        priors,
        ReviewBudget(max_tasks_to_review=2, max_tasks_per_file=None),
    )
    assert selection.selected_task_ids == ["a#h0", "z#h0"]


def test_generated_and_nested_test_paths_are_not_production():
    assert _is_production_path("src/main/java/OrderService.java") is True
    assert _is_production_path("src/main/generated/OrderDto.java") is False
    assert _is_production_path("src/main/test/OrderServiceTest.java") is False


def test_build_tasks_handles_deleted_file_and_pure_rename():
    deleted = (
        "diff --git a/Auth.java b/Auth.java\n"
        "deleted file mode 100644\n"
        "--- a/Auth.java\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-class Auth {}\n"
    )
    renamed = (
        "diff --git a/Old.java b/New.java\n"
        "similarity index 100%\n"
        "rename from Old.java\nrename to New.java\n"
    )
    assert [task.id for task in build_tasks(deleted)] == ["Auth.java#file"]
    assert [task.id for task in build_tasks(renamed)] == ["New.java#file"]


def test_build_tasks_filters_build_artifacts_but_keeps_source():
    diff = (
        "diff --git a/logo.png b/logo.png\n"
        "Binary files a/logo.png and b/logo.png differ\n"
        "diff --git a/script.sh b/script.sh\n"
        "old mode 100644\nnew mode 100755\n"
        "diff --git a/target/classes/Foo.class "
        "b/target/classes/Foo.class\n"
        "Binary files a/target/classes/Foo.class "
        "and b/target/classes/Foo.class differ\n"
    )
    assert [task.id for task in build_tasks(diff)] == ["script.sh#file"]


def test_changed_lines_ignores_no_newline_marker():
    hunk = (
        "@@ -1,3 +1,4 @@\n context\n-old\n+new\n"
        "\\ No newline at end of file\n+extra\n final"
    )
    assert _changed_lines(hunk, 1) == [2, 3]


def test_file_matches_task_exact_basename_and_mismatch():
    task = ReviewTask(
        id="a#h0",
        file="src/main/java/A.java",
        patch="",
    )
    assert file_matches_task("src/main/java/A.java", task)
    assert file_matches_task("A.java", task)
    assert not file_matches_task("B.java", task)


def test_noise_filter_keeps_actionable_test_file_findings():
    assert not is_noise_issue(
        "src/test/java/AuthServiceTest.java",
        "test coverage",
        "删除的边界用例会失去认证回归保护",
    )


def test_noise_filter_still_drops_comment_only_findings():
    assert is_noise_issue(
        "src/main/java/AuthService.java",
        "comment only",
        "只修改了注释",
    )
