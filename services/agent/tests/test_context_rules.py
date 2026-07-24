"""pipeline/context_rules.py 的单测（阶段3）。"""

from __future__ import annotations

from codeguard_agent.models.council import ContextFact
from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag
from codeguard_agent.pipeline.context.rules import (
    ContextLevel,
    ast_block_for_file,
    plan_context_calls,
    sensitive_api_rows_for_task,
    truncate_task_facts,
)


def _profile(task_id: str, *tags: RiskTag) -> RiskProfile:
    return RiskProfile(task_id=task_id, tag_scores={tag: 2 for tag in tags})


def test_general_review_does_not_trigger_level1():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -1,1 +1,3 @@",
        patch="+x",
        changed_lines=[2],
    )
    plan = plan_context_calls(
        [task],
        {"A.java#h0": _profile("A.java#h0", RiskTag.GENERAL_REVIEW)},
        ast_facts_by_file={},
    )
    assert plan.level1_calls == ()
    assert plan.skips == ()


def test_complexity_tag_triggers_code_metrics_keyed_by_file():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -1,1 +1,3 @@",
        patch="+x",
        changed_lines=[2],
    )
    plan = plan_context_calls(
        [task],
        {"A.java#h0": _profile("A.java#h0", RiskTag.COMPLEXITY_CONTROL_FLOW)},
        ast_facts_by_file={},
    )
    assert len(plan.level1_calls) == 1
    call = plan.level1_calls[0]
    assert call.level is ContextLevel.CODE_METRICS
    assert call.key == "A.java"
    assert call.task_ids == ("A.java#h0",)


def test_resource_lifecycle_triggers_find_callers_with_resolved_method():
    ast_block = (
        "AST for: A.java\n"
        "  class: A\n"
        "    public void save(Order order) [L10-L20]\n"
    )
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -12,2 +12,2 @@",
        patch="+x",
        changed_lines=[12],
    )
    plan = plan_context_calls(
        [task],
        {"A.java#h0": _profile("A.java#h0", RiskTag.RESOURCE_LIFECYCLE)},
        ast_facts_by_file={"a.java": ast_block},
    )
    assert len(plan.level1_calls) == 1
    call = plan.level1_calls[0]
    assert call.level is ContextLevel.FIND_CALLERS
    assert call.key == "A.java#save"


def test_method_unresolved_records_skip_not_call():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -1,1 +1,1 @@",
        patch="+x",
        changed_lines=[1],
    )
    plan = plan_context_calls(
        [task],
        {"A.java#h0": _profile("A.java#h0", RiskTag.RESOURCE_LIFECYCLE)},
        ast_facts_by_file={},
    )
    assert plan.level1_calls == ()
    assert len(plan.skips) == 1
    assert plan.skips[0].task_id == "A.java#h0"
    assert plan.skips[0].reason == "no_method_resolved"


def test_same_file_multiple_tasks_dedup_to_one_code_metrics_call():
    task_a = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -1,1 +1,1 @@",
        patch="+x",
        changed_lines=[1],
    )
    task_b = ReviewTask(
        id="A.java#h1",
        file="A.java",
        hunk_header="@@ -20,1 +20,1 @@",
        patch="+y",
        changed_lines=[20],
    )
    plan = plan_context_calls(
        [task_a, task_b],
        {
            "A.java#h0": _profile("A.java#h0", RiskTag.DUPLICATION_DESIGN),
            "A.java#h1": _profile("A.java#h1", RiskTag.OBSERVABILITY_TESTABILITY),
        },
        ast_facts_by_file={},
    )
    assert len(plan.level1_calls) == 1
    call = plan.level1_calls[0]
    assert call.key == "A.java"
    assert set(call.task_ids) == {"A.java#h0", "A.java#h1"}


def test_task_without_risk_profile_is_skipped_silently():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -1,1 +1,1 @@",
        patch="+x",
        changed_lines=[1],
    )
    plan = plan_context_calls([task], {}, ast_facts_by_file={})
    assert plan.level1_calls == ()
    assert plan.skips == ()


def test_ast_block_for_file_matches_by_normalized_path():
    ast_text = (
        "AST for: A.java\n"
        "  class: A\n"
        "    public void save() [L10-L12]\n"
        "AST for: B.java\n"
        "  class: B\n"
    )

    block = ast_block_for_file(ast_text, "a.java")

    assert block is not None
    assert block.startswith("AST for: A.java")
    assert "B.java" not in block


def test_ast_block_for_file_returns_none_when_no_match():
    assert ast_block_for_file("AST for: A.java\n  class: A\n", "C.java") is None


def test_sensitive_api_rows_for_task_filters_by_file_and_hunk_range():
    sensitive_text = (
        "# 敏感 API 扫描\n"
        "扫描 1 个文件, 跳过 0 个不可解析文件, 发现 2 处敏感 API 调用\n\n"
        "| 危险等级 | API | 文件 | 行号 | 调用参数 |\n"
        "|---------|-----|------|------|----------|\n"
        "| 🔴 HIGH | `Statement.execute` | A.java:12 | `sql` |\n"
        "| 🟡 MEDIUM | `Files.copy` | A.java:99 | `p1, p2` |\n"
    )
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -10,5 +10,5 @@",
        patch="+x",
        changed_lines=[12],
    )

    rows = sensitive_api_rows_for_task(sensitive_text, task)

    assert len(rows) == 1
    assert "Statement.execute" in rows[0]
    assert "Files.copy" not in "\n".join(rows)


def test_sensitive_api_rows_for_task_accepts_whole_file_for_fallback_task():
    sensitive_text = (
        "| 危险等级 | API | 文件 | 行号 | 调用参数 |\n"
        "|---------|-----|------|------|----------|\n"
        "| 🔴 HIGH | `Statement.execute` | A.java:500 | `sql` |\n"
    )
    task = ReviewTask(id="A.java#file", file="A.java", patch="+x", changed_lines=[])

    rows = sensitive_api_rows_for_task(sensitive_text, task)

    assert len(rows) == 1


def test_truncate_task_facts_keeps_all_when_within_budget():
    facts = [ContextFact(source="s1", kind="k", content="short")]

    kept, truncated = truncate_task_facts(facts, max_chars=100)

    assert kept == facts
    assert truncated is False


def test_truncate_task_facts_marks_clipped_fact_and_preserves_metadata():
    facts = [
        ContextFact(source="s1", kind="first", content="a" * 60),
        ContextFact(source="s2", kind="second", content="b" * 60),
    ]

    kept, truncated = truncate_task_facts(facts, max_chars=100)

    assert truncated is True
    assert sum(len(fact.content) for fact in kept) <= 100 + len("...(已截断)")
    assert kept[0] == facts[0]
    assert kept[1].source == "s2"
    assert kept[1].kind == "second"
    assert kept[1].content == "b" * 40 + "...(已截断)"
    assert kept[1].truncated is True


def test_truncate_task_facts_none_budget_means_unbounded():
    facts = [ContextFact(source="s1", kind="k", content="a" * 100_000)]

    kept, truncated = truncate_task_facts(facts, max_chars=None)

    assert kept == facts
    assert truncated is False
