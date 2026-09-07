"""受控审查模式的计划、候选和证据契约。

这些模型是 controlled pipeline 的内部协议，不改变产品 ``Issue`` schema，
也不作为 Gateway 请求协议。LLM 只能生成其中的语义字段；ID、证据绑定和
执行状态由 Python 运行时负责。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

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
    # Compatibility alias emitted by some providers when they confuse the
    # coverage decision with the proof scope.  Routing treats it as non-local
    # and still requires a validated GraphQuestion.
    GRAPH_NEEDED = "graph_needed"

    @classmethod
    def _missing_(cls, value: object):
        """Accept common provider spellings at the transport boundary."""

        aliases = {
            "cross-file": cls.CROSS_FILE,
            "crossfile": cls.CROSS_FILE,
            "impact_scope": cls.IMPACT,
            "security-propagation": cls.SECURITY_PROPAGATION,
            "security": cls.SECURITY_PROPAGATION,
            "graph": cls.GRAPH_NEEDED,
        }
        return aliases.get(str(value).strip().lower())


class EvidenceNeed(str, Enum):
    NONE = "none"
    INSPECT_STRUCTURE = "inspect_structure"
    INSPECT_PATH = "inspect_path"
    INSPECT_CHANGE_IMPACT = "inspect_change_impact"
    DOMAIN_TOOL = "domain_tool"

    @classmethod
    def _missing_(cls, value: object):
        """Accept common provider abbreviations without widening the enum."""

        normalized = str(value).strip().lower()
        if not normalized:
            # An omitted evidence request is equivalent to the schema
            # default.  It is deliberately not promoted to a graph tool;
            # only an explicit GraphQuestion can route the seed later.
            return cls.NONE
        aliases = {
            "inspect_struct": cls.INSPECT_STRUCTURE,
            "inspect_structure_graph": cls.INSPECT_STRUCTURE,
            "structure": cls.INSPECT_STRUCTURE,
            "inspect_change": cls.INSPECT_CHANGE_IMPACT,
            "change_impact": cls.INSPECT_CHANGE_IMPACT,
            "inspect_impact": cls.INSPECT_CHANGE_IMPACT,
            "impact": cls.INSPECT_CHANGE_IMPACT,
            "inspect_call_graph": cls.INSPECT_PATH,
            "call_graph": cls.INSPECT_PATH,
            "graph_path": cls.INSPECT_PATH,
            "path": cls.INSPECT_PATH,
            # DeepSeek-compatible function serialization occasionally swaps
            # the middle syllable in ``inspect_path``.  Treat this known wire
            # typo as the same request; no new tool or routing decision is
            # introduced by the alias.
            "inspire_path": cls.INSPECT_PATH,
            # ``graph_needed`` is a coverage/proof label, not a concrete
            # executable tool.  Preserve the explicit GraphQuestion and let
            # triage normalize its direction/path_kind instead of guessing
            # inspect_path versus inspect_change_impact here.
            "graph_needed": cls.NONE,
            "graph": cls.NONE,
            "inspect_graph": cls.NONE,
        }
        return aliases.get(normalized)


class AssessmentStatus(str, Enum):
    PROVED = "proved"
    CANDIDATE = "candidate"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"
    NEEDS_EVIDENCE = "needs_evidence"
    UNRESOLVED = "unresolved"
    # The model may distinguish "the graph cannot decide" from an explicit
    # unresolved conclusion.  Keep this value in the assessment contract so
    # that a natural indeterminate answer is not turned into a protocol
    # failure; the runtime still treats it as non-candidate.
    INDETERMINATE = "indeterminate"
    # Some providers use the proof vocabulary directly for an assessment.
    # Keep it as a wire-compatible status and let deterministic binding apply
    # the same conservative treatment as indeterminate.
    PARTIAL = "partial"
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
    # Coverage rows are explanatory envelopes and compatible providers often
    # append display-only metadata.  Ignore unknown metadata at this boundary;
    # the typed decision/change_unit fields remain strictly validated and no
    # ignored value participates in routing or candidate creation.
    model_config = ConfigDict(extra="ignore")

    # Runtime aligns the row with the task's canonical ChangeUnit.  Accepting
    # an empty provider placeholder lets that deterministic alignment happen
    # instead of discarding the complete triage response.
    change_unit_id: str = ""
    decision: CoverageDecision
    reason: str = ""
    # Explanatory fields occasionally emitted by compatible providers.  They
    # are retained for traceability but never participate in routing.
    reason_for_not_local: str = ""
    issues: tuple[Any, ...] = ()
    issue_type: str = ""
    coverage_issue_type: str = ""
    coverage_issue_type_top: str = ""
    limitations: Any = ()
    common_issue_types: tuple[Any, ...] = ()
    issues_top: str = ""
    decision_motivation: str = ""
    evidence_need: str = ""
    # Additional compatibility fields emitted by some OpenAI-compatible
    # providers.  They are descriptive only; routing still uses the typed
    # ``decision`` field and never trusts model-provided candidate metadata.
    evidence_need_top: str = ""
    evidence_basis: tuple[Any, ...] = ()
    confidence: Any = None
    confidence_note: str = ""
    evidence_need_note: str = ""
    evidence_need_note2: str = ""
    location_line_alt: Any = None
    location_line_note: str = ""
    limitations_note: str = ""
    claim_type: str = ""
    impact: str = ""
    impact_locale: str = ""
    suggestion: str = ""
    decided_change_units: tuple[Any, ...] = ()
    issue_type_top: str = ""


class GraphQuestion(ControlledModel):
    subject_ref: str = ""
    direction: Literal["downstream", "upstream"] = "downstream"
    path_kind: Literal["behavior", "security"] | None = None
    expected_targets: tuple[str, ...] = ()
    required_relationships: tuple[str, ...] = ()
    max_depth: StrictInt = Field(default=3, ge=1, le=3)
    question: str = ""
    # Harmless explanatory aliases emitted by some OpenAI-compatible models;
    # routing/proof use only the typed fields above.
    confidence_note: str = ""
    evidence: str = ""
    direction2: str = ""


class CandidateSeed(ControlledModel):
    """DirectTriage 的内部候选，不是最终产品 Issue。"""

    seed_id: str = Field(default="", description="系统绑定；LLM 输出时必须为空")
    reviewer: ReviewerKind
    change_unit_id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    issue_type: str = "controlled_review_finding"
    # A few providers emit an empty explanatory mechanism while still
    # returning a concrete claim.  Triage fills the missing mechanism from the
    # claim before routing; an empty value is never sent to GraphPlan.
    mechanism: str = ""
    # DeepSeek and several OpenAI-compatible models commonly return this
    # descriptive alias even when the prompt names the field `mechanism`.
    # Accept it explicitly (rather than allowing arbitrary extras) so a
    # harmless wording variation does not discard an otherwise valid seed.
    mechanism_note: str = ""
    mechanism_note2: str = ""
    graph_question_note: str = ""
    confidence_note: str = ""
    confidence_note2: str = ""
    evidence_note: str = ""
    evidence_basis_note: str = ""
    evidence_need_note: str = ""
    location_line_alt: Any = None
    location_line_note: str = ""
    overlaps_with: Any = None
    # ``claim_type`` is a harmless descriptive tag emitted by some
    # OpenAI-compatible structured-output models.  Routing continues to use
    # the typed proof_scope/evidence_need fields, so this is intentionally
    # informational and never affects a candidate decision.
    claim_type: str = ""
    limitations: Any = ()
    impact: str = ""
    impact_locale: str = ""
    suggestion: str = ""
    location_file: str = Field(min_length=1)
    location_line: StrictInt = Field(default=0, ge=0)
    proof_scope: ProofScope
    evidence_basis: tuple[
        Literal["changed_lines", "deletion_patch", "local_source", "knowledge"], ...
    ] = ()
    evidence_need: EvidenceNeed = EvidenceNeed.NONE
    graph_question: GraphQuestion | None = None
    confidence: StrictFloat = Field(default=0.5, ge=0.0, le=1.0)


class InvestigationSeed(ControlledModel):
    """初筛后交给 GraphPlan 的中性调查种子，不是问题候选。"""

    seed_id: str = Field(default="", description="系统绑定；LLM 输出时必须为空")
    reviewer: ReviewerKind
    change_unit_id: str = Field(min_length=1)
    observed_change: str = Field(min_length=1)
    investigation_question: str = Field(min_length=1)
    location_file: str = Field(min_length=1)
    location_line: StrictInt = Field(default=0, ge=0)
    initial_symbol_ids: tuple[str, ...] = Field(default=(), max_length=4)
    evidence_need: EvidenceNeed = EvidenceNeed.INSPECT_PATH
    allowed_tools: tuple[str, ...] = Field(default=(), max_length=3)
    # These fields preserve the semantic contract that selected the graph
    # query.  They are optional for provider-created seeds for compatibility,
    # but an explicit behavior/security path must never be merged with an
    # unspecified or differently directed investigation.
    path_kind: Literal["behavior", "security"] | None = None
    direction: Literal["downstream", "upstream"] | None = None
    risk_dimension: str = ""
    confidence: StrictFloat = Field(default=0.5, ge=0.0, le=1.0)


class DirectTriageResult(ControlledModel):
    # Runtime validation fills missing ChangeUnit rows deterministically.  A
    # provider omission must not discard otherwise valid top-level candidates.
    coverage: tuple[CoverageDeclaration, ...] = ()
    issues: tuple[CandidateSeed, ...] = ()
    # Graph-required work is intentionally not represented as a CandidateSeed.
    # It is a neutral investigation request; the subtask React is the first
    # component allowed to form an evidence-backed finding.
    investigation_seeds: tuple[InvestigationSeed, ...] = ()
    limitations: Any = ()


class InvestigationObservation(ControlledModel):
    """React 最终结论引用的本次子任务局部观察。"""

    observation_id: str = Field(min_length=1)
    role: Literal["relation", "mechanism", "impact", "counter", "location"]


class InvestigationFinding(ControlledModel):
    """子任务 React 根据证据形成的候选草案。"""

    claim: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    impact: str = ""
    observations: tuple[InvestigationObservation, ...] = Field(default=(), max_length=3)
    location_file: str = Field(min_length=1)
    location_line: StrictInt = Field(default=0, ge=0)
    suggestion: str = ""
    type_hint: str = ""


class InvestigationResult(ControlledModel):
    """一次子任务 React 的唯一终止结果。"""

    subtask_id: str = Field(min_length=1)
    outcome: Literal["findings", "no_finding", "inconclusive", "failed"]
    findings: tuple[InvestigationFinding, ...] = Field(default=(), max_length=2)
    limitations: tuple[str, ...] = Field(default=(), max_length=6)


class SubtaskInstruction(ControlledModel):
    """GraphPlan 交给单个子任务 React 的最小、封闭调查上下文。"""

    subtask_id: str = Field(min_length=1)
    seed_id: str = Field(min_length=1)
    reviewer: ReviewerKind
    change_unit_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    observed_change: str = Field(min_length=1)
    initial_symbol_ids: tuple[str, ...] = Field(default=(), max_length=4)
    # A coherent investigation may need one directional graph tool plus the
    # source reader.  ``primary_tool`` is only an ordering hint; runtime
    # validation still enforces the domain/direction allowlist.
    allowed_tools: tuple[str, ...] = Field(default=(), max_length=4)
    primary_tool: str = ""
    # Runtime copies these routing constraints from the neutral seed.  They
    # are not model-selected capabilities; they make the subtask contract
    # visible in traces and let the tool client reject a wrong path domain.
    path_kind: Literal["behavior", "security"] | None = None
    direction: Literal["downstream", "upstream"] | None = None
    required_facts: tuple[str, ...] = Field(default=(), max_length=6)
    stop_conditions: tuple[str, ...] = Field(default=(), max_length=6)
    max_tool_calls: StrictInt = Field(default=6, ge=0, le=20)
    max_rounds: StrictInt = Field(default=4, ge=1, le=12)


class SubtaskPlan(ControlledModel):
    """一个 reviewer 的 GraphPlan 输出；只包含调查指导，不包含候选。"""

    reviewer: ReviewerKind
    task_id: str = Field(min_length=1)
    subtasks: tuple[SubtaskInstruction, ...] = Field(default=(), max_length=12)


class EvidenceStep(ControlledModel):
    step_id: str = Field(default="", description="系统绑定；LLM 输出时必须为空")
    tool: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    path_kind: Literal["behavior", "security"] | None = None
    max_depth: StrictInt | None = Field(default=None, ge=1, le=3)
    # Compatible providers occasionally omit one of these explanatory fields.
    # The GraphPlan validator fills a deterministic question/tool description;
    # the executable subject and tool remain strictly validated.
    purpose: str = ""
    expected_fact: str = ""
    required: bool = True
    depends_on: tuple[str, ...] = ()
    result_selector: str = ""


class WorkItem(ControlledModel):
    work_item_id: str = Field(default="", description="系统绑定；LLM 输出时必须为空")
    seed_id: str = Field(min_length=1)
    reviewer: ReviewerKind
    hypothesis: str = Field(min_length=1)
    expected_mechanism: str = Field(min_length=1)
    # Permit one extra model-proposed step through parsing so the deterministic
    # GraphPlan validator can isolate the invalid WorkItem and use its bounded
    # baseline plan.  Runtime execution still enforces the 1–2 step contract.
    # Parse a provider's bounded overproduction so the deterministic plan
    # validator can trim it to the executable 1-2 step contract.  Keeping the
    # parser ceiling finite prevents an oversized response from becoming an
    # unbounded execution plan.
    evidence_steps: tuple[EvidenceStep, ...] = Field(min_length=1, max_length=4)
    candidate_criteria: str = Field(
        default="以 EvidenceStep 返回的目标关系/源码事实核对候选主张",
        min_length=1,
    )
    rejection_criteria: str = Field(
        default="证据缺少必要事实或仅为 partial/indeterminate 时不作确定结论",
        min_length=1,
    )
    # Some OpenAI-compatible models place the selector beside the step list
    # instead of on EvidenceStep.  It is informational and ignored by the
    # executor; accepting it avoids discarding the whole plan.
    result_selector: str = ""


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
    # Assessment responses similarly carry provider-side notes whose names are
    # not part of the executable contract.  Ignore only those unknown fields;
    # statuses, claims, proof scope and evidence refs stay typed below.
    model_config = ConfigDict(extra="ignore")

    work_item_id: str = Field(min_length=1)
    status: AssessmentStatus
    claim: str = Field(min_length=1)
    # The field is an explanatory echo; when a provider leaves it blank the
    # assessment remains valid and the runtime can retain the seed mechanism.
    mechanism: str = ""
    # Optional model-side explanation accepted for protocol compatibility;
    # the deterministic verifier ignores it and relies on status/refs.
    status_reason: str = ""
    supporting_refs_note: str = ""
    supporting_refs_note_placeholder: str = ""
    counter_refs_note: str = ""
    mechanism_note: str = ""
    mechanism_note_detail: str = ""
    claim_type: str = ""
    impact_locale_note: str = ""
    impact: str = ""
    impact_locale: str = ""
    # These fields are provider-side explanations/normalizations.  They are
    # retained for traceability but are never used as proof or as a keep/drop
    # decision by the deterministic verifier.
    proof_scope_confirmed: str = ""
    proof_scope_raw: str = ""
    counter_refs_note_placeholder: str = ""
    expected_fact_note: str = ""
    single_fact: bool = False
    proof_scope: ProofScope
    supporting_refs: tuple[str, ...] = Field(default=(), max_length=3)
    counter_refs: tuple[str, ...] = Field(default=(), max_length=3)
    limitations: Any = ()
    additional_evidence_question: str = ""
    # Providers may return a short ranked list; execution still consumes only
    # the first validated step per candidate and the task-level Delta budget.
    additional_steps: tuple[EvidenceStep, ...] = Field(default=(), max_length=2)


class EvidenceAssessmentBatch(ControlledModel):
    assessments: tuple[EvidenceAssessment, ...] = ()


class ProofMatch(ControlledModel):
    work_item_id: str = Field(min_length=1)
    status: ProofMatchStatus
    matched_targets: tuple[str, ...] = ()
    matched_relationships: tuple[str, ...] = ()
    limitations: tuple[Any, ...] = ()


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
    "InvestigationFinding",
    "InvestigationObservation",
    "InvestigationResult",
    "InvestigationSeed",
    "GraphQuestion",
    "KnowledgeRoutePlan",
    "ProofMatch",
    "ProofMatchStatus",
    "ProofScope",
    "ReviewerGraphPlan",
    "SubtaskInstruction",
    "SubtaskPlan",
    "TaskKnowledgeRoute",
    "WorkItem",
]
