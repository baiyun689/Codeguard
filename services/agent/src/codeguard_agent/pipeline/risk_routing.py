"""Deterministic routing from risk profiles to reviewer task scopes."""

from __future__ import annotations

from collections.abc import Mapping

from codeguard_agent.models.tasks import RiskProfile, ReviewTask, TaskSelection
from codeguard_agent.pipeline.risk_rules.catalog import reviewers_for_tag

_REVIEWER_NAMES = {
    "ThreatModelAgent": "threat_model",
    "BehaviorAgent": "behavior",
    "MaintainabilityAgent": "maintainability",
}
_REVIEWER_ALIASES = {
    **{name: name for name in _REVIEWER_NAMES},
    **{source: name for name, source in _REVIEWER_NAMES.items()},
}


def _canonical_reviewer(name: str) -> str:
    return _REVIEWER_ALIASES.get(name, name)


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
        if profile is not None and reviewer in reviewers_for_profile(profile):
            routed.append(task_id)
    return tuple(routed)


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
        tags = sorted(
            tag.value for tag, score in profile.tag_scores.items() if score > 0
        )
        signals = [
            f"{signal.source}:{signal.reason}"
            for signal in profile.signals
            if signal.tag in profile.tag_scores and profile.tag_scores[signal.tag] > 0
        ]
        parts.extend(
            [
                f'<task id="{task.id}" file="{task.file}">',
                f"<risk_tags>{','.join(tags)}</risk_tags>",
                f"<risk_signals>{'; '.join(signals)}</risk_signals>",
                "<patch>",
                task.patch,
                "</patch>",
                "</task>",
            ]
        )
    parts.append("</review_scope>")
    return "\n".join(parts)
