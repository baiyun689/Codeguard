"""任务构建、路由与审查预算模型。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, StrictInt


class ReviewerKind(str, Enum):
    THREAT_MODEL = "threat_model"
    BEHAVIOR = "behavior"
    MAINTAINABILITY = "maintainability"


class ReviewTier(str, Enum):
    DIRECT = "direct"
    REACT = "react"


class AssignmentReason(str, Enum):
    PLAN_SELECTED = "plan_selected"


class TaskRoute(BaseModel):
    """Task 级 Direct/Full 确定性路由。"""

    task_id: str
    route: Literal["direct", "full"]
    reason: str = ""


class DeletionAnchor(BaseModel):
    """删除片段在当前 revision 中的可解析、可评论锚点。

    deleted_snippet 只描述 patch 旧侧事实；anchor_line 必须是新版本仍存在的
    邻近行。两者不能混入 changed_lines，后者始终只表示新增行。
    """

    anchor_line: StrictInt = Field(gt=0)
    anchor_kind: Literal["next_surviving", "previous_surviving"]
    deleted_snippet: str = Field(min_length=1)


class ReviewTask(BaseModel):
    """最小调度单位：一个 hunk 或一个文件级 fallback 片段。"""

    id: str
    file: str
    hunk_header: str = ""
    patch: str
    changed_lines: list[int] = Field(default_factory=list)
    deletion_anchors: list[DeletionAnchor] = Field(default_factory=list)
    patch_complete: bool = True

    @property
    def resolution_lines(self) -> list[int]:
        """返回可用于当前 revision 符号解析的行，不改变 changed_lines 语义。"""
        return list(dict.fromkeys([
            *self.changed_lines,
            *(anchor.anchor_line for anchor in self.deletion_anchors),
        ]))


class ReviewMode(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class DiffMetrics(BaseModel):
    file_count: StrictInt = Field(default=0, ge=0)
    hunk_count: StrictInt = Field(default=0, ge=0)
    diff_chars: StrictInt = Field(default=0, ge=0)


class ReviewRouteThresholds(BaseModel):
    small_max_files: StrictInt = Field(default=3, ge=0)
    small_max_hunks: StrictInt = Field(default=5, ge=0)
    small_max_diff_chars: StrictInt = Field(default=8000, ge=0)
    medium_max_files: StrictInt = Field(default=15, ge=0)
    medium_max_diff_chars: StrictInt = Field(default=60000, ge=0)


class ReviewRoute(BaseModel):
    initial_mode: ReviewMode
    effective_mode: ReviewMode
    selected_node: Literal["file_task_builder", "diff_task_builder"]
    fallback: bool = False
    fallback_reason: str = ""
    fallback_exception_type: str = ""
    outcome: Literal["pending", "completed"] = "pending"
    metrics: DiffMetrics = Field(default_factory=DiffMetrics)
    thresholds: ReviewRouteThresholds = Field(default_factory=ReviewRouteThresholds)


class ReviewBudget(BaseModel):
    """覆盖与执行预算。"""

    max_tasks_to_review: StrictInt | None = Field(default=100, gt=0)
    max_tasks_per_file: StrictInt | None = Field(default=10, gt=0)
    max_context_chars_per_task: StrictInt | None = Field(default=4000, gt=0)
    small_max_files: StrictInt = Field(default=3, ge=0)
    small_max_hunks: StrictInt = Field(default=5, ge=0)
    small_max_diff_chars: StrictInt = Field(default=8000, ge=0)
    medium_max_files: StrictInt = Field(default=15, ge=0)
    medium_max_diff_chars: StrictInt = Field(default=60000, ge=0)


class SkippedTask(BaseModel):
    task_id: str
    reason: str
    review_priority: int = 0


class TaskSelection(BaseModel):
    selected_task_ids: list[str]
    skipped_tasks: list[SkippedTask] = Field(default_factory=list)
