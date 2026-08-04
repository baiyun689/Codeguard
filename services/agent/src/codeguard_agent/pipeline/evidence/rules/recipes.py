"""证据策略的 Gateway 工具调用配方。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from codeguard_agent.pipeline.evidence.strategy_types import (
    EvidenceCapability,
    ToolCallSpec,
)

if TYPE_CHECKING:
    from codeguard_agent.pipeline.evidence.planner import CandidateDossier


def file_only(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    return [
        ToolCallSpec(
            capability=EvidenceCapability.CURRENT_IMPLEMENTATION,
            arguments=(("file_path", dossier.task.file),),
        )
    ]


def file_sensitive(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    return [
        *file_only(dossier),
        *(
            [
                ToolCallSpec(
                    capability=EvidenceCapability.SECURITY_PATH,
                    arguments=(("symbol_id", symbol),),
                )
            ]
            if (symbol := _symbol_id(dossier))
            else []
        ),
    ]


def file_metrics(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    """收集文件内容，仅对 .java 文件额外调用 inspect_structure。"""
    calls = [*file_only(dossier)]
    if dossier.task.file.endswith(".java"):
        symbol = _symbol_id(dossier)
        if symbol:
            calls.append(
                ToolCallSpec(
                    capability=EvidenceCapability.STRUCTURAL_METRICS,
                    arguments=(("symbol_id", symbol),),
                )
            )
    return calls


def callers_upstream(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    symbol = _symbol_id(dossier)
    return (
        [
            ToolCallSpec(
                capability=EvidenceCapability.UPSTREAM_REACHABILITY,
                arguments=(("symbol_id", symbol),),
            )
        ]
        if symbol
        else []
    )


def _symbol_id(dossier: "CandidateDossier") -> str:
    if dossier.context_bundle is None:
        return ""
    for fact in dossier.context_bundle.facts:
        if fact.kind != "symbol_context" or fact.truncated:
            continue
        try:
            value = json.loads(fact.content)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        symbol = str(value.get("symbol_id", ""))
        if symbol:
            return symbol
    return ""
