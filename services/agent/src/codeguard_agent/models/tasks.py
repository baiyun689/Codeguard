"""风险路由任务链的内部状态模型（Phase 1）。

这些模型只用于图 State、trace 和 eval 诊断，不进入 ReviewResult 产品输出。
事实源单一所有者原则见 spec §3.3：TaskContextBundle 不复制 file/patch/RiskTag，
RiskProfile 不保存 total_score（分数是 TaskRank 的派生计算）。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

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


class ReviewTask(BaseModel):
    """最小调度单位：一个 hunk 或一个文件级 fallback 片段。"""

    id: str
    file: str
    hunk_header: str = ""
    patch: str
    changed_lines: list[int] = Field(default_factory=list)


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


class ReviewBudget(BaseModel):
    """预算入口。None 表示当前策略不施加该项限制；Phase 1 基线为全选。"""

    max_tasks_to_review: int | None = Field(default=100, gt=0)
    max_tasks_per_file: int | None = Field(default=10, gt=0)
    max_context_chars_per_task: int | None = Field(default=None, gt=0)
    max_final_issues: int | None = Field(default=None, gt=0)


class SkippedTask(BaseModel):
    """TaskRank 跳过的任务及原因。"""

    task_id: str
    reason: str
    risk_score: int = 0


class TaskSelection(BaseModel):
    """TaskRank 的唯一选择决策。"""

    selected_task_ids: list[str]
    skipped_tasks: list[SkippedTask] = Field(default_factory=list)


class TaskContextBundle(BaseModel):
    """按任务构建的上下文包。不复制 file/patch/RiskTag（通过 task_id 关联读取）。"""

    task_id: str
    facts: list[ContextFact] = Field(default_factory=list)
    truncated: bool = False
