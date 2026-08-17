"""ReviewCouncil 过程指标的计算入口。

Evidence Ledger 切换后,指标从候选映射、裁决与 trace 事件派生:
- 旧 facts/relations 推导字段(fact_count/replay_*/chain_used/recipe_fallback)
  暂置零,由后续"指标新旧替换"工作项整体迁移为 artifact/ref/judge 语义;
- 裁决、严重度转移、降级计数保持原口径。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from codeguard_agent.models.council import (
    CandidateIssue,
    CouncilRunStats,
    CouncilTrace,
    Verdict,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.pipeline.evidence.planner import DossierAssembly
from codeguard_agent.pipeline.council.dedup import CandidateDedupStats


def compute_council_run_stats(
    candidates: Sequence[CandidateIssue],
    assembly: DossierAssembly,
    verdicts: Sequence[Verdict],
    final_candidate_ids: Sequence[str],
    truncated_candidates: int,
    council_trace: Sequence[CouncilTrace],
    candidate_dedup_stats: Mapping[str, int] | CandidateDedupStats | None = None,
) -> CouncilRunStats:
    """从候选映射、裁决与 trace 派生审查过程指标。"""
    candidate_count = len(candidates)
    by_agent: dict[str, int] = {}
    for candidate in candidates:
        by_agent[candidate.source_agent] = by_agent.get(candidate.source_agent, 0) + 1

    severity_defaulted = sum(
        verdict.reason_code == "verification_failed" for verdict in verdicts
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

    # ── 降级指标:从 council_trace 事件中计数 ──
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
        critical_candidate_count=sum(
            verdict.action == "keep" and verdict.resolved_severity is Severity.CRITICAL
            for verdict in verdicts
        ),
        severity_transitions=severity_transitions,
        final_issue_count=final_issue_count,
        react_degraded_recursion_count=react_degraded_recursion_count,
        react_degraded_empty_count=react_degraded_empty_count,
        direct_tier_task_count=direct_tier_task_count,
        discoverer_failed_count=discoverer_failed_count,
        task_review_failed_count=task_review_failed_count,
        judge_synthesis_failed_count=severity_defaulted,
    )


__all__ = ["compute_council_run_stats"]
