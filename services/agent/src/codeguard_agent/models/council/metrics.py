"""ReviewCouncil 的 Trace 与统计模型。"""

from __future__ import annotations
from pydantic import BaseModel, Field


class CouncilTrace(BaseModel):
    node: str
    event: str
    detail: str = ""


class CouncilRunStats(BaseModel):
    candidate_count: int = 0
    candidate_count_by_agent: dict[str, int] = Field(default_factory=dict)
    truncated_candidates: int = 0
    verdict_count: int = 0
    removed_by_judge: int = 0
    critical_candidate_count: int = 0
    final_issue_count: int = 0
    final_issue_supported_count: int = 0
    final_issue_support_coverage: float | None = None
    artifact_count: int = 0
    patch_artifact_count: int = 0
    context_artifact_count: int = 0
    tool_artifact_count: int = 0
    reused_artifact_count: int = 0
    candidate_patch_only_count: int = 0
    candidate_context_backed_count: int = 0
    candidate_tool_backed_count: int = 0
    candidate_ungrounded_count: int = 0
    selected_reference_count: int = 0
    valid_reference_count: int = 0
    limited_reference_count: int = 0
    invalid_reference_count: int = 0
    evidence_gap_count: int = 0
    graph_indeterminate_count: int = 0
    replay_requested_count: int = 0
    replay_valid_count: int = 0
    replay_limited_count: int = 0
    replay_failed_count: int = 0
    judge_batch_call_count: int = 0
    judge_failed_candidate_count: int = 0
    judge_no_support_drop_count: int = 0
    direct_tier_task_count: int = 0
    discoverer_failed_count: int = 0
    task_review_failed_count: int = 0
    investigation_incomplete_count: int = 0
    judge_synthesis_failed_count: int = 0
