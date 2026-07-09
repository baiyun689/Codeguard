"""ADR-032 ReviewCouncil 的内部状态模型。

这些模型只用于图 State、trace 和 eval 诊断,不进入 `ReviewResult` 产品输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from codeguard_agent.models.schemas import Issue, Severity


SourceAgent = Literal["threat_model", "behavior", "maintainability"]


MAX_CANDIDATES_PER_AGENT = 10
MAX_EVIDENCE_REQUESTS_PER_CANDIDATE = 2
MAX_TOTAL_EVIDENCE_REQUESTS = 20
DEFAULT_MAX_EVIDENCE_ROUNDS = 2


# ── CouncilJudge 裁决模型 ──


@dataclass
class Verdict:
    """规则层产出的裁决结果。规则命中时返回此对象，返回 None 表示不命中。"""

    candidate_id: str
    action: Literal["keep", "drop", "downgrade", "merge", "needs_more_evidence"]
    reason_code: str
    reason: str = ""
    suggested_target_id: str = ""  # merge 时指向被合并方
    severity_override: Severity | None = None  # downgrade 时建议新级别
    suggested_tools: list[str] = field(default_factory=list)  # needs_more_evidence 时建议补证工具


class JudgeDecision(BaseModel):
    """LLM 终审的结构化输出：对单条候选的裁决。"""

    candidate_id: str
    action: Literal["keep", "drop", "downgrade", "merge", "needs_more_evidence"]
    reason: str = ""
    merge_target_id: str = ""  # merge 时指向被合并方
    adjusted_severity: Severity | None = None  # downgrade 时建议新级别
    suggested_tools: list[str] = Field(default_factory=list)  # needs_more_evidence 时建议工具


class JudgeDecisions(BaseModel):
    """包装模型：LLM 输出的裁决列表。

    用于 with_structured_output()——DeepSeek 等兼容端点不支持
    list[T] 泛型作为 response_format，必须用具名模型包装。
    """

    decisions: list[JudgeDecision] = Field(default_factory=list)


class ContextFact(BaseModel):
    """ContextProvider 收集到的一段事实。"""

    source: str = Field(description="事实来源,如 diff/tool:get_file_content")
    kind: str = Field(description="事实类型,如 sensitive_api/ast_structure")
    content: str = Field(description="事实内容")
    truncated: bool = Field(default=False, description="内容是否因预算被截断")


class ContextBundle(BaseModel):
    """ReviewCouncil 共享的只读上下文包。"""

    changed_files: list[str] = Field(default_factory=list)
    facts: list[ContextFact] = Field(default_factory=list)

    def render(self, budget: int = 6000) -> str:
        """渲染为 prompt 可读文本,并按字符预算截断。"""
        lines: list[str] = []
        if self.changed_files:
            lines.append("变更文件:")
            lines.extend(f"- {path}" for path in self.changed_files)
        if self.facts:
            if lines:
                lines.append("")
            lines.append("上下文事实:")
            for fact in self.facts:
                flag = " (已截断)" if fact.truncated else ""
                lines.append(f"- [{fact.source}/{fact.kind}]{flag} {fact.content}")
        text = "\n".join(lines).strip() or "(无额外上下文事实)"
        if len(text) <= budget:
            return text
        return text[:budget] + "\n...(ContextBundle 已达预算上限,后续省略)"


EvidenceNoteStatus = Literal["supported", "contradicted", "mixed", "insufficient", "not_found", "unsupported"]

# ── EvidenceAgent LLM 证据分析的结构化输出 ──


class EvidenceJudgment(BaseModel):
    """evidence_agent 的 LLM 对单条证据的语义分析结果。"""

    judgment: Literal["SUPPORTS", "CONTRADICTS", "INSUFFICIENT"] = Field(
        description="证据对候选主张意味着什么"
    )
    reasoning: str = Field(default="", description="推理依据")
class EvidenceRequest(BaseModel):
    """候选 issue 对证据的结构化请求。"""

    id: str = ""
    candidate_id: str
    target: str = ""
    question: str = ""
    preferred_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def assign_stable_id(self) -> "EvidenceRequest":
        if not self.id:
            payload = "\0".join(
                [
                    self.candidate_id,
                    self.target,
                    self.question,
                    *self.preferred_tools,
                ]
            )
            self.id = f"evidence-{sha256(payload.encode('utf-8')).hexdigest()[:16]}"
        return self


class CandidateIssue(BaseModel):
    """发现者 Agent 写入共享黑板的候选问题。"""

    id: str
    source_agent: str
    file: str
    line: int = 0
    type: str
    severity_proposal: Severity
    claim: str
    suggestion: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @classmethod
    def from_issue(
        cls,
        issue: Issue,
        *,
        index: int,
        source_agent: str,
    ) -> "CandidateIssue":
        """把现有 reviewer 输出转换为内部候选结构。"""
        cid = f"{source_agent}-{index}-{issue.file}:{issue.line}:{issue.type}"
        return cls(
            id=cid,
            source_agent=source_agent,
            file=issue.file,
            line=issue.line,
            type=issue.type,
            severity_proposal=issue.severity,
            claim=issue.message,
            suggestion=issue.suggestion,
            confidence=issue.confidence,
        )

    def to_issue(self) -> Issue:
        """裁决后转换回产品输出 Issue。"""
        return Issue(
            severity=self.severity_proposal,
            file=self.file,
            line=self.line,
            type=self.type,
            message=self.claim,
            suggestion=self.suggestion,
            confidence=self.confidence,
        )


def build_evidence_requests(candidate: CandidateIssue) -> list[EvidenceRequest]:
    if candidate.confidence >= 0.75 and candidate.line > 0:
        return []
    agent_tools = {
        "threat_model": ["find_sensitive_apis", "get_file_content"],
        "behavior": ["find_callers", "get_file_content"],
        "maintainability": ["get_code_metrics", "get_file_content"],
    }
    return [
        EvidenceRequest(
            candidate_id=candidate.id,
            target=candidate.file,
            question=(
                f"确认 {candidate.file} 中候选问题的相关代码片段"
                "是否支持该主张"
            ),
            preferred_tools=list(
                agent_tools.get(candidate.source_agent, ["get_file_content"])
            ),
        )
    ]


class EvidenceNote(BaseModel):
    """EvidenceAgent 写入的证据记录。

    supports/contradicts/unknowns 每条条目包含推理依据（格式: "[工具名] 判定: 推理"）。
    status 根据 supports/contradicts 分布自动计算。
    """

    request_id: str
    candidate_id: str
    status: EvidenceNoteStatus = "mixed"
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class CouncilTrace(BaseModel):
    """ReviewCouncil 的轻量过程事件。"""

    node: str
    event: str
    detail: str = ""


class CouncilRunStats(BaseModel):
    """供 eval/report 使用的 ReviewCouncil 统计。"""

    candidate_count: int = 0
    candidate_count_by_agent: dict[str, int] = Field(default_factory=dict)
    evidence_request_count: int = 0
    truncated_candidates: int = 0
    truncated_evidence_requests: int = 0
    evidence_rounds: int = 0
    challenge_count: int = 0
    removed_by_challenge: int = 0
    removed_by_aggregation: int = 0
    removed_by_fp_rules: int = 0
    removed_by_fp_llm: int = 0
