"""Risk prior → additive reviewer coverage contracts."""

from codeguard_agent.models.tasks import (
    AssignmentReason,
    ReviewCoveragePlan,
    ReviewTier,
    ReviewerKind,
    ReviewTask,
    RiskCoverage,
    RiskHypothesis,
    RiskTag,
    TaskRiskPrior,
    TaskReviewPlan,
    TaskSelection,
)
from codeguard_agent.pipeline.risk.routing import (
    coverage_task_ids,
    coverage_tiers,
    ensure_review_coverage,
    plan_review_coverage,
)


def _task(task_id: str = "src/main/java/OrderService.java#h0") -> ReviewTask:
    file = task_id.split("#", 1)[0]
    return ReviewTask(id=task_id, file=file, patch="+ service.save(order);")


def _prior(
    task_id: str,
    tag: RiskTag | None = None,
    *,
    coverage: RiskCoverage = RiskCoverage.CONFIDENT,
    confidence: float = 0.85,
    priority: int = 3,
) -> TaskRiskPrior:
    hypotheses = ()
    if tag is not None:
        hypotheses = (
            RiskHypothesis(
                tag=tag,
                match_confidence=confidence,
                review_priority=priority,
                source_kind="diff_text",
                source="text:added:test",
                reason="test signal",
            ),
        )
    return TaskRiskPrior(
        task_id=task_id,
        hypotheses=hypotheses,
        coverage=coverage,
    )


def _assignments(plan, task_id: str):
    return {
        item.reviewer: item
        for task in plan.tasks
        if task.task_id == task_id
        for item in task.assignments
    }


def test_unclassified_task_gets_all_three_reviewers_direct():
    task = _task()
    prior = _prior(task.id, coverage=RiskCoverage.UNCLASSIFIED)
    plan = plan_review_coverage(
        [task],
        {task.id: prior},
        TaskSelection(selected_task_ids=[task.id]),
        react_budget=10,
        tools_available=True,
    )

    assignments = _assignments(plan, task.id)
    assert set(assignments) == set(ReviewerKind)
    assert all(item.tier is ReviewTier.DIRECT for item in assignments.values())
    assert all(
        AssignmentReason.AMBIGUITY_FALLBACK in item.reasons
        for item in assignments.values()
    )


def test_risk_can_add_and_upgrade_but_not_remove_baseline():
    task = _task()
    prior = _prior(task.id, RiskTag.AUTHORIZATION)
    plan = plan_review_coverage(
        [task],
        {task.id: prior},
        TaskSelection(selected_task_ids=[task.id]),
        react_budget=10,
        tools_available=True,
    )

    assignments = _assignments(plan, task.id)
    assert ReviewerKind.BEHAVIOR in assignments
    assert ReviewerKind.THREAT_MODEL in assignments
    assert assignments[ReviewerKind.THREAT_MODEL].tier is ReviewTier.REACT
    assert AssignmentReason.RISK_UPGRADED in assignments[
        ReviewerKind.THREAT_MODEL
    ].reasons


def test_tools_unavailable_keeps_assignments_direct():
    task = _task()
    plan = plan_review_coverage(
        [task],
        {task.id: _prior(task.id, RiskTag.TRANSACTION_ATOMICITY)},
        TaskSelection(selected_task_ids=[task.id]),
        react_budget=10,
        tools_available=False,
    )
    assert all(
        assignment.tier is ReviewTier.DIRECT
        for assignment in _assignments(plan, task.id).values()
    )


def test_react_budget_exhaustion_keeps_remaining_assignment_direct():
    first = _task("src/main/java/AService.java#h0")
    second = _task("src/main/java/BService.java#h0")
    selection = TaskSelection(selected_task_ids=[first.id, second.id])
    plan = plan_review_coverage(
        [first, second],
        {
            first.id: _prior(first.id, RiskTag.TRANSACTION_ATOMICITY),
            second.id: _prior(second.id, RiskTag.TRANSACTION_ATOMICITY),
        },
        selection,
        react_budget=1,
        tools_available=True,
    )
    tiers = [
        assignment.tier
        for task in plan.tasks
        for assignment in task.assignments
        if assignment.reviewer is ReviewerKind.BEHAVIOR
    ]
    assert tiers.count(ReviewTier.REACT) == 1
    assert tiers.count(ReviewTier.DIRECT) == 1
    assert plan.truncated_react_assignment_count == 1


def test_missing_coverage_is_repaired_as_unclassified():
    task = _task()
    selection = TaskSelection(selected_task_ids=[task.id])
    repaired = ensure_review_coverage([task], None, selection)
    assert set(_assignments(repaired, task.id)) == set(ReviewerKind)
    assert repaired.tasks_with_zero_assignments == 0


def test_empty_assignment_plan_is_repaired_as_unclassified():
    task = _task()
    selection = TaskSelection(selected_task_ids=[task.id])
    malformed = ReviewCoveragePlan(
        tasks=(TaskReviewPlan(task_id=task.id, assignments=()),),
        tasks_with_zero_assignments=1,
    )

    repaired = ensure_review_coverage([task], malformed, selection)

    assert set(_assignments(repaired, task.id)) == set(ReviewerKind)
    assert repaired.tasks_with_zero_assignments == 0


def test_coverage_queries_follow_selection_order():
    first = _task("src/main/java/A.java#h0")
    second = _task("src/main/java/B.java#h0")
    selection = TaskSelection(selected_task_ids=[second.id, first.id])
    plan = plan_review_coverage(
        [first, second],
        {
            first.id: _prior(first.id, coverage=RiskCoverage.UNCLASSIFIED),
            second.id: _prior(second.id, coverage=RiskCoverage.UNCLASSIFIED),
        },
        selection,
        react_budget=0,
        tools_available=False,
    )
    assert coverage_task_ids("behavior", plan, selection) == (second.id, first.id)
    assert coverage_tiers("behavior", plan, selection) == {
        second.id: "direct",
        first.id: "direct",
    }


def test_force_react_uses_coverage_plan_not_removed_legacy_tier_logic():
    task = _task()
    plan = plan_review_coverage(
        [task],
        {task.id: _prior(task.id, coverage=RiskCoverage.UNCLASSIFIED)},
        TaskSelection(selected_task_ids=[task.id]),
        react_budget=3,
        tools_available=True,
        force_react=True,
    )
    assert all(
        assignment.tier is ReviewTier.REACT
        for assignment in _assignments(plan, task.id).values()
    )
    assert all(
        AssignmentReason.EXECUTION_OVERRIDE in assignment.reasons
        for assignment in _assignments(plan, task.id).values()
    )
    assert plan.execution_override_assignments == 3
    assert plan.risk_upgraded_assignments == 0
