"""Capability bundles for one coherent investigation React.

The graph tool locates related symbols and ``get_file_content`` explains the
behavior at those locations.  They are deliberately exposed together for one
investigation; splitting them into separate subtasks loses the dynamic symbol
frontier that makes the bounded React useful.
"""

from __future__ import annotations

from collections.abc import Iterable

from codeguard_agent.models.tasks import EvidenceNeed, InvestigationSeed, ReviewerKind


_GRAPH_TOOLS = {
    "query_relations",
    "read_symbol",
    "inspect_path",
    "inspect_change_impact",
    "inspect_structure",
}

_TOOL_ALIASES = {
    "get_file_content": "read_symbol",
    "inspect_path": "query_relations",
    "inspect_change_impact": "query_relations",
    "inspect_structure": "query_relations",
}

_GRAPH_TOOL_BY_NEED = {
    EvidenceNeed.INSPECT_PATH: "inspect_path",
    EvidenceNeed.INSPECT_CHANGE_IMPACT: "inspect_change_impact",
    EvidenceNeed.INSPECT_STRUCTURE: "inspect_structure",
}


def required_graph_tool(seed: InvestigationSeed) -> str | None:
    """Return the one directional graph capability for ``seed``."""

    requested = _GRAPH_TOOL_BY_NEED.get(seed.evidence_need)
    if requested == "inspect_path":
        if seed.direction == "upstream":
            return None
        return "inspect_path"
    if requested == "inspect_change_impact" and seed.direction == "downstream":
        return None
    return requested


def requested_graph_tools(
    seed: InvestigationSeed,
    requested_tools: Iterable[str],
) -> tuple[str, ...]:
    """Return the seed's graph capability plus an optional neutral supplement.

    ``requested_tools`` is a provider preference, not an authority to switch
    traversal direction.  Once a seed has been normalized, exactly one of
    ``inspect_path`` and ``inspect_change_impact`` can be executable.  A
    provider may still request ``inspect_structure`` as a neutral one-hop
    supplement, but an opposite-direction request is deliberately ignored
    here (rather than merely filtered later by the bundle builder).
    """

    raw_requested = tuple(str(tool) for tool in requested_tools)
    legacy_requested = any(tool in _GRAPH_TOOLS - {"query_relations", "read_symbol"}
                           or tool == "get_file_content" for tool in raw_requested)
    requested = {_TOOL_ALIASES.get(tool, tool) for tool in raw_requested}
    required = required_graph_tool(seed)
    if legacy_requested and "query_relations" not in raw_requested:
        legacy_required = required
        if legacy_required == "inspect_path" and seed.direction == "upstream":
            legacy_required = "inspect_change_impact"
        if legacy_required == "inspect_change_impact" and seed.direction == "downstream":
            legacy_required = "inspect_path"
        tools = [legacy_required] if legacy_required else []
        if "inspect_structure" in raw_requested and "inspect_structure" not in tools:
            tools.append("inspect_structure")
        return tuple(tools)
    tools: list[str] = []
    if required is not None:
        tools.append("query_relations")
    # ``inspect_structure`` is a neutral one-hop supplement.  It can be used
    # with either directional graph query to locate declarations/fields, but
    # the two opposite traversal tools must never be mixed in one subtask.
    if "query_relations" in requested and "query_relations" not in tools:
        tools.append("query_relations")
    return tuple(tools)


def normalize_investigation_seed(seed: InvestigationSeed) -> InvestigationSeed:
    """Fill the executable path contract without inventing a symbol.

    Provider omissions are common, but a directional path query cannot safely
    leave its domain unspecified.  The fixed reviewer supplies the only
    bounded default (behavior vs. security).  Explicitly contradictory
    directions are canonicalized to the matching graph tool; the caller can
    still record that repair in its diagnostics if desired.
    """

    default_path_kind = (
        "security" if seed.reviewer is ReviewerKind.THREAT_MODEL else "behavior"
    )
    if seed.evidence_need is EvidenceNeed.INSPECT_PATH:
        direction = seed.direction or "downstream"
        if direction == "upstream":
            return seed.model_copy(update={
                "evidence_need": EvidenceNeed.INSPECT_CHANGE_IMPACT,
                "path_kind": None,
                "direction": "upstream",
            })
        return seed.model_copy(update={
            "path_kind": seed.path_kind or default_path_kind,
            "direction": "downstream",
        })
    if seed.evidence_need is EvidenceNeed.INSPECT_CHANGE_IMPACT:
        direction = seed.direction or "upstream"
        if direction == "downstream":
            return seed.model_copy(update={
                "evidence_need": EvidenceNeed.INSPECT_PATH,
                "path_kind": seed.path_kind or default_path_kind,
                "direction": "downstream",
            })
        return seed.model_copy(update={
            "path_kind": None,
            "direction": "upstream",
        })
    return seed


def coherent_tool_bundle(
    seed: InvestigationSeed,
    requested_tools: Iterable[str],
    *,
    domain_tools: Iterable[str],
    enabled_tools: Iterable[str] | None = None,
    max_tools: int = 4,
) -> tuple[str, ...]:
    """Normalize one subtask to a directional graph+reader capability set.

    ``requested_tools`` is treated as a provider preference, not a reason to
    split the investigation.  The required graph tool is restored when the
    provider omitted it, and a source reader is added whenever the graph tool
    is available.  Other graph directions are discarded so a caller query
    cannot silently turn into a downstream exploration.
    """

    domain = set(domain_tools)
    enabled = set(enabled_tools) if enabled_tools is not None else None
    raw_requested = tuple(str(tool) for tool in requested_tools)
    requested = tuple(dict.fromkeys(
        _TOOL_ALIASES.get(tool, tool) for tool in raw_requested
    ))

    # Explicit planned_steps callers may still expose only legacy names.  Keep
    # their old bundle semantics while the new default path receives the two
    # canonical tools below.
    new_capabilities = {"read_symbol", "query_relations"}.intersection(domain)
    if not new_capabilities:
        legacy_domain = set(domain)
        legacy_enabled = set(enabled_tools) if enabled_tools is not None else None
        def legacy_available(name: str) -> bool:
            return name in legacy_domain and (
                legacy_enabled is None or name in legacy_enabled
            )
        required = required_graph_tool(seed)
        if required == "inspect_path" and seed.direction == "upstream":
            required = "inspect_change_impact"
        if required == "inspect_change_impact" and seed.direction == "downstream":
            required = "inspect_path"
        legacy_tools = [required] if required and legacy_available(required) else []
        if legacy_tools and legacy_available("get_file_content"):
            legacy_tools.append("get_file_content")
        if not legacy_tools and (
            "get_file_content" in raw_requested or "read_symbol" in raw_requested
        ) and legacy_available("get_file_content"):
            legacy_tools.append("get_file_content")
        return tuple(legacy_tools)[:max(0, max_tools)]

    def available(tool: str) -> bool:
        return tool in domain and (enabled is None or tool in enabled)

    graph_tool = "query_relations" if required_graph_tool(seed) else None
    tools: list[str] = []
    if graph_tool and available(graph_tool):
        tools.append(graph_tool)
        if available("query_relations") and "query_relations" not in tools:
            tools.append("query_relations")
        if available("read_symbol"):
            tools.append("read_symbol")
    elif graph_tool is None:
        # A source-only/compatibility seed has no directional graph contract.
        tools.extend(
            tool for tool in requested
            if tool not in _GRAPH_TOOLS and available(tool)
        )

    # Preserve a requested source reader even if no graph service is enabled;
    # this allows a direct source-only compatibility task to remain useful.
    if (
        "read_symbol" in requested
        and available("read_symbol")
        and "read_symbol" not in tools
    ):
        tools.append("read_symbol")

    if max_tools <= 0:
        return ()
    return tuple(dict.fromkeys(tools))[:max_tools]


__all__ = [
    "coherent_tool_bundle",
    "normalize_investigation_seed",
    "requested_graph_tools",
    "required_graph_tool",
]
