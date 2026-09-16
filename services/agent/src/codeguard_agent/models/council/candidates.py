"""ReviewCouncil 候选问题模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codeguard_agent.models.evidence import EvidenceRef, EvidenceRefError
from codeguard_agent.models.schemas import EvidenceLocation, Issue, Severity


class CandidateIssue(BaseModel):
    """发现者 Agent 写入共享黑板的候选问题。"""

    id: str = Field(description="候选问题的稳定 ID")
    task_id: str = Field(description="产生该候选的 ReviewTask ID")
    source_agent: str = Field(description="产生该候选的 Reviewer 标识")
    file: str = Field(description="候选问题所在文件路径")
    line: int = Field(default=0, description="候选问题所在行号，0 表示暂时无法定位")
    type: str = Field(description="问题类型")
    claim: str = Field(description="候选问题的具体主张及其原因")
    # 候选的补充说明供裁决模型使用，不进入对外 Issue；未提供时保留空值。
    mechanism: str = Field(default="", description="候选机制的补充说明", exclude=True)
    impact: str = Field(default="", description="候选影响的补充说明", exclude=True)
    impact_locale: str = Field(default="", description="候选影响对象/边界", exclude=True)
    claim_type: str = Field(default="", description="候选主张的内部类型标签", exclude=True)
    # 运行时从已绑定证据提取的行为事实，用于补充候选中的具体影响。
    evidence_observation: str = Field(default="", description="已验证的证据观察", exclude=True)
    # 发布 Issue 前根据已验证证据生成来源位置；不接收模型填写的位置元数据。
    root_cause: str = Field(default="", description="用户可读的已验证根因说明", exclude=True)
    evidence_locations: list[EvidenceLocation] = Field(
        default_factory=list,
        max_length=4,
        description="用户可读的证据位置摘要",
        exclude=True,
    )
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
            message=(
                self.claim
                if not self.evidence_observation.strip()
                else f"{self.claim.rstrip('。；; ')}；{self.evidence_observation.strip()}"
            ),
            root_cause=self.root_cause,
            evidence_locations=list(self.evidence_locations),
            suggestion=self.suggestion,
            confidence=self.confidence,
        )
