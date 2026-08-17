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

from pydantic import BaseModel, Field


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
