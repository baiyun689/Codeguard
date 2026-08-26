"""Reviewer Plan 与任务分派模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codeguard_agent.models.tasks.tasking import AssignmentReason, ReviewerKind, ReviewTier


class ReviewerAssignment(BaseModel):
    reviewer: ReviewerKind = Field(description="负责审查该 Task 的领域审查员")
    tier: ReviewTier = Field(description="该 Task 使用直连审查还是工具型 ReAct 审查")
    reasons: tuple[AssignmentReason, ...] = Field(
        description="将该审查员分配到 Task 的确定性原因"
    )


class TaskReviewPlan(BaseModel):
    task_id: str = Field(description="待执行审查任务的稳定 ID")
    assignments: tuple[ReviewerAssignment, ...] = Field(
        default=(), description="该 Task 的审查员执行分配"
    )


class ReviewerPlan(BaseModel):
    reviewer: ReviewerKind = Field(description="要执行审查的领域审查员")
    objectives: tuple[str, ...] = Field(
        default=(),
        description=(
            "针对当前 diff/PlanUnit 变更的具体检查目标；必须说明变更点和要验证的行为，"
            "不是问题结论、severity 或工具调用计划"
        ),
    )
    knowledge_topics: tuple[str, ...] = Field(
        default=(),
        description=(
            "该审查员可使用的知识主题 ID；只表示检查方法，不表示代码中一定存在该问题"
        ),
    )


class TaskAgentPlan(BaseModel):
    plan_unit_id: str = Field(description="本次计划覆盖的 PlanUnit 稳定 ID")
    reviewers: tuple[ReviewerKind, ...] = Field(
        default=(),
        description=(
            "选中的 Reviewer 汇总列表，必须与 reviewer_plans 中的 Reviewer 一一对应，"
            "不得重复"
        ),
    )
    reviewer_plans: tuple[ReviewerPlan, ...] = Field(
        default=(),
        description="每个选中 Reviewer 的具体检查目标和按需知识主题",
    )
    fallback: bool = Field(
        default=False,
        description="系统字段：仅当 Plan 失败并由系统生成兜底计划时为 true，LLM 不得设置",
    )
    fallback_reason: str = Field(
        default="",
        description="系统字段：Plan 兜底原因，LLM 输出时必须保持为空",
    )


class PlanUnit(BaseModel):
    id: str = Field(description="PlanUnit 的稳定 ID")
    file: str = Field(description="该 PlanUnit 主要覆盖的文件路径")
    task_ids: tuple[str, ...] = Field(
        default=(), description="该 PlanUnit 包含的一个或多个 ReviewTask ID"
    )


class ReviewAssignments(BaseModel):
    tasks: tuple[TaskReviewPlan, ...] = Field(
        default=(), description="所有待执行 Full Task 的审查员分配"
    )
