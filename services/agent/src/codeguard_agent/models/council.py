"""ReviewCouncil 的内部状态模型。

这些模型只用于图 State、trace 和 eval 诊断,不进入 `ReviewResult` 产品输出。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import BaseModel, Field, StringConstraints, model_validator

from codeguard_agent.models.schemas import Issue, Severity

if TYPE_CHECKING:
    from codeguard_agent.models.tasks import RiskTag  # noqa: F401


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
    # Phase 3: concern 对齐字段
    goal_id: str | None = None
    concern_id: str | None = None
    claim_ids: tuple[str, ...] = ()
    fact_type: "EvidenceFactType | None" = None

    @model_validator(mode="after")
    def assign_stable_id(self) -> "EvidenceRequest":
        if not self.id:
            parts = [
                self.candidate_id,
                self.strategy_id,
                self.purpose,
                self.target,
                self.question,
                *self.preferred_tools,
            ]
            # 保持旧 request 的稳定 ID；claim-driven request 才加入对齐维度。
            if self.goal_id is not None:
                parts.extend([
                    self.goal_id,
                    self.concern_id or "",
                    ",".join(self.claim_ids),
                    self.fact_type.value if self.fact_type is not None else "",
                ])
            payload = "\0".join(parts)
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
    # Phase 3: concern 对齐字段
    goal_id: str | None = None
    concern_id: str | None = None
    claim_ids: tuple[str, ...] = ()
    fact_type: "EvidenceFactType | None" = None

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


# ── Phase 3: Candidate Concern & Claim-based Evidence ──


class EvidenceFactType(str, Enum):
    """证据目标的事实类型——描述"要证明什么"，不是"用什么工具"。"""

    CHANGED_CONDITION = "changed_condition"
    VALUE_IDENTITY = "value_identity"
    CALL_PATH = "call_path"
    DATA_FLOW = "data_flow"
    STATE_TRANSITION = "state_transition"
    TRANSACTION_BOUNDARY = "transaction_boundary"
    ORDERING = "ordering"
    GUARD_PRESENCE = "guard_presence"
    REACHABILITY = "reachability"
    SIDE_EFFECT = "side_effect"
    OBSERVABLE_CONSEQUENCE = "observable_consequence"
    FIX_SCOPE = "fix_scope"
    IMPACT_FACTOR = "impact_factor"


class EvidencePolarity(str, Enum):
    SUPPORT = "support"
    COUNTER = "counter"
    IMPACT = "impact"


class CandidateClaim(BaseModel):
    """候选的结构化主张——代码中的错误机制，而非风险类别。"""

    claim_id: str = ""
    candidate_id: str = ""
    root_cause: str = ""
    trigger: str = ""
    observable_consequence: str = ""
    fix_location: str = ""
    fix_action: str = ""
    affected_path: tuple[str, ...] = ()
    unresolved_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def assign_claim_id(self) -> "CandidateClaim":
        if not self.claim_id:
            payload = "\0".join(
                [self.candidate_id, self.root_cause, self.trigger, self.observable_consequence,
                 self.fix_location, self.fix_action]
            )
            self.claim_id = f"claim-{sha256(payload.encode('utf-8')).hexdigest()[:12]}"
        return self


class ConcernTagResolution(BaseModel):
    """多标签视角：primary 表达 root cause，secondary 表达独立后果或验证方法。"""

    primary_tag: RiskTag | None = None
    secondary_tags: tuple[RiskTag, ...] = ()
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: Literal["deterministic", "llm", "mixed", "unclassified"] = "unclassified"
    reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_tag_set(self) -> "ConcernTagResolution":
        if len(self.secondary_tags) > 2:
            raise ValueError("secondary_tags must contain at most two tags")
        if len(set(self.secondary_tags)) != len(self.secondary_tags):
            raise ValueError("secondary_tags must be unique")
        if self.primary_tag is not None and self.primary_tag in self.secondary_tags:
            raise ValueError("primary_tag must not appear in secondary_tags")
        tags = tuple(
            tag for tag in (self.primary_tag, *self.secondary_tags)
            if tag is not None
        )
        if any(tag.value == "GENERAL_REVIEW" for tag in tags) and len(tags) > 1:
            raise ValueError("GENERAL_REVIEW cannot coexist with concrete tags")
        return self


class CandidateConcern(BaseModel):
    """一个或多个候选成员的结构化审查关注点。"""

    concern_id: str = ""
    group_id: str = ""
    member_candidate_ids: tuple[str, ...] = ()
    claims: tuple[CandidateClaim, ...] = ()
    tags: ConcernTagResolution = Field(default_factory=ConcernTagResolution)
    member_risk_tags: dict[str, tuple[RiskTag, ...]] = Field(default_factory=dict)
    source_agents: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    diagnostics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def assign_concern_id(self) -> "CandidateConcern":
        if not self.concern_id:
            payload = "\0".join(self.member_candidate_ids)
            self.concern_id = f"concern-{sha256(payload.encode('utf-8')).hexdigest()[:12]}"
        return self


class EvidenceGoal(BaseModel):
    """一个可判真假的证据命题——描述"要证明什么"。"""

    goal_id: str = ""
    concern_id: str = ""
    claim_ids: tuple[str, ...] = ()
    fact_type: EvidenceFactType = EvidenceFactType.VALUE_IDENTITY
    polarity: EvidencePolarity = EvidencePolarity.SUPPORT
    proposition: str = ""
    why_needed: str = ""
    preferred_capabilities: tuple[str, ...] = ()
    required: bool = True

    @model_validator(mode="after")
    def assign_goal_id(self) -> "EvidenceGoal":
        if not self.goal_id:
            payload = "\0".join(
                [self.concern_id, self.fact_type.value, self.polarity.value,
                 self.proposition]
            )
            self.goal_id = f"goal-{sha256(payload.encode('utf-8')).hexdigest()[:12]}"
        return self


class ConcernAnalysis(BaseModel):
    """ConcernAnalyzer 的输出：concerns + candidate 到 concern 的映射。"""

    concerns: tuple[CandidateConcern, ...] = ()
    candidate_to_concern: dict[str, str] = Field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()


class ConcernEvidencePlan(BaseModel):
    """单个 concern 的证据计划：goals + 对应的 requests。"""

    concern_id: str = ""
    goals: tuple[EvidenceGoal, ...] = ()
    requests: tuple[EvidenceRequest, ...] = ()
    uncovered_goals: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


# ── Phase 4: Evidence-factor Severity ──


class ImpactFactor(str, Enum):
    """可证明的影响因子——每个 factor 描述一个具体的后果维度。"""

    RUNTIME_REACHABLE = "runtime_reachable"
    EXTERNAL_ACTOR_CONTROLLED = "external_actor_controlled"
    AUTHORIZATION_BYPASS = "authorization_bypass"
    CROSS_TENANT_SCOPE = "cross_tenant_scope"
    PRIVILEGED_OPERATION = "privileged_operation"
    CONFIDENTIALITY_LOSS = "confidentiality_loss"
    INTEGRITY_LOSS = "integrity_loss"
    AVAILABILITY_LOSS = "availability_loss"
    FINANCIAL_IMPACT = "financial_impact"
    PERSISTENT_STATE_CORRUPTION = "persistent_state_corruption"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    MULTI_ENTITY_BLAST_RADIUS = "multi_entity_blast_radius"
    REPEATED_OR_AUTOMATIC_TRIGGER = "repeated_or_automatic_trigger"
    IRREVERSIBLE = "irreversible"
    AUTO_RECOVERABLE = "auto_recoverable"
    OPERATOR_RECOVERABLE = "operator_recoverable"
    LOCAL_MAINTAINABILITY_ONLY = "local_maintainability_only"


class FactorStatus(str, Enum):
    PROVEN = "proven"
    DISPROVEN = "disproven"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ImpactFactorAssessment(BaseModel):
    """单个影响因子的证据评估。"""

    factor: ImpactFactor
    status: FactorStatus
    evidence_ids: tuple[str, ...] = ()
    reason: str = ""

    @model_validator(mode="after")
    def validate_evidence_binding(self) -> "ImpactFactorAssessment":
        if (
            self.status in (FactorStatus.PROVEN, FactorStatus.DISPROVEN)
            and not self.evidence_ids
        ):
            self.status = FactorStatus.UNKNOWN
            self.reason = (
                f"{self.reason}; " if self.reason else ""
            ) + "factor status downgraded because no evidence_id was cited"
        if (
            self.status in (FactorStatus.UNKNOWN, FactorStatus.NOT_APPLICABLE)
            and self.evidence_ids
        ):
            self.evidence_ids = ()
        return self


class ImpactClass(str, Enum):
    MAINTAINABILITY = "maintainability"
    RUNTIME_CORRECTNESS = "runtime_correctness"
    SECURITY = "security"
    AVAILABILITY = "availability"


class ImpactAssessment(BaseModel):
    """一个 concern 的完整影响评估——所有 factor 的状态汇总。"""

    concern_id: str = ""
    impact_class: ImpactClass = ImpactClass.RUNTIME_CORRECTNESS
    factors: tuple[ImpactFactorAssessment, ...] = ()
    diagnostics: tuple[str, ...] = ()


class SeverityResolution(BaseModel):
    """确定性的严重级别裁决结果。"""

    concern_id: str = ""
    severity: Severity = Severity.WARNING
    rule_id: str = ""
    proven_factors: tuple[ImpactFactor, ...] = ()
    limiting_factors: tuple[ImpactFactor, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    fallback_used: bool = False
    rationale: str = ""


class CriticalPredicate(BaseModel):
    """CRITICAL 判定规则：一组 factor 条件的组合。"""

    rule_id: str = ""
    all_of: tuple[ImpactFactor, ...] = ()
    any_of: tuple[ImpactFactor, ...] = ()
    none_of: tuple[ImpactFactor, ...] = ()


class ImpactRubric(BaseModel):
    """一组 concern tags 对应的评估标准。"""

    rubric_id: str = ""
    required_factors: tuple[ImpactFactor, ...] = ()
    critical_predicates: tuple[CriticalPredicate, ...] = ()
