"""ADR-032 ReviewCouncil 的内部状态模型。

这些模型只用于图 State、trace 和 eval 诊断,不进入 `ReviewResult` 产品输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from codeguard_agent.models.schemas import Issue, Severity


SourceAgent = Literal["threat_model", "behavior", "maintainability"]


AGENT_CATEGORY_MAP: dict[str, str] = {
    "threat_model": "security",
    "behavior": "logic",
    "maintainability": "quality",
    # 旧名称兼容:ADR-032 第一版中间态可能仍以旧 reviewer 名写入。
    "security": "security",
    "logic": "logic",
    "quality": "quality",
}

AGENT_DISPLAY_NAME_MAP: dict[str, str] = {
    "threat_model": "ThreatModelAgent",
    "behavior": "BehaviorAgent",
    "maintainability": "MaintainabilityAgent",
}

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

    source: str = Field(description="事实来源,如 diff/summary/tool:get_file_content")
    kind: str = Field(description="事实类型,如 changed_file/summary/sensitive_api")
    content: str = Field(description="事实内容")
    truncated: bool = Field(default=False, description="内容是否因预算被截断")


class ContextBundle(BaseModel):
    """ReviewCouncil 共享的只读上下文包。"""

    changed_files: list[str] = Field(default_factory=list)
    diff_summary: str = ""
    facts: list[ContextFact] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    truncated: bool = False

    def render(self, budget: int = 6000) -> str:
        """渲染为 prompt 可读文本,并按字符预算截断。"""
        lines: list[str] = []
        if self.changed_files:
            lines.append("变更文件:")
            lines.extend(f"- {path}" for path in self.changed_files)
        if self.diff_summary:
            lines.append("")
            lines.append("变更摘要:")
            lines.append(self.diff_summary)
        if self.facts:
            lines.append("")
            lines.append("上下文事实:")
            for fact in self.facts:
                flag = " (已截断)" if fact.truncated else ""
                lines.append(f"- [{fact.source}/{fact.kind}]{flag} {fact.content}")
        text = "\n".join(lines).strip() or "(无额外上下文事实)"
        if len(text) <= budget:
            return text
        return text[:budget] + "\n...(ContextBundle 已达预算上限,后续省略)"


EvidenceStatus = Literal["sufficient", "partial", "missing"]
EvidenceNoteStatus = Literal["supported", "not_found", "unsupported", "mixed"]
ChallengeVerdict = Literal["keep", "downgrade", "merge", "drop", "needs_more_evidence"]


class EvidenceRequest(BaseModel):
    """候选 issue 对证据的结构化请求。"""

    candidate_id: str
    target: str = ""
    question: str = ""
    reason: str = ""
    preferred_tools: list[str] = Field(default_factory=list)
    reason_code: str = ""


class CandidateIssue(BaseModel):
    """发现者 Agent 写入共享黑板的候选问题。"""

    id: str
    source_agent: str
    category: str = ""
    file: str
    line: int = 0
    type: str
    severity_proposal: Severity
    claim: str
    suggestion: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_status: EvidenceStatus = "missing"
    needs_evidence: bool = False
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list)
    evidence_notes: list["EvidenceNote"] = Field(default_factory=list)
    challenge: "Challenge | None" = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @property
    def agent(self) -> str:
        """旧字段兼容别名。"""
        return self.source_agent

    @classmethod
    def from_issue(
        cls,
        issue: Issue,
        *,
        index: int,
        source_agent: str | None = None,
        category: str | None = None,
        agent: str | None = None,
    ) -> "CandidateIssue":
        """把现有 reviewer 输出转换为内部候选结构。"""
        resolved_agent = source_agent or agent or "unknown"
        resolved_category = category or AGENT_CATEGORY_MAP.get(resolved_agent, resolved_agent)
        cid = f"{resolved_agent}-{index}-{issue.file}:{issue.line}:{issue.type}"
        needs_evidence = issue.confidence < 0.75 or issue.line <= 0
        requests = []
        if needs_evidence:
            # 按 source_agent 分派默认证据工具:
            #   threat_model → 查敏感 API + 读文件
            #   behavior → 查调用方 + 读文件
            #   maintainability → 查代码度量 + 读文件
            # 低置信度或无行号时额外追加 get_file_content。
            agent_tools: dict[str, list[str]] = {
                "threat_model": ["find_sensitive_apis", "get_file_content"],
                "behavior": ["find_callers", "get_file_content"],
                "maintainability": ["get_code_metrics", "get_file_content"],
            }
            preferred = list(agent_tools.get(resolved_agent, ["get_file_content"]))
            requests.append(
                EvidenceRequest(
                    candidate_id=cid,
                    target=issue.file,
                    question=f"确认 {issue.file} 中候选问题的相关代码片段是否支持该主张",
                    reason="候选定位不完整或置信度偏低,需要补充代码事实",
                    preferred_tools=preferred,
                    reason_code="low_confidence_or_unlocated",
                )
            )
        return cls(
            id=cid,
            source_agent=resolved_agent,
            category=resolved_category,
            file=issue.file,
            line=issue.line,
            type=issue.type,
            severity_proposal=issue.severity,
            claim=issue.message,
            suggestion=issue.suggestion,
            evidence_status="partial" if needs_evidence else "sufficient",
            needs_evidence=needs_evidence,
            evidence_requests=requests,
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


class EvidenceNote(BaseModel):
    """EvidenceAgent 写入的证据记录。"""

    candidate_id: str
    status: EvidenceNoteStatus = "mixed"
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class Challenge(BaseModel):
    """ChallengeAgent 写入的候选质疑结果。"""

    candidate_id: str
    verdict: ChallengeVerdict = "keep"
    reason: str = ""
    suggested_target_id: str = ""


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
