"""ReviewCouncil 候选问题模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codeguard_agent.models.evidence import EvidenceRef, EvidenceRefError
from codeguard_agent.models.schemas import Issue, Severity


class CandidateIssue(BaseModel):
    """发现者 Agent 写入共享黑板的候选问题。"""

    id: str = Field(description="候选问题的稳定 ID")
    task_id: str = Field(description="产生该候选的 ReviewTask ID")
    source_agent: str = Field(description="产生该候选的 Reviewer 标识")
    file: str = Field(description="候选问题所在文件路径")
    line: int = Field(default=0, description="候选问题所在行号，0 表示暂时无法定位")
    type: str = Field(description="问题类型")
    claim: str = Field(description="候选问题的具体主张及其原因")
    suggestion: str = Field(default="", description="修复建议，可选")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="发现者对候选问题成立的置信度")
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, description="候选引用的证据账本条目")
    evidence_ref_errors: list[EvidenceRefError] = Field(default_factory=list, description="证据引用校验失败的诊断信息")

    def to_issue(self, severity: Severity) -> Issue:
        """裁决后转换为产品输出 Issue。"""

        return Issue(
            severity=severity,
            file=self.file,
            line=self.line,
            type=self.type,
            message=self.claim,
            suggestion=self.suggestion,
            confidence=self.confidence,
        )
