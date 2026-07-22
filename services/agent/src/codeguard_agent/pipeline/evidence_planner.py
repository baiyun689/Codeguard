"""候选证据主题到 EvidenceRequest 的纯规划层。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from codeguard_agent.models.council import (
    CandidateIssue,
    EvidenceNote,
    EvidencePurpose,
    EvidenceRequest,
)
from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag, TaskContextBundle
from codeguard_agent.pipeline import task_prep
from codeguard_agent.pipeline.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.evidence_rules import (
    CandidateTagResolution,
    EvidenceStrategy,
    resolve_candidate_evidence_tag,
    strategies_for,
)


MAX_INITIAL_REQUESTS_PER_CANDIDATE = 4


@dataclass(frozen=True)
class CandidateDossier:
    """规划单个候选所需的只读快照，不进入 graph State。"""

    candidate: CandidateIssue
    task: ReviewTask
    risk_profile: RiskProfile | None
    context_bundle: TaskContextBundle | None
    requests: tuple[EvidenceRequest, ...]
    notes: tuple[EvidenceNote, ...]


@dataclass(frozen=True)
class CandidateBindingFailure:
    """无法安全绑定到唯一 task 的候选。"""

    candidate: CandidateIssue
    reason: str


@dataclass(frozen=True)
class DossierAssembly:
    """按候选稳定顺序组装的有效 dossier 与显式失败。"""

    dossiers: tuple[CandidateDossier, ...]
    failures: tuple[CandidateBindingFailure, ...]
    trace: tuple[tuple[str, str], ...]


@dataclass
class EvidencePlan:
    """本轮新增请求与可机器解析的规划 trace。"""

    requests: list[EvidenceRequest] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)


def _stable_json(detail: dict[str, object]) -> str:
    return json.dumps(detail, ensure_ascii=False, sort_keys=True)


def assemble_dossiers(
    candidates: Sequence[CandidateIssue],
    tasks: Sequence[ReviewTask],
    profiles: Mapping[str, RiskProfile | None],
    bundles: Mapping[str, TaskContextBundle],
    requests: Sequence[EvidenceRequest],
    notes: Sequence[EvidenceNote],
) -> DossierAssembly:
    """把 graph state 关联为候选级只读快照，并显式保留绑定失败。"""
    tasks_by_id: dict[str, list[ReviewTask]] = {}
    for task in tasks:
        tasks_by_id.setdefault(task.id, []).append(task)
    requests_by_candidate: dict[str, list[EvidenceRequest]] = {}
    for request in requests:
        requests_by_candidate.setdefault(request.candidate_id, []).append(request)
    notes_by_candidate: dict[str, list[EvidenceNote]] = {}
    for note in notes:
        notes_by_candidate.setdefault(note.candidate_id, []).append(note)

    dossiers: list[CandidateDossier] = []
    failures: list[CandidateBindingFailure] = []
    trace: list[tuple[str, str]] = []
    for candidate in candidates:
        matches = tasks_by_id.get(candidate.task_id, [])
        if len(matches) != 1:
            reason = "missing_task" if not matches else "ambiguous_task"
            failures.append(CandidateBindingFailure(candidate, reason))
            trace.append(
                (
                    "candidate_binding_failed",
                    _stable_json(
                        {
                            "candidate_id": candidate.id,
                            "task_id": candidate.task_id,
                            "reason": reason,
                        }
                    ),
                )
            )
            continue
        task = matches[0]
        if not task_prep.file_matches_task(candidate.file, task):
            reason = "file_mismatch"
            failures.append(CandidateBindingFailure(candidate, reason))
            trace.append(
                (
                    "candidate_binding_failed",
                    _stable_json(
                        {
                            "candidate_id": candidate.id,
                            "task_id": candidate.task_id,
                            "reason": reason,
                        }
                    ),
                )
            )
            continue
        dossiers.append(
            CandidateDossier(
                candidate=candidate,
                task=task,
                risk_profile=profiles.get(task.id),
                context_bundle=bundles.get(task.id),
                requests=tuple(requests_by_candidate.get(candidate.id, ())),
                notes=tuple(notes_by_candidate.get(candidate.id, ())),
            )
        )
    return DossierAssembly(tuple(dossiers), tuple(failures), tuple(trace))


def _trace(plan: EvidencePlan, event: str, detail: dict[str, object]) -> None:
    plan.trace.append((event, _stable_json(detail)))


def _valid_binding(dossier: CandidateDossier) -> bool:
    return (
        dossier.candidate.task_id == dossier.task.id
        and task_prep.file_matches_task(dossier.candidate.file, dossier.task)
    )


def _trace_invalid_binding(plan: EvidencePlan, dossier: CandidateDossier) -> None:
    _trace(
        plan,
        "evidence_plan_skipped",
        {
            "candidate_id": dossier.candidate.id,
            "task_id": dossier.task.id,
            "reason": "invalid_candidate_binding",
        },
    )


def _positive_task_tags(dossier: CandidateDossier) -> list[RiskTag]:
    if dossier.risk_profile is None:
        return []
    return sorted(
        (
            tag
            for tag, score in dossier.risk_profile.tag_scores.items()
            if score > 0
        ),
        key=lambda tag: tag.value,
    )


def _trace_resolution(
    plan: EvidencePlan,
    dossier: CandidateDossier,
    resolution: CandidateTagResolution,
) -> CandidateTagResolution:
    task_tags = _positive_task_tags(dossier)
    _trace(
        plan,
        "candidate_evidence_tag_resolved",
        {
            "candidate_id": dossier.candidate.id,
            "task_id": dossier.task.id,
            "tag": resolution.tag.value,
            "confidence": resolution.confidence,
            "source": resolution.source,
            "reason": resolution.reason,
            "task_tags": [tag.value for tag in task_tags],
            "matches_task_prior": resolution.tag in task_tags,
        },
    )
    return resolution


def _resolve_dossiers(
    dossiers: Sequence[CandidateDossier],
    *,
    classifier_llm: Any,
    structured_method: str,
) -> list[CandidateTagResolution]:
    outcomes = run_bounded_parallel(
        list(dossiers),
        lambda dossier: resolve_candidate_evidence_tag(
            dossier,
            classifier_llm,
            structured_method=structured_method,
        ),
    )
    return [
        outcome
        if outcome is not None
        else CandidateTagResolution(
            tag=RiskTag.GENERAL_REVIEW,
            confidence=0.5,
            source="general",
            reason="候选证据主题并发解析失败",
        )
        for outcome in outcomes
    ]


def _next_strategy(
    tag: RiskTag,
    purpose: EvidencePurpose,
    excluded_strategy_ids: set[str],
) -> EvidenceStrategy | None:
    return next(
        (
            strategy
            for strategy in strategies_for(tag, purpose)
            if strategy.id not in excluded_strategy_ids
        ),
        None,
    )


def _build_request(
    dossier: CandidateDossier,
    strategy: EvidenceStrategy,
) -> EvidenceRequest:
    tool_calls = strategy.build_tool_calls(dossier)
    preferred_tools: list[str] = list(
        dict.fromkeys(call.tool_name for call in tool_calls)
    )
    return EvidenceRequest(
        candidate_id=dossier.candidate.id,
        strategy_id=strategy.id,
        purpose=strategy.purpose,
        target=dossier.task.file,
        question=strategy.question_template,
        preferred_tools=preferred_tools,
    )


def _append_request(
    plan: EvidencePlan,
    dossier: CandidateDossier,
    strategy: EvidenceStrategy,
    *,
    reason: str,
) -> None:
    request = _build_request(dossier, strategy)
    plan.requests.append(request)
    _trace(
        plan,
        "evidence_planned",
        {
            "candidate_id": dossier.candidate.id,
            "task_id": dossier.task.id,
            "strategy_id": request.strategy_id,
            "purpose": request.purpose,
            "target": request.target,
            "preferred_tools": request.preferred_tools,
            "reason": reason,
        },
    )


def _append_cap_skip(
    plan: EvidencePlan,
    dossier: CandidateDossier,
    resolution: CandidateTagResolution,
    purpose: str,
) -> None:
    _trace(
        plan,
        "evidence_plan_skipped",
        {
            "candidate_id": dossier.candidate.id,
            "task_id": dossier.task.id,
            "tag": resolution.tag.value,
            "purpose": purpose,
            "reason": "candidate_request_cap",
        },
    )


def _trace_no_initial_strategy(
    plan: EvidencePlan,
    dossier: CandidateDossier,
    resolution: CandidateTagResolution,
    purpose: EvidencePurpose,
) -> None:
    _trace(
        plan,
        "evidence_plan_skipped",
        {
            "candidate_id": dossier.candidate.id,
            "tag": resolution.tag.value,
            "purpose": purpose,
            "reason": "no_available_strategy",
        },
    )


def _plan_initial(
    dossiers: Sequence[CandidateDossier],
    *,
    classifier_llm: Any,
    structured_method: str,
) -> EvidencePlan:
    plan = EvidencePlan()
    resolved: list[
        tuple[CandidateDossier, CandidateTagResolution, set[str]]
    ] = []
    request_counts: dict[str, int] = {}
    valid_dossiers: list[CandidateDossier] = []
    for dossier in dossiers:
        if not _valid_binding(dossier):
            _trace_invalid_binding(plan, dossier)
            continue
        valid_dossiers.append(dossier)

    resolutions = _resolve_dossiers(
        valid_dossiers,
        classifier_llm=classifier_llm,
        structured_method=structured_method,
    )
    for dossier, resolution in zip(valid_dossiers, resolutions, strict=True):
        _trace_resolution(plan, dossier, resolution)
        resolved.append(
            (
                dossier,
                resolution,
                {request.strategy_id for request in dossier.requests},
            )
        )
        request_counts[dossier.candidate.id] = 0

    # All unqueued counter strategies in priority order (includes upstream).
    for dossier, resolution, excluded in resolved:
        for strategy in strategies_for(resolution.tag, "counter"):
            if strategy.id in excluded:
                continue
            if request_counts[dossier.candidate.id] >= MAX_INITIAL_REQUESTS_PER_CANDIDATE:
                _append_cap_skip(plan, dossier, resolution, "counter")
                break
            _append_request(
                plan, dossier, strategy, reason="initial_counter",
            )
            excluded.add(strategy.id)
            request_counts[dossier.candidate.id] += 1

    # One support strategy (mandatory).
    for dossier, resolution, excluded in resolved:
        if request_counts[dossier.candidate.id] >= MAX_INITIAL_REQUESTS_PER_CANDIDATE:
            _append_cap_skip(plan, dossier, resolution, "support")
            continue
        next_strategy = _next_strategy(resolution.tag, "support", excluded)
        if next_strategy is None:
            _trace_no_initial_strategy(plan, dossier, resolution, "support")
            continue
        _append_request(
            plan, dossier, next_strategy, reason="initial_support",
        )
        request_counts[dossier.candidate.id] += 1

    # One severity strategy (mandatory).
    for dossier, resolution, excluded in resolved:
        if request_counts[dossier.candidate.id] >= MAX_INITIAL_REQUESTS_PER_CANDIDATE:
            _append_cap_skip(plan, dossier, resolution, "severity")
            continue
        next_strategy = _next_strategy(resolution.tag, "severity", excluded)
        if next_strategy is None:
            _trace_no_initial_strategy(plan, dossier, resolution, "severity")
            continue
        _append_request(
            plan, dossier, next_strategy, reason="initial_severity",
        )
        request_counts[dossier.candidate.id] += 1

    return plan


def plan_evidence(
    dossiers: Sequence[CandidateDossier],
    *,
    classifier_llm: Any,
    structured_method: str,
) -> EvidencePlan:
    """One-pass complete evidence plan: all counter + support + severity."""
    return _plan_initial(
        dossiers,
        classifier_llm=classifier_llm,
        structured_method=structured_method,
    )


__all__ = [
    "CandidateBindingFailure",
    "CandidateDossier",
    "DossierAssembly",
    "EvidencePlan",
    "MAX_INITIAL_REQUESTS_PER_CANDIDATE",
    "assemble_dossiers",
    "plan_evidence",
]
