"""query_relations 图响应的确定性处理:结构化压缩 + 完整性护栏。

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
_TARGET_BRANCH_REPRESENTATIVE_LIMIT = 4
_GRAPH_HEADER_KEYS = (
    "schema_version",
    "outcome",
    "coverage",
    "source_scope",
    "subject_symbol_id",
    "unresolved_count",
    "limitations",
)
_GRAPH_SYMBOL_KEYS = ("id", "kind", "file", "startLine", "endLine", "source_set")
_GRAPH_RELATION_KEYS = (
    "sourceId",
    "targetId",
    "kind",
    "file",
    "line",
    "source_set",
    "resolution",
)
_LEGACY_SCOPE_KEYS = frozenset(
    {
        "main_symbols",
        "test_symbols",
        "generated_symbols",
        "main_relationships",
        "test_relationships",
        "generated_relationships",
    }
)
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
    deletion_anchor_lines: tuple[int, ...] = ()
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
    max_chars: int = _GRAPH_SUMMARY_MAX_CHARS,
    hard_max_chars: int = _GRAPH_SUMMARY_HARD_MAX_CHARS,
) -> str:
    """对 query_relations 图响应做确定性、路径感知的结构化压缩(零 LLM)。

    关系的遍历方向严格复现 Gateway 当前工具语义。可遍历 CALLS 形成完整
    maximal path，其他关系只作为附着事实；预算不足时丢弃整个路径，不截断
    JSON 或路径尾部。raw payload 永远由 Evidence Artifact 原样保存。
    """
    target_max_chars = max(512, min(max_chars, hard_max_chars))
    safety_max_chars = max(target_max_chars, hard_max_chars)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw[:target_max_chars]
    if not isinstance(payload, dict):
        return raw[:target_max_chars]
    summary: dict[str, Any] = {
        key: payload.get(key) for key in _GRAPH_HEADER_KEYS if key in payload
    }
    raw_symbols = [
        symbol for symbol in payload.get("symbols") or [] if isinstance(symbol, dict)
    ]
    raw_relations = [
        rel for rel in payload.get("relationships") or [] if isinstance(rel, dict)
    ]
    unresolved_relationships = _normalize_unresolved_relationships(
        payload.get("unresolved_relationships") or []
    )
    summary_seed = {
        key: payload.get(key) for key in _GRAPH_HEADER_KEYS if key in payload
    }
    edges = _normalize_edges(raw_relations)
    selection = _select_graph_facts(
        edges,
        subject=str(payload.get("subject_symbol_id", "")),
        tool=tool,
        arguments=arguments,
        focus=focus,
        symbols=raw_symbols,
        summary_seed=summary_seed,
        target_max_chars=target_max_chars,
        hard_max_chars=safety_max_chars,
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
    projected_unresolved, omitted_unresolved = _select_unresolved_relationships(
        summary_seed,
        unresolved_relationships,
        selected_edges,
        symbols,
        max_chars=target_max_chars,
    )
    omitted_relationships = len({edge.signature for edge in edges}) - len(
        selected_signatures
    )
    omitted_symbols = max(0, len(raw_symbols) - len(symbols))
    limitations = [
        str(item)
        for item in payload.get("limitations") or []
        if isinstance(item, str) and item
    ]
    if selection.enumeration_capped:
        limitations.append("path_enumeration_capped")
    if selection.hard_limit_exceeded:
        limitations.append("projection_hard_limit_exceeded")
    if omitted_unresolved:
        limitations.append("unresolved_relationships_truncated")
    truncated = bool(
        omitted_relationships
        or omitted_symbols
        or omitted_unresolved
        or selection.enumeration_capped
    )
    if truncated:
        limitations.append("projection_truncated")
    summary["symbols"] = symbols
    summary["relationships"] = [edge.payload for edge in selected_edges]
    summary["unresolved_relationships"] = list(projected_unresolved)
    summary["omitted_count"] = omitted_relationships
    summary["omitted_symbol_count"] = omitted_symbols
    summary["omitted_unresolved_count"] = omitted_unresolved
    summary["omitted_path_count"] = selection.omitted_path_count
    summary["limitations"] = list(dict.fromkeys(limitations))
    rendered = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= safety_max_chars:
        _attach_source_excerpts(summary, payload, max_chars=target_max_chars)
        rendered = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= safety_max_chars:
            return rendered
    summary["relationships"] = []
    summary["symbols"] = [
        {key: symbol.get(key) for key in _GRAPH_SYMBOL_KEYS if key in symbol}
        for symbol in raw_symbols
        if str(symbol.get("id", "")) == str(payload.get("subject_symbol_id", ""))
    ]
    summary["unresolved_relationships"] = []
    summary["omitted_count"] = len({edge.signature for edge in edges})
    summary["omitted_symbol_count"] = max(0, len(raw_symbols) - len(summary["symbols"]))
    summary["omitted_unresolved_count"] = len(unresolved_relationships)
    summary["omitted_path_count"] = max(
        selection.omitted_path_count, selection.total_path_count
    )
    summary["limitations"] = list(
        dict.fromkeys([*limitations, "projection_hard_limit_exceeded"])
    )
    _attach_source_excerpts(summary, payload, max_chars=0)
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def _attach_source_excerpts(
    summary: dict[str, Any], payload: dict[str, Any], *, max_chars: int
) -> None:
    """Spend remaining space on whole excerpts, never displace graph paths."""
    excerpts = {
        symbol["id"]: symbol["source_excerpt"]
        for symbol in payload.get("symbols", ())
        if isinstance(symbol, dict)
        and symbol.get("id")
        and symbol.get("source_excerpt")
    }
    if not excerpts and "omitted_source_excerpt_count" not in payload:
        return
    omitted = payload.get("omitted_source_excerpt_count", 0)
    omitted = omitted if type(omitted) is int and omitted >= 0 else 0
    summary["omitted_source_excerpt_count"] = omitted + len(excerpts)
    for symbol in summary["symbols"]:
        excerpt = excerpts.get(symbol.get("id"))
        if excerpt is None:
            continue
        symbol["source_excerpt"] = excerpt
        size = len(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
        if size + 40 > max_chars:
            del symbol["source_excerpt"]
        else:
            summary["omitted_source_excerpt_count"] -= 1
    if summary["omitted_source_excerpt_count"]:
        summary["limitations"] = list(
            dict.fromkeys([*summary["limitations"], "source_excerpts_omitted"])
        )


def _valid_source_excerpts(payload: dict[str, Any]) -> bool:
    subject = payload.get("subject_symbol_id")
    endpoints: set[str] = set()
    for edge in payload.get("relationships", ()):
        if (
            not isinstance(edge, dict)
            or str(edge.get("resolution", "")).upper() != "RESOLVED"
        ):
            continue
        if not isinstance(edge.get("sourceId"), str) or not isinstance(
            edge.get("targetId"), str
        ):
            continue
        if edge.get("sourceId") == subject:
            endpoints.add(edge["targetId"])
        elif edge.get("targetId") == subject:
            endpoints.add(edge["sourceId"])
    count = 0
    for symbol in payload["symbols"]:
        if "source_excerpt" not in symbol:
            continue
        count += 1
        excerpt = symbol["source_excerpt"]
        if (
            not isinstance(excerpt, dict)
            or not isinstance(symbol.get("id"), str)
            or symbol["id"] not in endpoints
            or (count > 3)
        ):
            return False
        first, last = (symbol.get("startLine"), symbol.get("endLine"))
        start, end = (excerpt.get("start_line"), excerpt.get("end_line"))
        text = excerpt.get("text")
        if not (
            type(first) is int
            and type(last) is int
            and (type(start) is int)
            and (type(end) is int)
        ):
            return False
        if (
            not 1 <= first <= start <= end <= last
            or end - start >= 24
            or (not isinstance(text, str))
            or (not text)
            or (len(text) > 1000)
            or (len(text.splitlines()) != end - start + 1)
            or (type(excerpt.get("truncated")) is not bool)
            or (excerpt["truncated"] != (start > first or end < last))
        ):
            return False
        if end < last:
            if (
                type(excerpt.get("next_cursor")) is not int
                or excerpt["next_cursor"] != end + 1
            ):
                return False
        elif "next_cursor" in excerpt:
            return False
    return True


def _normalize_edges(raw_relations: list[dict[str, Any]]) -> list[_GraphEdge]:
    edges: dict[tuple[str, ...], _GraphEdge] = {}
    for relation in raw_relations:
        source = str(relation.get("sourceId", ""))
        target = str(relation.get("targetId", ""))
        kind = str(relation.get("kind", "")).upper()
        signature = tuple(
            (
                str(relation.get(key, ""))
                for key in (
                    "sourceId",
                    "targetId",
                    "kind",
                    "file",
                    "line",
                    "source_set",
                    "resolution",
                )
            )
        )
        normalized = {
            key: relation.get(key) for key in _GRAPH_RELATION_KEYS if key in relation
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
    target_max_chars: int = _GRAPH_SUMMARY_MAX_CHARS,
    hard_max_chars: int = _GRAPH_SUMMARY_HARD_MAX_CHARS,
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
        edges, tool=tool, arguments=arguments, subject_kind=subject_kind
    )
    max_depth = _max_depth(arguments)
    paths, enumeration_capped = _enumerate_paths(
        traversal, subject=subject, max_depth=max_depth
    )
    path_order = _dedupe_path_families(
        sorted(
            paths,
            key=lambda path: _path_priority(
                path, attached, focus=focus, subject=subject
            ),
        )
    )
    canonical_path_count = len(path_order)
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
            omitted_path_count=max(0, canonical_path_count - len(selected_paths)),
            limitations=list(summary_seed.get("limitations") or []),
        )
        return len(value) <= target_max_chars

    selected_branches: set[str] = set()
    selected_families: set[tuple[tuple[str, str, str], ...]] = set()

    def add_path(path: _GraphPath, *, allow_target_overflow: bool = False) -> bool:
        nonlocal hard_limit_exceeded
        additions = [
            edge for edge in path.edges if edge.signature not in selected_edges
        ]
        if (
            path.edges
            and _serialized_size(summary_seed, list(path.edges), symbols, subject)
            > hard_max_chars
        ):
            hard_limit_exceeded = True
            return False
        candidate = [*selected_edges.values(), *additions]
        if not selected_paths and (not fits(candidate)):
            if (
                _serialized_size(summary_seed, candidate, symbols, subject)
                <= hard_max_chars
            ):
                for edge in additions:
                    selected_edges[edge.signature] = edge
                selected_paths.append(path)
                selected_families.add(_path_family_key(path))
                return True
            else:
                hard_limit_exceeded = True
            return False
        if (
            not additions
            or fits(candidate)
            or (
                allow_target_overflow
                and _serialized_size(summary_seed, candidate, symbols, subject)
                <= hard_max_chars
            )
        ):
            for edge in additions:
                selected_edges[edge.signature] = edge
            selected_paths.append(path)
            selected_families.add(_path_family_key(path))
            return True
        return False

    changed_paths = [
        path for path in path_order if _path_hits_changed_fact(path, focus)
    ]
    target_paths = [
        path
        for path in path_order
        if _path_has_semantic_target(path, attached, subject)
    ]
    for path in changed_paths:
        branch = path.edges[0].target if path.edges else subject
        if branch in selected_branches:
            continue
        if add_path(path):
            selected_branches.add(branch)
    target_representatives: dict[str, list[_GraphPath]] = {}
    for path in target_paths:
        target_family = _path_target_family(path, attached, subject)
        if not target_family:
            continue
        target_representatives.setdefault(target_family, []).append(path)
    for paths_for_family in target_representatives.values():
        paths_for_family.sort(
            key=lambda path: _path_priority(
                path, attached, focus=focus, subject=subject
            )
        )
    target_groups = sorted(
        {family.split(":", 1)[0] for family in target_representatives}
    )
    for ordinal in range(_TARGET_BRANCH_REPRESENTATIVE_LIMIT):
        for group in target_groups:
            families = sorted(
                (
                    family
                    for family in target_representatives
                    if family.startswith(f"{group}:")
                ),
                key=lambda family: _path_priority(
                    target_representatives[family][0],
                    attached,
                    focus=focus,
                    subject=subject,
                ),
            )[:_TARGET_BRANCH_REPRESENTATIVE_LIMIT]
            if ordinal >= len(families):
                continue
            path = target_representatives[families[ordinal]][0]
            if _path_family_key(path) not in selected_families:
                add_path(path, allow_target_overflow=True)
    for path in path_order:
        if _path_family_key(path) in selected_families:
            continue
        if _path_target_family(path, attached, subject) in target_representatives:
            continue
        add_path(path)
    if hard_limit_exceeded:
        selected_edges.clear()
        selected_paths.clear()
    path_nodes = {node for path in selected_paths for node in path.nodes}
    if not hard_limit_exceeded:
        attached_order = sorted(
            attached,
            key=lambda edge: _attached_priority(edge, path_nodes, focus, subject),
        )
        for edge in attached_order:
            if edge.signature in selected_edges:
                continue
            candidate = [*selected_edges.values(), edge]
            if fits(candidate):
                selected_edges[edge.signature] = edge
    if not paths:
        selected_edges.clear()
        for edge in sorted(
            attached,
            key=lambda item: _attached_priority(item, {subject}, focus, subject),
        ):
            candidate = [*selected_edges.values(), edge]
            if not selected_edges and (not fits(candidate)):
                if (
                    _serialized_size(summary_seed, candidate, symbols, subject)
                    > hard_max_chars
                ):
                    continue
            if fits(candidate) or not selected_edges:
                selected_edges[edge.signature] = edge
    omitted_paths = max(0, canonical_path_count - len(selected_paths))
    if enumeration_capped:
        omitted_paths = max(omitted_paths, 1)
    return _ProjectionSelection(
        relationships=tuple(selected_edges.values()),
        total_path_count=canonical_path_count,
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
        relation = str((arguments or {}).get("relation", "")).lower()
        if tool == "query_relations" and relation in {
            "callees",
            "callers",
            "field_readers",
            "field_writers",
            "implementations",
            "overrides",
            "parents",
            "children",
            "type_users",
            "type_references",
            "entrypoints",
        }:
            if relation == "callees" and edge.kind == "CALLS":
                traversal.append((edge, edge.source, edge.target))
            elif relation == "callers" and edge.kind == "CALLS":
                traversal.append((edge, edge.target, edge.source))
            elif relation in {"parents", "type_references"}:
                traversal.append((edge, edge.source, edge.target))
            elif relation in {"children", "type_users", "entrypoints"}:
                traversal.append((edge, edge.target, edge.source))
            elif relation in {"field_readers", "field_writers", "implementations", "overrides"}:
                attached.append(edge)
        else:
            attached.append(edge)
    return (traversal, attached)


def _reachable_traversal_edges(
    traversal: list[tuple[_GraphEdge, str, str]], subject: str
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
    traversal: list[tuple[_GraphEdge, str, str]], *, subject: str, max_depth: int
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
        node: str, path_edges: tuple[_GraphEdge, ...], path_nodes: tuple[str, ...]
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
            signature = tuple((edge.signature for edge in path_edges))
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
    return (paths, capped)


_SEMANTIC_EDGE_KINDS = frozenset(
    {
        "LISTENS_TO_EVENT",
        "SCHEDULED_BY",
        "READS_FIELD",
        "WRITES_FIELD",
        "IMPLEMENTS",
        "OVERRIDES",
        "EXPOSES_ROUTE",
        "EXTENDS",
        "REFERENCES_TYPE",
    }
)
_SEMANTIC_TARGET_GROUPS = (
    ("listener", ("listener", "event")),
    ("callback", ("callback",)),
    ("route", ("route", "interceptor")),
    ("relation", ("implements", "override")),
    ("state", ("state", "context", "synchronization", "retrycount", "cache")),
)


def _path_priority(
    path: _GraphPath,
    attached: list[_GraphEdge],
    *,
    focus: GraphProjectionFocus | None,
    subject: str,
) -> tuple[Any, ...]:
    nodes = set(path.nodes)
    path_edges = (
        *path.edges,
        *[
            edge
            for edge in attached
            if (edge.source in nodes or edge.target in nodes)
            and subject not in {edge.source, edge.target}
        ],
    )
    return (
        0 if _path_hits_changed_fact(path, focus) else 1,
        0 if _hits_non_subject_symbol(path_edges, focus, subject) else 1,
        _path_lifecycle_rank(path),
        _path_semantic_rank(path, attached, subject),
        len(path.edges),
        path.signature,
    )


def _path_lifecycle_rank(path: _GraphPath) -> int:
    """Prefer entry/open lifecycle callbacks when semantic branches compete.

    A bounded graph commonly contains sibling ``open``, ``onSuccess``,
    ``onError`` and ``close`` callbacks.  They are all useful, but an ``open``
    path is the first observer of newly-created state and is therefore the
    shortest proof for registration/initialisation timing changes.  This rank
    only breaks ties after changed-line and symbol focus; it never overrides a
    path explicitly anchored by the task's changed line.
    """
    targets = " ".join((edge.target.lower() for edge in path.edges))
    if "#open(" in targets or "#open" in targets:
        return 0
    if "#onerror" in targets or "#onsuccess" in targets:
        return 1
    if "#close(" in targets or "#close" in targets:
        return 2
    return 3


def _attached_priority(
    edge: _GraphEdge,
    path_nodes: set[str],
    focus: GraphProjectionFocus | None,
    subject: str,
) -> tuple[Any, ...]:
    return (
        0 if edge.source == subject or edge.target == subject else 1,
        0 if edge.source in path_nodes or edge.target in path_nodes else 1,
        0 if _hits_changed_line((edge,), focus) else 1,
        0 if _hits_non_subject_symbol((edge,), focus, subject) else 1,
        0 if edge.kind in _SEMANTIC_EDGE_KINDS else 1,
        edge.signature,
    )


def _hits_changed_line(
    edges: tuple[_GraphEdge, ...] | list[_GraphEdge], focus: GraphProjectionFocus | None
) -> bool:
    if focus is None or not focus.changed_file:
        return False
    lines = set(focus.changed_lines) | set(focus.deletion_anchor_lines)
    if not lines:
        return False
    if any(
        (
            str(edge.payload.get("file", "")) == focus.changed_file
            and _as_int(edge.payload.get("line")) in lines
            for edge in edges
        )
    ):
        return True
    return False


def _path_hits_changed_fact(
    path: _GraphPath, focus: GraphProjectionFocus | None
) -> bool:
    return _hits_changed_line(path.edges, focus)


def _path_family_key(path: _GraphPath) -> tuple[tuple[str, str, str], ...]:
    """Collapse duplicate paths that differ only by call-site metadata."""
    return tuple(((edge.source, edge.target, edge.kind) for edge in path.edges))


def _dedupe_path_families(paths: list[_GraphPath]) -> list[_GraphPath]:
    families: dict[tuple[tuple[str, str, str], ...], _GraphPath] = {}
    for path in paths:
        families.setdefault(_path_family_key(path), path)
    return list(families.values())


def _path_has_semantic_target(
    path: _GraphPath, attached: list[_GraphEdge], subject: str
) -> bool:
    return _path_semantic_rank(path, attached, subject) < 2


def _path_target_family(
    path: _GraphPath, attached: list[_GraphEdge], subject: str
) -> str:
    """Return a stable semantic target class for coverage reservation."""
    targets = [edge.target.lower() for edge in path.edges]
    target_groups: set[str] = set()
    for target in targets:
        group = _semantic_target_group(target)
        if group:
            target_groups.add(group)
    for group in ("listener", "callback", "route", "relation", "state"):
        if group in target_groups:
            branch = path.edges[0].target if path.edges else subject
            return f"{group}:{branch}"
    if any(
        (
            edge.kind in _SEMANTIC_EDGE_KINDS - {"READS_FIELD", "WRITES_FIELD"}
            and (edge.source in path.nodes or edge.target in path.nodes)
            and (subject not in {edge.source, edge.target})
            for edge in attached
        )
    ):
        return "semantic"
    return ""


def _path_semantic_rank(
    path: _GraphPath, attached: list[_GraphEdge], subject: str
) -> int:
    """Rank downstream semantic targets without letting subject metadata win."""
    best = 2
    for edge in path.edges:
        if edge.kind in _SEMANTIC_EDGE_KINDS:
            best = min(best, 0)
            continue
        target_group = _semantic_target_group(edge.target)
        if target_group in {"listener", "callback", "route", "relation"}:
            best = min(best, 0)
        elif target_group == "state":
            best = min(best, 1)
    if any(
        (
            edge.kind in _SEMANTIC_EDGE_KINDS - {"READS_FIELD", "WRITES_FIELD"}
            and (edge.source in path.nodes or edge.target in path.nodes)
            and (subject not in {edge.source, edge.target})
            for edge in attached
        )
    ):
        best = min(best, 0)
    return best


def _semantic_target_group(target: str) -> str | None:
    target_lower = target.lower()
    for group, tokens in _SEMANTIC_TARGET_GROUPS:
        if any((token in target_lower for token in tokens)):
            return group
    return None


def _hits_non_subject_symbol(
    edges: tuple[_GraphEdge, ...] | list[_GraphEdge],
    focus: GraphProjectionFocus | None,
    subject: str,
) -> bool:
    if focus is None:
        return False
    changed = set(focus.changed_symbol_ids) - {subject}
    return any((edge.source in changed or edge.target in changed for edge in edges))


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _max_depth(arguments: Mapping[str, Any] | None) -> int:
    raw = arguments or {}
    value = _as_int(raw.get("depth"))
    if value is None:
        value = _as_int(raw.get("max_depth"))
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
    return len(
        _candidate_summary(
            summary_seed,
            edges,
            selected_symbols,
            omitted_count=0,
            omitted_path_count=0,
            limitations=list(summary_seed.get("limitations") or []),
        )
    )


def _normalize_unresolved_relationships(
    raw_relationships: list[Any],
) -> list[dict[str, Any]]:
    """保留未解析关系的稳定、非敏感投影字段并去重。"""
    normalized: dict[tuple[str, ...], dict[str, Any]] = {}
    for relationship in raw_relationships:
        if not isinstance(relationship, dict):
            continue
        signature = tuple(
            (
                str(relationship.get(key, ""))
                for key in (
                    "sourceId",
                    "targetId",
                    "kind",
                    "file",
                    "line",
                    "source_set",
                    "resolution",
                )
            )
        )
        item = {
            key: relationship.get(key)
            for key in _GRAPH_RELATION_KEYS
            if key in relationship
        }
        if "kind" in item:
            item["kind"] = str(item["kind"]).upper()
        if "resolution" in item:
            item["resolution"] = str(item["resolution"]).upper()
        normalized.setdefault(signature, item)
    return [normalized[key] for key in sorted(normalized)]


def _select_unresolved_relationships(
    summary_seed: Mapping[str, Any],
    unresolved: list[dict[str, Any]],
    edges: list[_GraphEdge],
    symbols: list[dict[str, Any]],
    *,
    max_chars: int = _GRAPH_SUMMARY_MAX_CHARS,
) -> tuple[tuple[dict[str, Any], ...], int]:
    """在不挤掉完整 resolved 路径的前提下保留未解析事实。"""
    selected: list[dict[str, Any]] = []
    for relationship in unresolved:
        candidate_unresolved = [*selected, relationship]
        candidate = _candidate_summary(
            summary_seed,
            edges,
            symbols,
            omitted_count=0,
            omitted_path_count=0,
            limitations=list(summary_seed.get("limitations") or []),
            unresolved_relationships=candidate_unresolved,
            omitted_unresolved_count=max(
                0, len(unresolved) - len(candidate_unresolved)
            ),
        )
        if len(candidate) <= max_chars:
            selected.append(relationship)
    return (tuple(selected), max(0, len(unresolved) - len(selected)))


def _candidate_summary(
    summary_seed: Mapping[str, Any],
    edges: list[_GraphEdge],
    symbols: list[dict[str, Any]],
    *,
    omitted_count: int,
    omitted_path_count: int,
    limitations: list[str],
    unresolved_relationships: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    omitted_unresolved_count: int = 0,
) -> str:
    value = dict(summary_seed)
    value["symbols"] = symbols
    value["relationships"] = [edge.payload for edge in edges]
    value["unresolved_relationships"] = list(unresolved_relationships)
    value["omitted_count"] = omitted_count
    value["omitted_symbol_count"] = 0
    value["omitted_unresolved_count"] = omitted_unresolved_count
    value["omitted_path_count"] = omitted_path_count
    value["limitations"] = limitations
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def validate_graph_payload(
    raw: str, *, tool: str, expected_subject: str = ""
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
            EvidenceValidationStatus.INVALID, ("graph_protocol_mismatch",)
        )
    if _LEGACY_SCOPE_KEYS.intersection(payload):
        return GraphValidation(
            EvidenceValidationStatus.INVALID, ("graph_legacy_scope_fields",)
        )
    raw_limitations = payload.get("limitations")
    if not isinstance(raw_limitations, list) or any(
        (not isinstance(item, str) for item in raw_limitations)
    ):
        return GraphValidation(
            EvidenceValidationStatus.INVALID, ("invalid_graph_limitations",)
        )
    limitations = [item for item in raw_limitations if item]
    actual_subject = str(payload.get("subject_symbol_id", ""))
    if expected_subject and actual_subject != expected_subject:
        return GraphValidation(
            EvidenceValidationStatus.INVALID, ("graph_subject_mismatch",)
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
            EvidenceValidationStatus.INVALID, ("invalid_graph_source_scope",)
        )
    if not isinstance(relationships, list):
        return GraphValidation(
            EvidenceValidationStatus.INVALID, ("invalid_graph_relationships",)
        )
    if not isinstance(symbols, list) or any(
        (
            not isinstance(item, dict)
            or str(item.get("source_set", "")).upper() != source_scope
            for item in symbols
        )
    ):
        return GraphValidation(
            EvidenceValidationStatus.INVALID, ("graph_symbol_scope_mismatch",)
        )
    if any(
        (
            not isinstance(item, dict)
            or str(item.get("source_set", "")).upper() != source_scope
            or str(item.get("resolution", "")).upper() != "RESOLVED"
            for item in relationships
        )
    ):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_source_scope_or_resolution_mismatch",),
        )
    if not _valid_source_excerpts(payload):
        return GraphValidation(
            EvidenceValidationStatus.INVALID, ("invalid_graph_source_excerpt",)
        )
    if (
        not isinstance(unresolved_relationships, list)
        or not isinstance(unresolved_count, int)
        or isinstance(unresolved_count, bool)
        or (unresolved_count < len(unresolved_relationships))
        or any(
            (
                not isinstance(item, dict)
                or str(item.get("source_set", "")).upper() != source_scope
                or str(item.get("resolution", "")).upper() == "RESOLVED"
                for item in unresolved_relationships
            )
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
            EvidenceValidationStatus.INVALID, ("invalid_graph_outcome_coverage",)
        )
    if coverage == "complete" and (unresolved_count or unresolved_relationships):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_complete_with_unresolved_relationships",),
        )
    subject_fact = False
    if outcome == "found" and (not relationships) and (not subject_fact):
        return GraphValidation(
            EvidenceValidationStatus.INVALID, ("graph_found_without_fact",)
        )
    if outcome in {"not_found", "indeterminate"} and relationships:
        return GraphValidation(
            EvidenceValidationStatus.INVALID, ("graph_non_found_with_relationships",)
        )
    if outcome == "indeterminate":
        return GraphValidation(
            EvidenceValidationStatus.UNAVAILABLE,
            tuple(dict.fromkeys(["graph_indeterminate", *limitations])),
        )
    if coverage == "partial":
        limitations.append("graph_coverage_partial")
        return GraphValidation(
            EvidenceValidationStatus.LIMITED, tuple(dict.fromkeys(limitations))
        )
    return GraphValidation(
        EvidenceValidationStatus.VALID, tuple(dict.fromkeys(limitations))
    )


__all__ = [
    "GraphProjectionFocus",
    "GraphValidation",
    "summarize_graph",
    "validate_graph_payload",
]
