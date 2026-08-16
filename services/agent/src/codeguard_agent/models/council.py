"""ReviewCouncil 的内部状态模型。

这些模型只用于图 State、trace 和 eval 诊断,不进入 `ReviewResult` 产品输出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    model_validator,
)

from codeguard_agent.models.schemas import EvidenceTraceStep, Issue, Severity

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


class CandidateDirectAssessment(BaseModel):
    """统一裁决模型(ADR-046):完整档终审与无证据链消融档共用。

    完整档的 severity 必须引用证据(cited_fact_ids 非空);
    消融档无证据输入,cited_fact_ids 恒为空——两档唯一差异是输入里有没有证据。
    """

    candidate_id: NonBlankStr
    action: Literal["keep", "drop"]
    severity: Severity
    reason: str = ""
    cited_fact_ids: tuple[str, ...] = ()


class CandidateFact(BaseModel):
    """一条已取得的原始事实及其重放验证状态。

    replay_status:
    - verified:   链引用原文在重放结果中命中(规范化子串匹配)
    - unverified: 链有引用但重放结果中找不到
    - failed:     工具调用失败或图响应无效(沙箱拒绝/符号不存在/参数非法,详见 limitation)
    - recipe:     固定配方来源(非重放),无引用可验证
    """

    fact_id: NonBlankStr
    source: NonBlankStr
    raw: str = ""
    replay_status: Literal["verified", "unverified", "failed", "recipe"] = "unverified"
    limitation: str = ""


class FactRelation(BaseModel):
    """一条事实与候选主张之间的受约束关系(ADR-046 关系三元主轴)。

    一条事实对一条主张只有三种可能:支持它、否定它、说明不了什么——完备划分。
    """

    fact_id: NonBlankStr
    relation: Literal["supports", "contradicts", "insufficient"]
    strength: Literal["direct", "contextual"] = "contextual"
    observation: str = ""
    limitation: str = ""

    @model_validator(mode="after")
    def validate_safe_relation(self) -> "FactRelation":
        if self.relation in {"supports", "contradicts"} and not self.observation.strip():
            raise ValueError("supports/contradicts relation requires observation")
        if self.relation == "insufficient":
            if self.strength != "contextual":
                raise ValueError("insufficient relation must be contextual")
            if not self.limitation.strip():
                raise ValueError("insufficient relation requires limitation")
        return self


class ContextFact(BaseModel):
    """ContextProvider 收集到的一段事实。"""

    source: str = Field(description="事实来源,如 diff/tool:get_file_content")
    kind: str = Field(description="事实类型,如 symbol_context/ast_structure")
    content: str = Field(description="事实内容")
    truncated: bool = Field(default=False, description="内容是否因预算被截断")


class ContextBundle(BaseModel):
    """ReviewCouncil 共享的只读上下文包。"""

    changed_files: list[str] = Field(default_factory=list)
    facts: list[ContextFact] = Field(default_factory=list)


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
    evidence_chain: list[EvidenceTraceStep] = Field(
        default_factory=list,
        description="取证溯源:直接支撑该候选的工具调用与引文(ADR-046)",
    )

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
            evidence_chain=list(issue.evidence_chain),
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
            evidence_chain=list(self.evidence_chain),
        )


class CouncilTrace(BaseModel):
    """ReviewCouncil 的轻量过程事件。"""

    node: str
    event: str
    detail: str = ""


class CouncilRunStats(BaseModel):
    """供 eval/report 使用的 ReviewCouncil 统计。"""

    candidate_count: int = Field(default=0, description="本次进入 Evidence/Judge 的候选成员总数")
    candidate_count_by_agent: dict[str, int] = Field(default_factory=dict)
    raw_candidate_count: int = Field(default=0, description="归并前的原始候选总数")
    logical_candidate_count: int = Field(default=0, description="严格等价分组后的逻辑候选数")
    candidate_grouped_member_count: int = Field(
        default=0,
        description="逻辑分组减少量；成员仍会独立进入 Evidence/Judge",
    )
    candidate_dedup_removed_count: int = Field(default=0, description="归并阶段真实删除的候选数")
    candidate_dedup_llm_calls: int = Field(default=0, description="归并 LLM 调用次数")
    candidate_dedup_block_failure_count: int = Field(default=0, description="归并失败块数")
    truncated_candidates: int = Field(default=0, description="发现阶段因候选上限被截断的数量")
    verdict_count: int = Field(default=0, description="Judge 产生的候选裁决总数")
    removed_by_judge: int = Field(default=0, description="Judge 裁决为 drop 的候选数")
    no_support_candidate_count: int = Field(
        default=0, description="因缺少 support 证据而被 gate 拒绝的候选数"
    )
    no_support_retained_count: int = Field(
        default=0, description="缺少 support 证据但仍映射到最终 Issue 的候选数"
    )
    direct_counter_candidate_count: int = Field(
        default=0, description="具备 contradicts+direct 关系的候选数"
    )
    direct_counter_retained_count: int = Field(
        default=0, description="直接反证候选中仍映射到最终 Issue 的数量"
    )
    direct_counter_retained_rate: float | None = Field(
        default=None,
        description="direct_counter_retained_count/direct_counter_candidate_count；分母为零时 None",
    )
    all_insufficient_candidate_count: int = Field(
        default=0, description="关联关系非空且全部 insufficient 的候选数"
    )
    all_insufficient_retained_count: int = Field(
        default=0, description="全 insufficient 候选中仍映射到最终 Issue 的数量"
    )
    all_insufficient_retained_rate: float | None = Field(
        default=None,
        description="all_insufficient_retained_count/all_insufficient_candidate_count；分母为零时 None",
    )
    critical_candidate_count: int = Field(
        default=0, description="keep 且解析为 CRITICAL 的候选数"
    )
    severity_transitions: dict[str, int] = Field(
        default_factory=dict,
        description="severity_proposal 到 resolved_severity 的转移计数",
    )
    final_issue_count: int = Field(default=0, description="最终 Issue 对应的 survivor 候选数")
    final_issue_fact_covered_count: int = Field(
        default=0, description="survivor 中至少有关联非 insufficient 关系的数量"
    )
    final_issue_fact_coverage: float | None = Field(
        default=None,
        description="final_issue_fact_covered_count/final_issue_count；分母为零时 None",
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
    # ── 取证溯源统计(ADR-046) ──
    fact_count: int = Field(default=0, description="取证后按候选累计的事实总数")
    replay_verified_count: int = Field(default=0, description="链引用命中重放的 fact 数")
    replay_unverified_count: int = Field(default=0, description="链引用未命中重放的 fact 数")
    replay_failed_count: int = Field(default=0, description="重放调用失败的 fact 数")
    chain_used_count: int = Field(default=0, description="使用合法取证链的候选数")
    recipe_fallback_count: int = Field(default=0, description="无链/废链回退固定配方的候选数")
