"""把已验证证据转换成面向用户的根因和来源位置。

Evidence Ledger 的 Txx/Cxx/Fxxx 只属于内部协议。最终 Issue 需要能让人直接看懂
跨文件问题的来源，因此这里从已验证 Artifact 的真实元数据生成短摘要，不调用 LLM，
也不接受模型自由填写文件或行号。
"""

from __future__ import annotations
import json
import re
from collections.abc import Mapping
from typing import Any, Literal
from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.evidence import (
    CandidateVerification,
    EvidenceArtifact,
    EvidenceSourceKind,
    EvidenceValidationStatus,
)
from codeguard_agent.models.schemas import EvidenceLocation
from codeguard_agent.models.tasks import TaskSymbolContext

_GRAPH_TOOLS = frozenset({"query_relations"})
_INTERNAL_EVIDENCE_RE = re.compile(
    "(?:\\[\\s*证据编号\\s*(?:P|C|T|F)\\d+\\s*\\]|\\b(?:P|C|T|F)\\d{2,3}\\b)",
    flags=re.IGNORECASE,
)
_HEADER_RE = {
    "symbol": re.compile("(?m)^\\s*symbol_id:\\s*(\\S+)\\s*$"),
    "file": re.compile("(?m)^\\s*file:\\s*(.+?)\\s*$"),
    "lines": re.compile("(?m)^\\s*lines:\\s*(\\d+)\\s*-\\s*(\\d+)\\s*$"),
}


def scrub_user_text(value: str) -> str:
    """移除可能从 LLM 或工具回显中泄漏的内部账本编号。"""
    cleaned = _INTERNAL_EVIDENCE_RE.sub("", str(value or ""))
    cleaned = re.sub("[ \\t]{2,}", " ", cleaned)
    cleaned = re.sub("\\s+([，。；：])", "\\1", cleaned)
    return cleaned.strip()


def enrich_candidate_for_issue(
    candidate: CandidateIssue,
    *,
    symbol_context: TaskSymbolContext | None = None,
    verification: CandidateVerification | None = None,
    artifacts: Mapping[str, EvidenceArtifact] | None = None,
) -> CandidateIssue:
    """为最终 Issue 绑定确定性的根因摘要和来源位置。"""
    locations = _collect_locations(
        candidate,
        symbol_context=symbol_context,
        verification=verification,
        artifacts=artifacts or {},
    )
    root_cause = (
        _root_cause_text(candidate, locations)
        if verification is not None and verification.valid_evidence
        else ""
    )
    return candidate.model_copy(
        update={
            "root_cause": root_cause,
            "evidence_locations": locations,
            "claim": scrub_user_text(candidate.claim),
            "suggestion": scrub_user_text(candidate.suggestion),
            "evidence_observation": scrub_user_text(candidate.evidence_observation),
        }
    )


def _collect_locations(
    candidate: CandidateIssue,
    *,
    symbol_context: TaskSymbolContext | None,
    verification: CandidateVerification | None,
    artifacts: Mapping[str, EvidenceArtifact],
) -> list[EvidenceLocation]:
    locations: list[EvidenceLocation] = []
    changed_symbol_ids = {
        symbol.symbol_id
        for symbol in (symbol_context.symbols if symbol_context is not None else ())
    }
    changed = _changed_location(candidate, symbol_context=symbol_context)
    if changed is not None:
        locations.append(changed)
    if verification is None:
        return _dedupe_locations(locations)
    for evidence in verification.valid_evidence:
        if evidence.validation_status not in {
            EvidenceValidationStatus.VALID,
            EvidenceValidationStatus.LIMITED,
        }:
            continue
        artifact = artifacts.get(evidence.artifact_id)
        if artifact is None:
            continue
        if artifact.source_kind is EvidenceSourceKind.TASK_PATCH:
            continue
        if artifact.source_kind is EvidenceSourceKind.SYMBOL_CONTEXT:
            location = _symbol_context_location(artifact.payload)
            if location is not None and location.symbol:
                locations.append(location.model_copy(update={"kind": "changed_code"}))
            continue
        if artifact.tool in {"read_symbol", "read_symbol"}:
            location = _source_tool_location(artifact.payload, artifact.arguments)
            if location is not None:
                kind = (
                    "changed_code"
                    if location.symbol in changed_symbol_ids
                    or (
                        _same_file(location.file, candidate.file)
                        and _symbol_is_changed(location.symbol, changed_symbol_ids)
                    )
                    else "root_cause"
                )
                locations.append(location.model_copy(update={"kind": kind}))
            continue
        if artifact.tool in _GRAPH_TOOLS:
            locations.extend(
                _graph_locations(
                    artifact.payload,
                    artifact.arguments,
                    candidate=candidate,
                    changed_symbol_ids=changed_symbol_ids,
                )
            )
    return _dedupe_locations(locations)


def _changed_location(
    candidate: CandidateIssue, *, symbol_context: TaskSymbolContext | None
) -> EvidenceLocation | None:
    if not candidate.file or candidate.line <= 0:
        return None
    symbol = ""
    if symbol_context is not None:
        for item in symbol_context.symbols:
            if (
                _same_file(item.file, candidate.file)
                and item.start_line <= candidate.line <= item.end_line
            ):
                symbol = _display_symbol(item.symbol_id, item.signature)
                break
    return EvidenceLocation(
        file=candidate.file.replace("\\", "/"),
        symbol=symbol,
        start_line=candidate.line,
        end_line=candidate.line,
        kind="changed_code",
    )


def _symbol_context_location(payload: str) -> EvidenceLocation | None:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return _location_from_mapping(value, kind="root_cause")


def _source_tool_location(
    payload: str, arguments: Mapping[str, Any]
) -> EvidenceLocation | None:
    symbol_match = _HEADER_RE["symbol"].search(payload)
    file_match = _HEADER_RE["file"].search(payload)
    lines_match = _HEADER_RE["lines"].search(payload)
    symbol_id = (
        symbol_match.group(1).strip()
        if symbol_match
        else str(arguments.get("symbol_id", ""))
    )
    file = file_match.group(1).strip() if file_match else ""
    if not file:
        return None
    start = int(lines_match.group(1)) if lines_match else 0
    end = int(lines_match.group(2)) if lines_match else start
    return EvidenceLocation(
        file=file.replace("\\", "/"),
        symbol=_display_symbol(symbol_id),
        start_line=start,
        end_line=end,
        kind="root_cause",
    )


def _graph_locations(
    payload: str,
    arguments: Mapping[str, Any],
    *,
    candidate: CandidateIssue,
    changed_symbol_ids: set[str],
) -> list[EvidenceLocation]:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(value, dict):
        return []
    symbols: dict[str, EvidenceLocation] = {}
    for item in value.get("symbols") or ():
        if not isinstance(item, Mapping):
            continue
        location = _location_from_mapping(item, kind="related_path")
        symbol_id = str(item.get("id") or item.get("symbol_id") or "").strip()
        if location is not None and symbol_id:
            symbols[symbol_id] = location
    subject = str(
        arguments.get("symbol_id") or value.get("subject_symbol_id") or ""
    ).strip()
    result: list[EvidenceLocation] = []
    for relation in value.get("relationships") or ():
        if not isinstance(relation, Mapping):
            continue
        source_id = str(relation.get("sourceId", "")).strip()
        target_id = str(relation.get("targetId", "")).strip()
        source = symbols.get(source_id)
        target = symbols.get(target_id)
        if source is None or target is None:
            continue
        source_display = source.symbol or _display_symbol(source_id)
        target_display = target.symbol or _display_symbol(target_id)
        relation_text = f"{source_display} → {target_display}"
        if subject and source_id == subject:
            selected = target
            selected_id = target_id
        elif subject and target_id == subject:
            selected = source
            selected_id = source_id
        else:
            selected = target
            selected_id = target_id
        if selected_id in changed_symbol_ids:
            continue
        result.append(
            selected.model_copy(
                update={"kind": "related_path", "relation": relation_text}
            )
        )
    return result


def _location_from_mapping(
    value: Mapping[str, Any],
    *,
    kind: Literal["changed_code", "root_cause", "related_path"],
) -> EvidenceLocation | None:
    file = str(value.get("file") or "").strip()
    symbol_id = str(value.get("id") or value.get("symbol_id") or "").strip()
    if not file or not symbol_id:
        return None
    start = _int_value(
        value.get("startLine", value.get("start_line", value.get("line", 0)))
    )
    end = _int_value(
        value.get("endLine", value.get("end_line", value.get("line", start)))
    )
    return EvidenceLocation(
        file=file.replace("\\", "/"),
        symbol=_display_symbol(symbol_id, str(value.get("signature") or "")),
        start_line=start,
        end_line=end,
        kind=kind,
    )


def _display_symbol(symbol_id: str, signature: str = "") -> str:
    if signature.strip():
        return scrub_user_text(signature.strip())
    value = str(symbol_id or "").strip()
    if ":" in value:
        value = value.split(":", 1)[1]
    return value


def _symbol_is_changed(symbol: str, changed_symbol_ids: set[str]) -> bool:
    return any(
        (
            symbol == _display_symbol(item) or symbol == item
            for item in changed_symbol_ids
        )
    )


def _same_file(left: str, right: str) -> bool:
    return (
        left.replace("\\", "/").strip().lower()
        == right.replace("\\", "/").strip().lower()
    )


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _dedupe_locations(locations: list[EvidenceLocation]) -> list[EvidenceLocation]:
    order = {"root_cause": 0, "related_path": 1, "changed_code": 2}
    unique: dict[tuple[Any, ...], EvidenceLocation] = {}
    for item in locations:
        if not item.file:
            continue
        key = (
            item.file.lower(),
            item.symbol,
            item.start_line,
            item.end_line,
            item.kind,
            item.relation,
        )
        unique.setdefault(key, item)
    return sorted(
        unique.values(),
        key=lambda item: (
            order.get(item.kind, 9),
            item.file.lower(),
            item.start_line,
            item.symbol,
            item.relation,
        ),
    )[:4]


def _format_location(location: EvidenceLocation) -> str:
    position = location.file
    if location.start_line > 0:
        position += f":{location.start_line}"
        if location.end_line > location.start_line:
            position += f"-{location.end_line}"
    if location.symbol:
        position += f" · `{location.symbol}`"
    return position


def format_evidence_location(location: EvidenceLocation) -> str:
    """统一渲染最终报告中的来源位置,供 CLI/Markdown/评测共同使用。"""
    value = _format_location(location)
    if location.relation:
        value += f"（{location.relation}）"
    return value


def _root_cause_text(
    candidate: CandidateIssue, locations: list[EvidenceLocation]
) -> str:
    mechanism = scrub_user_text(
        candidate.evidence_observation or candidate.mechanism or candidate.claim
    )
    sources = [
        item for item in locations if item.kind in {"root_cause", "related_path"}
    ]
    if not sources:
        return mechanism
    source_text = "；".join((format_evidence_location(item) for item in sources[:2]))
    if mechanism:
        return f"{mechanism}（来源：{source_text}）"
    return f"相关源码位于 {source_text}。"


__all__ = ["enrich_candidate_for_issue", "format_evidence_location", "scrub_user_text"]
