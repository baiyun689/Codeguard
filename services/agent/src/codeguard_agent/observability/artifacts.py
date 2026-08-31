"""Trace Artifact 索引构建与事件原文归一化。"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from codeguard_agent.models.evidence import (
    EvidenceArtifact,
    EvidenceSourceKind,
    payload_digest,
)
from codeguard_agent.observability.serialization import normalize_tool_result
from codeguard_agent.observability.models import (
    TraceArtifactMeta,
    TraceEvent,
    TraceReport,
)
from codeguard_agent.pipeline.evidence.projection import (
    GraphProjectionFocus,
    ProjectionAudience,
    project_tool_payload,
)

_TRACE_REF_KEYS = (
    "call_id",
    "artifact_id",
    "tool",
    "arguments",
    "status",
    "duration_ms",
    "reuse_key",
    "reused_from_call_id",
    "reused_from_artifact_id",
)


def normalize_trace_report(
    report: TraceReport,
    artifacts: Mapping[str, EvidenceArtifact],
    *,
    focus_by_task: Mapping[str, GraphProjectionFocus] | None = None,
) -> TraceReport:
    """原文按 hash 单份入库，并从所有 Trace 事件中移除工具 payload。"""
    tool_artifacts = {
        artifact_id: artifact
        for artifact_id, artifact in artifacts.items()
        if artifact.source_kind is EvidenceSourceKind.TOOL_CALL
    }
    report.artifacts = {}
    report.payload_store = {}
    by_hash: dict[str, list[str]] = {}
    by_projection_hash: dict[str, list[str]] = {}
    by_call: dict[str, str] = {}
    for artifact_id, artifact in tool_artifacts.items():
        focus = (focus_by_task or {}).get(artifact.task_id)
        projection = project_tool_payload(
            artifact.tool,
            artifact.payload,
            ProjectionAudience.REVIEWER,
            arguments=artifact.arguments,
            focus=focus,
        )
        report.payload_store.setdefault(artifact.payload_hash, artifact.payload)
        report.artifacts[artifact_id] = TraceArtifactMeta(
            artifact_id=artifact_id,
            call_id=artifact.call_id,
            tool=artifact.tool,
            arguments=artifact.arguments,
            status=artifact.availability.value,
            capture_mode=artifact.capture_mode.value,
            payload_hash=artifact.payload_hash,
            preview=normalize_tool_result(
                projection.content or projection.summary,
            ),
            preview_truncated=projection.truncated,
            replayed_from_artifact_id=artifact.replayed_from_artifact_id,
        )
        by_hash.setdefault(artifact.payload_hash, []).append(artifact_id)
        by_projection_hash.setdefault(
            payload_digest(projection.content), []
        ).append(artifact_id)
        if artifact.call_id:
            by_call[artifact.call_id] = artifact_id
    native_artifacts = _native_artifact_links(report.events)

    report.events = [
        event.model_copy(update={
            "detail": _sanitize_event_detail(
                event,
                event.detail,
                report,
                by_hash,
                by_projection_hash,
                by_call,
                native_artifacts,
            )
        })
        for event in report.events
    ]
    return report


def _sanitize_event_detail(
    event: TraceEvent,
    detail: dict[str, Any],
    report: TraceReport,
    by_hash: dict[str, list[str]],
    by_projection_hash: dict[str, list[str]],
    by_call: dict[str, str],
    native_artifacts: dict[str, str],
) -> dict[str, Any]:
    if event.event_type in {"tool_end", "tool_error"}:
        artifact_id = native_artifacts.get(event.run_id, "") or (
            _artifact_for_value(
                detail.get("output"), report, by_hash, by_projection_hash
            )
        )
        meta = report.artifacts.get(artifact_id)
        return {
            "tool_name": str(detail.get("tool_name") or ""),
            "call_id": meta.call_id if meta is not None else event.run_id,
            "artifact_id": artifact_id,
            "status": "failed" if event.event_type == "tool_error" else "complete",
            "metadata": _sanitize_value(
                detail.get("metadata", {}), report, by_hash,
                by_projection_hash, by_call
            ),
        }
    normalized_detail = dict(detail)
    if "output" in normalized_detail:
        output = normalized_detail.pop("output")
        normalized_detail[
            "state_write" if event.event_type == "node_end" else "result"
        ] = output
    sanitized = _sanitize_value(
        normalized_detail, report, by_hash, by_projection_hash, by_call
    )
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_value(
    value: Any,
    report: TraceReport,
    by_hash: dict[str, list[str]],
    by_projection_hash: dict[str, list[str]],
    by_call: dict[str, str],
) -> Any:
    if isinstance(value, str):
        artifact_id = _artifact_for_text(
            value, report, by_hash, by_projection_hash
        )
        if artifact_id:
            meta = report.artifacts[artifact_id]
            return {
                "call_id": meta.call_id,
                "artifact_id": artifact_id,
                "payload_omitted": True,
            }
        return value
    if isinstance(value, list):
        return [
            _sanitize_value(item, report, by_hash, by_projection_hash, by_call)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    if _is_artifact(value):
        artifact_id = str(value.get("id") or value.get("artifact_id") or "")
        return {"artifact_id": artifact_id}
    if _is_catalog(value):
        aliases = value.get("alias_to_artifact_id")
        return {
            "task_id": str(value.get("task_id") or ""),
            "reviewer": str(value.get("reviewer") or ""),
            "aliases": dict(aliases) if isinstance(aliases, dict) else {},
            "artifact_count": len(value.get("artifacts") or {}),
        }
    if str(value.get("role") or "") == "tool" and "content" in value:
        compact = {
            key: _sanitize_value(
                item, report, by_hash, by_projection_hash, by_call
            )
            for key, item in value.items()
            if key != "content"
        }
        artifact_id = _artifact_for_value(
            value.get("content"), report, by_hash, by_projection_hash
        )
        compact["artifact_id"] = artifact_id
        compact["call_id"] = (
            report.artifacts[artifact_id].call_id
            if artifact_id
            else str(value.get("tool_call_id") or "")
        )
        compact["status"] = "linked" if artifact_id else "unresolved"
        compact["payload_omitted"] = True
        return compact

    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "tool_trace_records" and isinstance(item, list):
            result[key] = [
                _compact_trace_ref(record, by_call)
                for record in item
                if isinstance(record, dict)
            ]
        elif key in {"payload", "resolved_output"}:
            continue
        elif key == "evidence_artifacts" and isinstance(item, dict):
            result[key] = {str(artifact_id): {"artifact_id": str(artifact_id)}
                           for artifact_id in item}
        else:
            result[key] = _sanitize_value(
                item, report, by_hash, by_projection_hash, by_call
            )
    return result


def _compact_trace_ref(value: dict[str, Any], by_call: dict[str, str]) -> dict[str, Any]:
    compact = {key: value[key] for key in _TRACE_REF_KEYS if key in value}
    call_id = str(compact.get("call_id") or "")
    if not compact.get("artifact_id") and call_id in by_call:
        compact["artifact_id"] = by_call[call_id]
    reused_from_call_id = str(compact.get("reused_from_call_id") or "")
    if (
        not compact.get("reused_from_artifact_id")
        and reused_from_call_id in by_call
    ):
        compact["reused_from_artifact_id"] = by_call[reused_from_call_id]
    return compact


def _native_artifact_links(events: list[TraceEvent]) -> dict[str, str]:
    """用 reviewer/tool/arguments 将 LangChain native run 绑定到应用调用引用。"""
    refs_by_parent: dict[tuple[str, str, str, str], list[str]] = {}
    refs_by_key: dict[tuple[str, str, str], list[str]] = {}
    for event in events:
        output = event.detail.get("output")
        records = output.get("tool_trace_records") if isinstance(output, dict) else None
        if not isinstance(records, list):
            continue
        reviewer = str(event.node_path).split("/", 1)[0]
        for record in records:
            if not isinstance(record, dict):
                continue
            artifact_id = str(record.get("artifact_id") or "")
            if not artifact_id:
                continue
            key = _tool_key(reviewer, record.get("tool"), record.get("arguments"))
            refs_by_key.setdefault(key, []).append(artifact_id)
            refs_by_parent.setdefault((event.run_id, *key), []).append(artifact_id)

    links: dict[str, str] = {}
    for event in events:
        if event.event_type != "tool_start" or not event.run_id:
            continue
        reviewer = str(event.node_path).split("/", 1)[0]
        key = _tool_key(
            reviewer,
            event.detail.get("tool_name") or event.node_name,
            event.detail.get("input"),
        )
        candidates: list[str] = []
        for parent_id in reversed(event.parent_ids):
            candidates = refs_by_parent.get((parent_id, *key)) or []
            if candidates:
                break
        if not candidates:
            candidates = refs_by_key.get(key) or []
        if candidates:
            artifact_id = candidates.pop(0)
            links[event.run_id] = artifact_id
            fallback_candidates = refs_by_key.get(key) or []
            if fallback_candidates and fallback_candidates[0] == artifact_id:
                fallback_candidates.pop(0)
    return links


def _tool_key(reviewer: str, tool: Any, arguments: Any) -> tuple[str, str, str]:
    args = arguments if isinstance(arguments, dict) else {}
    return (
        reviewer,
        str(tool or ""),
        json.dumps(args, ensure_ascii=False, sort_keys=True),
    )


def _artifact_for_value(
    value: Any,
    report: TraceReport,
    by_hash: dict[str, list[str]],
    by_projection_hash: dict[str, list[str]],
) -> str:
    if isinstance(value, str):
        return _artifact_for_text(value, report, by_hash, by_projection_hash)
    if isinstance(value, dict):
        for key in ("content", "result", "output"):
            if key in value:
                artifact_id = _artifact_for_value(
                    value[key], report, by_hash, by_projection_hash
                )
                if artifact_id:
                    return artifact_id
    return ""


def _artifact_for_text(
    value: str,
    report: TraceReport,
    by_hash: dict[str, list[str]],
    by_projection_hash: dict[str, list[str]],
) -> str:
    candidates = [value]
    marker = "\n\n[证据编号 "
    if marker in value:
        candidates.append(value.split(marker, 1)[0])
    for candidate in candidates:
        digest = payload_digest(candidate)
        ids = by_hash.get(digest, []) or by_projection_hash.get(digest, [])
        if len(ids) == 1:
            return ids[0]
    return ""


def _is_artifact(value: dict[str, Any]) -> bool:
    return "payload_hash" in value and "payload" in value and (
        "id" in value or "artifact_id" in value
    )


def _is_catalog(value: dict[str, Any]) -> bool:
    return "alias_to_artifact_id" in value and "artifacts" in value


__all__ = ["normalize_trace_report"]
