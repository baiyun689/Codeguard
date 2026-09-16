"""注册证据目录并绑定候选引用。

将任务 patch、符号解析上下文及工具结果保存为内容寻址证据，分配模型可用的短别名。
模型提交候选后，运行时将短别名转换为稳定证据标识；证据原文由运行时捕获。
"""

from __future__ import annotations

from typing import Any, Sequence

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.evidence import (
    ArtifactAvailability,
    EvidenceArtifact,
    EvidenceCaptureMode,
    EvidenceCatalog,
    EvidenceRef,
    EvidenceRefError,
    EvidenceRefErrorReason,
    EvidenceSourceKind,
    ToolCaptureBatch,
    ToolTraceRef,
)
from codeguard_agent.models.schemas import EvidenceRole
from codeguard_agent.pipeline.evidence.projection import (
    GRAPH_TOOLS,
    GraphProjectionFocus,
    ProjectionAudience,
    project_tool_payload,
)
from codeguard_agent.pipeline.execution.discovery import (
    COMPLETE_PATCH_RESULT,
    REPEATED_TOOL_RESULT,
)

_TOOL_STATUS_MAP = {
    "complete": ArtifactAvailability.AVAILABLE,
    "failed": ArtifactAvailability.FAILED,
    "rejected": ArtifactAvailability.REJECTED,
    "not_found": ArtifactAvailability.MISSING,
}

_CATALOG_MAX_CHARS = 12000
_CATALOG_PAYLOAD_MAX_CHARS = 2000


class EvidenceCatalogBuilder:
    """把运行时事实注册进证据目录;调用方只理解 Artifact 与别名两个概念。"""

    def build_initial(
        self,
        *,
        task: Any,
        symbol_context: Any,
        reviewer: str,
        revision: str,
    ) -> EvidenceCatalog:
        """创建含 P01(当前 task patch)与 Cxx(稳定符号事实)的初始目录。

        patch Artifact 不调用 Gateway、不重放;截断事实标 PARTIAL 并带限制声明。
        """
        catalog = EvidenceCatalog(task_id=task.id, reviewer=reviewer, revision=revision)
        patch_artifact = EvidenceArtifact.build(
            task_id=task.id,
            reviewer=reviewer,
            revision=revision,
            source_kind=EvidenceSourceKind.TASK_PATCH,
            payload=task.patch,
            availability=ArtifactAvailability.AVAILABLE,
            capture_mode=EvidenceCaptureMode.GENERATED,
            arguments={"file_path": task.file},
        )
        catalog.artifacts[patch_artifact.id] = patch_artifact
        catalog.alias_to_artifact_id["P01"] = patch_artifact.id

        symbols = symbol_context.symbols if symbol_context is not None else []
        for idx, symbol in enumerate(symbols, start=1):
            artifact = EvidenceArtifact.build(
                task_id=task.id,
                reviewer=reviewer,
                revision=revision,
                source_kind=EvidenceSourceKind.SYMBOL_CONTEXT,
                tool="resolve_change_context",
                arguments={"symbol_id": symbol.symbol_id},
                payload=symbol.model_dump_json(),
                availability=ArtifactAvailability.AVAILABLE,
                capture_mode=EvidenceCaptureMode.GENERATED,
                limitations=tuple(symbol_context.limitations),
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
        return capture_tool_records(catalog, records).catalog


def capture_tool_records(
    catalog: EvidenceCatalog,
    records: Sequence[Any],
) -> ToolCaptureBatch:
    """原始记录落入 Ledger，并只向外返回不含 payload 的 Trace 引用。"""
    call_to_artifact = {
        artifact.call_id: artifact.id
        for artifact in catalog.artifacts.values()
        if artifact.call_id
    }
    patch_artifact_id = catalog.alias_to_artifact_id.get("P01", "")
    trace_refs: list[ToolTraceRef] = []
    for record in records or ():
        output = str(getattr(record, "output", ""))
        status = str(getattr(record, "status", "complete"))
        call_id = str(getattr(record, "call_id", ""))
        reused_from_call_id = str(
            getattr(record, "reused_from_call_id", "")
        )
        arguments = {
            key: str(value)
            for key, value in dict(
                getattr(record, "arguments", {}) or {}
            ).items()
            if isinstance(value, (str, int, float, bool))
        }
        reused_from_artifact_id = call_to_artifact.get(
            reused_from_call_id, ""
        )
        artifact_id = ""
        if output == COMPLETE_PATCH_RESULT:
            artifact_id = patch_artifact_id
        elif output == REPEATED_TOOL_RESULT:
            artifact_id = reused_from_artifact_id
        else:
            if status == "reused":
                capture_mode = EvidenceCaptureMode.REUSED
                availability = ArtifactAvailability.AVAILABLE
            else:
                capture_mode = EvidenceCaptureMode.EXECUTED
                availability = _TOOL_STATUS_MAP.get(
                    status, ArtifactAvailability.INVALID
                )
            payload = str(
                getattr(record, "resolved_output", "") or output
            )
            artifact = EvidenceArtifact.build(
                task_id=catalog.task_id,
                reviewer=catalog.reviewer,
                revision=catalog.revision,
                source_kind=EvidenceSourceKind.TOOL_CALL,
                tool=str(getattr(record, "tool", "")),
                arguments=arguments,
                payload=payload,
                availability=availability,
                capture_mode=capture_mode,
                call_id=call_id,
                reused_from_artifact_id=reused_from_artifact_id,
            )
            tool_count = sum(
                1
                for item in catalog.artifacts.values()
                if item.source_kind is EvidenceSourceKind.TOOL_CALL
            )
            catalog.artifacts[artifact.id] = artifact
            catalog.alias_to_artifact_id[
                f"T{tool_count + 1:02d}"
            ] = artifact.id
            artifact_id = artifact.id
            if call_id:
                call_to_artifact[call_id] = artifact.id
        trace_refs.append(ToolTraceRef(
            call_id=call_id,
            subtask_id=str(getattr(record, "subtask_id", "")),
            artifact_id=artifact_id,
            tool=str(getattr(record, "tool", "")),
            arguments=arguments,
            status=status,
            duration_ms=float(getattr(record, "duration_ms", 0.0)),
            reuse_key=str(getattr(record, "reuse_key", "")),
            reused_from_call_id=reused_from_call_id,
            reused_from_artifact_id=reused_from_artifact_id,
        ))
    return ToolCaptureBatch(catalog=catalog, trace_refs=trace_refs)


def bind_discovered_issue(
    issue: Any,
    *,
    task: Any,
    reviewer: str,
    catalog: EvidenceCatalog,
    candidate_index: int,
) -> CandidateIssue:
    """将审查输出绑定为内部候选，并解析证据短别名。

    生成稳定候选标识，自动绑定任务 patch；按模型引用顺序检查任务、版本和证据状态。
    相同证据去重，外部引用最多保留三条。无有效工具引用时仅保留 patch 证据，
    仍由验证和裁决阶段判断是否足以支持候选。
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
        if artifact.availability is ArtifactAvailability.INVALID:
            errors.append(
                EvidenceRefError(
                    alias=alias,
                    reason=EvidenceRefErrorReason.ARTIFACT_UNAVAILABLE,
                    detail=f"artifact 可用性 {artifact.availability.value}",
                )
            )
            continue
        if artifact_id in seen_ids:
            continue
        seen_ids.add(artifact_id)
        external_count += 1
        role_raw = getattr(selection, "role", EvidenceRole.MECHANISM)
        # str-Enum 成员在 3.11+ 下 str() 返回限定名("EvidenceRole.REACHABILITY"),
        # 必须取 .value 再构造,否则绑定器抛 ValueError 拖垮发现者节点。
        role = (
            role_raw
            if isinstance(role_raw, EvidenceRole)
            else EvidenceRole(str(role_raw))
        )
        refs.append(
            EvidenceRef(artifact_id=artifact_id, declared_role=role)
        )
    return CandidateIssue(
        id=cid,
        task_id=task.id,
        source_agent=reviewer,
        file=issue.file,
        line=issue.line,
        type=issue.type,
        claim=issue.message,
        suggestion=issue.suggestion,
        confidence=issue.confidence,
        evidence_refs=refs,
        evidence_ref_errors=errors,
    )


def _citeable(artifact: EvidenceArtifact) -> bool:
    return artifact.availability is ArtifactAvailability.AVAILABLE


def _catalog_payload(
    artifact: EvidenceArtifact,
    *,
    focus: GraphProjectionFocus | None = None,
) -> str:
    """Catalog 内单条 payload 预算:图摘要化、文件截 2000 字符(修正③)。"""
    if artifact.tool in GRAPH_TOOLS:
        return project_tool_payload(
            artifact.tool,
            artifact.payload,
            ProjectionAudience.REVIEWER,
            arguments=artifact.arguments,
            focus=focus,
        ).content
    truncated = len(artifact.payload) > _CATALOG_PAYLOAD_MAX_CHARS
    return (
        artifact.payload[:_CATALOG_PAYLOAD_MAX_CHARS]
        + ("\n...[truncated]" if truncated else "")
    )


def render_evidence_catalog(
    catalog: EvidenceCatalog,
    *,
    max_chars: int = _CATALOG_MAX_CHARS,
    focus: GraphProjectionFocus | None = None,
) -> str:
    """把证据目录渲染为合成提示词的 <evidence_catalog> 段(修正③)。

    patch 由运行时内部自动绑定,不向 LLM 暴露 patch 编号;
    渲染顺序 Cxx → Txx;总硬上限按顺序逐条截断(渲染发生在
    引用已知前,截断规则只能是"顺序+长度"式,不依赖引用)。
    """
    blocks: list[str] = []
    used = 0
    for alias in (*catalog.symbol_aliases(), *catalog.tool_aliases()):
        artifact = catalog.artifacts[catalog.alias_to_artifact_id[alias]]
        block = (
            f'<artifact id="{alias}" source="{artifact.source_kind.value}" '
            f'availability="{artifact.availability.value}" tool="{artifact.tool}" '
            f'args="{_args_text(artifact.arguments)}" '
            f'citeable="{str(_citeable(artifact)).lower()}" '
            f'capture_mode="{artifact.capture_mode.value}">\n'
            f"{_catalog_payload(artifact, focus=focus)}\n"
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
