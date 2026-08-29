"""inspect_* 图响应的确定性处理:结构化压缩 + 完整性护栏。

压缩与护栏自旧 verifier 迁移(Evidence Ledger 切换后保留,源文档 §7.3):
- 14KB 图 JSON 不能全文进 LLM 载荷,确定性结构化压缩保留
  schema/outcome/coverage/scope/subject/relationships/limitations;
- subject/source_scope/outcome 护栏是图工具调用正确性的关键检查
  (历史教训:该校验缺失导致过整档评测作废)。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codeguard_agent.models.evidence import EvidenceValidationStatus

_GRAPH_SUMMARY_MAX_CHARS = 8000
_GRAPH_SUMMARY_HARD_MAX_CHARS = 16000
_GRAPH_MAX_ENUMERATED_PATHS = 4096
_GRAPH_HEADER_KEYS = (
    "schema_version", "outcome", "coverage", "source_scope",
    "subject_symbol_id", "unresolved_count", "limitations",
)
_GRAPH_SYMBOL_KEYS = (
    "id", "kind", "file", "startLine", "endLine", "source_set",
)
_GRAPH_RELATION_KEYS = (
    "sourceId", "targetId", "kind", "file", "line", "source_set", "resolution",
)
_LEGACY_SCOPE_KEYS = frozenset({
    "main_symbols", "test_symbols", "generated_symbols",
    "main_relationships", "test_relationships", "generated_relationships",
})

_VALID_SOURCE_SCOPES = {"MAIN", "TEST", "GENERATED"}
_GRAPH_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class GraphValidation:
    status: EvidenceValidationStatus
    limitations: tuple[str, ...] = ()
    replayable: bool = False


@dataclass(frozen=True)
class GraphProjectionFocus:
    """Task-local relevance facts used by deterministic graph projection."""

    changed_file: str | None = None
    changed_lines: tuple[int, ...] = ()
    changed_symbol_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _GraphEdge:
    payload: dict[str, Any]
    signature: tuple[str, ...]
    kind: str
    source: str
    target: str


@dataclass(frozen=True)
class _GraphPath:
    edges: tuple[_GraphEdge, ...]
    nodes: tuple[str, ...]
    signature: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _ProjectionSelection:
    relationships: tuple[_GraphEdge, ...]
    total_path_count: int
    omitted_path_count: int
    enumeration_capped: bool
    hard_limit_exceeded: bool


def summarize_graph(
    raw: str,
    *,
    tool: str = "",
    arguments: Mapping[str, Any] | None = None,
    focus: GraphProjectionFocus | None = None,
) -> str:
    """对 inspect_* 图响应做确定性、路径感知的结构化压缩(零 LLM)。

    关系的遍历方向严格复现 Gateway 当前工具语义。可遍历 CALLS 形成完整
    maximal path，其他关系只作为附着事实；预算不足时丢弃整个路径，不截断
    JSON 或路径尾部。raw payload 永远由 Evidence Artifact 原样保存。
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw[:_GRAPH_SUMMARY_MAX_CHARS]
    if not isinstance(payload, dict):
        return raw[:_GRAPH_SUMMARY_MAX_CHARS]

    summary: dict[str, Any] = {
        key: payload.get(key) for key in _GRAPH_HEADER_KEYS if key in payload
    }
    raw_symbols = [
        symbol for symbol in payload.get("symbols") or []
        if isinstance(symbol, dict)
    ]
    raw_relations = [
        rel for rel in payload.get("relationships") or []
        if isinstance(rel, dict)
    ]
    edges = _normalize_edges(raw_relations)
    selection = _select_graph_facts(
        edges,
        subject=str(payload.get("subject_symbol_id", "")),
        tool=tool,
        arguments=arguments,
        focus=focus,
        symbols=raw_symbols,
        summary_seed={
            key: payload.get(key)
            for key in _GRAPH_HEADER_KEYS
            if key in payload
        },
    )
    selected_edges = list(selection.relationships)
    selected_signatures = {edge.signature for edge in selected_edges}
    selected_symbol_ids = {str(payload.get("subject_symbol_id", ""))}
    for edge in selected_edges:
        selected_symbol_ids.update((edge.source, edge.target))
    symbols = [
        {key: symbol.get(key) for key in _GRAPH_SYMBOL_KEYS if key in symbol}
        for symbol in raw_symbols
        if str(symbol.get("id", "")) in selected_symbol_ids
    ]
    omitted_relationships = len({edge.signature for edge in edges}) - len(
        selected_signatures
    )
    omitted_symbols = max(0, len(raw_symbols) - len(symbols))
    limitations = [
        str(item) for item in payload.get("limitations") or []
        if isinstance(item, str) and item
    ]
    if selection.enumeration_capped:
        limitations.append("path_enumeration_capped")
    if selection.hard_limit_exceeded:
        limitations.append("projection_hard_limit_exceeded")
    truncated = bool(
        omitted_relationships or omitted_symbols or selection.enumeration_capped
    )
    if truncated:
        limitations.append("projection_truncated")
    summary["symbols"] = symbols
    summary["relationships"] = [edge.payload for edge in selected_edges]
    summary["omitted_count"] = omitted_relationships
    summary["omitted_symbol_count"] = omitted_symbols
    summary["omitted_path_count"] = selection.omitted_path_count
    summary["limitations"] = list(dict.fromkeys(limitations))
    rendered = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= _GRAPH_SUMMARY_HARD_MAX_CHARS:
        return rendered

    # A single path larger than the hard safety limit must never be half emitted.
    # Keep the subject/header contract and all omission diagnostics instead.
    summary["relationships"] = []
    summary["symbols"] = [
        {key: symbol.get(key) for key in _GRAPH_SYMBOL_KEYS if key in symbol}
        for symbol in raw_symbols
        if str(symbol.get("id", "")) == str(payload.get("subject_symbol_id", ""))
    ]
    summary["omitted_count"] = len({edge.signature for edge in edges})
    summary["omitted_symbol_count"] = max(
        0, len(raw_symbols) - len(summary["symbols"])
    )
    summary["omitted_path_count"] = max(
        selection.omitted_path_count,
        selection.total_path_count,
    )
    summary["limitations"] = list(dict.fromkeys(
        [*limitations, "projection_hard_limit_exceeded"]
    ))
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def _normalize_edges(raw_relations: list[dict[str, Any]]) -> list[_GraphEdge]:
    edges: dict[tuple[str, ...], _GraphEdge] = {}
    for relation in raw_relations:
        source = str(relation.get("sourceId", ""))
        target = str(relation.get("targetId", ""))
        kind = str(relation.get("kind", "")).upper()
        signature = tuple(
            str(relation.get(key, ""))
            for key in (
                "sourceId", "targetId", "kind", "file", "line",
                "source_set", "resolution",
            )
        )
        normalized = {
            key: relation.get(key)
            for key in _GRAPH_RELATION_KEYS
            if key in relation
        }
        if "kind" in normalized:
            normalized["kind"] = kind
        edges.setdefault(
            signature,
            _GraphEdge(
                payload=normalized,
                signature=signature,
                kind=kind,
                source=source,
                target=target,
            ),
        )
    return sorted(edges.values(), key=lambda edge: edge.signature)


def _select_graph_facts(
    edges: list[_GraphEdge],
    *,
    subject: str,
    tool: str,
    arguments: Mapping[str, Any] | None,
    focus: GraphProjectionFocus | None,
    symbols: list[dict[str, Any]],
    summary_seed: Mapping[str, Any],
) -> _ProjectionSelection:
    subject_kind = next(
        (
            str(symbol.get("kind", "")).upper()
            for symbol in symbols
            if str(symbol.get("id", "")) == subject
        ),
        "",
    )
    traversal, attached = _classify_edges(
        edges,
        tool=tool,
        arguments=arguments,
        subject_kind=subject_kind,
    )
    if (
        tool == "inspect_path"
        and str((arguments or {}).get("path_kind", "")).lower() == "security"
    ):
        reachable = _reachable_traversal_edges(traversal, subject)
        reachable_signatures = {edge.signature for edge, _, _ in reachable}
        attached.extend(
            edge
            for edge, _, _ in traversal
            if edge.signature not in reachable_signatures
        )
        traversal = reachable
    max_depth = _max_depth(arguments)
    paths, enumeration_capped = _enumerate_paths(
        traversal,
        subject=subject,
        max_depth=max_depth,
    )
    path_order = sorted(
        paths,
        key=lambda path: _path_priority(
            path,
            attached,
            symbols,
            focus=focus,
            subject=subject,
        ),
    )
    selected_edges: dict[tuple[str, ...], _GraphEdge] = {}
    selected_paths: list[_GraphPath] = []
    hard_limit_exceeded = False

    def fits(candidate: list[_GraphEdge]) -> bool:
        ids = {subject}
        for edge in candidate:
            ids.update((edge.source, edge.target))
        selected_symbols = [
            {key: symbol.get(key) for key in _GRAPH_SYMBOL_KEYS if key in symbol}
            for symbol in symbols
            if str(symbol.get("id", "")) in ids
        ]
        value = _candidate_summary(
            summary_seed,
            candidate,
            selected_symbols,
            omitted_count=max(0, len(edges) - len(candidate)),
            omitted_path_count=max(0, len(paths) - len(selected_paths)),
            limitations=list(summary_seed.get("limitations") or []),
        )
        return len(value) <= _GRAPH_SUMMARY_MAX_CHARS

    # Complete traversal paths are the primary units. Shared edges count once.
    for path in path_order:
        additions = [
            edge for edge in path.edges if edge.signature not in selected_edges
        ]
        candidate = [*selected_edges.values(), *additions]
        if not selected_paths and not fits(candidate):
            # The first path may exceed target_budget, but only as a complete unit.
            if _serialized_size(summary_seed, candidate, symbols, subject) <= (
                _GRAPH_SUMMARY_HARD_MAX_CHARS
            ):
                for edge in additions:
                    selected_edges[edge.signature] = edge
                selected_paths.append(path)
            else:
                hard_limit_exceeded = True
            continue
        if not additions or fits(candidate):
            for edge in additions:
                selected_edges[edge.signature] = edge
            selected_paths.append(path)

    path_nodes = {
        node
        for path in selected_paths
        for node in path.nodes
    }
    attached_order = sorted(
        attached,
        key=lambda edge: _attached_priority(
            edge, path_nodes, symbols, focus, subject
        ),
    )
    for edge in attached_order:
        if edge.signature in selected_edges:
            continue
        candidate = [*selected_edges.values(), edge]
        if fits(candidate):
            selected_edges[edge.signature] = edge

    # Structure has no traversal paths; select one-hop facts with the same budget
    # and stable ordering rather than applying an arbitrary relationship prefix.
    if not paths:
        selected_edges.clear()
        for edge in sorted(
            attached,
            key=lambda item: _attached_priority(
                item, {subject}, symbols, focus, subject
            ),
        ):
            candidate = [*selected_edges.values(), edge]
            if not selected_edges and not fits(candidate):
                if _serialized_size(summary_seed, candidate, symbols, subject) > (
                    _GRAPH_SUMMARY_HARD_MAX_CHARS
                ):
                    continue
            if fits(candidate) or not selected_edges:
                selected_edges[edge.signature] = edge

    omitted_paths = max(0, len(paths) - len(selected_paths))
    if enumeration_capped:
        omitted_paths = max(omitted_paths, 1)
    return _ProjectionSelection(
        relationships=tuple(selected_edges.values()),
        total_path_count=len(paths),
        omitted_path_count=omitted_paths,
        enumeration_capped=enumeration_capped,
        hard_limit_exceeded=hard_limit_exceeded,
    )


def _classify_edges(
    edges: list[_GraphEdge],
    *,
    tool: str,
    arguments: Mapping[str, Any] | None,
    subject_kind: str,
) -> tuple[list[tuple[_GraphEdge, str, str]], list[_GraphEdge]]:
    """Return traversal edges as (edge, from, to), plus non-expanding facts."""
    traversal: list[tuple[_GraphEdge, str, str]] = []
    attached: list[_GraphEdge] = []
    for edge in edges:
        if tool == "inspect_path" and edge.kind == "CALLS":
            traversal.append((edge, edge.source, edge.target))
        elif (
            tool == "inspect_change_impact"
            and subject_kind not in {"FIELD", "TYPE"}
            and edge.kind == "CALLS"
        ):
            traversal.append((edge, edge.target, edge.source))
        else:
            attached.append(edge)
    # Security and behavior both follow only resolved CALLS; validation already
    # excludes unresolved relationships from the canonical relationships array.
    return traversal, attached


def _reachable_traversal_edges(
    traversal: list[tuple[_GraphEdge, str, str]],
    subject: str,
) -> list[tuple[_GraphEdge, str, str]]:
    """Keep only edges reachable from subject in the returned Gateway facts."""
    adjacency: dict[str, list[tuple[_GraphEdge, str]]] = {}
    for edge, source, target in traversal:
        adjacency.setdefault(source, []).append((edge, target))
    reachable: list[tuple[_GraphEdge, str, str]] = []
    frontier = [subject]
    visited: set[str] = set()
    seen_edges: set[tuple[str, ...]] = set()
    while frontier:
        current = frontier.pop(0)
        if current in visited:
            continue
        visited.add(current)
        for edge, target in sorted(
            adjacency.get(current, []), key=lambda item: item[0].signature
        ):
            if edge.signature in seen_edges:
                continue
            seen_edges.add(edge.signature)
            reachable.append((edge, current, target))
            frontier.append(target)
    return reachable


def _enumerate_paths(
    traversal: list[tuple[_GraphEdge, str, str]],
    *,
    subject: str,
    max_depth: int,
) -> tuple[list[_GraphPath], bool]:
    adjacency: dict[str, list[tuple[_GraphEdge, str]]] = {}
    for edge, source, target in traversal:
        adjacency.setdefault(source, []).append((edge, target))
    for values in adjacency.values():
        values.sort(key=lambda pair: pair[0].signature)
    roots = [subject] if subject in adjacency else []

    paths: list[_GraphPath] = []
    seen: set[tuple[tuple[str, ...], ...]] = set()
    capped = False

    def walk(
        node: str,
        path_edges: tuple[_GraphEdge, ...],
        path_nodes: tuple[str, ...],
    ) -> None:
        nonlocal capped
        if len(paths) >= _GRAPH_MAX_ENUMERATED_PATHS:
            capped = True
            return
        options = [
            (edge, target)
            for edge, target in adjacency.get(node, [])
            if edge.signature not in {item.signature for item in path_edges}
        ]
        if len(path_edges) >= max_depth or not options:
            signature = tuple(edge.signature for edge in path_edges)
            if signature and signature not in seen:
                seen.add(signature)
                paths.append(_GraphPath(path_edges, path_nodes, signature))
            return
        for edge, target in options:
            walk(target, (*path_edges, edge), (*path_nodes, target))
            if capped:
                return

    for root in roots:
        walk(root, (), (root,))
        if capped:
            break
    return paths, capped


_SEMANTIC_EDGE_KINDS = frozenset({
    "LISTENS_TO_EVENT", "SCHEDULED_BY", "READS_FIELD", "WRITES_FIELD",
    "IMPLEMENTS", "OVERRIDES", "EXPOSES_ROUTE",
})


def _path_priority(
    path: _GraphPath,
    attached: list[_GraphEdge],
    symbols: list[dict[str, Any]],
    *,
    focus: GraphProjectionFocus | None,
    subject: str,
) -> tuple[Any, ...]:
    nodes = set(path.nodes)
    path_edges = (*path.edges, *[
        edge for edge in attached
        if edge.source in nodes or edge.target in nodes
    ])
    return (
        0 if _hits_changed_line(path_edges, symbols, path.nodes, focus) else 1,
        0 if _hits_non_subject_symbol(path_edges, focus, subject) else 1,
        0 if any(edge.kind in _SEMANTIC_EDGE_KINDS for edge in path_edges) else 1,
        len(path.edges),
        path.signature,
    )


def _attached_priority(
    edge: _GraphEdge,
    path_nodes: set[str],
    symbols: list[dict[str, Any]],
    focus: GraphProjectionFocus | None,
    subject: str,
) -> tuple[Any, ...]:
    return (
        0 if edge.source == subject or edge.target == subject else 1,
        0 if edge.source in path_nodes or edge.target in path_nodes else 1,
        0 if _hits_changed_line((edge,), symbols, tuple(path_nodes), focus) else 1,
        0 if _hits_non_subject_symbol((edge,), focus, subject) else 1,
        0 if edge.kind in _SEMANTIC_EDGE_KINDS else 1,
        edge.signature,
    )


def _hits_changed_line(
    edges: tuple[_GraphEdge, ...] | list[_GraphEdge],
    symbols: list[dict[str, Any]],
    path_nodes: tuple[str, ...],
    focus: GraphProjectionFocus | None,
) -> bool:
    if focus is None or not focus.changed_file or not focus.changed_lines:
        return False
    lines = set(focus.changed_lines)
    if any(
        str(edge.payload.get("file", "")) == focus.changed_file
        and _as_int(edge.payload.get("line")) in lines
        for edge in edges
    ):
        return True
    return any(
        str(symbol.get("id", "")) in path_nodes
        and str(symbol.get("file", "")) == focus.changed_file
        and _range_hits_lines(symbol, lines)
        for symbol in symbols
    )


def _range_hits_lines(symbol: dict[str, Any], lines: set[int]) -> bool:
    start = _as_int(symbol.get("startLine"))
    end = _as_int(symbol.get("endLine"))
    return start is not None and end is not None and any(
        start <= line <= end for line in lines
    )


def _hits_non_subject_symbol(
    edges: tuple[_GraphEdge, ...] | list[_GraphEdge],
    focus: GraphProjectionFocus | None,
    subject: str,
) -> bool:
    if focus is None:
        return False
    changed = set(focus.changed_symbol_ids) - {subject}
    return any(edge.source in changed or edge.target in changed for edge in edges)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _max_depth(arguments: Mapping[str, Any] | None) -> int:
    value = _as_int((arguments or {}).get("max_depth"))
    return max(1, min(3, value or 3))


def _serialized_size(
    summary_seed: Mapping[str, Any],
    edges: list[_GraphEdge],
    symbols: list[dict[str, Any]],
    subject: str,
) -> int:
    ids = {subject}
    for edge in edges:
        ids.update((edge.source, edge.target))
    selected_symbols = [
        {key: symbol.get(key) for key in _GRAPH_SYMBOL_KEYS if key in symbol}
        for symbol in symbols
        if str(symbol.get("id", "")) in ids
    ]
    return len(_candidate_summary(
        summary_seed,
        edges,
        selected_symbols,
        omitted_count=0,
        omitted_path_count=0,
        limitations=list(summary_seed.get("limitations") or []),
    ))


def _candidate_summary(
    summary_seed: Mapping[str, Any],
    edges: list[_GraphEdge],
    symbols: list[dict[str, Any]],
    *,
    omitted_count: int,
    omitted_path_count: int,
    limitations: list[str],
) -> str:
    value = dict(summary_seed)
    value["symbols"] = symbols
    value["relationships"] = [edge.payload for edge in edges]
    value["omitted_count"] = omitted_count
    value["omitted_symbol_count"] = 0
    value["omitted_path_count"] = omitted_path_count
    value["limitations"] = limitations
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def validate_graph_payload(
    raw: str,
    *,
    tool: str,
    expected_subject: str = "",
) -> GraphValidation:
    """验证 v2 图响应；旧 status 合同直接判为协议不兼容。"""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_payload_unparseable",),
            replayable=True,
        )
    if not isinstance(payload, dict):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_payload_unparseable",),
            replayable=True,
        )

    if payload.get("schema_version") != _GRAPH_SCHEMA_VERSION:
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_protocol_mismatch",),
        )
    if _LEGACY_SCOPE_KEYS.intersection(payload):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_legacy_scope_fields",),
        )

    raw_limitations = payload.get("limitations")
    if not isinstance(raw_limitations, list) or any(
        not isinstance(item, str) for item in raw_limitations
    ):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("invalid_graph_limitations",),
        )
    limitations = [item for item in raw_limitations if item]
    actual_subject = str(payload.get("subject_symbol_id", ""))
    if expected_subject and actual_subject != expected_subject:
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_subject_mismatch",),
        )
    outcome = payload.get("outcome")
    coverage = payload.get("coverage")
    source_scope = str(payload.get("source_scope", "")).upper()
    relationships = payload.get("relationships")
    unresolved_relationships = payload.get("unresolved_relationships")
    unresolved_count = payload.get("unresolved_count")
    symbols = payload.get("symbols")
    if source_scope not in _VALID_SOURCE_SCOPES:
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("invalid_graph_source_scope",),
        )
    if not isinstance(relationships, list):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("invalid_graph_relationships",),
        )
    if (
        not isinstance(symbols, list)
        or any(
            not isinstance(item, dict)
            or str(item.get("source_set", "")).upper() != source_scope
            for item in symbols
        )
    ):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_symbol_scope_mismatch",),
        )
    if any(
        not isinstance(item, dict)
        or str(item.get("source_set", "")).upper() != source_scope
        or str(item.get("resolution", "")).upper() != "RESOLVED"
        for item in relationships
    ):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_source_scope_or_resolution_mismatch",),
        )
    if (
        not isinstance(unresolved_relationships, list)
        or not isinstance(unresolved_count, int)
        or isinstance(unresolved_count, bool)
        or unresolved_count < len(unresolved_relationships)
        or any(
            not isinstance(item, dict)
            or str(item.get("source_set", "")).upper() != source_scope
            or str(item.get("resolution", "")).upper() == "RESOLVED"
            for item in unresolved_relationships
        )
    ):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("invalid_graph_unresolved_relationships",),
        )
    valid_combination = (outcome, coverage) in {
        ("found", "complete"),
        ("found", "partial"),
        ("not_found", "complete"),
        ("indeterminate", "partial"),
    }
    if not valid_combination:
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("invalid_graph_outcome_coverage",),
        )
    if coverage == "complete" and (
        unresolved_count or unresolved_relationships
    ):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_complete_with_unresolved_relationships",),
        )
    subject_fact = tool == "inspect_structure" and isinstance(symbols, list) and bool(symbols)
    if outcome == "found" and not relationships and not subject_fact:
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_found_without_fact",),
        )
    if outcome in {"not_found", "indeterminate"} and relationships:
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_non_found_with_relationships",),
        )
    if outcome == "indeterminate":
        return GraphValidation(
            EvidenceValidationStatus.UNAVAILABLE,
            tuple(dict.fromkeys(["graph_indeterminate", *limitations])),
        )
    if coverage == "partial":
        limitations.append("graph_coverage_partial")
        return GraphValidation(
            EvidenceValidationStatus.LIMITED,
            tuple(dict.fromkeys(limitations)),
        )
    return GraphValidation(
        EvidenceValidationStatus.VALID,
        tuple(dict.fromkeys(limitations)),
    )


__all__ = [
    "GraphProjectionFocus",
    "GraphValidation",
    "summarize_graph",
    "validate_graph_payload",
]
