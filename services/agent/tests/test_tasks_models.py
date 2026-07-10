"""风险路由任务模型协议测试（Phase 1）。"""

from __future__ import annotations

from codeguard_agent.models.council import ContextFact
from codeguard_agent.models.tasks import (
    ReviewBudget,
    ReviewTask,
    RiskProfile,
    RiskSignal,
    RiskTag,
    SkippedTask,
    TaskContextBundle,
    TaskSelection,
)


def test_review_task_minimal_fields():
    task = ReviewTask(id="A.java#h0", file="A.java", patch="@@ -1 +1 @@\n+x")
    assert task.hunk_header == ""
    assert task.changed_lines == []


def test_risk_profile_defaults_empty():
    profile = RiskProfile(task_id="A.java#h0")
    assert profile.tag_scores == {}
    assert profile.signals == []


def test_risk_signal_carries_source_and_reason():
    sig = RiskSignal(tag=RiskTag.AUTHORIZATION, score=3, source="rule:auth", reason="Controller 无权限注解")
    assert sig.line is None
    assert sig.tag == RiskTag.AUTHORIZATION


def test_review_budget_defaults_to_no_limit():
    budget = ReviewBudget()
    assert budget.max_tasks_to_review is None
    assert budget.max_final_issues is None


def test_task_selection_records_skips():
    sel = TaskSelection(
        selected_task_ids=["A.java#h0"],
        skipped_tasks=[SkippedTask(task_id="B.java#h0", reason="低价值文件")],
    )
    assert sel.selected_task_ids == ["A.java#h0"]
    assert sel.skipped_tasks[0].risk_score == 0


def test_task_context_bundle_does_not_duplicate_task_facts():
    bundle = TaskContextBundle(
        task_id="A.java#h0",
        facts=[ContextFact(source="diff", kind="hunk", content="x")],
    )
    keys = set(bundle.model_dump())
    assert keys == {"task_id", "facts", "truncated"}
    assert "file" not in keys
    assert "patch" not in keys


def test_profile_has_no_total_score_field():
    # total_score 是 TaskRank 的派生计算，不得成为第二份可变事实（spec §3.2）。
    assert "total_score" not in RiskProfile.model_fields
