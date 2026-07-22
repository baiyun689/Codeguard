"""ReviewCouncil 证据链过程指标的唯一计算入口。"""

from __future__ import annotations

import json
from collections.abc import Sequence

from codeguard_agent.models.council import (
    CandidateIssue,
    CouncilRunStats,
    CouncilTrace,
    EvidenceFinding,
    Verdict,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_agent import bound_evidence, request_strategy_mismatch
from codeguard_agent.pipeline.evidence_planner import CandidateDossier, DossierAssembly
from codeguard_agent.pipeline.evidence_rules import strategies_for


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _valid_findings(dossier: CandidateDossier) -> list[EvidenceFinding]:
    return [item.finding for item in bound_evidence(dossier)]


def _has_valid_request(dossier: CandidateDossier) -> bool:
    return any(
        request_strategy_mismatch(request, dossier) is None
        for request in dossier.requests
    )


def _registry_coverage() -> tuple[int, int]:
    required_purposes = {"counter", "support", "severity"}
    covered = sum(
        {
            strategy.purpose
            for strategy in strategies_for(tag)
        }
        >= required_purposes
        for tag in RiskTag
    )
    return covered, len(RiskTag)


def compute_council_run_stats(
    *,
    candidates: Sequence[CandidateIssue],
    assembly: DossierAssembly,
    verdicts: Sequence[Verdict],
    final_candidate_ids: Sequence[str],
    evidence_request_count: int,
    truncated_candidates: int,
    council_trace: Sequence[CouncilTrace],
) -> CouncilRunStats:
    """从稳定候选映射和结构化证据计算 Phase 5 过程指标。"""
    final_ids = set(final_candidate_ids)
    findings_by_candidate = {
        dossier.candidate.id: _valid_findings(dossier)
        for dossier in assembly.dossiers
    }
    direct_counter_ids = {
        dossier.candidate.id
        for dossier in assembly.dossiers
        if any(
            item.request.purpose == "counter"
            and item.finding.strength == "direct"
            and item.finding.relation == "contradicts"
            for item in bound_evidence(dossier)
        )
    }
    all_insufficient_ids = {
        candidate_id
        for candidate_id, findings in findings_by_candidate.items()
        if findings and all(finding.relation == "insufficient" for finding in findings)
    }
    final_dossiers = [
        dossier
        for dossier in assembly.dossiers
        if dossier.candidate.id in final_ids
    ]

    strategy_covered = sum(_has_valid_request(dossier) for dossier in final_dossiers)
    fact_covered = sum(
        any(
            finding.relation != "insufficient"
            for finding in findings_by_candidate[dossier.candidate.id]
        )
        for dossier in final_dossiers
    )
    registry_covered, registry_total = _registry_coverage()
    actual_tool_calls = sum(
        trace.node == "evidence_agent" and trace.event == "evidence_tool_called"
        for trace in council_trace
    )
    candidate_count = len(candidates)
    by_agent: dict[str, int] = {}
    for candidate in candidates:
        by_agent[candidate.source_agent] = by_agent.get(candidate.source_agent, 0) + 1

    direct_retained = len(direct_counter_ids & final_ids)
    insufficient_retained = len(all_insufficient_ids & final_ids)
    no_support_ids = {
        verdict.candidate_id
        for verdict in verdicts
        if verdict.reason_code == "no_supporting_evidence"
    }
    no_support_retained = len(no_support_ids & final_ids)

    severity_events: list[dict[str, object]] = []
    for trace in council_trace:
        if trace.node != "council_judge" or trace.event != "severity_resolved":
            continue
        try:
            detail = json.loads(trace.detail)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(detail, dict):
            severity_events.append(detail)

    severity_defaulted = sum(
        verdict.reason_code == "severity_evidence_incomplete"
        for verdict in verdicts
    )
    critical_policy_matched = sum(
        str(event.get("matched_rule", "")).endswith(".critical")
        for event in severity_events
    )
    critical_missing_factors = sum(
        len(missing)
        for event in severity_events
        if isinstance((missing := event.get("missing_critical_factors", [])), list)
    )
    proposals = {candidate.id: candidate.severity_proposal for candidate in candidates}
    severity_transitions: dict[str, int] = {}
    for verdict in verdicts:
        proposed = proposals.get(verdict.candidate_id)
        resolved = verdict.resolved_severity
        if verdict.action != "keep" or proposed is None or resolved is None:
            continue
        key = f"{proposed.value}->{resolved.value}"
        severity_transitions[key] = severity_transitions.get(key, 0) + 1

    final_issue_count = len(final_candidate_ids)
    return CouncilRunStats(
        candidate_count=candidate_count,
        candidate_count_by_agent=by_agent,
        evidence_request_count=evidence_request_count,
        truncated_candidates=truncated_candidates,
        verdict_count=len(verdicts),
        removed_by_judge=sum(verdict.action == "drop" for verdict in verdicts),
        no_support_candidate_count=len(no_support_ids),
        no_support_retained_count=no_support_retained,
        direct_counter_candidate_count=len(direct_counter_ids),
        direct_counter_retained_count=direct_retained,
        direct_counter_retained_rate=_ratio(
            direct_retained,
            len(direct_counter_ids),
        ),
        all_insufficient_candidate_count=len(all_insufficient_ids),
        all_insufficient_retained_count=insufficient_retained,
        all_insufficient_retained_rate=_ratio(
            insufficient_retained,
            len(all_insufficient_ids),
        ),
        severity_defaulted_count=severity_defaulted,
        critical_candidate_count=sum(
            verdict.action == "keep" and verdict.resolved_severity is Severity.CRITICAL
            for verdict in verdicts
        ),
        critical_policy_matched_count=critical_policy_matched,
        critical_missing_factor_count=critical_missing_factors,
        severity_transitions=severity_transitions,
        final_issue_count=final_issue_count,
        final_issue_strategy_covered_count=strategy_covered,
        final_issue_strategy_coverage=_ratio(strategy_covered, final_issue_count),
        final_issue_fact_covered_count=fact_covered,
        final_issue_fact_coverage=_ratio(fact_covered, final_issue_count),
        registry_risk_tag_covered_count=registry_covered,
        registry_risk_tag_total=registry_total,
        registry_risk_tag_coverage=_ratio(registry_covered, registry_total),
        actual_evidence_tool_calls=actual_tool_calls,
        average_evidence_tool_calls=(
            actual_tool_calls / candidate_count if candidate_count else 0.0
        ),
    )


__all__ = ["compute_council_run_stats"]
