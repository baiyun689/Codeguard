"""inspect_* 图响应的确定性处理:结构化压缩 + 完整性护栏。

压缩与护栏自旧 verifier 迁移(Evidence Ledger 切换后保留,源文档 §7.3):
- 14KB 图 JSON 不能全文进 LLM 载荷,确定性结构化压缩保留
  status/coverage/scope/subject/relationships/limitations;
- subject/source_scope/status 护栏是图工具调用正确性的关键检查
  (历史教训:该校验缺失导致过整档评测作废)。
"""

from __future__ import annotations

import json
from typing import Any

_GRAPH_SUMMARY_MAX_CHARS = 8000
_GRAPH_HEADER_KEYS = (
    "status", "coverage", "source_scope", "subject_symbol_id", "limitations",
)
_GRAPH_SYMBOL_KEYS = ("id", "kind", "file", "startLine", "endLine")
_GRAPH_RELATION_KEYS = ("sourceId", "targetId", "kind", "file", "line")
_GRAPH_FALLBACK_KEYS = (
    "main_relationships", "test_relationships", "generated_relationships",
)

_VALID_SOURCE_SCOPES = {"MAIN", "TEST", "GENERATED"}


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
) -> tuple[str, list[str]]:
    """图响应完整性护栏。返回 (健康状态, 限制声明列表)。

    健康状态:
    - "valid":   响应完整可用
    - "limited": coverage=partial 等边界情况,保留正事实但标注限制
    - "invalid": subject/scope/status 违约,不得作为支持证据
    - "replay":  响应无法解析或 status=unknown,进入异常重放队列
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "replay", ["graph_payload_unparseable"]
    if not isinstance(payload, dict):
        return "replay", ["graph_payload_unparseable"]

    limitations: list[str] = []
    actual_subject = str(payload.get("subject_symbol_id", ""))
    if expected_subject and actual_subject and actual_subject != expected_subject:
        return "invalid", ["graph_subject_mismatch"]
    status = payload.get("status")
    coverage = payload.get("coverage")
    source_scope = str(payload.get("source_scope", "")).upper()
    relationships = payload.get("relationships")
    test_relationships = payload.get("test_relationships")
    if source_scope:
        if source_scope not in _VALID_SOURCE_SCOPES:
            return "invalid", ["invalid_graph_source_scope"]
        if isinstance(relationships, list) and any(
            str(item.get("source_set", "")).upper() not in {"", source_scope}
            for item in relationships
            if isinstance(item, dict)
        ):
            return "invalid", ["graph_source_scope_mismatch"]
        if isinstance(test_relationships, list) and any(
            str(item.get("source_set", "")).upper() not in {"", "TEST"}
            for item in test_relationships
            if isinstance(item, dict)
        ):
            return "invalid", ["graph_source_scope_mismatch"]
        if (
            tool in {"inspect_change_impact", "inspect_security_path"}
            and source_scope in {"MAIN", "GENERATED"}
            and status == "confirmed"
            and isinstance(relationships, list)
            and not relationships
            and isinstance(test_relationships, list)
            and bool(test_relationships)
        ):
            return "invalid", ["graph_test_only_confirmation"]
    if status == "unknown":
        return "replay", ["graph_unknown"]
    if status not in {"confirmed", "not_found"}:
        return "invalid", ["invalid_graph_status"]
    # coverage=partial 只表示图数据可能不全,不整体废弃——confirmed 的调用方/
    # 入口事实仍应保留,数据边界由 limitations 供裁决层自行判断。
    if coverage == "partial":
        limitations.append("graph_coverage_partial")
    return "limited" if limitations else "valid", limitations


__all__ = ["summarize_graph", "validate_graph_payload"]
