"""证据目录构建与候选引用绑定(Evidence Ledger 的运行时注册入口)。

把 patch(P01)、预取上下文(Cxx)、真实工具结果(Txx)注册为内容寻址
Artifact 并分配短别名;发现者输出短编号后,在离开发现子图前绑定为
内部稳定 artifact ID。LLM 只选择编号,不生产证据内容。

设计依据:docs/superpowers/plans/2026-08-17-evidence-ledger-refactor.md §5/§6。
"""

from __future__ import annotations

from typing import Any, Sequence

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.evidence import (
    EvidenceArtifact,
    EvidenceArtifactStatus,
    EvidenceCaptureMode,
    EvidenceCatalog,
    EvidenceRef,
    EvidenceRefError,
    EvidenceRefErrorReason,
    EvidenceSourceKind,
)
from codeguard_agent.models.schemas import EvidenceRole
from codeguard_agent.pipeline.evidence.graph_response import summarize_graph
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

_GRAPH_TOOLS = ("inspect_change_impact", "inspect_security_path", "inspect_structure")
_CATALOG_MAX_CHARS = 12000
_CATALOG_PAYLOAD_MAX_CHARS = 2000


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


def bind_discovered_issue(
    issue: Any,
    *,
    task: Any,
    reviewer: str,
    catalog: EvidenceCatalog,
    candidate_index: int,
) -> CandidateIssue:
    """把发现者输出(DiscoveredIssue 或 mock 的 Issue)绑定为内部候选(源文档 §6)。

    步骤:稳定候选 ID → 自动绑定 P01 → 按 LLM 原顺序解析外部 refs
    (未知别名/跨任务/跨 revision/失败 Artifact 留痕) → 同 Artifact 去重、
    最多 3 条外部引用。工具引用全无效时候选退化为 patch-only,仍正常进入
    Verifier/Judge——LLM 无法通过编造编号获得证据。
    """
    cid = f"{reviewer}-{candidate_index}-{issue.file}:{issue.line}:{issue.type}"
    refs: list[EvidenceRef] = []
    errors: list[EvidenceRefError] = []
    patch_alias = catalog.patch_alias()
    patch_artifact_id = catalog.alias_to_artifact_id.get(patch_alias, "")
    if patch_artifact_id and patch_artifact_id in catalog.artifacts:
        refs.append(
            EvidenceRef(
                artifact_id=patch_artifact_id,
                declared_role=EvidenceRole.MECHANISM,
                auto_bound=True,
            )
        )
    seen_ids = {patch_artifact_id}
    external_count = 0
    for selection in getattr(issue, "evidence_refs", []) or []:
        alias = str(getattr(selection, "alias", "") or "").strip()
        if not alias:
            continue
        if external_count >= 3:
            break  # 最多 3 条外部引用(不含自动 patch)
        artifact_id = catalog.alias_to_artifact_id.get(alias, "")
        if not artifact_id or artifact_id not in catalog.artifacts:
            errors.append(
                EvidenceRefError(
                    alias=alias,
                    reason=EvidenceRefErrorReason.UNKNOWN_ALIAS,
                    detail="目录中不存在该编号",
                )
            )
            continue
        artifact = catalog.artifacts[artifact_id]
        if artifact.task_id != task.id:
            errors.append(
                EvidenceRefError(
                    alias=alias,
                    reason=EvidenceRefErrorReason.CROSS_TASK_REFERENCE,
                    detail=f"artifact 属于 task {artifact.task_id}",
                )
            )
            continue
        if artifact.revision != catalog.revision:
            errors.append(
                EvidenceRefError(
                    alias=alias,
                    reason=EvidenceRefErrorReason.CROSS_REVISION_REFERENCE,
                    detail="artifact revision 与当前审查不一致",
                )
            )
            continue
        if artifact.status in {
            EvidenceArtifactStatus.FAILED,
            EvidenceArtifactStatus.REJECTED,
            EvidenceArtifactStatus.NOT_FOUND,
        }:
            errors.append(
                EvidenceRefError(
                    alias=alias,
                    reason=EvidenceRefErrorReason.ARTIFACT_FAILED,
                    detail=f"artifact 状态 {artifact.status.value}",
                )
            )
            continue
        if artifact_id in seen_ids:
            continue
        seen_ids.add(artifact_id)
        external_count += 1
        refs.append(
            EvidenceRef(
                artifact_id=artifact_id,
                declared_role=EvidenceRole(
                    str(getattr(selection, "role", "mechanism"))
                ),
            )
        )
    return CandidateIssue(
        id=cid,
        task_id=task.id,
        source_agent=reviewer,
        file=issue.file,
        line=issue.line,
        type=issue.type,
        severity_proposal=issue.severity,
        claim=issue.message,
        suggestion=issue.suggestion,
        confidence=issue.confidence,
        evidence_refs=refs,
        evidence_ref_errors=errors,
    )


def _citeable(artifact: EvidenceArtifact) -> bool:
    return artifact.status in {
        EvidenceArtifactStatus.COMPLETE,
        EvidenceArtifactStatus.PARTIAL,
        EvidenceArtifactStatus.UNKNOWN,
    }


def _catalog_payload(artifact: EvidenceArtifact) -> str:
    """Catalog 内单条 payload 预算:图摘要化、文件截 2000 字符(修正③)。"""
    if artifact.tool in _GRAPH_TOOLS:
        return summarize_graph(artifact.payload)
    truncated = len(artifact.payload) > _CATALOG_PAYLOAD_MAX_CHARS
    return (
        artifact.payload[:_CATALOG_PAYLOAD_MAX_CHARS]
        + ("\n...[truncated]" if truncated else "")
    )


def render_evidence_catalog(
    catalog: EvidenceCatalog,
    *,
    max_chars: int = _CATALOG_MAX_CHARS,
) -> str:
    """把证据目录渲染为合成提示词的 <evidence_catalog> 段(修正③)。

    patch 已在原 <task_patch> 标签带 evidence_id="P01",不重复全文;
    渲染顺序 P 指针 → Cxx → Txx;总硬上限按顺序逐条截断(渲染发生在
    引用已知前,截断规则只能是"顺序+长度"式,不依赖引用)。
    """
    blocks: list[str] = []
    used = 0
    if catalog.patch_alias():
        blocks.append(
            '<artifact id="P01" source="task_patch" citeable="true" '
            'ref="task_patch_tag"/>'
        )
    for alias in (*catalog.context_aliases(), *catalog.tool_aliases()):
        artifact = catalog.artifacts[catalog.alias_to_artifact_id[alias]]
        block = (
            f'<artifact id="{alias}" source="{artifact.source_kind.value}" '
            f'status="{artifact.status.value}" tool="{artifact.tool}" '
            f'args="{_args_text(artifact.arguments)}" '
            f'citeable="{str(_citeable(artifact)).lower()}" '
            f'capture_mode="{artifact.capture_mode.value}">\n'
            f"{_catalog_payload(artifact)}\n"
            f"</artifact>"
        )
        remaining = max_chars - used
        if remaining <= 0:
            break
        blocks.append(block[:remaining])
        used += min(len(block), remaining)
    return "\n".join(blocks)


def _args_text(arguments: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in arguments.items())
