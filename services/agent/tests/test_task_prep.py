"""任务准备纯函数测试（Phase 1）。"""

from __future__ import annotations

from codeguard_agent.models.tasks import ReviewBudget, ReviewTask
from codeguard_agent.pipeline.task_prep import (
    _changed_lines,
    build_tasks,
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


def test_triage_tasks_returns_empty_profile_per_task():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    profiles = triage_tasks(tasks)
    assert set(profiles) == {"A.java#h0", "A.java#h1"}
    assert profiles["A.java#h0"].tag_scores == {}


def test_rank_tasks_selects_all_by_default():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    profiles = triage_tasks(tasks)
    sel = rank_tasks(tasks, profiles, ReviewBudget())
    assert sel.selected_task_ids == ["A.java#h0", "A.java#h1"]
    assert sel.skipped_tasks == []


def test_map_candidate_uses_changed_line_first():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    # 行 12 命中 hunk1 的 changed_lines
    assert map_candidate_to_task("A.java", 12, tasks) == "A.java#h1"


def test_map_candidate_falls_back_to_first_file_task():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    # 行 999 不在任何 changed_lines → 落到该文件第一个 task
    assert map_candidate_to_task("A.java", 999, tasks) == "A.java#h0"


def test_map_candidate_matches_by_basename():
    tasks = [ReviewTask(id="src/A.java#h0", file="src/A.java", patch="")]
    # LLM 常只给 basename
    assert map_candidate_to_task("A.java", 0, tasks) == "src/A.java#h0"


def test_map_candidate_returns_none_when_file_absent():
    tasks = build_tasks(_TWO_HUNK_DIFF)
    assert map_candidate_to_task("Ghost.java", 1, tasks) is None


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
        ReviewTask(id="src/Foo.java#h0", file="src/Foo.java", patch=""),
        ReviewTask(id="test/Foo.java#h0", file="test/Foo.java", patch=""),
    ]
    assert map_candidate_to_task("test/Foo.java", 1, tasks) == "test/Foo.java#h0"
    assert map_candidate_to_task("src/Foo.java", 1, tasks) == "src/Foo.java#h0"
