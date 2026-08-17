"""证据目录构建器(Evidence Ledger 的运行时注册入口)。

把 patch(P01)、预取上下文(Cxx)、真实工具结果(Txx)注册为内容寻址
Artifact 并分配短别名。LLM 只选择编号,不生产证据内容。

设计依据:docs/superpowers/plans/2026-08-17-evidence-ledger-refactor.md §5。
"""

from __future__ import annotations

from typing import Any, Sequence

from codeguard_agent.models.evidence import (
    EvidenceArtifact,
    EvidenceArtifactStatus,
    EvidenceCaptureMode,
    EvidenceCatalog,
    EvidenceSourceKind,
)
from codeguard_agent.pipeline.risk.discovery import (
    COMPLETE_PATCH_RESULT,
    REPEATED_TOOL_RESULT,
)

_TOOL_STATUS_MAP = {
    "complete": EvidenceArtifactStatus.COMPLETE,
    "failed": EvidenceArtifactStatus.FAILED,
    "rejected": EvidenceArtifactStatus.REJECTED,
    "not_found": EvidenceArtifactStatus.NOT_FOUND,
}


class EvidenceCatalogBuilder:
    """把运行时事实注册进证据目录;调用方只理解 Artifact 与别名两个概念。"""

    def build_initial(
        self,
        *,
        task: Any,
        context_bundle: Any,
        reviewer: str,
        revision: str,
    ) -> EvidenceCatalog:
        """创建含 P01(当前 task patch)与 Cxx(预取上下文事实)的初始目录。

        patch Artifact 不调用 Gateway、不重放;截断事实标 PARTIAL 并带限制声明。
        """
        catalog = EvidenceCatalog(task_id=task.id, reviewer=reviewer, revision=revision)
        patch_artifact = EvidenceArtifact.build(
            task_id=task.id,
            reviewer=reviewer,
            revision=revision,
            source_kind=EvidenceSourceKind.TASK_PATCH,
            payload=task.patch,
            status=EvidenceArtifactStatus.COMPLETE,
            capture_mode=EvidenceCaptureMode.GENERATED,
            arguments={"file_path": task.file},
        )
        catalog.artifacts[patch_artifact.id] = patch_artifact
        catalog.alias_to_artifact_id["P01"] = patch_artifact.id

        facts = context_bundle.facts if context_bundle is not None else []
        for idx, fact in enumerate(facts, start=1):
            truncated = bool(getattr(fact, "truncated", False))
            artifact = EvidenceArtifact.build(
                task_id=task.id,
                reviewer=reviewer,
                revision=revision,
                source_kind=EvidenceSourceKind.PREFETCHED_CONTEXT,
                tool=str(getattr(fact, "source", "")),
                payload=str(getattr(fact, "content", "")),
                status=(
                    EvidenceArtifactStatus.PARTIAL
                    if truncated
                    else EvidenceArtifactStatus.COMPLETE
                ),
                capture_mode=EvidenceCaptureMode.GENERATED,
                limitations=("context_truncated",) if truncated else (),
            )
            catalog.artifacts[artifact.id] = artifact
            catalog.alias_to_artifact_id[f"C{idx:02d}"] = artifact.id
        return catalog

    def append_tool_records(
        self,
        catalog: EvidenceCatalog,
        records: Sequence[Any],
    ) -> EvidenceCatalog:
        """把 ReAct 探索的真实工具记录追加为 Txx Artifact。

        - 短标记记录(COMPLETE_PATCH_RESULT / REPEATED_TOOL_RESULT)不建工具
          Artifact:前者解析到 P01,后者(同任务二次调用)解析到本目录首次 Artifact;
        - 跨任务协调器复用(status=reused 且 LLM 看到真实内容)注册为 REUSED
          Artifact,供该任务候选引用;
        - failed/rejected/not_found 也留 Artifact 供 Trace,默认不作支持证据。
        """
        for record in records or ():
            output = str(getattr(record, "output", ""))
            status = str(getattr(record, "status", "complete"))
            if output in {COMPLETE_PATCH_RESULT, REPEATED_TOOL_RESULT}:
                continue
            if status == "reused":
                capture_mode = EvidenceCaptureMode.REUSED
                artifact_status = EvidenceArtifactStatus.COMPLETE
            else:
                capture_mode = EvidenceCaptureMode.EXECUTED
                artifact_status = _TOOL_STATUS_MAP.get(
                    status, EvidenceArtifactStatus.UNKNOWN
                )
            payload = str(getattr(record, "resolved_output", "") or output)
            arguments = dict(getattr(record, "arguments", {}) or {})
            artifact = EvidenceArtifact.build(
                task_id=catalog.task_id,
                reviewer=catalog.reviewer,
                revision=catalog.revision,
                source_kind=EvidenceSourceKind.TOOL_CALL,
                tool=str(getattr(record, "tool", "")),
                arguments={k: v for k, v in arguments.items() if isinstance(v, str)},
                payload=payload,
                status=artifact_status,
                capture_mode=capture_mode,
                call_id=str(getattr(record, "call_id", "")),
            )
            tool_count = sum(
                1
                for item in catalog.artifacts.values()
                if item.source_kind is EvidenceSourceKind.TOOL_CALL
            )
            catalog.artifacts[artifact.id] = artifact
            catalog.alias_to_artifact_id[f"T{tool_count + 1:02d}"] = artifact.id
        return catalog
