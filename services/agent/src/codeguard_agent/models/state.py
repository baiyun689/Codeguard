"""LangGraph 审查状态与共享 reducer。

状态模型只描述节点之间传递的数据，不包含节点实现或业务判断逻辑。
产品输出模型仍位于 :mod:`models.schemas`，领域工作模型位于各自的模型模块。
"""

from __future__ import annotations

import operator
from typing import Annotated, Protocol, TypedDict

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
    EvidenceCatalog,
    ToolTraceRef,
    merge_evidence_artifacts,
)
from codeguard_agent.models.schemas import DiscoveredIssue, DiscoveryReviewResult, Issue, ReviewResult
from codeguard_agent.models.tasks import (
    PlanUnit,
    ReviewAssignments,
    ReviewBudget,
    ReviewRoute,
    ReviewTask,
    TaskAgentPlan,
    TaskRoute,
    TaskSelection,
    TaskSymbolContext,
)


class ReviewerOutcomeLike(Protocol):
    """发现者执行结果的最小状态接口，避免状态模型依赖 execution 引擎。"""

    result: DiscoveryReviewResult | ReviewResult
    tool_trace_records: list[ToolTraceRef]
    execution_events: list[str]
    evidence_catalog: EvidenceCatalog | None


def collect_candidate_reducer(
    existing: list[CandidateIssue] | None,
    new: list[CandidateIssue] | None,
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

    # Input: 本次审查的只读事实
    diff_text: str
    evidence_revision: str

    # Config: 本次运行的策略和预算
    enabled_tools: list[str] | None
    enabled_evidence_tools: list[str] | None
    max_retries: int
    structured_method: str
    react_recursion_limit: int
    allow_direct_fallback: bool
    review_budget: ReviewBudget

    # Plan: 确定性规划结果
    review_mode: str
    review_route: ReviewRoute
    direct_review_status: str
    review_tasks: list[ReviewTask]
    task_routes: dict[str, TaskRoute]
    plan_units: list[PlanUnit]
    task_plans: dict[str, TaskAgentPlan]
    direct_final_issues: list[Issue]
    task_selection: TaskSelection
    review_assignments: ReviewAssignments

    # Working: 跨节点传递、会影响后续决策的审查工作集
    diff_summary: str
    task_symbol_contexts: dict[str, TaskSymbolContext]
    raw_candidate_issues: Annotated[list[CandidateIssue], collect_candidate_reducer]
    candidate_issues: list[CandidateIssue]
    candidate_verifications: dict[str, CandidateVerification]
    evidence_artifacts: Annotated[dict[str, EvidenceArtifact], merge_evidence_artifacts]
    review_summaries: Annotated[list[str], operator.add]
    judge_survivor_ids: list[str]
    causal_profiles: dict[str, CausalProfile]
    causal_comparisons: list[CausalComparison]
    causal_merge_groups: list[CausalMergeGroup]
    causal_merge_stats: dict[str, int]

    # Output: 对外 ReviewResult 的来源
    final_issues: list[Issue]
    summary: str

    # Diagnostics: Trace / eval 数据，不属于产品输出
    symbol_resolution_diagnostics: dict[str, str]
    council_stats: CouncilRunStats
    council_trace: Annotated[list[CouncilTrace], operator.add]
    truncated_candidates: Annotated[int, operator.add]
    tool_trace_records: Annotated[list[ToolTraceRef], operator.add]


class ReviewerState(TypedDict, total=False):
    """单个发现者 Agent 子图的局部状态。"""

    # Input / 策略：由顶层 ReviewState 投影而来
    diff_text: str
    enabled_tools: list[str] | None
    max_retries: int
    structured_method: str
    diff_summary: str
    react_recursion_limit: int
    allow_direct_fallback: bool
    task_knowledge: str
    plan_objectives: tuple[str, ...]
    knowledge_topics: tuple[str, ...]
    review_task: ReviewTask
    task_symbol_context: TaskSymbolContext
    tier: str
    task_scope: str
    review_tool_client: object

    # 当前 task 的证据目录和结构化 Prompt
    evidence_revision: str
    evidence_catalog: EvidenceCatalog | None

    issues: list[DiscoveredIssue]
    tool_trace_records: list[ToolTraceRef]
    review_summaries: list[str]
    council_trace: Annotated[list[CouncilTrace], operator.add]
    user_prompt: str
    outcome: ReviewerOutcomeLike
