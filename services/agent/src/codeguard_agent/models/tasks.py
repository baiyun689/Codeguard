"""任务拆分、DirectGate 与 Plan 编排的内部状态模型。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, StrictInt

from codeguard_agent.models.council import ContextFact


class ReviewerKind(str, Enum):
    """发现者的稳定 source_agent 标识。"""

    THREAT_MODEL = "threat_model"
    BEHAVIOR = "behavior"
    MAINTAINABILITY = "maintainability"


class ReviewTier(str, Enum):
    DIRECT = "direct"
    REACT = "react"


class AssignmentReason(str, Enum):
    PLAN_SELECTED = "plan_selected"


class ReviewerAssignment(BaseModel):
    reviewer: ReviewerKind
    tier: ReviewTier
    reasons: tuple[AssignmentReason, ...]


class TaskReviewPlan(BaseModel):
    task_id: str
    assignments: tuple[ReviewerAssignment, ...] = ()


class ReviewerPlan(BaseModel):
    """Plan 为单个 Reviewer 生成的审查目标和知识主题选择。"""

    reviewer: ReviewerKind
    objectives: tuple[str, ...] = ()
    knowledge_topics: tuple[str, ...] = ()


class TaskAgentPlan(BaseModel):
    """一个 PlanUnit 的 LLM 审查计划。"""

    plan_unit_id: str
    reviewers: tuple[ReviewerKind, ...] = ()
    reviewer_plans: tuple[ReviewerPlan, ...] = ()
    fallback: bool = False
    fallback_reason: str = ""


class TaskRoute(BaseModel):
    """Task 级 Direct/Full 确定性路由。"""

    task_id: str
    route: Literal["direct", "full"]
    reason: str = ""


class PlanUnit(BaseModel):
    """一次 Plan 调用覆盖的任务集合；large 模式按文件复用。"""

    id: str
    file: str
    task_ids: tuple[str, ...] = ()


class ReviewAssignments(BaseModel):
    """Plan 生成的 task → reviewer 执行计划。"""

    tasks: tuple[TaskReviewPlan, ...] = ()


class ReviewTask(BaseModel):
    """最小调度单位：一个 hunk 或一个文件级 fallback 片段。"""

    id: str
    file: str
    hunk_header: str = ""
    patch: str
    changed_lines: list[int] = Field(default_factory=list)
    patch_complete: bool = True


class ReviewMode(str, Enum):
    """PR 体量自适应审查模式。"""

    SMALL = "small"      # 直接审查整个 diff，不拆分、不走管线
    MEDIUM = "medium"    # 按文件拆分，走完整管线
    LARGE = "large"      # 按 hunk 拆分 + 预算控制（现状）


class DiffMetrics(BaseModel):
    """PR 规模路由消费并写入 Trace 的稳定统计。"""

    file_count: StrictInt = Field(default=0, ge=0)
    hunk_count: StrictInt = Field(default=0, ge=0)
    diff_chars: StrictInt = Field(default=0, ge=0)


class ReviewRouteThresholds(BaseModel):
    """做出规模判定时实际使用的阈值快照。"""

    small_max_files: StrictInt = Field(default=3, ge=0)
    small_max_hunks: StrictInt = Field(default=5, ge=0)
    small_max_diff_chars: StrictInt = Field(default=8000, ge=0)
    medium_max_files: StrictInt = Field(default=15, ge=0)
    medium_max_diff_chars: StrictInt = Field(default=60000, ge=0)


class ReviewRoute(BaseModel):
    """一次审查最终可解释、可序列化的规模路由决策。"""

    initial_mode: ReviewMode
    effective_mode: ReviewMode
    selected_node: Literal[
        "file_task_builder",
        "diff_task_builder",
    ]
    fallback: bool = False
    fallback_reason: str = ""
    fallback_exception_type: str = ""
    outcome: Literal["pending", "completed"] = "pending"
    metrics: DiffMetrics = Field(default_factory=DiffMetrics)
    thresholds: ReviewRouteThresholds = Field(default_factory=ReviewRouteThresholds)


class ReviewBudget(BaseModel):
    """覆盖与执行预算。普通模式解除 task 上限，大 diff 才消费配置的覆盖上限。"""

    # ── 任务数量预算 ──
    max_tasks_to_review: StrictInt | None = Field(default=100, gt=0)
    max_tasks_per_file: StrictInt | None = Field(default=10, gt=0)
    max_context_chars_per_task: StrictInt | None = Field(default=4000, gt=0)

    # ── PR 体量分类阈值（可配置，方便评测调参） ──
    # 设为 0 时该模式永不被选中（如 small_max_files=0 → 永远不走 small）
    small_max_files: StrictInt = Field(default=3, ge=0)
    small_max_hunks: StrictInt = Field(default=5, ge=0)
    small_max_diff_chars: StrictInt = Field(default=8000, ge=0)
    medium_max_files: StrictInt = Field(default=15, ge=0)
    medium_max_diff_chars: StrictInt = Field(default=60000, ge=0)


class SkippedTask(BaseModel):
    """未进入 Full 审查的任务及原因。"""

    task_id: str
    reason: str
    review_priority: int = 0


class TaskSelection(BaseModel):
    """任务选择结果；当前由确定性规模限制产生。"""

    selected_task_ids: list[str]
    skipped_tasks: list[SkippedTask] = Field(default_factory=list)


class ContextStatus(BaseModel):
    """某类预取上下文没有形成事实时的实际状态。"""

    kind: str
    status: Literal["skipped", "failed", "unavailable"]
    reason: str


class TaskContextBundle(BaseModel):
    """按任务构建的上下文包。"""

    task_id: str
    facts: list[ContextFact] = Field(default_factory=list)
    statuses: list[ContextStatus] = Field(default_factory=list)
    truncated: bool = False
