"""风险路由任务链的内部状态模型（Phase 1）。

这些模型只用于图 State、trace 和 eval 诊断，不进入 ReviewResult 产品输出。
事实源单一所有者原则见 spec §3.3：TaskContextBundle 不复制 file/patch/RiskTag，
RiskProfile 不保存 total_score（分数是 TaskRank 的派生计算）。
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
    AMBIGUITY_FALLBACK = "ambiguity_fallback"


class RiskHypothesis(BaseModel):
    """从旧 RiskProfile 派生的、不可直接裁决问题成立的先验。"""

    tag: RiskTag
    match_confidence: float = Field(ge=0.0, le=1.0)
    review_priority: int = Field(ge=1, le=3)
    source_kind: Literal["diff_text", "path", "symbol", "ast", "fallback"]
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
    """单条风险信号：说明某个 RiskTag 来自哪里、为什么。"""

    tag: RiskTag
    score: int
    source: str
    reason: str
    line: int | None = None


class RiskProfile(BaseModel):
    """一个任务的风险画像。不保存 total_score（派生计算）。"""

    task_id: str
    tag_scores: dict[RiskTag, int] = Field(default_factory=dict)
    signals: list[RiskSignal] = Field(default_factory=list)


class ReviewMode(str, Enum):
    """PR 体量自适应审查模式。"""

    SMALL = "small"      # 直接审查整个 diff，不拆分、不走管线
    MEDIUM = "medium"    # 按文件拆分，走完整管线
    LARGE = "large"      # 按 hunk 拆分 + 预算控制（现状）


class ReviewBudget(BaseModel):
    """覆盖与执行预算。普通模式解除 task 上限，大 diff 才消费配置的覆盖上限。"""

    # ── 任务数量预算 ──
    max_tasks_to_review: StrictInt | None = Field(default=100, gt=0)
    max_tasks_per_file: StrictInt | None = Field(default=10, gt=0)
    max_context_chars_per_task: StrictInt | None = Field(default=4000, gt=0)
    max_react_tasks: StrictInt = Field(default=20, gt=0)
    max_final_issues: StrictInt | None = Field(default=None, gt=0)

    # ── PR 体量分类阈值（可配置，方便评测调参） ──
    small_max_files: StrictInt = Field(default=3, gt=0)
    small_max_changed_lines: StrictInt = Field(default=200, gt=0)
    small_max_hunks: StrictInt = Field(default=5, gt=0)
    medium_max_files: StrictInt = Field(default=15, gt=0)
    medium_max_changed_lines: StrictInt = Field(default=2000, gt=0)
    # 文件级审查时，单文件变更行数超过此阈值则内部回退 hunk 级
    medium_file_changed_lines_fallback: StrictInt = Field(default=500, gt=0)


class SkippedTask(BaseModel):
    """TaskRank 跳过的任务及原因。"""

    task_id: str
    reason: str
    risk_score: int = 0


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

    def render(self, budget: int = 4000) -> str:
        """渲染为 prompt 可读文本，并按字符预算截断。"""
        if not self.facts and not self.statuses and not self.truncated:
            return "(无任务上下文事实)"
        lines = [f'任务上下文事实(bundle_truncated="{str(self.truncated).lower()}"):']
        for fact in self.facts:
            flag = " (已截断)" if fact.truncated else ""
            lines.append(f"- [{fact.source}/{fact.kind}]{flag} {fact.content}")
        for status in self.statuses:
            lines.append(
                f"- [{status.kind}] status={status.status} reason={status.reason}"
            )
        if len(lines) == 1:
            lines.append("(无任务上下文事实)")
        text = "\n".join(lines).strip()
        if len(text) <= budget:
            return text
        return text[:budget] + "\n...(TaskContextBundle 已达预算上限,后续省略)"
