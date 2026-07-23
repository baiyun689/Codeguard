from __future__ import annotations

import json
import posixpath
from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from typing import Any

from codeguard_agent.tools.tool_client import ToolResponse

DISCOVERY_GATEWAY_TOOLS = frozenset({
    "get_file_content",
    "find_sensitive_apis",
    "find_callers",
    "get_code_metrics",
})
REPEATED_TOOL_RESULT = (
    "该工具和参数已经在当前对话中成功返回；请复用前述结果，不要重复读取。"
)
ToolKey = tuple[str, str]


def _normalize_path(value: str) -> str:
    normalized = posixpath.normpath(value.replace("\\", "/"))
    return "." if normalized == "" else normalized


def _canonical_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(arguments)
    file_path = normalized.get("file_path")
    if isinstance(file_path, str):
        normalized["file_path"] = _normalize_path(file_path)
    query = normalized.get("query")
    if isinstance(query, str) and "#" in query:
        path, method = query.split("#", 1)
        normalized["query"] = f"{_normalize_path(path)}#{method}"
    return normalized


def canonical_tool_key(tool_name: str, arguments: dict[str, Any]) -> ToolKey:
    payload = json.dumps(
        _canonical_arguments(arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return tool_name, payload


def _cacheable(response: ToolResponse) -> bool:
    return response.success and bool((response.result or "").strip())


class DiscoveryToolCoordinator:
    def __init__(self) -> None:
        self._lock = Lock()
        self._completed: dict[ToolKey, ToolResponse] = {}
        self._in_flight: dict[ToolKey, Future[ToolResponse]] = {}

    def execute(
        self,
        key: ToolKey,
        call: Callable[[], ToolResponse],
    ) -> ToolResponse:
        with self._lock:
            cached = self._completed.get(key)
            if cached is not None:
                return cached
            future = self._in_flight.get(key)
            leader = future is None
            if future is None:
                future = Future()
                self._in_flight[key] = future

        if not leader:
            return future.result()

        try:
            try:
                response = call()
            except Exception as exc:  # noqa: BLE001
                response = ToolResponse(success=False, error=str(exc))
            with self._lock:
                if _cacheable(response):
                    self._completed[key] = response
                self._in_flight.pop(key, None)
            future.set_result(response)
            return response
        except BaseException as exc:
            with self._lock:
                self._in_flight.pop(key, None)
            future.set_exception(exc)
            raise


class CoordinatedDiscoveryToolClient:
    def __init__(self, delegate: Any, coordinator: DiscoveryToolCoordinator) -> None:
        self._delegate = delegate
        self._coordinator = coordinator
        self._seen: set[ToolKey] = set()

    def _invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call: Callable[[], ToolResponse],
    ) -> ToolResponse:
        key = canonical_tool_key(tool_name, arguments)
        if key in self._seen:
            return ToolResponse(success=True, result=REPEATED_TOOL_RESULT)
        response = self._coordinator.execute(key, call)
        if _cacheable(response):
            self._seen.add(key)
        return response

    def get_file_content(self, file_path: str) -> ToolResponse:
        return self._invoke(
            "get_file_content",
            {"file_path": file_path},
            lambda: self._delegate.get_file_content(file_path),
        )

    def find_sensitive_apis(self) -> ToolResponse:
        return self._invoke(
            "find_sensitive_apis", {}, self._delegate.find_sensitive_apis
        )

    def find_callers(self, query: str) -> ToolResponse:
        return self._invoke(
            "find_callers",
            {"query": query},
            lambda: self._delegate.find_callers(query),
        )

    def get_code_metrics(self, file_path: str) -> ToolResponse:
        return self._invoke(
            "get_code_metrics",
            {"file_path": file_path},
            lambda: self._delegate.get_code_metrics(file_path),
        )
