"""任务准备纯函数测试（Phase 1）。"""

from __future__ import annotations

from codeguard_agent.models.tasks import ReviewBudget, RiskProfile, RiskSignal, ReviewTask, RiskTag
from codeguard_agent.pipeline.risk.task_prep import (
    _changed_lines,
    _is_production_path,
    build_tasks,
    file_matches_task,
    map_candidate_to_task,
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


def test_build_tasks_one_task_per_hunk():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    assert [t.id for t in tasks] == ["A.java#h0", "A.java#h1"]
    assert all(t.file == "A.java" for t in tasks)


def test_build_tasks_records_added_line_numbers():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    # hunk0 新起点 1：上下文行 a=0(1)、新增 b=1(2)、上下文 c=2(3) → 新增行号 [2]
    assert tasks[0].changed_lines == [2]
    # hunk1 新起点 11：上下文 call(11)、新增 guard(12) → [12]
    assert tasks[1].changed_lines == [12]


def test_build_tasks_falls_back_to_file_level_when_no_hunk():
    # 无 @@ hunk 头（例如纯 rename/二进制）→ 文件级 fallback task
    diff = "diff --git a/B.java b/B.java\nrename from B.java\nrename to B.java\n+++ b/B.java\n"
    tasks = build_tasks(diff)
    assert len(tasks) == 1
    assert tasks[0].id == "B.java#file"
    assert tasks[0].changed_lines == []


def test_triage_tasks_returns_profile_per_task():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    profiles = triage_tasks(tasks).profiles
    assert set(profiles) == {"A.java#h0", "A.java#h1"}
    assert profiles["A.java#h0"].tag_scores == {RiskTag.GENERAL_REVIEW: 1}


def test_rank_tasks_selects_all_by_default():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    profiles = triage_tasks(tasks).profiles
    sel = rank_tasks(tasks, profiles, ReviewBudget())
    assert sel.selected_task_ids == ["A.java#h0", "A.java#h1"]
    assert sel.skipped_tasks == []


def _rank_task(task_id: str, file: str) -> ReviewTask:
    return ReviewTask(id=task_id, file=file, patch="+changed")


def _rank_profile(task_id: str, tag: RiskTag, score: int = 1, *, deleted: bool = False) -> RiskProfile:
    source = "text:deleted:test" if deleted else "text:added:test"
    return RiskProfile(
        task_id=task_id,
        tag_scores={tag: score},
        signals=[RiskSignal(tag=tag, score=score, source=source, reason="test")],
    )


def test_rank_tasks_applies_total_budget_and_keeps_highest_risk_first():
    tasks = [_rank_task(f"A.java#h{i}", "src/main/A.java") for i in range(3)]
    profiles = {
        tasks[0].id: _rank_profile(tasks[0].id, RiskTag.PERFORMANCE, 1),
        tasks[1].id: _rank_profile(tasks[1].id, RiskTag.INJECTION, 3),
        tasks[2].id: _rank_profile(tasks[2].id, RiskTag.TRANSACTION_ATOMICITY, 2),
    }

    selection = rank_tasks(tasks, profiles, ReviewBudget(max_tasks_to_review=2, max_tasks_per_file=None))

    assert selection.selected_task_ids == ["A.java#h1", "A.java#h2"]
    assert [(item.task_id, item.reason, item.risk_score) for item in selection.skipped_tasks] == [
        ("A.java#h0", "total_limit", 1)
    ]


def test_rank_tasks_applies_per_file_budget_before_moving_to_next_file():
    tasks = [
        _rank_task("A.java#h0", "src/main/A.java"),
        _rank_task("A.java#h1", "src/main/A.java"),
        _rank_task("B.java#h0", "src/main/B.java"),
    ]
    profiles = {task.id: _rank_profile(task.id, RiskTag.PERFORMANCE, 1) for task in tasks}

    selection = rank_tasks(tasks, profiles, ReviewBudget(max_tasks_to_review=None, max_tasks_per_file=1))

    assert selection.selected_task_ids == ["A.java#h0", "B.java#h0"]
    assert [(item.task_id, item.reason) for item in selection.skipped_tasks] == [
        ("A.java#h1", "per_file_limit")
    ]


def test_rank_tasks_none_budget_limits_and_task_id_breaks_ties():
    tasks = [
        _rank_task("z#h0", "test/z.java"),
        _rank_task("a#h0", "test/a.java"),
    ]
    profiles = {task.id: _rank_profile(task.id, RiskTag.GENERAL_REVIEW, 1) for task in tasks}

    selection = rank_tasks(tasks, profiles, ReviewBudget(max_tasks_to_review=None, max_tasks_per_file=None))

    assert selection.selected_task_ids == ["a#h0", "z#h0"]
    assert selection.skipped_tasks == []


def test_rank_tasks_prefers_concrete_risk_over_general_review():
    tasks = [
        _rank_task("general#h0", "src/main/General.java"),
        _rank_task("specific#h0", "src/main/Specific.java"),
    ]
    profiles = {
        "general#h0": _rank_profile("general#h0", RiskTag.GENERAL_REVIEW, 1),
        "specific#h0": _rank_profile("specific#h0", RiskTag.INJECTION, 1),
    }

    selection = rank_tasks(tasks, profiles, ReviewBudget(max_tasks_to_review=1, max_tasks_per_file=None))

    assert selection.selected_task_ids == ["specific#h0"]


def test_generated_and_nested_test_paths_are_not_production():
    assert _is_production_path("src/main/java/OrderService.java") is True
    assert _is_production_path("src/main/generated/OrderDto.java") is False
    assert _is_production_path("src/main/test/OrderServiceTest.java") is False


def test_map_candidate_uses_changed_line_first():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    # 行 12 命中 hunk1 的 changed_lines
    assert map_candidate_to_task("A.java", 12, tasks) == "A.java#h1"


def test_map_candidate_binds_context_line_to_containing_hunk():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    # 行 11 是 hunk1 的上下文行（不在 changed_lines[12]，但落在 hunk1 范围 [11,12]）
    # → 归属正确的 hunk1，绝不落到"第一个"hunk0
    assert map_candidate_to_task("A.java", 11, tasks) == "A.java#h1"
    # 行 3 落在 hunk0 范围 [1,3] → hunk0
    assert map_candidate_to_task("A.java", 3, tasks) == "A.java#h0"


def test_map_candidate_rejects_line_outside_all_hunks():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    # 行 999 不在任何 changed_lines、不落在任何 hunk 范围、无文件级 fallback → 拒绝
    assert map_candidate_to_task("A.java", 999, tasks) is None


def test_map_candidate_matches_by_basename():
    # LLM 常只给 basename；文件级 fallback task 对任意行都可绑定
    tasks = [ReviewTask(id="src/A.java#file", file="src/A.java", patch="")]
    assert map_candidate_to_task("A.java", 0, tasks) == "src/A.java#file"


def test_map_candidate_returns_none_when_file_absent():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    assert map_candidate_to_task("Ghost.java", 1, tasks) is None


def test_build_tasks_creates_fallback_for_deleted_file():
    # 删除文件（+++ /dev/null）：split_diff_by_file 会漏掉，需补文件级 fallback 取旧路径
    diff = (
        "diff --git a/Auth.java b/Auth.java\n"
        "deleted file mode 100644\n"
        "index a81d7c2..0000000\n"
        "--- a/Auth.java\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-class Auth {}\n"
        "-void check() {}\n"
    )
    tasks = build_tasks(diff)
    assert [t.id for t in tasks] == ["Auth.java#file"]
    assert tasks[0].file == "Auth.java"
    # 删除文件仍能被候选绑定（reviewer 发现"删了鉴权"时不会被丢弃）
    assert map_candidate_to_task("Auth.java", 1, tasks) == "Auth.java#file"


def test_build_tasks_creates_fallback_for_pure_rename():
    # 纯重命名（100% 相似、无 +++）：取新路径建文件级 fallback
    diff = (
        "diff --git a/Old.java b/New.java\n"
        "similarity index 100%\n"
        "rename from Old.java\n"
        "rename to New.java\n"
    )
    tasks = build_tasks(diff)
    assert [t.id for t in tasks] == ["New.java#file"]


def test_build_tasks_filters_build_artifacts_but_keeps_source():
    diff = (
        "diff --git a/logo.png b/logo.png\n"
        "index 111..222 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
        "diff --git a/script.sh b/script.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
        "diff --git a/target/classes/Foo.class b/target/classes/Foo.class\n"
        "Binary files a/target/classes/Foo.class and b/target/classes/Foo.class differ\n"
    )
    tasks = build_tasks(diff)
    # .png 和 .class (target/) 是构建产物，应过滤；.sh 是源码，应保留
    assert [task.id for task in tasks] == ["script.sh#file"]


def test_changed_lines_ignores_no_newline_marker():
    # `\ No newline at end of file` 是 diff 级标记，不占新文件行号。
    hunk = (
        "@@ -1,3 +1,4 @@\n"
        " context\n"
        "-old\n"
        "+new\n"
        "\\ No newline at end of file\n"
        "+extra\n"
        " final"
    )
    # 新文件: context(1) / new(2) / extra(3) / final(4) → 新增行号 [2, 3]
    assert _changed_lines(hunk, 1) == [2, 3]


def test_map_candidate_prefers_full_path_over_basename_collision():
    # 同 basename 不同目录：候选给出全路径时应精确命中，不被另一个同名文件抢走。
    tasks = [
        ReviewTask(id="src/Foo.java#h0", file="src/Foo.java", patch="", changed_lines=[1]),
        ReviewTask(id="test/Foo.java#h0", file="test/Foo.java", patch="", changed_lines=[1]),
    ]
    assert map_candidate_to_task("test/Foo.java", 1, tasks) == "test/Foo.java#h0"
    assert map_candidate_to_task("src/Foo.java", 1, tasks) == "src/Foo.java#h0"


def test_file_matches_task_true_for_exact_path():
    task = ReviewTask(id="a#h0", file="src/main/java/A.java", patch="")
    assert file_matches_task("src/main/java/A.java", task) is True


def test_file_matches_task_true_for_basename_fallback():
    task = ReviewTask(id="a#h0", file="src/main/java/A.java", patch="")
    assert file_matches_task("A.java", task) is True


def test_file_matches_task_false_for_different_file():
    task = ReviewTask(id="a#h0", file="src/main/java/A.java", patch="")
    assert file_matches_task("B.java", task) is False
