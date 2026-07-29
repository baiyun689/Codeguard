"""确定性路由：RiskProfile → 发现者任务分派范围。

供单 task 调用和 render_task_scope 共用，避免两处重复实现。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from codeguard_agent.models.tasks import (
    AssignmentReason,
    ReviewCoveragePlan,
    ReviewTier,
    ReviewerAssignment,
    ReviewerKind,
    RiskCoverage,
    RiskHypothesis,
    RiskProfile,
    RiskSignal,
    RiskTag,
    ReviewTask,
    TaskReviewPlan,
    TaskRiskPrior,
    TaskSelection,
)
from codeguard_agent.pipeline.risk.rules.catalog import reviewers_for_tag

_REVIEWER_NAMES = {
    "ThreatModelAgent": "threat_model",
    "BehaviorAgent": "behavior",
    "MaintainabilityAgent": "maintainability",
}
_REVIEWER_ALIASES = {
    **{name: name for name in _REVIEWER_NAMES},
    **{source: name for name, source in _REVIEWER_NAMES.items()},
}

_REVIEWER_ORDER = (
    ReviewerKind.THREAT_MODEL,
    ReviewerKind.BEHAVIOR,
    ReviewerKind.MAINTAINABILITY,
)

_SOURCE_TO_KIND = {
    "ThreatModelAgent": ReviewerKind.THREAT_MODEL,
    "threat_model": ReviewerKind.THREAT_MODEL,
    "BehaviorAgent": ReviewerKind.BEHAVIOR,
    "behavior": ReviewerKind.BEHAVIOR,
    "MaintainabilityAgent": ReviewerKind.MAINTAINABILITY,
    "maintainability": ReviewerKind.MAINTAINABILITY,
}


def _canonical_reviewer(name: str) -> str:
    return _REVIEWER_ALIASES.get(name, name)


def _reviewer_kind(name: str) -> ReviewerKind | None:
    return _SOURCE_TO_KIND.get(name)


def _hypothesis_from_signals(
    tag: RiskTag,
    signals: list[RiskSignal],
    *,
    fallback_score: int | None = None,
) -> RiskHypothesis:
    """将旧 score 映射为先验，旧规则迁移完成前只在本 Module 内使用。"""
    if not signals:
        raise ValueError("at least one risk signal is required")
    score = fallback_score if fallback_score is not None else max(signal.score for signal in signals)
    source_kind: Literal["diff_text", "path", "symbol", "ast", "fallback"]
    strongest = max(signals, key=lambda item: (item.score, item.source, item.line or 0))
    if all(signal.source.startswith("path:") for signal in signals):
        source_kind = "path"
        confidence_cap = 0.60
    elif all(signal.source.startswith("fallback:") for signal in signals):
        source_kind = "fallback"
        confidence_cap = 0.45
    elif any(signal.source.startswith("text:") for signal in signals):
        source_kind = "diff_text"
        confidence_cap = 0.90
    else:
        source_kind = "symbol"
        confidence_cap = 0.70
    individual = []
    for signal in signals:
        effective_signal_score = (
            fallback_score
            if len(signals) == 1 and fallback_score is not None and fallback_score > signal.score
            else signal.score
        )
        signal_confidence = {1: 0.45, 2: 0.70, 3: 0.85}.get(
            min(effective_signal_score, 3), 0.85
        )
        if signal.source.startswith("path:"):
            signal_confidence = min(signal_confidence, 0.60)
        individual.append(signal_confidence)
    confidence = 1.0
    for signal_confidence in individual:
        confidence *= 1.0 - signal_confidence
    confidence = 1.0 - confidence
    return RiskHypothesis(
        tag=tag,
        match_confidence=min(confidence, confidence_cap),
        review_priority=min(max(score, 1), 3),
        source_kind=source_kind,
        source="+".join(sorted({signal.source for signal in signals})),
        reason="; ".join(sorted({signal.reason for signal in signals})),
        line=strongest.line,
    )


def build_risk_prior(task: ReviewTask, profile: RiskProfile | None) -> TaskRiskPrior:
    """从兼容 RiskProfile 构造 additive routing 使用的 TaskRiskPrior。"""
    if profile is None:
        return TaskRiskPrior(task_id=task.id, coverage=RiskCoverage.UNCLASSIFIED)

    concrete = [
        (tag, score)
        for tag, score in profile.tag_scores.items()
        if tag is not RiskTag.GENERAL_REVIEW and score > 0
    ]
    if not concrete:
        return TaskRiskPrior(task_id=task.id, coverage=RiskCoverage.UNCLASSIFIED)

    signals_by_tag: dict[RiskTag, list] = {}
    for signal in profile.signals:
        if signal.tag in dict(concrete) and signal.tag is not RiskTag.GENERAL_REVIEW:
            signals_by_tag.setdefault(signal.tag, []).append(signal)

    hypotheses: list[RiskHypothesis] = []
    for tag, score in concrete:
        signals = signals_by_tag.get(tag)
        if signals:
            hypotheses.append(_hypothesis_from_signals(tag, signals, fallback_score=score))
        else:
            synthetic = RiskSignal(
                tag=tag,
                score=score,
                source="legacy:profile",
                reason="由兼容 RiskProfile score 派生",
            )
            hypotheses.append(_hypothesis_from_signals(tag, [synthetic], fallback_score=score))

    hypotheses.sort(key=lambda item: (-item.match_confidence, -item.review_priority, item.tag.value))
    ambiguous = max((item.match_confidence for item in hypotheses), default=0.0) < 0.65
    if len(hypotheses) >= 2:
        first, second = hypotheses[0], hypotheses[1]
        first_reviewers = reviewers_for_tag(first.tag)
        second_reviewers = reviewers_for_tag(second.tag)
        if abs(first.match_confidence - second.match_confidence) < 0.10 and first_reviewers != second_reviewers:
            ambiguous = True
    coverage = RiskCoverage.AMBIGUOUS if ambiguous else RiskCoverage.CONFIDENT
    return TaskRiskPrior(task_id=task.id, hypotheses=tuple(hypotheses), coverage=coverage)


def build_risk_priors(
    tasks: list[ReviewTask], profiles: Mapping[str, RiskProfile]
) -> dict[str, TaskRiskPrior]:
    return {task.id: build_risk_prior(task, profiles.get(task.id)) for task in tasks}


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
                    and prior.coverage is RiskCoverage.CONFIDENT
                    and hypothesis.match_confidence >= 0.75
                    and hypothesis.review_priority >= 2
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
                            set(assignment.reasons) | {AssignmentReason.RISK_UPGRADED},
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
        risk_upgraded_assignments=len(upgraded),
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
    covered = {plan.task_id for plan in coverage.tasks}
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
    merged_tasks = coverage.tasks + additions.tasks
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


def reviewers_for_profile(profile: RiskProfile) -> frozenset[str]:
    """Derive the reviewer union from positive tag scores only."""
    reviewers: set[str] = set()
    for tag, score in profile.tag_scores.items():
        if score > 0:
            reviewers.update(reviewers_for_tag(tag))
    return frozenset(reviewers)


def routed_task_ids(
    reviewer_source_agent: str,
    tasks: list[ReviewTask],
    profiles: Mapping[str, RiskProfile],
    selection: TaskSelection,
) -> tuple[str, ...]:
    """Return selected task ids assigned to one reviewer in selection order."""
    reviewer = _canonical_reviewer(reviewer_source_agent)
    task_by_id = {task.id: task for task in tasks}
    routed: list[str] = []
    for task_id in selection.selected_task_ids:
        if task_id not in task_by_id:
            continue
        profile = profiles.get(task_id)
        # 风险画像缺失是上游不变量破坏；保守地让三路发现者都审一次，
        # 由 decide_tier(None) 降级 Direct，避免静默漏审。
        if profile is None or reviewer in reviewers_for_profile(profile):
            routed.append(task_id)
    return tuple(routed)


def render_single_task_risk(task: ReviewTask, profile: RiskProfile) -> str:
    """渲染单个 task 的风险标签块(<task><risk_tags><risk_signals><patch>),
    供单 task 调用和 render_task_scope 共用，避免两处重复实现。"""
    tags = sorted(tag.value for tag, score in profile.tag_scores.items() if score > 0)
    signals = [
        f"{signal.source}:{signal.reason}"
        for signal in profile.signals
        if signal.tag in profile.tag_scores and profile.tag_scores[signal.tag] > 0
    ]
    parts = [
        f'<task id="{task.id}" file="{task.file}">',
        f"<risk_tags>{','.join(tags)}</risk_tags>",
        f"<risk_signals>{'; '.join(signals)}</risk_signals>",
        "<patch>",
        task.patch,
        "</patch>",
        "</task>",
    ]
    return "\n".join(parts)


def render_task_scope(
    reviewer_source_agent: str,
    tasks: list[ReviewTask],
    profiles: Mapping[str, RiskProfile],
    selection: TaskSelection,
) -> str:
    """Render only this reviewer's selected tasks and their evidence."""
    reviewer = _canonical_reviewer(reviewer_source_agent)
    task_by_id = {task.id: task for task in tasks}
    parts = [f'<review_scope reviewer="{_REVIEWER_NAMES.get(reviewer, reviewer)}">']
    for task_id in routed_task_ids(reviewer_source_agent, tasks, profiles, selection):
        task = task_by_id[task_id]
        profile = profiles[task_id]
        parts.append(render_single_task_risk(task, profile))
    parts.append("</review_scope>")
    return "\n".join(parts)


def decide_tier(profile: RiskProfile | None) -> Literal["react", "direct"]:
    """按 task 的 RiskProfile 强度决定发现引擎:score>=2(含强信号)进 ReAct,
    否则(纯弱信号或 GENERAL_REVIEW)降级为无工具单次调用。

    分层理由见 spec:score=2 已涵盖控制流/数据流/资源生命周期/一致性类问题
    (如 RESOURCE_LIFECYCLE/TRANSACTION_ATOMICITY),这类问题往往需要工具核实,
    阈值定得比"只有 score=3"更保守，避免因分层误伤这类中危问题。
    """
    if profile is None:
        return "direct"
    max_score = max(
        (
            score
            for tag, score in profile.tag_scores.items()
            if tag is not RiskTag.GENERAL_REVIEW
        ),
        default=0,
    )
    return "react" if max_score >= 2 else "direct"


def plan_task_tiers(
    selected_task_ids: list[str],
    profiles: Mapping[str, RiskProfile],
    max_react_tasks: int,
    *,
    tools_available: bool,
) -> dict[str, Literal["react", "direct"]]:
    """按稳定风险顺序分配有限 ReAct 名额；所有 task 都保留 Direct 兜底。"""
    remaining = max_react_tasks if tools_available else 0
    tiers: dict[str, Literal["react", "direct"]] = {}
    for task_id in selected_task_ids:
        eligible = decide_tier(profiles.get(task_id)) == "react"
        use_react = eligible and remaining > 0
        tiers[task_id] = "react" if use_react else "direct"
        if use_react:
            remaining -= 1
    return tiers
