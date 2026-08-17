"""ReviewCouncil 过程指标的计算入口(Evidence Ledger 定稿)。

从候选映射、裁决、验证结果与 Artifact 账本派生过程指标:
- Artifact 账本:按 source_kind 与 capture_mode 统计;
- 候选证据画像:patch-only / context-backed / tool-backed / ungrounded;
- 引用与重放:从 CandidateVerification 与 trace 事件统计;
- 支持覆盖:final_issue_supported_count = keep 且引用 ≥1 支持事实的 survivor 数。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from codeguard_agent.models.council import (
    CandidateIssue,
    CouncilRunStats,
    CouncilTrace,
    Verdict,
)
from codeguard_agent.models.evidence import (
    EvidenceArtifact,
    EvidenceCaptureMode,
    EvidenceSourceKind,
    EvidenceValidationStatus,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.pipeline.evidence.planner import DossierAssembly
from codeguard_agent.pipeline.council.dedup import CandidateDedupStats


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _count_event(trace: Sequence[CouncilTrace], event: str) -> int:
    return sum(item.event == event for item in trace)


def compute_council_run_stats(
    candidates: Sequence[CandidateIssue],
    assembly: DossierAssembly,
    verdicts: Sequence[Verdict],
    final_candidate_ids: Sequence[str],
    truncated_candidates: int,
    council_trace: Sequence[CouncilTrace],
    candidate_dedup_stats: Mapping[str, int] | CandidateDedupStats | None = None,
    artifacts: Mapping[str, EvidenceArtifact] | None = None,
    verifications: Mapping[str, Any] | None = None,
) -> CouncilRunStats:
    """从候选映射、裁决、验证结果与 Artifact 账本派生审查过程指标。"""
    final_ids = set(final_candidate_ids)
    candidate_count = len(candidates)
    by_agent: dict[str, int] = {}
    for candidate in candidates:
        by_agent[candidate.source_agent] = by_agent.get(candidate.source_agent, 0) + 1

    # ── Evidence Ledger:Artifact 账本 ──
    artifact_items = list((artifacts or {}).values())
    patch_count = sum(
        item.source_kind is EvidenceSourceKind.TASK_PATCH for item in artifact_items
    )
    context_count = sum(
        item.source_kind is EvidenceSourceKind.PREFETCHED_CONTEXT
        for item in artifact_items
    )
    tool_count = sum(
        item.source_kind is EvidenceSourceKind.TOOL_CALL for item in artifact_items
    )
    reused_count = sum(
        item.capture_mode is EvidenceCaptureMode.REUSED for item in artifact_items
    )

    # ── 候选证据画像与引用统计 ──
    patch_only = 0
    context_backed = 0
    tool_backed = 0
    ungrounded = 0
    refs_selected = 0
    refs_valid = 0
    refs_limited = 0
    refs_invalid = 0
    for verification in (verifications or {}).values():
        kinds = set(verification.source_kinds or ())
        if str(verification.grounding_status) == "ungrounded":
            ungrounded += 1
        if EvidenceSourceKind.TOOL_CALL in kinds:
            tool_backed += 1
        elif EvidenceSourceKind.PREFETCHED_CONTEXT in kinds:
            context_backed += 1
        else:
            patch_only += 1
        for item in verification.valid_evidence or []:
            refs_selected += 1
            if item.validation_status in {
                EvidenceValidationStatus.VALID,
                EvidenceValidationStatus.REPLAY_CONFIRMED,
            }:
                refs_valid += 1
            else:
                refs_limited += 1
        refs_invalid += len(verification.invalid_references or [])

    # ── 重放与 Judge 批调用(trace 事件) ──
    replay_requested = _count_event(council_trace, "evidence_replay_requested")
    replay_confirmed = _count_event(council_trace, "evidence_replay_completed")
    replay_failed = _count_event(council_trace, "evidence_replay_failed")
    judge_batch_calls = _count_event(council_trace, "evidence_judge_batch_started")

    # ── 裁决 ──
    severity_defaulted = sum(
        verdict.reason_code == "verification_failed" for verdict in verdicts
    )
    judge_no_support_drop = sum(
        verdict.reason_code == "no_supporting_evidence" for verdict in verdicts
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
    final_issue_supported = sum(
        verdict.candidate_id in final_ids and verdict.supported
        for verdict in verdicts
    )
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
    react_degraded_recursion_count = _count_event(
        council_trace, "react_degraded_recursion"
    )
    react_degraded_empty_count = _count_event(council_trace, "react_degraded_empty")
    direct_tier_task_count = _count_event(council_trace, "tier_direct")
    discoverer_failed_count = _count_event(council_trace, "discover_failed")
    task_review_failed_count = _count_event(council_trace, "task_review_failed")
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
        final_issue_supported_count=final_issue_supported,
        final_issue_support_coverage=_ratio(final_issue_supported, final_issue_count),
        artifact_count=len(artifact_items),
        patch_artifact_count=patch_count,
        context_artifact_count=context_count,
        tool_artifact_count=tool_count,
        reused_artifact_count=reused_count,
        candidate_patch_only_count=patch_only,
        candidate_context_backed_count=context_backed,
        candidate_tool_backed_count=tool_backed,
        candidate_ungrounded_count=ungrounded,
        selected_reference_count=refs_selected,
        valid_reference_count=refs_valid,
        limited_reference_count=refs_limited,
        invalid_reference_count=refs_invalid,
        replay_requested_count=replay_requested,
        replay_confirmed_count=replay_confirmed,
        replay_failed_count=replay_failed,
        judge_batch_call_count=judge_batch_calls,
        judge_failed_candidate_count=severity_defaulted,
        judge_no_support_drop_count=judge_no_support_drop,
        react_degraded_recursion_count=react_degraded_recursion_count,
        react_degraded_empty_count=react_degraded_empty_count,
        direct_tier_task_count=direct_tier_task_count,
        discoverer_failed_count=discoverer_failed_count,
        task_review_failed_count=task_review_failed_count,
        judge_synthesis_failed_count=severity_defaulted,
    )


__all__ = ["compute_council_run_stats"]
