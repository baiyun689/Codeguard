"""受控审查模式的计划、候选和证据契约。

这些模型是 controlled pipeline 的内部协议，不改变产品 ``Issue`` schema，
也不作为 Gateway 请求协议。LLM 只能生成其中的语义字段；ID、证据绑定和
执行状态由 Python 运行时负责。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictFloat

from codeguard_agent.models.tasks.tasking import ReviewerKind


class CoverageDecision(str, Enum):
    LOCAL_ONLY = "local_only"
    GRAPH_NEEDED = "graph_needed"
    NOT_APPLICABLE = "not_applicable"


class ProofScope(str, Enum):
    LOCAL = "local"
    STRUCTURAL = "structural"
    CROSS_FILE = "cross_file"
    IMPACT = "impact"
    SECURITY_PROPAGATION = "security_propagation"


class EvidenceNeed(str, Enum):
    NONE = "none"
    INSPECT_STRUCTURE = "inspect_structure"
    INSPECT_PATH = "inspect_path"
    INSPECT_CHANGE_IMPACT = "inspect_change_impact"
    DOMAIN_TOOL = "domain_tool"


class AssessmentStatus(str, Enum):
    CANDIDATE = "candidate"
    REJECTED = "rejected"
    NEEDS_EVIDENCE = "needs_evidence"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


class ProofMatchStatus(str, Enum):
    PROVED = "proved"
    PARTIAL = "partial"
    NOT_FOUND = "not_found"
    INDETERMINATE = "indeterminate"


class ControlledModel(BaseModel):
    """所有受控内部模型禁止未声明字段，避免 LLM 静默扩展协议。"""

    model_config = ConfigDict(extra="forbid")


class CoverageDeclaration(ControlledModel):
    change_unit_id: str = Field(min_length=1)
    decision: CoverageDecision
    reason: str = Field(min_length=1)


class GraphQuestion(ControlledModel):
    subject_ref: str = Field(min_length=1)
    direction: Literal["downstream", "upstream"]
    path_kind: Literal["behavior", "security"] | None = None
    expected_targets: tuple[str, ...] = ()
    required_relationships: tuple[str, ...] = ()
    max_depth: StrictInt = Field(default=3, ge=1, le=3)
    question: str = Field(min_length=1)


class CandidateSeed(ControlledModel):
    """DirectTriage 的内部候选，不是最终产品 Issue。"""

    seed_id: str = Field(default="", description="系统绑定；LLM 输出时必须为空")
    reviewer: ReviewerKind
    change_unit_id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    issue_type: str = "controlled_review_finding"
    mechanism: str = Field(min_length=1)
    impact: str = ""
    suggestion: str = ""
    location_file: str = Field(min_length=1)
    location_line: StrictInt = Field(default=0, ge=0)
    proof_scope: ProofScope
    evidence_basis: tuple[Literal["changed_lines", "local_source", "knowledge"], ...] = ()
    evidence_need: EvidenceNeed = EvidenceNeed.NONE
    graph_question: GraphQuestion | None = None
    confidence: StrictFloat = Field(default=0.5, ge=0.0, le=1.0)


class DirectTriageResult(ControlledModel):
    coverage: tuple[CoverageDeclaration, ...]
    issues: tuple[CandidateSeed, ...] = ()
    limitations: tuple[str, ...] = ()


class EvidenceStep(ControlledModel):
    step_id: str = Field(default="", description="系统绑定；LLM 输出时必须为空")
    tool: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    path_kind: Literal["behavior", "security"] | None = None
    max_depth: StrictInt | None = Field(default=None, ge=1, le=3)
    purpose: str = Field(min_length=1)
    expected_fact: str = Field(min_length=1)
    required: bool = True
    depends_on: tuple[str, ...] = ()
    result_selector: str = ""


class WorkItem(ControlledModel):
    work_item_id: str = Field(default="", description="系统绑定；LLM 输出时必须为空")
    seed_id: str = Field(min_length=1)
    reviewer: ReviewerKind
    hypothesis: str = Field(min_length=1)
    expected_mechanism: str = Field(min_length=1)
    evidence_steps: tuple[EvidenceStep, ...] = Field(min_length=1, max_length=2)
    candidate_criteria: str = Field(min_length=1)
    rejection_criteria: str = Field(min_length=1)


class ReviewerGraphPlan(ControlledModel):
    reviewer: ReviewerKind
    task_id: str = Field(min_length=1)
    work_items: tuple[WorkItem, ...] = Field(default=(), max_length=4)


class TaskKnowledgeRoute(ControlledModel):
    task_id: str = Field(min_length=1)
    knowledge_topics: tuple[str, ...] = Field(default=(), max_length=4)


class KnowledgeRoutePlan(ControlledModel):
    plan_unit_id: str = Field(min_length=1)
    task_routes: tuple[TaskKnowledgeRoute, ...] = ()


class EvidenceAssessment(ControlledModel):
    work_item_id: str = Field(min_length=1)
    status: AssessmentStatus
    claim: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    impact: str = ""
    proof_scope: ProofScope
    supporting_refs: tuple[str, ...] = Field(default=(), max_length=3)
    counter_refs: tuple[str, ...] = Field(default=(), max_length=3)
    limitations: tuple[str, ...] = ()
    additional_evidence_question: str = ""
    additional_steps: tuple[EvidenceStep, ...] = Field(default=(), max_length=1)


class EvidenceAssessmentBatch(ControlledModel):
    assessments: tuple[EvidenceAssessment, ...] = ()


class ProofMatch(ControlledModel):
    work_item_id: str = Field(min_length=1)
    status: ProofMatchStatus
    matched_targets: tuple[str, ...] = ()
    matched_relationships: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


__all__ = [
    "AssessmentStatus",
    "CandidateSeed",
    "CoverageDeclaration",
    "CoverageDecision",
    "DirectTriageResult",
    "EvidenceAssessment",
    "EvidenceAssessmentBatch",
    "EvidenceNeed",
    "EvidenceStep",
    "GraphQuestion",
    "KnowledgeRoutePlan",
    "ProofMatch",
    "ProofMatchStatus",
    "ProofScope",
    "ReviewerGraphPlan",
    "TaskKnowledgeRoute",
    "WorkItem",
]
