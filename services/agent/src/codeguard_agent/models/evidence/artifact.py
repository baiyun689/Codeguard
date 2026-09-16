"""Evidence Ledger 的 Artifact、Catalog 与工具调用模型。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum

from pydantic import BaseModel, Field


class EvidenceSourceKind(str, Enum):
    """Artifact 的证据来源。"""

    TASK_PATCH = "task_patch"
    SYMBOL_CONTEXT = "symbol_context"
    TOOL_CALL = "tool_call"


class ArtifactAvailability(str, Enum):
    """Artifact 捕获可用性；内容边界由 limitations 独立表达。"""

    AVAILABLE = "available"
    FAILED = "failed"
    REJECTED = "rejected"
    MISSING = "missing"
    INVALID = "invalid"


class EvidenceCaptureMode(str, Enum):
    """Artifact 的捕获方式。"""

    GENERATED = "generated"
    EXECUTED = "executed"
    REUSED = "reused"


def stable_json(obj: Mapping[str, object]) -> str:
    """规范化 JSON，保证相同参数不同键序得到同一字符串。"""

    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_digest(payload: str) -> str:
    """返回 payload 的 SHA-256 摘要。"""

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_artifact_id(
    revision: str,
    task_id: str,
    source_kind: EvidenceSourceKind,
    tool: str,
    arguments: dict[str, str],
    payload: str,
    *,
    provenance_id: str = "",
) -> str:
    """按 revision/task/来源/工具/参数/payload 生成内容寻址 ID。"""

    identity_parts = [
        revision,
        task_id,
        source_kind.value,
        tool,
        stable_json(arguments),
        payload_digest(payload),
    ]
    if provenance_id:
        identity_parts.append(provenance_id)
    seed = "\0".join(identity_parts)
    return "ev-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


class EvidenceArtifact(BaseModel):
    """一条被运行时捕获的证据事实。"""

    id: str = Field(description="内容寻址 ID")
    task_id: str
    reviewer: str
    revision: str
    source_kind: EvidenceSourceKind
    tool: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)
    payload: str = Field(description="原始事实内容")
    payload_hash: str
    availability: ArtifactAvailability
    capture_mode: EvidenceCaptureMode
    call_id: str = ""
    reused_from_artifact_id: str = ""
    replayed_from_artifact_id: str = ""
    limitations: tuple[str, ...] = Field(default_factory=tuple)

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        reviewer: str,
        revision: str,
        source_kind: EvidenceSourceKind,
        payload: str,
        availability: ArtifactAvailability,
        capture_mode: EvidenceCaptureMode,
        tool: str = "",
        arguments: dict[str, str] | None = None,
        call_id: str = "",
        reused_from_artifact_id: str = "",
        replayed_from_artifact_id: str = "",
        limitations: tuple[str, ...] = (),
    ) -> "EvidenceArtifact":
        """构造 Artifact，并自动计算 ID 与 payload_hash。"""

        args = dict(arguments or {})
        artifact_id = compute_artifact_id(
            revision,
            task_id,
            source_kind,
            tool,
            args,
            payload,
            provenance_id=replayed_from_artifact_id,
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
            availability=availability,
            capture_mode=capture_mode,
            call_id=call_id,
            reused_from_artifact_id=reused_from_artifact_id,
            replayed_from_artifact_id=replayed_from_artifact_id,
            limitations=limitations,
        )


def merge_evidence_artifacts(
    left: dict[str, EvidenceArtifact] | None,
    right: dict[str, EvidenceArtifact] | None,
) -> dict[str, EvidenceArtifact]:
    """并行审查员 fan-in 时合并 Artifact。"""

    merged: dict[str, EvidenceArtifact] = dict(left or {})
    merged.update(right or {})
    return merged


class EvidenceCatalog(BaseModel):
    """一次 reviewer task 的短别名到 Artifact 映射。"""

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
        """返回当前 task patch 的别名。"""

        aliases = self._aliases_of(EvidenceSourceKind.TASK_PATCH)
        return aliases[0] if aliases else ""

    def symbol_aliases(self) -> list[str]:
        """返回符号解析 Artifact 别名。"""

        return self._aliases_of(EvidenceSourceKind.SYMBOL_CONTEXT)

    def tool_aliases(self) -> list[str]:
        """返回工具 Artifact 别名。"""

        return self._aliases_of(EvidenceSourceKind.TOOL_CALL)


class ToolTraceRef(BaseModel):
    """进入 LangGraph State 的紧凑工具调用引用。"""

    call_id: str
    # 调查组标识，用于将工具轨迹关联到具体调查；不参与证据内容标识的计算。
    subtask_id: str = ""
    artifact_id: str = ""
    tool: str
    arguments: dict[str, str] = Field(default_factory=dict)
    status: str
    duration_ms: float = 0.0
    reuse_key: str = ""
    reused_from_call_id: str = ""
    reused_from_artifact_id: str = ""


class ToolCaptureBatch(BaseModel):
    """一次工具记录捕获的 Catalog 与紧凑 Trace 引用。"""

    catalog: EvidenceCatalog
    trace_refs: list[ToolTraceRef] = Field(default_factory=list)
