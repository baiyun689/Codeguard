"""确定性审查覆盖：风险先验只能增加覆盖或升级执行预算。"""

from __future__ import annotations

from collections.abc import Mapping
from codeguard_agent.models.tasks import (
    AssignmentReason,
    ReviewCoveragePlan,
    ReviewTier,
    ReviewerAssignment,
    ReviewerKind,
    RiskCoverage,
    RiskTag,
    ReviewTask,
    TaskReviewPlan,
    TaskRiskPrior,
    TaskSelection,
)
from codeguard_agent.pipeline.risk.rules.catalog import reviewers_for_tag

_REVIEWER_ORDER = (
    ReviewerKind.THREAT_MODEL,
    ReviewerKind.BEHAVIOR,
    ReviewerKind.MAINTAINABILITY,
)

# ── ReAct 升格阈值(启发式默认值,标定工具 = eval-triage-off 消融档)──
# 只有高置信 + 高优先级的假设才有资格把该 (task, reviewer) 从 Direct 升为
# ReAct;阈值越低 ReAct 越多(成本↑ 覆盖↑),反向同理。调参后需对照重跑。
_REACT_UPGRADE_MIN_CONFIDENCE = 0.75
_REACT_UPGRADE_MIN_PRIORITY = 2

_SOURCE_TO_KIND = {
    "ThreatModelAgent": ReviewerKind.THREAT_MODEL,
    "threat_model": ReviewerKind.THREAT_MODEL,
    "BehaviorAgent": ReviewerKind.BEHAVIOR,
    "behavior": ReviewerKind.BEHAVIOR,
    "MaintainabilityAgent": ReviewerKind.MAINTAINABILITY,
    "maintainability": ReviewerKind.MAINTAINABILITY,
}


def _reviewer_kind(name: str) -> ReviewerKind | None:
    return _SOURCE_TO_KIND.get(name)


def _is_production_path(path: str) -> bool:
    normalized = (path or "").replace("\\", "/").lower()
    basename = normalized.rsplit("/", 1)[-1]
    config_names = {
        "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
        "settings.gradle.kts", "gradlew", "gradlew.bat", "mvnw", "mvnw.cmd",
        "dockerfile", "makefile",
    }
    config_suffixes = (".yaml", ".yml", ".properties", ".toml", ".ini", ".conf")
    config_markers = ("/migration/", "/migrations/", "/db/changelog/", "/flyway/", "/liquibase/")
    if normalized.startswith(("test/", "tests/", "docs/", "generated/")):
        return False
    if any(marker in normalized for marker in ("/test/", "/tests/", "/docs/", "/generated/", "/build/", "/target/")):
        return False
    return basename not in config_names and not basename.endswith(config_suffixes) and not any(
        marker in normalized for marker in config_markers
    )


def _is_security_boundary(task: ReviewTask) -> bool:
    value = f"{task.file}\n{task.patch}".lower().replace("\\", "/")
    markers = (
        "controller", "filter", "interceptor", "webhook", "upload", "download",
        "security", "auth", "session", "preauthorize", "requestmapping",
        "deserialize", "objectinputstream", "runtime.exec", "jdbc", "sql",
    )
    return any(marker in value for marker in markers)


def _baseline_reviewers(task: ReviewTask, prior: TaskRiskPrior) -> tuple[ReviewerKind, ...]:
    if prior.coverage in (RiskCoverage.AMBIGUOUS, RiskCoverage.UNCLASSIFIED):
        return _REVIEWER_ORDER
    baseline: list[ReviewerKind] = []
    if _is_production_path(task.file):
        baseline.append(ReviewerKind.BEHAVIOR)
    else:
        baseline.append(ReviewerKind.MAINTAINABILITY)
    if _is_security_boundary(task):
        baseline.append(ReviewerKind.THREAT_MODEL)
    return tuple(kind for kind in _REVIEWER_ORDER if kind in baseline)


def plan_review_coverage(
    tasks: list[ReviewTask],
    priors: Mapping[str, TaskRiskPrior],
    selection: TaskSelection,
    *,
    react_budget: int,
    tools_available: bool,
    force_react: bool = False,
) -> ReviewCoveragePlan:
    """为 selected tasks 生成基础覆盖 + 风险增强的稳定 Reviewer 计划。"""
    task_by_id = {task.id: task for task in tasks}
    plans: list[TaskReviewPlan] = []
    react_candidates: list[tuple[int, float, int, int, str, ReviewerKind, RiskTag]] = []
    selected_index = {task_id: index for index, task_id in enumerate(selection.selected_task_ids)}

    for task_id in selection.selected_task_ids:
        task = task_by_id.get(task_id) or ReviewTask(id=task_id, file="", patch="")
        prior = priors.get(task_id) or TaskRiskPrior(
            task_id=task_id, coverage=RiskCoverage.UNCLASSIFIED
        )
        baseline = set(_baseline_reviewers(task, prior))
        default_reason = (
            AssignmentReason.AMBIGUITY_FALLBACK
            if prior.coverage in (RiskCoverage.AMBIGUOUS, RiskCoverage.UNCLASSIFIED)
            else AssignmentReason.BASELINE
        )
        reasons: dict[ReviewerKind, set[AssignmentReason]] = {
            reviewer: {default_reason} for reviewer in baseline
        }
        tags: dict[ReviewerKind, set[RiskTag]] = {reviewer: set() for reviewer in baseline}
        for hypothesis in prior.hypotheses:
            mapped = {
                kind
                for name in reviewers_for_tag(hypothesis.tag)
                if (kind := _reviewer_kind(name)) is not None
            }
            for reviewer in mapped:
                if reviewer not in reasons:
                    reasons[reviewer] = set()
                    tags[reviewer] = set()
                    reasons[reviewer].add(
                        AssignmentReason.AMBIGUITY_FALLBACK
                        if prior.coverage is not RiskCoverage.CONFIDENT
                        else AssignmentReason.RISK_ADDED
                    )
                tags[reviewer].add(hypothesis.tag)
                if (
                    tools_available
                    and (
                        force_react
                        or (
                            prior.coverage is RiskCoverage.CONFIDENT
                            and hypothesis.match_confidence >= _REACT_UPGRADE_MIN_CONFIDENCE
                            and hypothesis.review_priority >= _REACT_UPGRADE_MIN_PRIORITY
                        )
                    )
                ):
                    react_candidates.append(
                        (
                            -hypothesis.review_priority,
                            -hypothesis.match_confidence,
                            selected_index[task_id],
                            _REVIEWER_ORDER.index(reviewer),
                            task_id,
                            reviewer,
                            hypothesis.tag,
                        )
                    )
        if tools_available and force_react:
            for reviewer in reasons:
                if not any(
                    candidate[4] == task_id and candidate[5] is reviewer
                    for candidate in react_candidates
                ):
                    react_candidates.append(
                        (
                            0,
                            0.0,
                            selected_index[task_id],
                            _REVIEWER_ORDER.index(reviewer),
                            task_id,
                            reviewer,
                            RiskTag.GENERAL_REVIEW,
                        )
                    )
        if prior.coverage in (RiskCoverage.AMBIGUOUS, RiskCoverage.UNCLASSIFIED):
            for reviewer in _REVIEWER_ORDER:
                reasons.setdefault(reviewer, {AssignmentReason.AMBIGUITY_FALLBACK})
                tags.setdefault(reviewer, set())
        assignments = tuple(
            ReviewerAssignment(
                reviewer=reviewer,
                tier=ReviewTier.DIRECT,
                reasons=tuple(sorted(reasons[reviewer], key=lambda value: value.value)),
                hypothesis_tags=tuple(sorted(tags[reviewer], key=lambda value: value.value)),
            )
            for reviewer in _REVIEWER_ORDER
            if reviewer in reasons
        )
        plans.append(TaskReviewPlan(task_id=task_id, assignments=assignments))

    # 同一 task/reviewer 可能被多个风险假设同时增强；ReAct 预算按 assignment
    # 计算，必须先保留该 assignment 的最佳排序项，否则重复假设会消耗多个名额。
    best_candidate_by_assignment: dict[
        tuple[str, ReviewerKind],
        tuple[int, float, int, int, str, ReviewerKind, RiskTag],
    ] = {}
    for candidate in react_candidates:
        key = (candidate[4], candidate[5])
        current = best_candidate_by_assignment.get(key)
        if current is None or candidate < current:
            best_candidate_by_assignment[key] = candidate
    ordered_candidates = sorted(best_candidate_by_assignment.values())
    upgraded = {
        (task_id, reviewer)
        for _priority, _confidence, _task_index, _reviewer_index, task_id, reviewer, _tag
        in ordered_candidates[: max(react_budget, 0)]
    }
    selected_react_tasks = {task_id for task_id, _reviewer in upgraded}

    updated_plans: list[TaskReviewPlan] = []
    for plan in plans:
        assignments = tuple(
            assignment.model_copy(
                update={
                    "tier": ReviewTier.REACT,
                    "reasons": tuple(
                        sorted(
                            set(assignment.reasons)
                            | {
                                AssignmentReason.EXECUTION_OVERRIDE
                                if force_react
                                else AssignmentReason.RISK_UPGRADED
                            },
                            key=lambda value: value.value,
                        )
                    ),
                }
            )
            if (plan.task_id, assignment.reviewer) in upgraded
            else assignment
            for assignment in plan.assignments
        )
        updated_plans.append(plan.model_copy(update={"assignments": assignments}))
    candidate_keys = set(best_candidate_by_assignment)
    candidate_task_ids = {task_id for task_id, _reviewer in candidate_keys}
    truncated_assignment_count = max(0, len(candidate_keys) - len(upgraded))
    baseline_assignments = sum(
        AssignmentReason.BASELINE in assignment.reasons
        for plan in plans
        for assignment in plan.assignments
    )
    risk_added_assignments = sum(
        AssignmentReason.RISK_ADDED in assignment.reasons
        for plan in plans
        for assignment in plan.assignments
    )
    ambiguity_fallback_assignments = sum(
        AssignmentReason.AMBIGUITY_FALLBACK in assignment.reasons
        for plan in plans
        for assignment in plan.assignments
    )
    return ReviewCoveragePlan(
        tasks=tuple(updated_plans),
        baseline_assignments=baseline_assignments,
        risk_added_assignments=risk_added_assignments,
        ambiguity_fallback_assignments=ambiguity_fallback_assignments,
        react_candidate_tasks=len(candidate_task_ids),
        react_task_count=len(selected_react_tasks),
        react_assignment_count=len(upgraded),
        risk_upgraded_assignments=0 if force_react else len(upgraded),
        execution_override_assignments=(
            len(upgraded) if force_react else 0
        ),
        truncated_react_task_count=len(candidate_task_ids - selected_react_tasks),
        truncated_react_assignment_count=truncated_assignment_count,
        unclassified_tasks=sum(
            (priors.get(task_id) or TaskRiskPrior(task_id=task_id, coverage=RiskCoverage.UNCLASSIFIED)).coverage
            is RiskCoverage.UNCLASSIFIED
            for task_id in selection.selected_task_ids
        ),
        tasks_with_zero_assignments=sum(
            not plan.assignments for plan in updated_plans
        ),
    )


def ensure_review_coverage(
    tasks: list[ReviewTask],
    coverage: ReviewCoveragePlan | None,
    selection: TaskSelection,
) -> ReviewCoveragePlan:
    """补齐恢复/兼容状态中遗漏的 selected task，避免静默零审查。"""
    if coverage is None:
        return plan_review_coverage(
            tasks,
            {
                task_id: TaskRiskPrior(task_id=task_id, coverage=RiskCoverage.UNCLASSIFIED)
                for task_id in selection.selected_task_ids
            },
            selection,
            react_budget=0,
            tools_available=False,
        )
    covered = {plan.task_id for plan in coverage.tasks if plan.assignments}
    missing = [task_id for task_id in selection.selected_task_ids if task_id not in covered]
    if not missing:
        return coverage
    additions = plan_review_coverage(
        tasks,
        {
            task_id: TaskRiskPrior(task_id=task_id, coverage=RiskCoverage.UNCLASSIFIED)
            for task_id in missing
        },
        TaskSelection(selected_task_ids=missing),
        react_budget=0,
        tools_available=False,
    )
    missing_set = set(missing)
    merged_tasks = (
        tuple(plan for plan in coverage.tasks if plan.task_id not in missing_set)
        + additions.tasks
    )
    assignments = [a for plan in merged_tasks for a in plan.assignments]
    return coverage.model_copy(
        update={
            "tasks": merged_tasks,
            "baseline_assignments": sum(AssignmentReason.BASELINE in a.reasons for a in assignments),
            "risk_added_assignments": sum(AssignmentReason.RISK_ADDED in a.reasons for a in assignments),
            "ambiguity_fallback_assignments": sum(AssignmentReason.AMBIGUITY_FALLBACK in a.reasons for a in assignments),
            "react_task_count": len({plan.task_id for plan in merged_tasks for a in plan.assignments if a.tier is ReviewTier.REACT}),
            "react_assignment_count": sum(a.tier is ReviewTier.REACT for a in assignments),
            "risk_upgraded_assignments": sum(AssignmentReason.RISK_UPGRADED in a.reasons for a in assignments),
            "execution_override_assignments": sum(
                AssignmentReason.EXECUTION_OVERRIDE in a.reasons
                for a in assignments
            ),
            "truncated_react_assignment_count": max(
                0,
                coverage.truncated_react_assignment_count,
            ),
            "unclassified_tasks": coverage.unclassified_tasks + len(missing),
            "tasks_with_zero_assignments": sum(not plan.assignments for plan in merged_tasks),
        }
    )


def coverage_task_ids(
    reviewer_source_agent: str,
    coverage: ReviewCoveragePlan | None,
    selection: TaskSelection,
) -> tuple[str, ...]:
    """从覆盖计划读取一个 Reviewer 的 selected task，保持 selection 顺序。"""
    reviewer = _reviewer_kind(reviewer_source_agent)
    if reviewer is None or coverage is None:
        return ()
    selected = set(selection.selected_task_ids)
    by_id = {plan.task_id: plan for plan in coverage.tasks}
    return tuple(
        task_id
        for task_id in selection.selected_task_ids
        if task_id in selected
        and any(
            assignment.reviewer is reviewer
            for assignment in by_id.get(task_id, TaskReviewPlan(task_id=task_id)).assignments
        )
    )


def coverage_tiers(
    reviewer_source_agent: str,
    coverage: ReviewCoveragePlan | None,
    selection: TaskSelection,
) -> dict[str, str]:
    reviewer = _reviewer_kind(reviewer_source_agent)
    if reviewer is None or coverage is None:
        return {}
    by_id = {plan.task_id: plan for plan in coverage.tasks}
    return {
        task_id: next(
            assignment.tier.value
            for assignment in by_id[task_id].assignments
            if assignment.reviewer is reviewer
        )
        for task_id in selection.selected_task_ids
        if task_id in by_id
        and any(assignment.reviewer is reviewer for assignment in by_id[task_id].assignments)
    }
