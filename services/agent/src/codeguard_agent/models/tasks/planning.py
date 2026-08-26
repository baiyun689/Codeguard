"""Reviewer Plan 与任务分派模型。"""

from __future__ import annotations

from pydantic import BaseModel

from codeguard_agent.models.tasks.tasking import AssignmentReason, ReviewerKind, ReviewTier


class ReviewerAssignment(BaseModel):
    reviewer: ReviewerKind
    tier: ReviewTier
    reasons: tuple[AssignmentReason, ...]


class TaskReviewPlan(BaseModel):
    task_id: str
    assignments: tuple[ReviewerAssignment, ...] = ()


class ReviewerPlan(BaseModel):
    reviewer: ReviewerKind
    objectives: tuple[str, ...] = ()
    knowledge_topics: tuple[str, ...] = ()


class TaskAgentPlan(BaseModel):
    plan_unit_id: str
    reviewers: tuple[ReviewerKind, ...] = ()
    reviewer_plans: tuple[ReviewerPlan, ...] = ()
    fallback: bool = False
    fallback_reason: str = ""


class PlanUnit(BaseModel):
    id: str
    file: str
    task_ids: tuple[str, ...] = ()


class ReviewAssignments(BaseModel):
    tasks: tuple[TaskReviewPlan, ...] = ()
