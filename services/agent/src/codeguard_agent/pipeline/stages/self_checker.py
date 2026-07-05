"""ADR-032 SelfChecker:包装旧聚合与误报过滤能力。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from codeguard_agent.models.council import CandidateIssue, Challenge, CouncilRunStats
from codeguard_agent.models.schemas import Issue
from codeguard_agent.pipeline.stages.aggregation import AggregationStage
from codeguard_agent.pipeline.stages.base import PipelineContext, PipelineStage
from codeguard_agent.pipeline.stages.fp_filter import FalsePositiveFilterStage

logger = logging.getLogger("codeguard")


@dataclass
class SelfCheckerOutcome:
    issues: list[Issue]
    stats: CouncilRunStats
    filter_stats: object | None


class SelfCheckerStage(PipelineStage):
    """最终裁决节点。

    第一版先复用 AggregationStage 与 FalsePositiveFilterStage,同时把移除来源归入
    CouncilRunStats。
    """

    def __init__(self, enable_fp_llm_verification: bool = False) -> None:
        self._enable_fp_llm_verification = enable_fp_llm_verification

    @property
    def name(self) -> str:
        return "self_checker"

    def execute(self, context: PipelineContext) -> PipelineContext:
        AggregationStage().execute(context)
        FalsePositiveFilterStage(
            enable_llm_verification=self._enable_fp_llm_verification
        ).execute(context)
        return context

    def decide(
        self,
        context: PipelineContext,
        *,
        candidates: list[CandidateIssue],
        challenges: list[Challenge],
        evidence_rounds: int,
        evidence_request_count: int = 0,
        truncated_candidates: int = 0,
        truncated_evidence_requests: int = 0,
    ) -> SelfCheckerOutcome:
        drop_ids = {c.candidate_id for c in challenges if c.verdict == "drop"}
        challenge_kept = [c for c in candidates if c.id not in drop_ids]

        context.issues = [c.to_issue() for c in challenge_kept]
        before_aggregation = len(context.issues)
        AggregationStage().execute(context)
        after_aggregation = len(context.issues)
        FalsePositiveFilterStage(
            enable_llm_verification=self._enable_fp_llm_verification
        ).execute(context)
        after_fp = len(context.issues)

        fp_stats = context.filter_stats
        by_agent: dict[str, int] = {}
        for candidate in candidates:
            by_agent[candidate.source_agent] = by_agent.get(candidate.source_agent, 0) + 1
        stats = CouncilRunStats(
            candidate_count=len(candidates),
            candidate_count_by_agent=by_agent,
            evidence_request_count=evidence_request_count,
            truncated_candidates=truncated_candidates,
            truncated_evidence_requests=truncated_evidence_requests,
            evidence_rounds=evidence_rounds,
            challenge_count=len(challenges),
            removed_by_challenge=len(drop_ids),
            removed_by_aggregation=max(0, before_aggregation - after_aggregation),
            removed_by_fp_rules=getattr(fp_stats, "removed_by_rules", 0) or 0,
            removed_by_fp_llm=getattr(fp_stats, "removed_by_llm", 0) or 0,
        )
        logger.info(
            "管线阶段 [self_checker]:候选 %d, challenge 剔除 %d,聚合剔除 %d,FP 后 %d",
            len(candidates),
            stats.removed_by_challenge,
            stats.removed_by_aggregation,
            after_fp,
        )
        return SelfCheckerOutcome(
            issues=list(context.issues),
            stats=stats,
            filter_stats=context.filter_stats,
        )
