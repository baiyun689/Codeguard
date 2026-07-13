"""候选证据主题到 EvidenceRequest 的纯规划层。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from codeguard_agent.models.council import (
    CandidateIssue,
    EvidenceNote,
    EvidencePurpose,
    EvidenceRequest,
    Verdict,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag, TaskContextBundle
from codeguard_agent.pipeline import task_prep
from codeguard_agent.pipeline.evidence_rules import (
    CandidateTagResolution,
    EvidenceStrategy,
    resolve_candidate_evidence_tag,
    strategies_for,
)
from codeguard_agent.pipeline.risk_routing import decide_tier


MAX_INITIAL_REQUESTS_PER_CANDIDATE = 2
MAX_FOLLOWUP_REQUESTS_PER_CANDIDATE = 1


@dataclass(frozen=True)
class CandidateDossier:
    """规划单个候选所需的只读快照，不进入 graph State。"""

    candidate: CandidateIssue
    task: ReviewTask
    risk_profile: RiskProfile | None
    context_bundle: TaskContextBundle | None
    requests: tuple[EvidenceRequest, ...]
    notes: tuple[EvidenceNote, ...]
    latest_verdict: Verdict | None


@dataclass
class EvidencePlan:
    """本轮新增请求与可机器解析的规划 trace。"""

    requests: list[EvidenceRequest] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)


def _stable_json(detail: dict[str, object]) -> str:
    return json.dumps(detail, ensure_ascii=False, sort_keys=True)


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


def _resolve_and_trace(
    plan: EvidencePlan,
    dossier: CandidateDossier,
    classifier_llm: Any,
    structured_method: str,
) -> CandidateTagResolution:
    resolution = resolve_candidate_evidence_tag(
        dossier,
        classifier_llm,
        structured_method=structured_method,
    )
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
    evidence_round: int,
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
            "evidence_round": evidence_round,
            "reason": reason,
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


def _needs_initial_support(dossier: CandidateDossier) -> bool:
    return (
        dossier.candidate.severity_proposal is Severity.CRITICAL
        or decide_tier(dossier.risk_profile) == "react"
        or dossier.candidate.confidence < 0.9
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
    for dossier in dossiers:
        if not _valid_binding(dossier):
            _trace_invalid_binding(plan, dossier)
            continue
        resolution = _resolve_and_trace(
            plan,
            dossier,
            classifier_llm,
            structured_method,
        )
        resolved.append(
            (
                dossier,
                resolution,
                {request.strategy_id for request in dossier.requests},
            )
        )
        request_counts[dossier.candidate.id] = 0

    for dossier, resolution, excluded in resolved:
        if request_counts[dossier.candidate.id] >= MAX_INITIAL_REQUESTS_PER_CANDIDATE:
            continue
        strategy = _next_strategy(resolution.tag, "counter", excluded)
        if strategy is None:
            _trace_no_initial_strategy(plan, dossier, resolution, "counter")
            continue
        _append_request(
            plan,
            dossier,
            strategy,
            evidence_round=0,
            reason="initial_counter",
        )
        excluded.add(strategy.id)
        request_counts[dossier.candidate.id] += 1

    for dossier, resolution, excluded in resolved:
        if (
            request_counts[dossier.candidate.id]
            >= MAX_INITIAL_REQUESTS_PER_CANDIDATE
            or not _needs_initial_support(dossier)
        ):
            continue
        strategy = _next_strategy(resolution.tag, "support", excluded)
        if strategy is None:
            _trace_no_initial_strategy(plan, dossier, resolution, "support")
            continue
        _append_request(
            plan,
            dossier,
            strategy,
            evidence_round=0,
            reason="initial_support_gate",
        )
        request_counts[dossier.candidate.id] += 1

    return plan


def _plan_followup(
    dossiers: Sequence[CandidateDossier],
    *,
    evidence_round: int,
    classifier_llm: Any,
    structured_method: str,
) -> EvidencePlan:
    plan = EvidencePlan()
    for dossier in dossiers:
        request_count = 0
        verdict = dossier.latest_verdict
        if verdict is None or verdict.action != "needs_more_evidence":
            continue
        if not _valid_binding(dossier):
            _trace_invalid_binding(plan, dossier)
            continue
        purpose = verdict.requested_purpose
        if purpose is None:
            _trace(
                plan,
                "evidence_plan_invalid_verdict",
                {
                    "candidate_id": dossier.candidate.id,
                    "task_id": dossier.task.id,
                    "evidence_round": evidence_round,
                    "reason": "requested_purpose_missing",
                },
            )
            continue
        resolution = _resolve_and_trace(
            plan,
            dossier,
            classifier_llm,
            structured_method,
        )
        excluded = {request.strategy_id for request in dossier.requests}
        strategy = _next_strategy(resolution.tag, purpose, excluded)
        if strategy is None:
            _trace(
                plan,
                "evidence_plan_exhausted",
                {
                    "candidate_id": dossier.candidate.id,
                    "tag": resolution.tag.value,
                    "purpose": purpose,
                    "evidence_round": evidence_round,
                    "reason": "no_remaining_strategy",
                },
            )
            continue
        if request_count >= MAX_FOLLOWUP_REQUESTS_PER_CANDIDATE:
            continue
        _append_request(
            plan,
            dossier,
            strategy,
            evidence_round=evidence_round,
            reason="followup_requested_purpose",
        )
        request_count += 1
    return plan


def plan_evidence(
    dossiers: Sequence[CandidateDossier],
    *,
    evidence_round: int,
    classifier_llm: Any,
    structured_method: str,
) -> EvidencePlan:
    """规划本轮 EvidenceRequest，不执行策略中的潜在工具调用。"""
    if evidence_round == 0:
        return _plan_initial(
            dossiers,
            classifier_llm=classifier_llm,
            structured_method=structured_method,
        )
    return _plan_followup(
        dossiers,
        evidence_round=evidence_round,
        classifier_llm=classifier_llm,
        structured_method=structured_method,
    )


__all__ = [
    "CandidateDossier",
    "EvidencePlan",
    "MAX_FOLLOWUP_REQUESTS_PER_CANDIDATE",
    "MAX_INITIAL_REQUESTS_PER_CANDIDATE",
    "plan_evidence",
]
