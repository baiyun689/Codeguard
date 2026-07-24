"""ADR-032 ReviewCouncil 的内部状态模型。

这些模型只用于图 State、trace 和 eval 诊断,不进入 `ReviewResult` 产品输出。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from codeguard_agent.models.schemas import Issue, Severity


SourceAgent = Literal["threat_model", "behavior", "maintainability"]
EvidencePurpose = Literal["support", "counter", "severity"]


MAX_CANDIDATES_PER_AGENT = 10
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# ── CouncilJudge 裁决模型 ──


@dataclass
class Verdict:
    """Evidence-gate + synthesis adjudication outcome."""

    candidate_id: str
    action: Literal["keep", "drop"]
    reason_code: str
    reason: str = ""
    resolved_severity: Severity | None = None


# ── Evidence synthesis models (ADR-032 evidence-gated severity) ──


class SeverityFactorAssessment(BaseModel):
    """LLM evidence synthesizer 对单个 severity factor 的评估。"""

    factor_id: NonBlankStr
    status: Literal["proven", "disproven", "unknown"]
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class CandidateEvidenceAssessment(BaseModel):
    """LLM evidence synthesizer 对单个候选的完整证据综合。"""

    candidate_id: NonBlankStr
    claim_status: Literal["supported", "refuted", "unresolved"]
    counter_effect: Literal["none", "partial", "complete", "unknown"]
    severity_factors: list[SeverityFactorAssessment] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    reason: str = ""


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


class EvidenceRequest(BaseModel):
    """候选 issue 对证据的结构化请求。"""

    id: str = ""
    candidate_id: NonBlankStr
    strategy_id: NonBlankStr
    purpose: EvidencePurpose
    target: NonBlankStr
    question: NonBlankStr
    preferred_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def assign_stable_id(self) -> "EvidenceRequest":
        if not self.id:
            payload = "\0".join(
                [
                    self.candidate_id,
                    self.strategy_id,
                    self.purpose,
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
    task_id: str
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
        task_id: str,
    ) -> "CandidateIssue":
        """把现有 reviewer 输出转换为内部候选结构。task_id 必填（spec §3.2）。"""
        cid = f"{source_agent}-{index}-{issue.file}:{issue.line}:{issue.type}"
        return cls(
            id=cid,
            task_id=task_id,
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


class EvidenceFinding(BaseModel):
    """一项事实与候选主张之间的受约束关系。"""

    evidence_id: NonBlankStr
    source: NonBlankStr
    observation: str
    relation: Literal["supports", "contradicts", "insufficient"]
    strength: Literal["direct", "contextual"]
    limitation: str = ""

    @model_validator(mode="after")
    def validate_safe_relation(self) -> "EvidenceFinding":
        if self.relation in {"supports", "contradicts"} and not self.observation.strip():
            raise ValueError("supports/contradicts finding requires observation")
        if self.relation == "insufficient":
            if self.strength != "contextual":
                raise ValueError("insufficient finding must be contextual")
            if not self.limitation.strip():
                raise ValueError("insufficient finding requires limitation")
        return self


class EvidenceNote(BaseModel):
    """一个请求对应的非空证据发现集合。"""

    request_id: NonBlankStr
    candidate_id: NonBlankStr
    findings: list[EvidenceFinding] = Field(min_length=1)


class CouncilTrace(BaseModel):
    """ReviewCouncil 的轻量过程事件。"""

    node: str
    event: str
    detail: str = ""


class CouncilRunStats(BaseModel):
    """供 eval/report 使用的 ReviewCouncil 统计。"""

    candidate_count: int = Field(default=0, description="本次进入 Council 的候选总数（归并后）")
    candidate_count_by_agent: dict[str, int] = Field(default_factory=dict)
    raw_candidate_count: int = Field(default=0, description="归并前的原始候选总数")
    candidate_dedup_removed_count: int = Field(default=0, description="归并移除的候选数")
    candidate_dedup_llm_calls: int = Field(default=0, description="归并 LLM 调用次数")
    candidate_dedup_block_failure_count: int = Field(default=0, description="归并失败块数")
    evidence_request_count: int = Field(default=0, description="累计证据请求总数")
    truncated_candidates: int = Field(default=0, description="发现阶段因候选上限被截断的数量")
    verdict_count: int = Field(default=0, description="Judge 产生的候选裁决总数")
    removed_by_judge: int = Field(default=0, description="Judge 裁决为 drop 的候选数")
    removed_by_fp_rules: int = 0
    removed_by_fp_llm: int = 0
    no_support_candidate_count: int = Field(
        default=0, description="因缺少 support 证据而被 gate 拒绝的候选数"
    )
    no_support_retained_count: int = Field(
        default=0, description="缺少 support 证据但仍映射到最终 Issue 的候选数"
    )
    direct_counter_candidate_count: int = Field(
        default=0, description="具备 counter+direct+contradicts finding 的候选数"
    )
    direct_counter_retained_count: int = Field(
        default=0, description="直接反证候选中仍映射到最终 Issue 的数量"
    )
    direct_counter_retained_rate: float | None = Field(
        default=None,
        description="direct_counter_retained_count/direct_counter_candidate_count；分母为零时 None",
    )
    all_insufficient_candidate_count: int = Field(
        default=0, description="关联 finding 非空且全部 insufficient 的候选数"
    )
    all_insufficient_retained_count: int = Field(
        default=0, description="全 insufficient 候选中仍映射到最终 Issue 的数量"
    )
    all_insufficient_retained_rate: float | None = Field(
        default=None,
        description="all_insufficient_retained_count/all_insufficient_candidate_count；分母为零时 None",
    )
    severity_defaulted_count: int = Field(
        default=0, description="使用 RiskTag 固定默认等级的候选数"
    )
    critical_candidate_count: int = Field(
        default=0, description="最终解析为 CRITICAL 的候选数"
    )
    critical_policy_matched_count: int = Field(
        default=0, description="满足完整 CRITICAL policy 的候选数"
    )
    critical_missing_factor_count: int = Field(
        default=0, description="所有候选累计缺失的 CRITICAL factor 数"
    )
    severity_transitions: dict[str, int] = Field(
        default_factory=dict,
        description="severity_proposal 到 resolved_severity 的转移计数",
    )
    final_issue_count: int = Field(default=0, description="最终 Issue 对应的 survivor 候选数")
    final_issue_strategy_covered_count: int = Field(
        default=0, description="survivor 中至少关联一条有效 EvidenceRequest 的数量"
    )
    final_issue_strategy_coverage: float | None = Field(
        default=None,
        description="final_issue_strategy_covered_count/final_issue_count；分母为零时 None",
    )
    final_issue_fact_covered_count: int = Field(
        default=0, description="survivor 中至少有关联非 insufficient finding 的数量"
    )
    final_issue_fact_coverage: float | None = Field(
        default=None,
        description="final_issue_fact_covered_count/final_issue_count；分母为零时 None",
    )
    registry_risk_tag_covered_count: int = Field(
        default=0, description="同时具有 counter/support/severity 策略的 RiskTag 数"
    )
    registry_risk_tag_total: int = Field(default=0, description="当前 RiskTag 枚举值总数")
    registry_risk_tag_coverage: float | None = Field(
        default=None,
        description="registry_risk_tag_covered_count/registry_risk_tag_total；分母为零时 None",
    )
    actual_evidence_tool_calls: int = Field(
        default=0, description="EvidenceAgent 实际新工具调用数；缓存复用不计"
    )
    average_evidence_tool_calls: float = Field(
        default=0.0,
        description="actual_evidence_tool_calls/candidate_count；无候选时固定为 0.0",
    )
    # ── 降级指标 ──
    react_degraded_recursion_count: int = Field(
        default=0, description="ReAct 撞递归上限降级 DirectEngine 的次数"
    )
    react_degraded_empty_count: int = Field(
        default=0, description="ReAct 空结果降级 DirectEngine 的次数"
    )
    direct_tier_task_count: int = Field(
        default=0, description="路由为 tier=direct（不使用 ReAct）的 task 数"
    )
    discoverer_failed_count: int = Field(
        default=0, description="完全失败（异常跳过）的发现者调用次数"
    )
    task_review_failed_count: int = Field(
        default=0, description="per-task 审查调用返回 None 的次数"
    )
    judge_synthesis_failed_count: int = Field(
        default=0, description="CouncilJudge LLM synthesis 失败使用默认 severity 的次数"
    )
    evidence_plan_skipped_count: int = Field(
        default=0, description="证据规划因超 cap 跳过的请求数"
    )
