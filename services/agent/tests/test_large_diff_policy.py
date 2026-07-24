from codeguard_agent.models.tasks import ReviewBudget, ReviewTask, TaskSelection
from codeguard_agent.pipeline.risk.large_diff import plan_large_diff


def _task(index: int, patch: str = "+x") -> ReviewTask:
    return ReviewTask(
        id=f"src/F{index}.java#h0",
        file=f"src/F{index}.java",
        hunk_header="@@ -1 +1 @@",
        patch=f"@@ -1 +1 @@\n{patch}",
        changed_lines=[1],
    )


def test_large_diff_activates_only_above_line_threshold():
    budget = ReviewBudget()

    assert not plan_large_diff("\n".join("x" for _ in range(5000)), [_task(1)], budget).active
    assert not plan_large_diff("\n".join("x" for _ in range(5000)) + "\n", [_task(1)], budget).active
    assert plan_large_diff("\n".join("x" for _ in range(5001)), [_task(1)], budget).active
    many_tasks = plan_large_diff("small", [_task(i) for i in range(200)], budget)
    assert many_tasks.active is False
    assert many_tasks.effective_budget.max_tasks_to_review is None
    assert many_tasks.effective_budget.max_tasks_per_file is None


def test_large_diff_tightens_budget_without_loosening_user_limits():
    tasks = [_task(i) for i in range(51)]
    large_diff = "\n".join("x" for _ in range(5001))
    plan = plan_large_diff(large_diff, tasks, ReviewBudget())
    stricter = plan_large_diff(
        large_diff,
        tasks,
        ReviewBudget(
            max_tasks_to_review=7,
            max_tasks_per_file=2,
            max_context_chars_per_task=1000,
            max_final_issues=4,
        ),
    )

    assert plan.effective_budget == ReviewBudget(
        max_tasks_to_review=20,
        max_tasks_per_file=3,
        max_context_chars_per_task=2000,
    )
    assert stricter.effective_budget == ReviewBudget(
        max_tasks_to_review=7,
        max_tasks_per_file=2,
        max_context_chars_per_task=1000,
        max_final_issues=4,
    )


def test_selected_diff_contains_only_selected_tasks_and_is_bounded():
    selected = _task(1, "+selected")
    skipped = _task(2, "+not-selected")
    huge = _task(3, "+" + "z" * 70_000)
    tasks = [selected, skipped, huge] + [_task(i) for i in range(4, 52)]
    plan = plan_large_diff("\n".join("x" for _ in range(5001)), tasks, ReviewBudget())
    selection = TaskSelection(selected_task_ids=[selected.id, huge.id])

    rendered = plan.selected_diff(tasks, selection)

    assert "+selected" in rendered
    assert "+not-selected" not in rendered
    assert len(rendered) <= 60_100
    assert "大 diff 选中范围已截断" in rendered


def test_large_diff_scopes_single_task_patch_and_reports_partial_coverage():
    tasks = [_task(i) for i in range(51)]
    plan = plan_large_diff("\n".join("x" for _ in range(5001)), tasks, ReviewBudget())
    selection = TaskSelection(
        selected_task_ids=[tasks[0].id, tasks[1].id],
        skipped_tasks=[],
    )

    scoped = plan.scoped_patch("x" * 20_000)
    notice = plan.coverage_notice(selection)

    assert len(scoped) <= 12_100
    assert scoped.endswith("...(大 diff 单任务 patch 已截断)")
    assert "共 51 个任务" in notice
    assert "审查 2 个" in notice
    assert "跳过 49 个" in notice
    assert "不代表完整覆盖" in notice


def test_normal_diff_preserves_full_patch_and_has_no_notice():
    task = _task(1, "+normal")
    plan = plan_large_diff("small", [task], ReviewBudget())
    selection = TaskSelection(selected_task_ids=[task.id])

    assert plan.scoped_patch(task.patch) == task.patch
    assert plan.coverage_notice(selection) == ""
