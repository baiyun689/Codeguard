"""inspect_* 图响应的确定性处理:结构化压缩 + 完整性护栏。

压缩与护栏自旧 verifier 迁移(Evidence Ledger 切换后保留,源文档 §7.3):
- 14KB 图 JSON 不能全文进 LLM 载荷,确定性结构化压缩保留
  schema/outcome/coverage/scope/subject/relationships/limitations;
- subject/source_scope/outcome 护栏是图工具调用正确性的关键检查
  (历史教训:该校验缺失导致过整档评测作废)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from codeguard_agent.models.evidence import EvidenceValidationStatus

_GRAPH_SUMMARY_MAX_CHARS = 8000
_GRAPH_HEADER_KEYS = (
    "schema_version", "outcome", "coverage", "source_scope",
    "subject_symbol_id", "unresolved_count", "limitations",
)
_GRAPH_SYMBOL_KEYS = ("id", "kind", "file", "startLine", "endLine")
_GRAPH_RELATION_KEYS = (
    "sourceId", "targetId", "kind", "file", "line", "source_set", "resolution",
)
_GRAPH_FALLBACK_KEYS = (
    "main_relationships", "test_relationships", "generated_relationships",
)

_VALID_SOURCE_SCOPES = {"MAIN", "TEST", "GENERATED"}
_GRAPH_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class GraphValidation:
    status: EvidenceValidationStatus
    limitations: tuple[str, ...] = ()
    replayable: bool = False


def summarize_graph(raw: str) -> str:
    """对 inspect_* 图响应做确定性结构化压缩(零 LLM)。

    主 relationships 非空时丢弃四个 fallback 数组(体积减半以上);
    主数组为空(not_found + test 边透传场景)时补 fallback 数组,
    裁决层才能判断不足而不是瞎判。非 JSON 按上限截断兜底。
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
    symbols = [
        {key: symbol.get(key) for key in _GRAPH_SYMBOL_KEYS if key in symbol}
        for symbol in payload.get("symbols") or []
        if isinstance(symbol, dict)
    ]
    relations = [
        {key: rel.get(key) for key in _GRAPH_RELATION_KEYS if key in rel}
        for rel in payload.get("relationships") or []
        if isinstance(rel, dict)
    ]
    if not relations:
        for key in _GRAPH_FALLBACK_KEYS:
            extra = [
                {k: rel.get(k) for k in _GRAPH_RELATION_KEYS if k in rel}
                for rel in payload.get(key) or []
                if isinstance(rel, dict)
            ]
            if extra:
                summary[key] = extra
    summary["symbols"] = symbols
    summary["relationships"] = relations
    return _fit_graph_summary(json.dumps(summary, ensure_ascii=False))


def _fit_graph_summary(text: str, *, max_chars: int = _GRAPH_SUMMARY_MAX_CHARS) -> str:
    """长度阶梯(信息牺牲从小到大):删 symbols → 边截 60/30/10 → 硬截断。

    删符号先于截边:调用关系是核心,符号先让位;每级截断后
    都是合法 JSON(硬截断是极端图的最后防线,现实中边截 10 已足够)。
    """
    if len(text) <= max_chars:
        return text
    payload: Any = json.loads(text)
    if isinstance(payload, dict) and payload.get("symbols"):
        del payload["symbols"]
        text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    if isinstance(payload, dict):
        for limit in (60, 30, 10):
            relations = payload.get("relationships") or []
            if len(relations) > limit:
                payload["relationships"] = relations[:limit]
                text = json.dumps(payload, ensure_ascii=False)
            if len(text) <= max_chars:
                return text
    return text[:max_chars]


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
    test_relationships = payload.get("test_relationships")
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
    if isinstance(test_relationships, list) and any(
        str(item.get("source_set", "")).upper() != "TEST"
        for item in test_relationships
        if isinstance(item, dict)
    ):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_source_scope_mismatch",),
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
    if (
        tool in {"inspect_change_impact", "inspect_security_path"}
        and source_scope in {"MAIN", "GENERATED"}
        and outcome == "found"
        and not relationships
        and isinstance(test_relationships, list)
        and bool(test_relationships)
    ):
        return GraphValidation(
            EvidenceValidationStatus.INVALID,
            ("graph_test_only_confirmation",),
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


__all__ = ["GraphValidation", "summarize_graph", "validate_graph_payload"]
