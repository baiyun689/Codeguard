"""ReviewCouncil 候选问题模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codeguard_agent.models.evidence import EvidenceRef, EvidenceRefError
from codeguard_agent.models.schemas import Issue, Severity


class CandidateIssue(BaseModel):
    """发现者 Agent 写入共享黑板的候选问题。"""

    id: str
    task_id: str
    source_agent: str
    file: str
    line: int = 0
    type: str
    severity_proposal: Severity
    claim: str
    suggestion: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    evidence_ref_errors: list[EvidenceRefError] = Field(default_factory=list)

    def to_issue(self) -> Issue:
        """裁决后转换为产品输出 Issue。"""

        return Issue(
            severity=self.severity_proposal,
            file=self.file,
            line=self.line,
            type=self.type,
            message=self.claim,
            suggestion=self.suggestion,
            confidence=self.confidence,
        )
