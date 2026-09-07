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
    "inspect_path",
    "inspect_change_impact",
    "inspect_structure",
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

    requested = set(requested_tools)
    required = required_graph_tool(seed)
    tools: list[str] = []
    if required is not None:
        tools.append(required)
    # ``inspect_structure`` is a neutral one-hop supplement.  It can be used
    # with either directional graph query to locate declarations/fields, but
    # the two opposite traversal tools must never be mixed in one subtask.
    if "inspect_structure" in requested and "inspect_structure" not in tools:
        tools.append("inspect_structure")
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
    requested = tuple(dict.fromkeys(str(tool) for tool in requested_tools))

    def available(tool: str) -> bool:
        return tool in domain and (enabled is None or tool in enabled)

    graph_tool = required_graph_tool(seed)
    requested_graph = requested_graph_tools(seed, requested)
    tools: list[str] = []
    if graph_tool and available(graph_tool):
        tools.append(graph_tool)
        if "inspect_structure" in requested_graph and available("inspect_structure"):
            tools.append("inspect_structure")
        if available("get_file_content"):
            tools.append("get_file_content")
    elif graph_tool is None:
        # A source-only/compatibility seed has no directional graph contract.
        tools.extend(
            tool for tool in requested
            if tool not in _GRAPH_TOOLS and available(tool)
        )

    # Preserve a requested source reader even if no graph service is enabled;
    # this allows a direct source-only compatibility task to remain useful.
    if (
        "get_file_content" in requested
        and available("get_file_content")
        and "get_file_content" not in tools
    ):
        tools.append("get_file_content")

    if max_tools <= 0:
        return ()
    return tuple(dict.fromkeys(tools))[:max_tools]


__all__ = [
    "coherent_tool_bundle",
    "normalize_investigation_seed",
    "requested_graph_tools",
    "required_graph_tool",
]
