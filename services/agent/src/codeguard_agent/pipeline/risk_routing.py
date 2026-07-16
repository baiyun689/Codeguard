"""Deterministic routing from risk profiles to reviewer task scopes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

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
        # 风险画像缺失是上游不变量破坏；保守地让三路发现者都审一次，
        # 由 decide_tier(None) 降级 Direct，避免静默漏审。
        if profile is None or reviewer in reviewers_for_profile(profile):
            routed.append(task_id)
    return tuple(routed)


def render_single_task_risk(task: ReviewTask, profile: RiskProfile) -> str:
    """渲染单个 task 的风险标签块(<task><risk_tags><risk_signals><patch>),
    供 Phase4 单 task 调用和 render_task_scope 共用,避免两处重复实现。"""
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
    max_score = max(profile.tag_scores.values(), default=0)
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
