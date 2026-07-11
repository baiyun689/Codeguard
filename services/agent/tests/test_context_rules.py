"""pipeline/context_rules.py 的单测（阶段3）。"""

from __future__ import annotations

from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag
from codeguard_agent.pipeline.context_rules import ContextLevel, plan_context_calls


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
