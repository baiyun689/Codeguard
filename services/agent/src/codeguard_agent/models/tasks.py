"""风险先验、审查覆盖与 PR 体量路由的内部状态模型。

这些模型只用于图 State、trace 和 eval 诊断，不进入 ReviewResult 产品输出。
RiskTriage 的唯一输出是 ``TaskRiskPrior``；后续节点不得重新解释规则分数。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, StrictInt

from codeguard_agent.models.council import ContextFact


class RiskTag(str, Enum):
    """路由信号标签——只说明"应从哪些角度审"，不代表"这里已有问题"。"""

    AUTHORIZATION = "AUTHORIZATION"
    AUTHENTICATION_SESSION = "AUTHENTICATION_SESSION"
    WEB_SECURITY_CONFIG = "WEB_SECURITY_CONFIG"
    INPUT_VALIDATION = "INPUT_VALIDATION"
    INJECTION = "INJECTION"
    SQL_DATA_ACCESS = "SQL_DATA_ACCESS"
    FILE_PATH_IO = "FILE_PATH_IO"
    SSRF_OUTBOUND = "SSRF_OUTBOUND"
    CONFIG_SECURITY = "CONFIG_SECURITY"
    DATA_EXPOSURE = "DATA_EXPOSURE"
    DESERIALIZATION = "DESERIALIZATION"
    TRANSACTION_ATOMICITY = "TRANSACTION_ATOMICITY"
    CONCURRENCY_CONSISTENCY = "CONCURRENCY_CONSISTENCY"
    IDEMPOTENCY_RETRY = "IDEMPOTENCY_RETRY"
    CACHE_CONSISTENCY = "CACHE_CONSISTENCY"
    MESSAGE_DELIVERY = "MESSAGE_DELIVERY"
    ERROR_HANDLING = "ERROR_HANDLING"
    NULL_STATE_SAFETY = "NULL_STATE_SAFETY"
    RESOURCE_LIFECYCLE = "RESOURCE_LIFECYCLE"
    API_CONTRACT = "API_CONTRACT"
    PERFORMANCE = "PERFORMANCE"
    COMPLEXITY_CONTROL_FLOW = "COMPLEXITY_CONTROL_FLOW"
    DUPLICATION_DESIGN = "DUPLICATION_DESIGN"
    OBSERVABILITY_TESTABILITY = "OBSERVABILITY_TESTABILITY"
    GENERAL_REVIEW = "GENERAL_REVIEW"


class RiskCoverage(str, Enum):
    """确定性风险先验的覆盖状态。"""

    CONFIDENT = "confident"
    AMBIGUOUS = "ambiguous"
    UNCLASSIFIED = "unclassified"


class ReviewerKind(str, Enum):
    """发现者的稳定 source_agent 标识。"""

    THREAT_MODEL = "threat_model"
    BEHAVIOR = "behavior"
    MAINTAINABILITY = "maintainability"


class ReviewTier(str, Enum):
    DIRECT = "direct"
    REACT = "react"


class AssignmentReason(str, Enum):
    BASELINE = "baseline"
    RISK_ADDED = "risk_added"
    RISK_UPGRADED = "risk_upgraded"
    EXECUTION_OVERRIDE = "execution_override"
    AMBIGUITY_FALLBACK = "ambiguity_fallback"


class RiskHypothesis(BaseModel):
    """不可直接裁决问题成立的风险先验。"""

    tag: RiskTag
    match_confidence: float = Field(ge=0.0, le=1.0)
    review_priority: int = Field(ge=1, le=3)
    source_kind: Literal["diff_text", "path", "symbol", "ast"]
    source: str
    reason: str
    line: int | None = None


class TaskRiskPrior(BaseModel):
    task_id: str
    hypotheses: tuple[RiskHypothesis, ...] = ()
    coverage: RiskCoverage


class ReviewerAssignment(BaseModel):
    reviewer: ReviewerKind
    tier: ReviewTier
    reasons: tuple[AssignmentReason, ...]
    hypothesis_tags: tuple[RiskTag, ...] = ()


class TaskReviewPlan(BaseModel):
    task_id: str
    assignments: tuple[ReviewerAssignment, ...] = ()


class ReviewCoveragePlan(BaseModel):
    """选中任务的基础覆盖、风险增强和 ReAct 预算结果。"""

    tasks: tuple[TaskReviewPlan, ...] = ()
    baseline_assignments: int = 0
    risk_added_assignments: int = 0
    ambiguity_fallback_assignments: int = 0
    react_candidate_tasks: int = 0
    react_task_count: int = 0
    react_assignment_count: int = 0
    risk_upgraded_assignments: int = 0
    execution_override_assignments: int = 0
    truncated_react_task_count: int = 0
    truncated_react_assignment_count: int = 0
    unclassified_tasks: int = 0
    tasks_with_zero_assignments: int = 0


class ReviewTask(BaseModel):
    """最小调度单位：一个 hunk 或一个文件级 fallback 片段。"""

    id: str
    file: str
    hunk_header: str = ""
    patch: str
    changed_lines: list[int] = Field(default_factory=list)
    patch_complete: bool = True


class RiskSignal(BaseModel):
    """规则内部的原始风险信号。

    信号显式区分匹配可信度与审查优先级，聚合后形成 ``RiskHypothesis``。
    Graph State 不保存原始信号，避免规则输出成为第二套风险事实源。
    """

    tag: RiskTag
    match_confidence: float = Field(ge=0.0, le=1.0)
    review_priority: int = Field(ge=1, le=3)
    source_kind: Literal["diff_text", "path", "symbol", "ast"]
    source: str
    reason: str
    line: int | None = None


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
        "direct_review",
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
    max_react_assignments: StrictInt = Field(default=20, gt=0)

    # ── PR 体量分类阈值（可配置，方便评测调参） ──
    # 设为 0 时该模式永不被选中（如 small_max_files=0 → 永远不走 small）
    small_max_files: StrictInt = Field(default=3, ge=0)
    small_max_hunks: StrictInt = Field(default=5, ge=0)
    small_max_diff_chars: StrictInt = Field(default=8000, ge=0)
    medium_max_files: StrictInt = Field(default=15, ge=0)
    medium_max_diff_chars: StrictInt = Field(default=60000, ge=0)
    # 评测开关：让已分配且有工具的 reviewer 进入 ReAct，仍受预算约束。
    force_react: bool = False


class SkippedTask(BaseModel):
    """TaskRank 跳过的任务及原因。"""

    task_id: str
    reason: str
    review_priority: int = 0


class TaskSelection(BaseModel):
    """TaskRank 的唯一选择决策。"""

    selected_task_ids: list[str]
    skipped_tasks: list[SkippedTask] = Field(default_factory=list)


class ContextStatus(BaseModel):
    """某类预取上下文没有形成事实时的实际状态。"""

    kind: str
    status: Literal["skipped", "failed", "unavailable"]
    reason: str


class TaskContextBundle(BaseModel):
    """按任务构建的上下文包。不复制 file/patch/RiskTag（通过 task_id 关联读取）。"""

    task_id: str
    facts: list[ContextFact] = Field(default_factory=list)
    statuses: list[ContextStatus] = Field(default_factory=list)
    truncated: bool = False
