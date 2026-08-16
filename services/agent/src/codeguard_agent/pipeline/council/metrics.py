"""ReviewCouncil 证据链过程指标的计算入口(ADR-046 定稿)。

从稳定候选映射、事实/关系与裁决派生过程指标:
- 关系推导字段(direct counter / all insufficient / fact coverage)全部从 relations 出;
- 事实总数与重放状态统计(verified/unverified/failed)从 facts 出;
- critical_candidate_count = keep 且 resolved_severity 为 CRITICAL 的 verdict 数。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from codeguard_agent.models.council import (
    CandidateFact,
    CandidateIssue,
    CouncilRunStats,
    CouncilTrace,
    FactRelation,
    Verdict,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.pipeline.evidence.planner import DossierAssembly
from codeguard_agent.pipeline.council.dedup import CandidateDedupStats


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def compute_council_run_stats(
    candidates: Sequence[CandidateIssue],
    assembly: DossierAssembly,
    verdicts: Sequence[Verdict],
    final_candidate_ids: Sequence[str],
    facts_by_candidate: Mapping[str, Sequence[CandidateFact]],
    relations_by_candidate: Mapping[str, Sequence[FactRelation]],
    truncated_candidates: int,
    council_trace: Sequence[CouncilTrace],
    candidate_dedup_stats: Mapping[str, int] | CandidateDedupStats | None = None,
) -> CouncilRunStats:
    """从稳定候选映射、事实/关系与裁决派生审查过程指标。"""
    final_ids = set(final_candidate_ids)
    fact_count = sum(len(facts) for facts in facts_by_candidate.values())
    replay_verified_count = sum(
        fact.replay_status == "verified"
        for facts in facts_by_candidate.values()
        for fact in facts
    )
    replay_unverified_count = sum(
        fact.replay_status == "unverified"
        for facts in facts_by_candidate.values()
        for fact in facts
    )
    replay_failed_count = sum(
        fact.replay_status == "failed"
        for facts in facts_by_candidate.values()
        for fact in facts
    )
    relations_by_cid: dict[str, Sequence[FactRelation]] = {
        cid: tuple(rels) for cid, rels in relations_by_candidate.items()
    }
    direct_counter_ids = {
        cid
        for cid, rels in relations_by_cid.items()
        if any(
            rel.relation == "contradicts" and rel.strength == "direct"
            for rel in rels
        )
    }
    all_insufficient_ids = {
        cid
        for cid, rels in relations_by_cid.items()
        if rels and all(rel.relation == "insufficient" for rel in rels)
    }
    fact_covered = sum(
        any(
            rel.relation != "insufficient"
            for rel in relations_by_cid.get(dossier.candidate.id, ())
        )
        for dossier in assembly.dossiers
        if dossier.candidate.id in final_ids
    )
    actual_tool_calls = sum(
        trace.node == "evidence_verifier" and trace.event == "evidence_tool_called"
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

    severity_defaulted = sum(
        verdict.reason_code == "severity_evidence_incomplete"
        for verdict in verdicts
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
    dedup = candidate_dedup_stats or {}
    raw_candidate_count = dedup.get("raw_candidate_count", candidate_count)
    logical_candidate_count = dedup.get(
        "logical_candidate_count",
        candidate_count,
    )
    candidate_grouped_member_count = dedup.get(
        "grouped_member_count",
        max(0, raw_candidate_count - logical_candidate_count),
    )
    candidate_dedup_removed_count = dedup.get(
        "removed_count",
        max(0, raw_candidate_count - candidate_count),
    )
    candidate_dedup_llm_calls = dedup.get("llm_call_count", 0)
    candidate_dedup_block_failure_count = dedup.get("block_failure_count", 0)

    # ── 降级指标：从 council_trace 事件中计数 ──
    react_degraded_recursion_count = sum(
        trace.event == "react_degraded_recursion" for trace in council_trace
    )
    react_degraded_empty_count = sum(
        trace.event == "react_degraded_empty" for trace in council_trace
    )
    direct_tier_task_count = sum(
        trace.event == "tier_direct" for trace in council_trace
    )
    discoverer_failed_count = sum(
        trace.event == "discover_failed" for trace in council_trace
    )
    task_review_failed_count = sum(
        trace.event == "task_review_failed" for trace in council_trace
    )
    evidence_plan_skipped_count = sum(
        trace.event == "evidence_plan_skipped" for trace in council_trace
    )
    return CouncilRunStats(
        candidate_count=candidate_count,
        candidate_count_by_agent=by_agent,
        truncated_candidates=truncated_candidates,
        raw_candidate_count=raw_candidate_count,
        logical_candidate_count=logical_candidate_count,
        candidate_grouped_member_count=candidate_grouped_member_count,
        candidate_dedup_removed_count=candidate_dedup_removed_count,
        candidate_dedup_llm_calls=candidate_dedup_llm_calls,
        candidate_dedup_block_failure_count=candidate_dedup_block_failure_count,
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
        critical_candidate_count=sum(
            verdict.action == "keep" and verdict.resolved_severity is Severity.CRITICAL
            for verdict in verdicts
        ),
        severity_transitions=severity_transitions,
        final_issue_count=final_issue_count,
        final_issue_fact_covered_count=fact_covered,
        final_issue_fact_coverage=_ratio(fact_covered, final_issue_count),
        actual_evidence_tool_calls=actual_tool_calls,
        average_evidence_tool_calls=(
            actual_tool_calls / candidate_count if candidate_count else 0.0
        ),
        react_degraded_recursion_count=react_degraded_recursion_count,
        react_degraded_empty_count=react_degraded_empty_count,
        direct_tier_task_count=direct_tier_task_count,
        discoverer_failed_count=discoverer_failed_count,
        task_review_failed_count=task_review_failed_count,
        judge_synthesis_failed_count=severity_defaulted,
        evidence_plan_skipped_count=evidence_plan_skipped_count,
        fact_count=fact_count,
        replay_verified_count=replay_verified_count,
        replay_unverified_count=replay_unverified_count,
        replay_failed_count=replay_failed_count,
    )


__all__ = ["compute_council_run_stats"]
