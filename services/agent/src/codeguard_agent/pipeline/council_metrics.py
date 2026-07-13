"""ReviewCouncil 证据链过程指标的唯一计算入口。"""

from __future__ import annotations

from collections.abc import Sequence

from codeguard_agent.models.council import (
    CandidateIssue,
    CouncilRunStats,
    CouncilTrace,
    EvidenceFinding,
    Verdict,
)
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_agent import request_strategy_mismatch
from codeguard_agent.pipeline.evidence_planner import CandidateDossier, DossierAssembly
from codeguard_agent.pipeline.evidence_rules import strategies_for


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _valid_findings(dossier: CandidateDossier) -> list[EvidenceFinding]:
    valid_request_ids = {
        request.id
        for request in dossier.requests
        if request_strategy_mismatch(request, dossier) is None
    }
    return [
        finding
        for note in dossier.notes
        if note.candidate_id == dossier.candidate.id
        and note.request_id in valid_request_ids
        for finding in note.findings
    ]


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
    evidence_rounds: int,
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
            request.purpose == "counter"
            and note.request_id == request.id
            and finding.strength == "direct"
            and finding.relation == "contradicts"
            for request in dossier.requests
            if request_strategy_mismatch(request, dossier) is None
            for note in dossier.notes
            if note.candidate_id == dossier.candidate.id
            for finding in note.findings
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
    final_issue_count = len(final_candidate_ids)
    return CouncilRunStats(
        candidate_count=candidate_count,
        candidate_count_by_agent=by_agent,
        evidence_request_count=evidence_request_count,
        truncated_candidates=truncated_candidates,
        evidence_rounds=evidence_rounds,
        verdict_count=len(verdicts),
        removed_by_judge=sum(verdict.action == "drop" for verdict in verdicts),
        removed_by_aggregation=sum(
            verdict.action == "merge" for verdict in verdicts
        ),
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
