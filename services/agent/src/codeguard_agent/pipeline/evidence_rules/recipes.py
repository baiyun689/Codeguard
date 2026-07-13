"""证据策略的 Gateway 工具调用配方。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from codeguard_agent.pipeline.context_rules import resolve_method_name
from codeguard_agent.pipeline.evidence_rules.types import ToolCallSpec

if TYPE_CHECKING:
    from codeguard_agent.pipeline.evidence_planner import CandidateDossier  # type: ignore[import-untyped]


def file_only(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    return [
        ToolCallSpec(
            tool_name="get_file_content",
            arguments=(("file_path", dossier.task.file),),
        )
    ]


def file_sensitive(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    return [
        *file_only(dossier),
        ToolCallSpec(tool_name="find_sensitive_apis", arguments=()),
    ]


def file_metrics(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    return [
        *file_only(dossier),
        ToolCallSpec(
            tool_name="get_code_metrics",
            arguments=(("file_path", dossier.task.file),),
        ),
    ]


def file_callers(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    return [*file_only(dossier), *callers_upstream(dossier)]


def file_metrics_callers(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    return [*file_metrics(dossier), *callers_upstream(dossier)]


def callers_upstream(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    for fact in dossier.context_bundle.facts:
        if fact.kind != "ast_structure" or fact.truncated:
            continue
        method = resolve_method_name(fact.content, dossier.task)
        if method is not None:
            return [
                ToolCallSpec(
                    tool_name="find_callers",
                    arguments=(("query", f"{dossier.task.file}#{method}"),),
                )
            ]
    return []
