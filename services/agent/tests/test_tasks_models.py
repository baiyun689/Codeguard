"""风险路由任务模型协议测试（Phase 1）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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


def test_risk_tag_has_exact_phase_2_values():
    assert {tag.value for tag in RiskTag} == {
        "AUTHORIZATION",
        "AUTHENTICATION_SESSION",
        "WEB_SECURITY_CONFIG",
        "INPUT_VALIDATION",
        "INJECTION",
        "SQL_DATA_ACCESS",
        "FILE_PATH_IO",
        "SSRF_OUTBOUND",
        "CONFIG_SECURITY",
        "DATA_EXPOSURE",
        "TRANSACTION_ATOMICITY",
        "CONCURRENCY_CONSISTENCY",
        "IDEMPOTENCY_RETRY",
        "CACHE_CONSISTENCY",
        "MESSAGE_DELIVERY",
        "ERROR_HANDLING",
        "NULL_STATE_SAFETY",
        "RESOURCE_LIFECYCLE",
        "API_CONTRACT",
        "PERFORMANCE",
        "COMPLEXITY_CONTROL_FLOW",
        "DUPLICATION_DESIGN",
        "OBSERVABILITY_TESTABILITY",
        "GENERAL_REVIEW",
    }


def test_risk_signal_carries_source_and_reason():
    sig = RiskSignal(tag=RiskTag.AUTHORIZATION, score=3, source="rule:auth", reason="Controller 无权限注解")
    assert sig.line is None
    assert sig.tag == RiskTag.AUTHORIZATION


def test_review_budget_has_phase_2_defaults():
    budget = ReviewBudget()
    assert budget.max_tasks_to_review == 100
    assert budget.max_tasks_per_file == 10
    assert budget.max_final_issues is None


def test_review_budget_defaults_context_chars_per_task_to_4000():
    budget = ReviewBudget()
    assert budget.max_context_chars_per_task == 4000


@pytest.mark.parametrize(
    "field",
    [
        "max_tasks_to_review",
        "max_tasks_per_file",
        "max_context_chars_per_task",
        "max_final_issues",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_review_budget_rejects_non_positive_values(field, value):
    with pytest.raises(ValidationError):
        ReviewBudget(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "max_tasks_to_review",
        "max_tasks_per_file",
        "max_context_chars_per_task",
        "max_final_issues",
    ],
)
@pytest.mark.parametrize("value", [True, "1"])
def test_review_budget_rejects_non_strict_integer_values(field, value):
    with pytest.raises(ValidationError):
        ReviewBudget(**{field: value})


def test_review_budget_accepts_positive_integer_values():
    budget = ReviewBudget(
        max_tasks_to_review=1,
        max_tasks_per_file=2,
        max_context_chars_per_task=3,
        max_final_issues=4,
    )
    assert budget.max_tasks_to_review == 1
    assert budget.max_tasks_per_file == 2
    assert budget.max_context_chars_per_task == 3
    assert budget.max_final_issues == 4


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


def test_profile_has_only_task_id_scores_and_signals_fields():
    assert set(RiskProfile.model_fields) == {"task_id", "tag_scores", "signals"}
