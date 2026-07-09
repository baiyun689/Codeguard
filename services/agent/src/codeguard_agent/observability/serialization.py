"""把追踪期间的运行时对象无损转换为 JSON 可表示的数据。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

_MAX_DEPTH = 40


def serialize_trace_value(value: Any) -> Any:
    """递归序列化 Pydantic、dataclass、消息对象和普通容器。"""
    return _serialize(value, seen=set(), depth=0)


def _serialize(value: Any, *, seen: set[int], depth: int) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth > _MAX_DEPTH:
        return {
            "__trace_error__": "max_depth",
            "__type__": type(value).__name__,
        }
    if isinstance(value, Enum):
        return _serialize(value.value, seen=seen, depth=depth + 1)

    track_identity = (
        isinstance(value, (Mapping, list, tuple, set, frozenset))
        or is_dataclass(value)
        or hasattr(value, "model_dump")
        or hasattr(value, "__dict__")
    )
    value_id = id(value)
    if track_identity and value_id in seen:
        return {
            "__trace_error__": "cycle",
            "__type__": type(value).__name__,
        }
    if track_identity:
        seen.add(value_id)

    try:
        if hasattr(value, "model_dump"):
            dumped = value.model_dump(mode="python")
            return _serialize(dumped, seen=seen, depth=depth + 1)
        if is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: _serialize(
                    getattr(value, field.name),
                    seen=seen,
                    depth=depth + 1,
                )
                for field in fields(value)
            }
        if isinstance(value, Mapping):
            return {
                str(key): _serialize(item, seen=seen, depth=depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [
                _serialize(item, seen=seen, depth=depth + 1)
                for item in value
            ]
        if hasattr(value, "__dict__"):
            public = {
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_")
            }
            if public:
                return {
                    "__type__": type(value).__name__,
                    **{
                        key: _serialize(
                            item,
                            seen=seen,
                            depth=depth + 1,
                        )
                        for key, item in public.items()
                    },
                }
        return {
            "__type__": type(value).__name__,
            "__repr__": repr(value),
        }
    except Exception as exc:  # noqa: BLE001 - 单字段失败不能丢掉整个 trace
        return {
            "__trace_error__": str(exc),
            "__type__": type(value).__name__,
            "__repr__": _safe_repr(value),
        }
    finally:
        if track_identity:
            seen.discard(value_id)


def serialize_messages(
    value: Any,
    *,
    max_content_length: int = 0,
) -> list[dict[str, Any]]:
    """序列化单批或嵌套批次的 LangChain 输入消息。"""
    raw = value.get("messages", []) if isinstance(value, Mapping) else value
    messages: list[Any] = []
    _flatten_messages(raw, messages)

    result: list[dict[str, Any]] = []
    for message in messages:
        if _is_message_tuple(message):
            entry: dict[str, Any] = {
                "role": str(message[0]),
            }
            _put_content(entry, message[1], max_content_length)
            if len(message) > 2:
                entry["extra"] = serialize_trace_value(list(message[2:]))
            result.append(entry)
            continue

        role = getattr(message, "type", None) or getattr(message, "role", None)
        entry = {
            "role": str(role or type(message).__name__),
        }
        _put_content(
            entry,
            getattr(message, "content", ""),
            max_content_length,
        )
        for name in (
            "tool_calls",
            "invalid_tool_calls",
            "additional_kwargs",
            "response_metadata",
            "usage_metadata",
            "name",
            "id",
            "tool_call_id",
        ):
            if hasattr(message, name):
                attribute = getattr(message, name)
                if attribute not in (None, "", [], {}):
                    entry[name] = serialize_trace_value(attribute)
        result.append(entry)
    return result


def serialize_llm_response(
    value: Any,
    *,
    max_content_length: int = 0,
) -> dict[str, Any]:
    """序列化完整 LLM 响应，包括 content 为空时的工具调用决策。"""
    serialized = serialize_trace_value(value)
    result = dict(serialized) if isinstance(serialized, dict) else {
        "value": serialized
    }
    result.setdefault("type", getattr(value, "type", type(value).__name__))

    if hasattr(value, "content"):
        _put_content(
            result,
            getattr(value, "content"),
            max_content_length,
        )
    for name in (
        "tool_calls",
        "invalid_tool_calls",
        "additional_kwargs",
        "response_metadata",
        "usage_metadata",
        "name",
        "id",
    ):
        if hasattr(value, name):
            result[name] = serialize_trace_value(getattr(value, name))
    return result


def _flatten_messages(value: Any, target: list[Any]) -> None:
    if value is None:
        return
    if _is_message_tuple(value) or hasattr(value, "content"):
        target.append(value)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _flatten_messages(item, target)


def _is_message_tuple(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[0], str)
    )


def _put_content(
    target: dict[str, Any],
    content: Any,
    max_content_length: int,
) -> None:
    if (
        isinstance(content, str)
        and max_content_length > 0
        and len(content) > max_content_length
    ):
        target["content"] = content[:max_content_length]
        target["content_truncated"] = True
        target["content_original_length"] = len(content)
        return
    target["content"] = serialize_trace_value(content)


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:  # noqa: BLE001
        return f"<{type(value).__name__} repr failed>"
