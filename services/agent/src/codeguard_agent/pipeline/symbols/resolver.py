"""把 Full ReviewTask 的变更位置批量解析为稳定项目符号。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import ValidationError

from codeguard_agent.models.tasks import (
    ResolvedReference,
    ResolvedSymbol,
    ReviewTask,
    SymbolResolutionStatus,
    TaskSymbolContext,
)

logger = logging.getLogger("codeguard")

_LEGAL_OUTCOMES = {
    ("found", "complete"),
    ("found", "partial"),
    ("not_found", "complete"),
    ("indeterminate", "partial"),
}
_SOURCE_SETS = {"MAIN", "TEST", "GENERATED"}
_MAX_REFERENCES_PER_TASK = 32


@dataclass(frozen=True)
class SymbolResolutionBatch:
    """一次 Gateway 批量解析的 task 级结果。"""

    contexts: dict[str, TaskSymbolContext]
    diagnostics: dict[str, str]


def _normalize_path(path: str) -> str:
    return (path or "").replace("\\", "/").lower()


def _context(
    task: ReviewTask,
    status: SymbolResolutionStatus,
    *,
    symbols: Sequence[ResolvedSymbol] = (),
    references: Sequence[ResolvedReference] = (),
    limitations: Sequence[str] = (),
    truncated: bool = False,
) -> TaskSymbolContext:
    return TaskSymbolContext(
        task_id=task.id,
        symbols=tuple(symbols),
        references=tuple(references),
        status=status,
        limitations=tuple(dict.fromkeys(str(item) for item in limitations if str(item))),
        truncated=truncated,
    )


def _all_with_status(
    tasks: Sequence[ReviewTask],
    status: SymbolResolutionStatus,
    reason: str,
) -> SymbolResolutionBatch:
    return SymbolResolutionBatch(
        contexts={
            task.id: _context(task, status, limitations=(reason,)) for task in tasks
        },
        diagnostics={"symbol_resolution": reason},
    )


def _parse_symbol(item: Any) -> ResolvedSymbol:
    if not isinstance(item, dict) or item.get("resolution") != "resolved":
        raise ValueError("context_not_resolved")
    symbol = ResolvedSymbol.model_validate(item)
    if symbol.end_line < symbol.start_line:
        raise ValueError("invalid_symbol_range")
    return symbol


def _parse_references(item: Any) -> tuple[ResolvedReference, ...]:
    """Parse only concrete changed-line references emitted by Gateway."""
    if not isinstance(item, dict):
        return ()
    raw = item.get("references", [])
    if not isinstance(raw, list):
        return ()
    parsed: list[ResolvedReference] = []
    for reference in raw:
        try:
            parsed.append(ResolvedReference.model_validate(reference))
        except ValidationError:
            # A malformed optional reference must not invalidate the enclosing
            # symbol; it simply cannot be used as a navigation root.
            continue
    return tuple(parsed)


def _limit_symbols(
    symbols: Sequence[ResolvedSymbol], max_chars: int | None
) -> tuple[tuple[ResolvedSymbol, ...], bool]:
    """只在完整对象之间截断，绝不裁切单个 JSON 或 symbol_id。"""
    if max_chars is None:
        return tuple(symbols), False
    kept: list[ResolvedSymbol] = []
    used = 0
    for symbol in symbols:
        candidate = symbol
        size = len(candidate.model_dump_json())
        compacted = False
        if not kept and size > max_chars and symbol.control_flow:
            candidate = symbol.model_copy(update={"control_flow": ()})
            size = len(candidate.model_dump_json())
            compacted = True
        if kept and used + size > max_chars:
            return tuple(kept), True
        kept.append(candidate)
        used += size
        if compacted:
            return tuple(kept), True
    return tuple(kept), False


def _limit_references(
    references: Sequence[ResolvedReference],
    max_count: int = _MAX_REFERENCES_PER_TASK,
) -> tuple[tuple[ResolvedReference, ...], bool]:
    """Bound navigation metadata without dropping the enclosing symbols."""

    # Keep the first occurrence of a concrete edge.  Gateway already emits a
    # stable order; this also protects compatibility responses that repeat an
    # edge for several AST nodes on the same changed line.
    unique: list[ResolvedReference] = []
    seen: set[tuple[str, str, str, int]] = set()
    for reference in references:
        key = (
            reference.symbol_id,
            reference.relation,
            reference.file,
            reference.line,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(reference)
    return tuple(unique[:max_count]), len(unique) > max_count


def resolve_task_symbols(
    tasks: Sequence[ReviewTask],
    *,
    tool_client=None,
    max_chars_per_task: int | None = 4000,
) -> SymbolResolutionBatch:
    """批量解析 task 的当前 revision 变更入口，并返回 task-scoped 强类型结果。

    ``changed_lines`` 仍只表示新增行；纯删除片段通过 task.deletion_anchors
    提供当前版本中可解析的邻近行。
    """
    ordered_tasks = list(tasks)
    if not ordered_tasks:
        return SymbolResolutionBatch(contexts={}, diagnostics={})
    if tool_client is None:
        return _all_with_status(
            ordered_tasks,
            SymbolResolutionStatus.UNAVAILABLE,
            "tool_server_not_configured",
        )

    changes = [
        {"file": task.file, "lines": task.resolution_lines} for task in ordered_tasks
    ]
    response = tool_client.resolve_change_context(changes)
    if not getattr(response, "success", False):
        reason = str(getattr(response, "error", "tool_failed") or "tool_failed")
        return _all_with_status(
            ordered_tasks, SymbolResolutionStatus.UNAVAILABLE, reason
        )

    try:
        content = (
            response.as_tool_output()
            if hasattr(response, "as_tool_output")
            else str(response)
        )
        payload = json.loads(content)
        if not isinstance(payload, dict) or payload.get("schema_version") != 2:
            raise ValueError("graph_protocol_mismatch")
        outcome = str(payload.get("outcome", ""))
        coverage = str(payload.get("coverage", ""))
        if (outcome, coverage) not in _LEGAL_OUTCOMES:
            raise ValueError(f"invalid_outcome_coverage:{outcome}:{coverage}")
        source_scope = str(payload.get("source_scope", ""))
        source_scopes = payload.get("source_scopes", [])
        if source_scope not in _SOURCE_SETS:
            raise ValueError("invalid_source_scope")
        if (
            not isinstance(source_scopes, list)
            or not source_scopes
            or any(str(item) not in _SOURCE_SETS for item in source_scopes)
        ):
            raise ValueError("invalid_source_scopes")
        raw_limitations = payload.get("limitations", [])
        if not isinstance(raw_limitations, list):
            raise ValueError("invalid_limitations")
        limitations = tuple(str(item) for item in raw_limitations if str(item))
        raw_contexts = payload.get("contexts", [])
        if not isinstance(raw_contexts, list):
            raise ValueError("invalid_contexts")
        parsed_entries = tuple(
            (_parse_symbol(item), _parse_references(item)) for item in raw_contexts
        )
        parsed_symbols = tuple(symbol for symbol, _ in parsed_entries)
        references_by_symbol: dict[str, tuple[ResolvedReference, ...]] = {
            symbol.symbol_id: references
            for symbol, references in parsed_entries
            if references
        }
        symbols = tuple(
            {
                (_normalize_path(symbol.file), symbol.symbol_id): symbol
                for symbol in parsed_symbols
            }.values()
        )
        if any(symbol.source_set not in source_scopes for symbol in symbols):
            raise ValueError("symbol_source_set_out_of_scope")
        if outcome == "found" and not symbols:
            raise ValueError("found_without_contexts")
        if outcome != "found" and symbols:
            raise ValueError("contexts_without_found")
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        return _all_with_status(
            ordered_tasks,
            SymbolResolutionStatus.INVALID,
            f"invalid_graph_response:{exc}",
        )

    by_task: dict[str, TaskSymbolContext] = {}
    for task in ordered_tasks:
        task_lines = set(task.resolution_lines)
        matches = [
            symbol
            for symbol in symbols
            if _normalize_path(symbol.file) == _normalize_path(task.file)
            and any(symbol.start_line <= line <= symbol.end_line for line in task_lines)
        ]
        limited, truncated = _limit_symbols(matches, max_chars_per_task)
        task_references = tuple(
            reference
            for symbol in limited
            for reference in references_by_symbol.get(symbol.symbol_id, ())
        )
        task_references, references_truncated = _limit_references(task_references)
        task_limitations = list(limitations)
        if truncated:
            task_limitations.append("symbol_context_truncated")
        if references_truncated:
            task_limitations.append("reference_context_truncated")
        if limited:
            status = SymbolResolutionStatus.RESOLVED
        elif coverage == "complete":
            status = SymbolResolutionStatus.NOT_FOUND
        else:
            status = SymbolResolutionStatus.UNAVAILABLE
        by_task[task.id] = _context(
            task,
            status,
            symbols=limited,
            references=task_references,
            limitations=task_limitations,
            truncated=truncated or references_truncated,
        )

    logger.info(
        "[symbol_resolution] tasks=%d resolved=%d symbols=%d",
        len(ordered_tasks),
        sum(item.status is SymbolResolutionStatus.RESOLVED for item in by_task.values()),
        sum(len(item.symbols) for item in by_task.values()),
    )
    diagnostics = (
        {"symbol_resolution": "; ".join(limitations)} if limitations else {}
    )
    return SymbolResolutionBatch(contexts=by_task, diagnostics=diagnostics)


__all__ = ["SymbolResolutionBatch", "resolve_task_symbols"]
