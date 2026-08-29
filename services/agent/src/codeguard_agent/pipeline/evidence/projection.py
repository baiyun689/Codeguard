"""工具原始响应面向 Reviewer、Judge 与 Trace 的确定性投影。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel

from codeguard_agent.models.evidence import EvidenceValidationStatus
from codeguard_agent.pipeline.evidence.graph_response import (
    GraphProjectionFocus,
    summarize_graph,
    validate_graph_payload,
)

GRAPH_TOOLS = frozenset({
    "inspect_path",
    "inspect_change_impact",
    "inspect_structure",
})


class ProjectionAudience(str, Enum):
    EVIDENCE = "evidence"
    REVIEWER = "reviewer"
    JUDGE = "judge"
    TRACE = "trace"


class PayloadProjection(BaseModel):
    content: str
    summary: str
    truncated: bool = False


def project_tool_payload(
    tool: str,
    raw_payload: str,
    audience: ProjectionAudience,
    *,
    arguments: Mapping[str, Any] | None = None,
    focus: GraphProjectionFocus | None = None,
) -> PayloadProjection:
    """保留 Evidence 原文，其余消费者只接收各自需要的确定性视图。"""
    if audience is ProjectionAudience.EVIDENCE:
        return PayloadProjection(
            content=raw_payload,
            summary=_plain_summary(tool, raw_payload),
        )
    if tool in GRAPH_TOOLS:
        if not _is_projectable_graph_payload(tool, raw_payload, arguments):
            content = json.dumps({
                "schema_version": 2,
                "outcome": "indeterminate",
                "coverage": "partial",
                "relationships": [],
                "unresolved_relationships": [],
                "unresolved_count": 0,
                "omitted_count": 0,
                "omitted_symbol_count": 0,
                "omitted_unresolved_count": 0,
                "omitted_path_count": 0,
                "limitations": ["graph_projection_unavailable"],
            }, ensure_ascii=False, separators=(",", ":"))
            return PayloadProjection(
                content=content,
                summary="图谱响应无法投影",
            )
        content = summarize_graph(
            raw_payload,
            tool=tool,
            arguments=arguments,
            focus=focus,
        )
        return PayloadProjection(
            content=content,
            summary=_graph_summary(raw_payload),
            truncated=_graph_projection_truncated(raw_payload, content),
        )
    if audience is ProjectionAudience.TRACE:
        return PayloadProjection(
            content="",
            summary=_plain_summary(tool, raw_payload),
        )
    return PayloadProjection(
        content=raw_payload,
        summary=_plain_summary(tool, raw_payload),
    )


def graph_projection_focus(task: Any, symbol_context: Any = None) -> GraphProjectionFocus:
    """Build task-only focus facts; tool arguments remain the source of truth."""
    symbols = getattr(symbol_context, "symbols", ()) if symbol_context is not None else ()
    changed_file = str(getattr(task, "file", "")).replace("\\", "/")
    return GraphProjectionFocus(
        changed_file=changed_file or None,
        changed_lines=tuple(
            int(line) for line in (getattr(task, "changed_lines", ()) or ())
        ),
        changed_symbol_ids=tuple(
            str(getattr(symbol, "symbol_id", ""))
            for symbol in symbols
            if getattr(symbol, "symbol_id", "")
        ),
    )


def _graph_summary(raw_payload: str) -> str:
    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "图谱响应无法解析"
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        return "图谱协议不兼容"
    relationships = payload.get("relationships")
    resolved = len(relationships) if isinstance(relationships, list) else 0
    unresolved = payload.get("unresolved_count")
    unresolved_count = unresolved if isinstance(unresolved, int) else 0
    return (
        f"{payload.get('outcome', 'invalid')}/"
        f"{payload.get('coverage', 'invalid')} · "
        f"已解析 {resolved} · 未解析 {unresolved_count}"
    )


def _plain_summary(tool: str, raw_payload: str) -> str:
    return f"{tool or 'tool'} · {len(raw_payload)} 字符"


def _graph_projection_truncated(raw_payload: str, content: str) -> bool:
    try:
        raw = json.loads(raw_payload)
        projected = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    if not isinstance(raw, dict) or not isinstance(projected, dict):
        return True
    return any(
        len(projected.get(key) or []) < len(raw.get(key) or [])
        for key in (
            "symbols",
            "relationships",
            "unresolved_relationships",
        )
    )


def _is_projectable_graph_payload(
    tool: str,
    raw_payload: str,
    arguments: Mapping[str, Any] | None,
) -> bool:
    validation = validate_graph_payload(
        raw_payload,
        tool=tool,
        expected_subject=str((arguments or {}).get("symbol_id", "")),
    )
    return validation.status is not EvidenceValidationStatus.INVALID


__all__ = [
    "GRAPH_TOOLS",
    "GraphProjectionFocus",
    "PayloadProjection",
    "ProjectionAudience",
    "graph_projection_focus",
    "project_tool_payload",
]
