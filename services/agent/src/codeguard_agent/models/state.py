"""LangGraph 审查状态与共享 reducer。

状态模型只描述节点之间传递的数据，不包含节点实现或业务判断逻辑。
产品输出模型仍位于 :mod:`models.schemas`，领域工作模型位于各自的模型模块。
"""

from __future__ import annotations
import operator
from typing import Annotated, TypedDict
from codeguard_agent.models.council import (
    CandidateIssue,
    CausalComparison,
    CausalMergeGroup,
    CausalProfile,
    CouncilRunStats,
    CouncilTrace,
)
from codeguard_agent.models.evidence import (
    CandidateVerification,
    EvidenceArtifact,
    ToolTraceRef,
    merge_evidence_artifacts,
)
from codeguard_agent.models.schemas import Issue
from codeguard_agent.models.tasks import (
    InvestigationResult,
    ReviewBudget,
    ReviewRoute,
    ReviewTask,
    TaskRoute,
    TaskSelection,
    TaskSymbolContext,
    SubtaskPlan,
)


def collect_candidate_reducer(
    existing: list[CandidateIssue] | None, new: list[CandidateIssue] | None
) -> list[CandidateIssue]:
    """按 candidate ID 去重，保留第一次出现的候选 payload。"""
    merged = list(existing or []) + list(new or [])
    seen: set[str] = set()
    result: list[CandidateIssue] = []
    for candidate in merged:
        if candidate.id in seen:
            continue
        seen.add(candidate.id)
        result.append(candidate)
    return result


class ReviewState(TypedDict, total=False):
    """顶层审查图共享状态。"""

    diff_text: str
    evidence_revision: str
    enabled_tools: list[str] | None
    enabled_evidence_tools: list[str] | None
    max_retries: int
    structured_method: str
    review_budget: ReviewBudget
    review_mode: str
    review_route: ReviewRoute
    review_tasks: list[ReviewTask]
    task_routes: dict[str, TaskRoute]
    direct_final_issues: list[Issue]
    task_selection: TaskSelection
    controlled_max_path_depth: int
    controlled_subtask_plans: dict[str, SubtaskPlan]
    controlled_subtask_results: dict[str, InvestigationResult]
    controlled_subtask_outcomes: dict[str, str]
    controlled_subtask_reasons: dict[str, str]
    controlled_candidate_contexts: dict[str, dict[str, str]]
    task_symbol_contexts: dict[str, TaskSymbolContext]
    raw_candidate_issues: Annotated[list[CandidateIssue], collect_candidate_reducer]
    candidate_issues: list[CandidateIssue]
    candidate_verifications: dict[str, CandidateVerification]
    evidence_artifacts: Annotated[dict[str, EvidenceArtifact], merge_evidence_artifacts]
    judge_survivor_ids: list[str]
    causal_profiles: dict[str, CausalProfile]
    causal_comparisons: list[CausalComparison]
    causal_merge_groups: list[CausalMergeGroup]
    causal_merge_stats: dict[str, int]
    final_issues: list[Issue]
    summary: str
    symbol_resolution_diagnostics: dict[str, str]
    council_stats: CouncilRunStats
    council_trace: Annotated[list[CouncilTrace], operator.add]
    truncated_candidates: Annotated[int, operator.add]
    tool_trace_records: Annotated[list[ToolTraceRef], operator.add]
