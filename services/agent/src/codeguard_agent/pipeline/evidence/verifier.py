"""Evidence Ledger 验证节点:Artifact 健康检查 + 图护栏 + guard 扫描 + 异常重放。

正常路径零 LLM、零重放:只证明 Artifact 真实、可用、属于候选可见范围,
不判断 candidate claim 是否成立——支持/反驳判定整体移交批量 EvidenceJudge。
仅异常 Artifact(未知/失败/revision 不一致/响应不可解析)进入重放队列,
并受 enabled_evidence_tools 白名单约束。

设计依据:docs/superpowers/plans/2026-08-17-evidence-ledger-refactor.md §7。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from codeguard_agent.models.evidence import (
    CandidateVerification,
    EvidenceArtifact,
    EvidenceArtifactStatus,
    EvidenceRefError,
    EvidenceRefErrorReason,
    EvidenceSourceKind,
    EvidenceValidationStatus,
    VerificationBatch,
    VerifiedEvidence,
    payload_digest,
)
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.graph_response import validate_graph_payload
from codeguard_agent.pipeline.evidence.guard_scan import scan_guard_content
from codeguard_agent.pipeline.evidence.planner import CandidateDossier

logger = logging.getLogger("codeguard")

_GRAPH_TOOLS = ("inspect_change_impact", "inspect_security_path", "inspect_structure")
_DISCOVERY_TOOLS = (
    "get_file_content",
    "inspect_change_impact",
    "inspect_security_path",
    "inspect_structure",
)


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _replay_allowed(tool: str, enabled_replay_tools: list[str] | None) -> bool:
    """重放白名单:None 沿用发现工具;空列表禁止重放(源文档 §7.4)。"""
    allowed = _DISCOVERY_TOOLS if enabled_replay_tools is None else enabled_replay_tools
    return tool in allowed


def _execute_replay(
    tool_client: Any,
    tool: str,
    arguments: dict[str, str],
) -> tuple[str, str]:
    """异常重放:执行一次 Gateway 调用,返回 (raw, limitation)。失败不抛。"""
    try:
        response = getattr(tool_client, tool)(**dict(arguments))
    except Exception as exc:  # noqa: BLE001 单次工具异常收敛为不足证据
        return "", f"replay_tool_error:{exc}"
    success = bool(getattr(response, "success", True))
    raw = getattr(response, "result", None)
    if raw is None and hasattr(response, "as_tool_output"):
        raw = response.as_tool_output()
    text = str(raw or "")
    if not success:
        return text, "replay_tool_failed"
    if not text.strip():
        return "", "replay_tool_empty"
    return text, ""


def _health_tool_artifact(
    artifact: EvidenceArtifact,
    *,
    revision: str,
    tool_client: Any,
    enabled_replay_tools: list[str] | None,
    batch: VerificationBatch,
    replay_cache: dict[str, tuple[EvidenceValidationStatus, str, list[str]]],
) -> tuple[EvidenceValidationStatus, str, list[str]]:
    """工具 Artifact 健康检查与异常重放。返回 (status, content, limitations)。

    重放队列(源文档 §7.4):状态异常/revision 不一致/图响应无法解析或
    status=unknown 进入;相同 artifact 批内只执行一次,结果缓存复用。
    """
    cached = replay_cache.get(artifact.id)
    if cached is not None:
        return cached
    limitations: list[str] = []
    needs_replay = False
    if artifact.revision != revision:
        needs_replay = True
        limitations.append("revision_mismatch")
    if artifact.status is not EvidenceArtifactStatus.COMPLETE:
        needs_replay = True

    if not needs_replay:
        if not artifact.payload.strip():
            result = (
                EvidenceValidationStatus.LIMITED,
                artifact.payload,
                ["empty_payload"],
            )
            replay_cache[artifact.id] = result
            return result
        if artifact.tool in _GRAPH_TOOLS:
            health, found = validate_graph_payload(
                artifact.payload,
                tool=artifact.tool,
                expected_subject=str(artifact.arguments.get("symbol_id", "")),
            )
            if health == "invalid":
                result = (EvidenceValidationStatus.INVALID, artifact.payload, found)
                replay_cache[artifact.id] = result
                return result
            if health == "limited":
                result = (EvidenceValidationStatus.LIMITED, artifact.payload, found)
                replay_cache[artifact.id] = result
                return result
            if health == "replay":
                needs_replay = True
                limitations.extend(found)
            else:
                result = (EvidenceValidationStatus.VALID, artifact.payload, found)
                replay_cache[artifact.id] = result
                return result
        else:
            result = (EvidenceValidationStatus.VALID, artifact.payload, limitations)
            replay_cache[artifact.id] = result
            return result

    batch.trace.append(
        ("evidence_replay_requested", _stable_json({
            "artifact_id": artifact.id, "tool": artifact.tool,
            "arguments": artifact.arguments,
        }))
    )
    if not _replay_allowed(artifact.tool, enabled_replay_tools):
        limitations.append("replay_not_enabled")
        batch.replayed_artifact_ids.append(artifact.id)
        result = (EvidenceValidationStatus.LIMITED, artifact.payload, limitations)
        replay_cache[artifact.id] = result
        return result
    raw, limitation = _execute_replay(tool_client, artifact.tool, artifact.arguments)
    batch.replayed_artifact_ids.append(artifact.id)
    if raw and not limitation:
        batch.trace.append(
            ("evidence_replay_completed", _stable_json({
                "artifact_id": artifact.id, "tool": artifact.tool,
            }))
        )
        result = (EvidenceValidationStatus.REPLAY_CONFIRMED, raw, [])
        replay_cache[artifact.id] = result
        return result
    batch.trace.append(
        ("evidence_replay_failed", _stable_json({
            "artifact_id": artifact.id, "tool": artifact.tool,
            "limitation": limitation or "replay_empty",
        }))
    )
    limitations.append(limitation or "replay_failed")
    result = (EvidenceValidationStatus.LIMITED, artifact.payload, limitations)
    replay_cache[artifact.id] = result
    return result


def _verify_candidate(
    dossier: CandidateDossier,
    *,
    artifacts: dict[str, EvidenceArtifact],
    revision: str,
    tool_client: Any,
    enabled_replay_tools: list[str] | None,
    tag: RiskTag,
    batch: VerificationBatch,
    replay_cache: dict[str, tuple[EvidenceValidationStatus, str, list[str]]],
) -> CandidateVerification:
    """单个候选的确定性验证:引用核对 → 健康检查 → guard 扫描 → grounding。"""
    candidate = dossier.candidate
    valid_evidence: list[VerifiedEvidence] = []
    invalid_references: list[EvidenceRefError] = []
    source_kinds: set[EvidenceSourceKind] = set()
    patch_valid = False
    guard_hit = ""

    for ref in candidate.evidence_refs:
        artifact = artifacts.get(ref.artifact_id)
        if artifact is None:
            invalid_references.append(
                EvidenceRefError(
                    alias="", reason=EvidenceRefErrorReason.ARTIFACT_UNAVAILABLE,
                    detail=ref.artifact_id,
                )
            )
            continue
        if artifact.task_id != candidate.task_id:
            invalid_references.append(
                EvidenceRefError(
                    alias="", reason=EvidenceRefErrorReason.CROSS_TASK_REFERENCE,
                    detail=f"artifact 属于 task {artifact.task_id}",
                )
            )
            continue
        if artifact.source_kind is EvidenceSourceKind.TASK_PATCH:
            if artifact.payload_hash != payload_digest(artifact.payload):
                invalid_references.append(
                    EvidenceRefError(
                        alias="", reason=EvidenceRefErrorReason.ARTIFACT_UNAVAILABLE,
                        detail="patch payload 摘要不一致",
                    )
                )
                continue
            patch_valid = True
            source_kinds.add(EvidenceSourceKind.TASK_PATCH)
            valid_evidence.append(
                VerifiedEvidence(
                    artifact_id=artifact.id,
                    source_kind=artifact.source_kind,
                    content=artifact.payload,
                    validation_status=EvidenceValidationStatus.VALID,
                )
            )
            continue
        if artifact.source_kind is EvidenceSourceKind.PREFETCHED_CONTEXT:
            status = (
                EvidenceValidationStatus.LIMITED
                if artifact.status is EvidenceArtifactStatus.PARTIAL
                else EvidenceValidationStatus.VALID
            )
            source_kinds.add(EvidenceSourceKind.PREFETCHED_CONTEXT)
            valid_evidence.append(
                VerifiedEvidence(
                    artifact_id=artifact.id,
                    source_kind=artifact.source_kind,
                    tool=artifact.tool,
                    arguments=dict(artifact.arguments),
                    content=artifact.payload,
                    validation_status=status,
                    limitations=tuple(artifact.limitations),
                )
            )
            continue
        # TOOL_CALL
        status, content, limitations = _health_tool_artifact(
            artifact, revision=revision, tool_client=tool_client,
            enabled_replay_tools=enabled_replay_tools, batch=batch,
            replay_cache=replay_cache,
        )
        if status is EvidenceValidationStatus.INVALID:
            invalid_references.append(
                EvidenceRefError(
                    alias="", reason=EvidenceRefErrorReason.ARTIFACT_UNAVAILABLE,
                    detail="; ".join(limitations),
                )
            )
            continue
        source_kinds.add(EvidenceSourceKind.TOOL_CALL)
        valid_evidence.append(
            VerifiedEvidence(
                artifact_id=artifact.id,
                source_kind=artifact.source_kind,
                tool=artifact.tool,
                arguments=dict(artifact.arguments),
                content=content,
                validation_status=status,
                limitations=tuple(limitations),
            )
        )

    # guard 确定性扫描:patch/文件内容中的明确保护机制 → 直接反证(门控残留)。
    if not invalid_references:
        for item in valid_evidence:
            content = item.content.strip()
            if not content:
                continue
            observation = scan_guard_content(dossier, content, tag)
            if observation:
                guard_hit = observation
                break

    if not patch_valid:
        grounding = "ungrounded"
        rejection = "patch_artifact_missing_or_corrupt"
    elif invalid_references or any(
        item.validation_status is EvidenceValidationStatus.LIMITED
        for item in valid_evidence
    ):
        grounding = "partially_grounded"
        rejection = ""
    else:
        grounding = "grounded"
        rejection = ""
    if guard_hit:
        rejection = "direct_counter_guard"

    eligible = patch_valid and not guard_hit
    verification = CandidateVerification(
        candidate_id=candidate.id,
        source_kinds=source_kinds,
        valid_evidence=valid_evidence,
        invalid_references=invalid_references,
        grounding_status=grounding,  # type: ignore[arg-type]
        eligible_for_judge=eligible,
        rejection_reason=rejection,
    )
    batch.trace.append(
        ("candidate_verification_completed", _stable_json({
            "candidate_id": candidate.id,
            "grounding": verification.grounding_status,
            "eligible": eligible,
            "rejection_reason": rejection,
            "guard_hit": guard_hit,
        }))
    )
    return verification


def verify_evidence(
    dossiers: list[CandidateDossier],
    *,
    artifacts: dict[str, EvidenceArtifact],
    tool_client: Any,
    revision: str,
    enabled_replay_tools: list[str] | None,
    tag_by_candidate: dict[str, RiskTag],
) -> VerificationBatch:
    """证据验证主入口:零 LLM、正常 Artifact 零重放。

    artifacts:本次审查运行时捕获的全部内容寻址 Artifact(跨审查员归并);
    候选只能引用本 task 内可见的引用,引用核对与健康检查均确定性完成。
    """
    batch = VerificationBatch()
    replay_cache: dict[
        str, tuple[EvidenceValidationStatus, str, list[str]]
    ] = {}
    for dossier in dossiers:
        cid = dossier.candidate.id
        batch.candidates[cid] = _verify_candidate(
            dossier,
            artifacts=artifacts,
            revision=revision,
            tool_client=tool_client,
            enabled_replay_tools=enabled_replay_tools,
            tag=tag_by_candidate.get(cid, RiskTag.GENERAL_REVIEW),
            batch=batch,
            replay_cache=replay_cache,
        )

    replay_requested = sum(
        1 for event, _detail in batch.trace if event == "evidence_replay_requested"
    )
    replay_confirmed = sum(
        1 for event, _detail in batch.trace if event == "evidence_replay_completed"
    )
    replay_failed = sum(
        1 for event, _detail in batch.trace if event == "evidence_replay_failed"
    )
    ref_stats = {"selected": 0, "valid": 0, "limited": 0, "invalid": 0}
    for verification in batch.candidates.values():
        ref_stats["selected"] += len(verification.valid_evidence) + len(
            verification.invalid_references
        )
        ref_stats["invalid"] += len(verification.invalid_references)
        for item in verification.valid_evidence:
            if item.validation_status is EvidenceValidationStatus.VALID:
                ref_stats["valid"] += 1
            elif item.validation_status is EvidenceValidationStatus.REPLAY_CONFIRMED:
                ref_stats["valid"] += 1
            else:
                ref_stats["limited"] += 1
    source_counts = {"patch": 0, "context": 0, "tool": 0}
    for artifact in artifacts.values():
        if artifact.source_kind is EvidenceSourceKind.TASK_PATCH:
            source_counts["patch"] += 1
        elif artifact.source_kind is EvidenceSourceKind.PREFETCHED_CONTEXT:
            source_counts["context"] += 1
        else:
            source_counts["tool"] += 1
    batch.trace.append(
        ("evidence_verification_metrics", _stable_json({
            "candidates": len(dossiers),
            "artifacts_patch": source_counts["patch"],
            "artifacts_context": source_counts["context"],
            "artifacts_tool": source_counts["tool"],
            "refs_selected": ref_stats["selected"],
            "refs_valid": ref_stats["valid"],
            "refs_limited": ref_stats["limited"],
            "refs_invalid": ref_stats["invalid"],
            "replay_requested": replay_requested,
            "replay_confirmed": replay_confirmed,
            "replay_failed": replay_failed,
            "judge_eligible": sum(
                v.eligible_for_judge for v in batch.candidates.values()
            ),
            "judge_rejected": sum(
                not v.eligible_for_judge for v in batch.candidates.values()
            ),
        }))
    )
    return batch


__all__ = ["verify_evidence"]
