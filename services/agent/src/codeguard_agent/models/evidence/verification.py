"""Evidence Ledger 的引用与确定性验证结果模型。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from codeguard_agent.models.evidence.artifact import EvidenceArtifact, EvidenceSourceKind
from codeguard_agent.models.schemas import EvidenceRole


class EvidenceRef(BaseModel):
    """候选对一条已绑定 Artifact 的引用。"""

    artifact_id: str
    declared_role: EvidenceRole
    auto_bound: bool = False


class EvidenceRefErrorReason(str, Enum):
    UNKNOWN_ALIAS = "unknown_alias"
    CROSS_TASK_REFERENCE = "cross_task_reference"
    CROSS_REVISION_REFERENCE = "cross_revision_reference"
    ARTIFACT_FAILED = "artifact_failed"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"


class EvidenceRefError(BaseModel):
    alias: str
    reason: EvidenceRefErrorReason
    detail: str = ""


class EvidenceValidationStatus(str, Enum):
    VALID = "valid"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class VerifiedEvidence(BaseModel):
    """一条经确定性验证、可供 Judge 引用的事实。"""

    artifact_id: str
    source_kind: EvidenceSourceKind
    tool: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)
    declared_role: EvidenceRole = EvidenceRole.MECHANISM
    content: str
    validation_status: EvidenceValidationStatus
    limitations: tuple[str, ...] = ()


class EvidenceGap(BaseModel):
    """一次真实证据查询未能产生可引用事实。"""

    artifact_id: str
    tool: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)
    declared_role: EvidenceRole
    reason: str
    limitations: tuple[str, ...] = ()


class CandidateVerification(BaseModel):
    candidate_id: str
    source_kinds: set[EvidenceSourceKind] = Field(default_factory=set)
    valid_evidence: list[VerifiedEvidence] = Field(default_factory=list)
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list)
    invalid_references: list[EvidenceRefError] = Field(default_factory=list)
    grounding_status: Literal["grounded", "partially_grounded", "ungrounded"]
    eligible_for_judge: bool
    rejection_reason: str = ""


class VerificationBatch(BaseModel):
    candidates: dict[str, CandidateVerification] = Field(default_factory=dict)
    replayed_artifact_ids: list[str] = Field(default_factory=list)
    replayed_artifacts: dict[str, EvidenceArtifact] = Field(default_factory=dict)
    trace: list[tuple[str, str]] = Field(default_factory=list)
