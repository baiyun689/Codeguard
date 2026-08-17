"""证据账本内部模型(Evidence Ledger)。

证据所有权从 LLM 移到运行时:审查员只输出短别名引用(T01/Cxx)声明
"哪条事实支持哪个结论",工具调用/上下文/patch 由运行时代码捕获为
内容寻址 Artifact。LLM 不能生成事实、调用记录或原始引文。

设计依据:docs/superpowers/plans/2026-08-17-evidence-ledger-refactor.md §4.3。
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from codeguard_agent.models.schemas import EvidenceRole, Severity


class EvidenceSourceKind(str, Enum):
    """Artifact 的证据来源。"""

    TASK_PATCH = "task_patch"          # 当前 task 的 diff patch(P01,自动绑定)
    PREFETCHED_CONTEXT = "prefetched_context"  # context_provider 预取事实(Cxx)
    TOOL_CALL = "tool_call"            # 本次真实调用 Gateway 工具(Txx)


class EvidenceArtifactStatus(str, Enum):
    """Artifact 健康状态。"""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    FAILED = "failed"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"


class EvidenceCaptureMode(str, Enum):
    """Artifact 的捕获方式。"""

    GENERATED = "generated"   # patch/context 由管线确定性生成
    EXECUTED = "executed"     # 本次真实调用 Gateway
    REUSED = "reused"         # 复用本 review 已有真实结果


def stable_json(obj: dict) -> str:
    """规范化 JSON:键排序、无空白,保证相同参数不同键序得到同一字符串。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_digest(payload: str) -> str:
    """payload 的内容摘要(sha256 十六进制)。"""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_artifact_id(
    revision: str,
    task_id: str,
    source_kind: EvidenceSourceKind,
    tool: str,
    arguments: dict[str, str],
    payload: str,
) -> str:
    """内容寻址 Artifact ID。

    相同 revision/task/来源/工具/参数/payload 得到相同 ID;
    payload 或 revision 改变会生成新 ID。ID 不可被 LLM 猜测,
    短别名(T01 等)只在一次合成内有效,离开发现子图即绑定为内部 ID。
    """
    seed = "\0".join(
        [
            revision,
            task_id,
            source_kind.value,
            tool,
            stable_json(arguments),
            payload_digest(payload),
        ]
    )
    return "ev-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


class EvidenceArtifact(BaseModel):
    """一条被运行时捕获的证据事实(内容寻址,不可由 LLM 伪造)。"""

    id: str = Field(description="内容寻址 ID(compute_artifact_id 计算)")
    task_id: str = Field(description="所属 reviewer task")
    reviewer: str = Field(description="捕获该事实的审查员(source_agent)")
    revision: str = Field(description="捕获时的仓库 revision")

    source_kind: EvidenceSourceKind = Field(description="证据来源")
    tool: str = Field(default="", description="Gateway 工具名(patch/context 为空)")
    arguments: dict[str, str] = Field(
        default_factory=dict, description="规范化后的调用参数"
    )

    payload: str = Field(description="原始事实内容")
    payload_hash: str = Field(description="payload 摘要")
    status: EvidenceArtifactStatus = Field(description="健康状态")
    capture_mode: EvidenceCaptureMode = Field(description="捕获方式")

    call_id: str = Field(default="", description="Gateway 调用 ID")
    reused_from_artifact_id: str = Field(
        default="", description="reused 时指向首次 Artifact,不复制 payload"
    )
    limitations: tuple[str, ...] = Field(
        default_factory=tuple, description="范围/截断等限制声明"
    )

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        reviewer: str,
        revision: str,
        source_kind: EvidenceSourceKind,
        payload: str,
        status: EvidenceArtifactStatus,
        capture_mode: EvidenceCaptureMode,
        tool: str = "",
        arguments: dict[str, str] | None = None,
        call_id: str = "",
        reused_from_artifact_id: str = "",
        limitations: tuple[str, ...] = (),
    ) -> "EvidenceArtifact":
        """按内容寻址规则构造 Artifact(id/payload_hash 由输入计算,不手填)。"""
        args = dict(arguments or {})
        artifact_id = compute_artifact_id(
            revision, task_id, source_kind, tool, args, payload
        )
        return cls(
            id=artifact_id,
            task_id=task_id,
            reviewer=reviewer,
            revision=revision,
            source_kind=source_kind,
            tool=tool,
            arguments=args,
            payload=payload,
            payload_hash=payload_digest(payload),
            status=status,
            capture_mode=capture_mode,
            call_id=call_id,
            reused_from_artifact_id=reused_from_artifact_id,
            limitations=limitations,
        )


def merge_evidence_artifacts(
    left: dict[str, EvidenceArtifact] | None,
    right: dict[str, EvidenceArtifact] | None,
) -> dict[str, EvidenceArtifact]:
    """并行审查员 fan-in 时的 Artifact 归并 reducer(按内容寻址 ID 合并,后写覆盖)。"""
    merged: dict[str, EvidenceArtifact] = dict(left or {})
    merged.update(right or {})
    return merged


class EvidenceCatalog(BaseModel):
    """一次 reviewer task 的证据目录:短别名 ↔ 内容寻址 Artifact。

    短别名(P01/Cxx/Txx)只在一次结构化合成内有效,LLM 输出完成后
    立即绑定为内部 artifact ID;外层图 State 不依赖别名。
    """

    task_id: str
    reviewer: str
    revision: str
    artifacts: dict[str, EvidenceArtifact] = Field(default_factory=dict)
    alias_to_artifact_id: dict[str, str] = Field(default_factory=dict)

    def _aliases_of(self, source_kind: EvidenceSourceKind) -> list[str]:
        return [
            alias
            for alias, artifact_id in self.alias_to_artifact_id.items()
            if self.artifacts.get(artifact_id) is not None
            and self.artifacts[artifact_id].source_kind is source_kind
        ]

    def patch_alias(self) -> str:
        """patch Artifact 的别名(每 task 恰一个 P01);无则空串。"""
        aliases = self._aliases_of(EvidenceSourceKind.TASK_PATCH)
        return aliases[0] if aliases else ""

    def context_aliases(self) -> list[str]:
        """上下文 Artifact 的别名(C01...),保持注册顺序。"""
        return self._aliases_of(EvidenceSourceKind.PREFETCHED_CONTEXT)

    def tool_aliases(self) -> list[str]:
        """工具 Artifact 的别名(T01...),保持首次出现顺序。"""
        return self._aliases_of(EvidenceSourceKind.TOOL_CALL)


# ── Candidate 引用模型(源文档 §4.5) ──


class EvidenceRef(BaseModel):
    """候选对一条已绑定 Artifact 的引用。"""

    artifact_id: str
    declared_role: EvidenceRole
    auto_bound: bool = False  # 系统自动绑定的 patch 引用


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


# ── Verifier 输出模型(源文档 §4.6) ──


class EvidenceValidationStatus(str, Enum):
    VALID = "valid"
    LIMITED = "limited"
    REPLAY_CONFIRMED = "replay_confirmed"
    INVALID = "invalid"


class VerifiedEvidence(BaseModel):
    """一条经确定性验证、可供 Judge 引用的事实。"""

    artifact_id: str
    source_kind: EvidenceSourceKind
    tool: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)
    content: str
    validation_status: EvidenceValidationStatus
    limitations: tuple[str, ...] = ()


class CandidateVerification(BaseModel):
    candidate_id: str
    source_kinds: set[EvidenceSourceKind] = Field(default_factory=set)
    valid_evidence: list[VerifiedEvidence] = Field(default_factory=list)
    invalid_references: list[EvidenceRefError] = Field(default_factory=list)
    grounding_status: Literal["grounded", "partially_grounded", "ungrounded"]
    eligible_for_judge: bool
    rejection_reason: str = ""


class VerificationBatch(BaseModel):
    candidates: dict[str, CandidateVerification] = Field(default_factory=dict)
    replayed_artifact_ids: list[str] = Field(default_factory=list)
    trace: list[tuple[str, str]] = Field(default_factory=list)


# ── Judge 输出模型(源文档 §4.7) ──


class EvidenceJudgeAssessment(BaseModel):
    candidate_id: str
    action: Literal["keep", "drop"]
    severity: Severity | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    counter_evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class EvidenceJudgeBatch(BaseModel):
    assessments: list[EvidenceJudgeAssessment] = Field(default_factory=list)
