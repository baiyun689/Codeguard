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
)

from codeguard_agent.models.evidence import EvidenceRef, EvidenceRefError
from codeguard_agent.models.schemas import Issue, Severity

MAX_CANDIDATES_PER_AGENT = 10
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CausalProfile(BaseModel):
    """LLM 从已裁决候选中提取的 Cause/Effect 画像。"""

    candidate_id: str
    defect_mechanism: str = "unknown"
    location: list[str] = Field(default_factory=list)
    trigger: str = "unknown"
    runtime_consequence: str = "unknown"
    affected_scope: str = "unknown"
    observable_behavior: str = "unknown"
    cause_evidence_ids: list[str] = Field(default_factory=list)
    effect_evidence_ids: list[str] = Field(default_factory=list)


class CausalComparison(BaseModel):
    """两个候选的 Cause/Effect 语义比较结果。"""

    left_candidate_id: str
    right_candidate_id: str
    same_cause: bool | None = None
    same_effect: bool | None = None


class CausalAnalysisBatch(BaseModel):
    """一次 Cause/Effect 分析批次的结构化输出。"""

    profiles: list[CausalProfile] = Field(default_factory=list)
    comparisons: list[CausalComparison] = Field(default_factory=list)


class CausalMergeGroup(BaseModel):
    """由确定性代码根据 pairwise duplicate 关系生成的合并组。"""

    id: str
    member_ids: tuple[str, ...]


# ── CouncilJudge 裁决模型 ──


@dataclass
class Verdict:
    """Evidence-gate + synthesis adjudication outcome。"""

    candidate_id: str
    action: Literal["keep", "drop"]
    reason_code: str
    reason: str = ""
    resolved_severity: Severity | None = None
    supported: bool = False  # keep 且引用 ≥1 支持事实(Evidence Ledger 支持覆盖口径)


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
    """发现者 Agent 写入共享黑板的候选问题。

    证据以引用形式携带:evidence_refs 是已绑定为内容寻址 artifact ID 的
    引用(含系统自动绑定的 patch 引用);无效引用留痕于 evidence_ref_errors,
    候选退化为 patch-only 继续走 Verifier/Judge(Evidence Ledger 设计)。
    """

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
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        description="已绑定的证据引用(含自动 patch 引用,见 pipeline/evidence/ledger.bind_discovered_issue)",
    )
    evidence_ref_errors: list[EvidenceRefError] = Field(
        default_factory=list,
        description="无效引用的留痕(unknown alias 等);不阻止候选进入后续阶段",
    )

    def to_issue(self) -> Issue:
        """裁决后转换回产品输出 Issue(产品 Issue 不含证据字段)。"""
        return Issue(
            severity=self.severity_proposal,
            file=self.file,
            line=self.line,
            type=self.type,
            message=self.claim,
            suggestion=self.suggestion,
            confidence=self.confidence,
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
    truncated_candidates: int = Field(default=0, description="发现阶段因候选上限被截断的数量")
    verdict_count: int = Field(default=0, description="Judge 产生的候选裁决总数")
    removed_by_judge: int = Field(default=0, description="Judge 裁决为 drop 的候选数")
    critical_candidate_count: int = Field(
        default=0, description="keep 且解析为 CRITICAL 的候选数"
    )
    severity_transitions: dict[str, int] = Field(
        default_factory=dict,
        description="severity_proposal 到 resolved_severity 的转移计数",
    )
    final_issue_count: int = Field(default=0, description="最终 Issue 对应的 survivor 候选数")
    final_issue_supported_count: int = Field(
        default=0, description="survivor 中 Judge keep 且引用 ≥1 支持事实的数量"
    )
    final_issue_support_coverage: float | None = Field(
        default=None,
        description="final_issue_supported_count/final_issue_count；分母为零时 None",
    )
    # ── Evidence Ledger 统计 ──
    artifact_count: int = Field(default=0, description="运行时捕获的 Artifact 总数")
    patch_artifact_count: int = Field(default=0, description="patch Artifact 数(P01)")
    context_artifact_count: int = Field(default=0, description="预取上下文 Artifact 数(Cxx)")
    tool_artifact_count: int = Field(default=0, description="工具 Artifact 数(Txx)")
    reused_artifact_count: int = Field(default=0, description="跨任务复用捕获的 Artifact 数")
    candidate_patch_only_count: int = Field(default=0, description="仅 patch 证据的候选数")
    candidate_context_backed_count: int = Field(default=0, description="patch+context 的候选数")
    candidate_tool_backed_count: int = Field(default=0, description="引用工具事实的候选数")
    candidate_ungrounded_count: int = Field(default=0, description="ungrounded(无有效 patch)候选数")
    selected_reference_count: int = Field(default=0, description="候选引用总数(含自动 patch)")
    valid_reference_count: int = Field(default=0, description="验证为 valid 的引用数")
    limited_reference_count: int = Field(default=0, description="验证为 limited 的引用数")
    invalid_reference_count: int = Field(default=0, description="无效引用数")
    evidence_gap_count: int = Field(default=0, description="没有形成可引用事实的证据查询数")
    graph_indeterminate_count: int = Field(default=0, description="图谱无法得出事实的查询数")
    replay_requested_count: int = Field(default=0, description="进入重放队列的 Artifact 数")
    replay_valid_count: int = Field(default=0, description="重放后验证为 valid 的 Artifact 数")
    replay_limited_count: int = Field(default=0, description="重放后验证为 limited 的 Artifact 数")
    replay_failed_count: int = Field(default=0, description="重放失败的 Artifact 数")
    judge_batch_call_count: int = Field(default=0, description="批量 Judge LLM 调用次数")
    judge_failed_candidate_count: int = Field(
        default=0, description="Judge 失败/合同违约 fail-closed 的候选数"
    )
    judge_no_support_drop_count: int = Field(
        default=0, description="Judge 因无支持事实而 drop 的候选数"
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
