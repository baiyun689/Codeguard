"""Provider-facing schemas for controlled structured-output calls.

OpenAI-compatible providers sometimes append display-only fields to otherwise
valid objects (for example ``confidence_note`` or an alternate location
label).  The internal controlled models deliberately keep ``extra=forbid`` so
runtime code cannot silently consume an unowned field.  These small transport
subclasses are the only tolerant boundary: unknown metadata is ignored by the
provider parser, then the parsed object is converted back to the strict model
before any routing or execution occurs.
"""

from __future__ import annotations

from typing import Any, get_args

from pydantic import ConfigDict, model_validator

from codeguard_agent.models.tasks import (
    CandidateSeed,
    CoverageDeclaration,
    DirectTriageResult,
    EvidenceAssessment,
    EvidenceAssessmentBatch,
    EvidenceStep,
    GraphQuestion,
    ReviewerGraphPlan,
    WorkItem,
)
from codeguard_agent.models.tasks.controlled import ProofScope, ReviewerKind


class _ProviderEnvelope:
    """Tolerate JSON null for fields that have a model-side default.

    OpenAI-compatible providers frequently serialize an omitted optional field
    as ``null``.  Pydantic would normally reject that before the controlled
    boundary can apply its per-row salvage.  Removing only non-required fields
    keeps claims, routing discriminators, and other semantic requirements
    strict while treating null exactly like omission.
    """

    @model_validator(mode="before")
    @classmethod
    def _drop_null_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        fields = cls.model_fields
        # A second, narrower compatibility repair handles display-only string
        # fields that some providers serialize as booleans/numbers.  Semantic
        # strings (claim, symbol IDs, questions, executable tool names) remain
        # strict and therefore still fail closed when their type is wrong.
        display_fields = {
            "reason",
            "reason_for_not_local",
            "issue_type",
            "coverage_issue_type",
            "coverage_issue_type_top",
            "issues_top",
            "decision_motivation",
            "evidence_need",
            "evidence_need_top",
            "confidence_note",
            "confidence_note2",
            "evidence_note",
            "evidence_basis_note",
            "evidence_need_note",
            "evidence_need_note2",
            "location_line_note",
            "limitations_note",
            "claim_type",
            "impact",
            "impact_locale",
            "impact_locale_note",
            "suggestion",
            "status_reason",
            "supporting_refs_note",
            "supporting_refs_note_placeholder",
            "counter_refs_note",
            "counter_refs_note_placeholder",
            "mechanism_note",
            "mechanism_note2",
            "mechanism_note_detail",
            "proof_scope_confirmed",
            "proof_scope_raw",
            "expected_fact_note",
            "direction2",
            "evidence",
            "result_selector",
        }
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if item is None and key in fields and not fields[key].is_required():
                continue
            annotation = fields[key].annotation if key in fields else None
            string_compatible = annotation is str or str in get_args(annotation)
            if (
                key in display_fields
                and string_compatible
                and item is not None
                and not isinstance(item, str)
            ):
                item = str(item)
            normalized[key] = item
        return normalized


class LlmGraphQuestion(_ProviderEnvelope, GraphQuestion):
    model_config = ConfigDict(extra="ignore")
    # Empty optional literals are a frequent OpenAI-compatible serialization
    # artifact.  Keep them as strings at the transport boundary; triage
    # normalizes blank values before strict runtime validation.
    direction: str = "downstream"
    path_kind: str | None = None


class LlmCandidateSeed(_ProviderEnvelope, CandidateSeed):
    model_config = ConfigDict(extra="ignore")
    # A few OpenAI-compatible models emit an empty placeholder item after a
    # valid candidate.  Keep the provider envelope permissive enough to parse
    # that placeholder; triage filters it before converting to strict
    # CandidateSeed.  Non-empty malformed candidates still fail closed.
    reviewer: ReviewerKind | None = None
    change_unit_id: str = ""
    claim: str = ""
    proof_scope: ProofScope | None = None
    location_file: str = ""
    graph_question: LlmGraphQuestion | None = None


class LlmCoverageDeclaration(_ProviderEnvelope, CoverageDeclaration):
    model_config = ConfigDict(extra="ignore")


class LlmDirectTriageResult(_ProviderEnvelope, DirectTriageResult):
    model_config = ConfigDict(extra="ignore")
    coverage: tuple[LlmCoverageDeclaration, ...] = ()
    issues: tuple[LlmCandidateSeed, ...] = ()


class LlmEvidenceStep(_ProviderEnvelope, EvidenceStep):
    model_config = ConfigDict(extra="ignore")


class LlmWorkItem(_ProviderEnvelope, WorkItem):
    model_config = ConfigDict(extra="ignore")
    evidence_steps: tuple[LlmEvidenceStep, ...]


class LlmReviewerGraphPlan(_ProviderEnvelope, ReviewerGraphPlan):
    model_config = ConfigDict(extra="ignore")
    work_items: tuple[LlmWorkItem, ...] = ()


class LlmEvidenceAssessment(_ProviderEnvelope, EvidenceAssessment):
    model_config = ConfigDict(extra="ignore")
    additional_steps: tuple[LlmEvidenceStep, ...] = ()


class LlmEvidenceAssessmentBatch(_ProviderEnvelope, EvidenceAssessmentBatch):
    model_config = ConfigDict(extra="ignore")
    assessments: tuple[LlmEvidenceAssessment, ...] = ()


__all__ = [
    "LlmDirectTriageResult",
    "LlmEvidenceAssessmentBatch",
    "LlmReviewerGraphPlan",
]
