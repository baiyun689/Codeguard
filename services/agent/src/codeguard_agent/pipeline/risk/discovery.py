from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import uuid4

from codeguard_agent.tools.tool_client import ToolResponse

DISCOVERY_GATEWAY_TOOLS = frozenset({
    "get_file_content",
    "inspect_security_path",
    "inspect_change_impact",
    "inspect_structure",
})
REPEATED_TOOL_RESULT = (
    "该工具和参数已经在当前对话中成功返回；请复用前述结果，不要重复读取。"
)
COMPLETE_PATCH_RESULT = (
    "当前 task patch 已包含该新增文件的完整内容；请直接复用 patch，不要重复读取。"
)
ToolKey = tuple[str, str]


@dataclass(frozen=True)
class DiscoveryToolRecord:
    call_id: str
    tool: str
    arguments: dict[str, Any]
    output: str
    duration_ms: float
    status: str
    reuse_key: str
    reused_from_call_id: str = ""
    resolved_output: str = ""  # 运行时真实原始结果;reused 记录 output 是短标记,真实 payload 在此


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


def _response_status(response: ToolResponse) -> str:
    if response.success:
        return "complete"
    error = (response.error or "").strip()
    if error.startswith("unconfirmed_path:"):
        return "rejected"
    if error.startswith("文件不存在:"):
        return "not_found"
    return "failed"


class DiscoveryToolCoordinator:
    def __init__(self) -> None:
        self._lock = Lock()
        self._completed: dict[ToolKey, tuple[ToolResponse, str]] = {}
        self._in_flight: dict[ToolKey, Future[tuple[ToolResponse, str]]] = {}

    def first_payload_for(self, key: ToolKey) -> str:
        """reused 记录解析:首次真实调用的原始 payload(无则空串)。

        让复用方总能取到真实内容——ToolMessage 可继续返回短标记避免
        重复大文本,但证据账本与 gathered context 不丢事实。
        """
        with self._lock:
            entry = self._completed.get(key)
            if entry is None:
                return ""
            return entry[0].as_tool_output()

    def execute(
        self,
        key: ToolKey,
        call: Callable[[], ToolResponse],
    ) -> ToolResponse:
        response, _, _ = self.execute_with_trace(key, call)
        return response

    def execute_with_trace(
        self,
        key: ToolKey,
        call: Callable[[], ToolResponse],
    ) -> tuple[ToolResponse, bool, str]:
        with self._lock:
            cached = self._completed.get(key)
            if cached is not None:
                return cached[0], True, cached[1]
            future = self._in_flight.get(key)
            leader = future is None
            if future is None:
                future = Future()
                self._in_flight[key] = future
                first_call_id = f"discovery-tool-{uuid4()}"

        if not leader:
            response, first_call_id = future.result()
            return response, True, first_call_id

        try:
            try:
                response = call()
            except Exception as exc:  # noqa: BLE001
                response = ToolResponse(success=False, error=str(exc))
            with self._lock:
                if _cacheable(response):
                    self._completed[key] = (response, first_call_id)
            future.set_result((response, first_call_id))
            with self._lock:
                self._in_flight.pop(key, None)
            return response, False, first_call_id
        except BaseException as exc:
            future.set_exception(exc)
            with self._lock:
                self._in_flight.pop(key, None)
            raise


class CoordinatedDiscoveryToolClient:
    def __init__(
        self,
        delegate: Any,
        coordinator: DiscoveryToolCoordinator,
        *,
        complete_patch_files: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self._delegate = delegate
        self._coordinator = coordinator
        self._lock = Lock()
        self._seen: set[ToolKey] = set()
        self._in_flight: dict[ToolKey, Future[ToolResponse]] = {}
        self._records: list[DiscoveryToolRecord] = []
        self._first_call_ids: dict[ToolKey, str] = {}
        self._complete_patch_keys = {
            canonical_tool_key("get_file_content", {"file_path": path})
            for path in complete_patch_files
        }

    def _invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call: Callable[[], ToolResponse],
    ) -> ToolResponse:
        key = canonical_tool_key(tool_name, arguments)
        started = perf_counter()
        with self._lock:
            already_seen = key in self._seen
            if already_seen:
                future = None
                leader = False
            else:
                future = self._in_flight.get(key)
                leader = future is None
                if future is None:
                    future = Future()
                    self._in_flight[key] = future

        if already_seen:
            response = ToolResponse(success=True, result=REPEATED_TOOL_RESULT)
            self._record(
                tool_name,
                arguments,
                response,
                started,
                "reused",
                reused_from_call_id=self._first_call_ids.get(key, ""),
            )
            return response

        if not leader:
            assert future is not None
            response = future.result()
            if _cacheable(response):
                repeated = ToolResponse(success=True, result=REPEATED_TOOL_RESULT)
                self._record(
                    tool_name,
                    arguments,
                    repeated,
                    started,
                    "reused",
                    reused_from_call_id=self._first_call_ids.get(key, ""),
                )
                return repeated
            self._record(tool_name, arguments, response, started)
            return response

        try:
            assert future is not None
            response, coordinator_reused, first_call_id = (
                self._coordinator.execute_with_trace(key, call)
            )
            with self._lock:
                if _cacheable(response):
                    self._seen.add(key)
                    self._first_call_ids[key] = first_call_id
            self._record(
                tool_name,
                arguments,
                response,
                started,
                "reused" if coordinator_reused else None,
                call_id=(None if coordinator_reused else first_call_id),
                reused_from_call_id=(first_call_id if coordinator_reused else ""),
            )
            future.set_result(response)
            with self._lock:
                self._in_flight.pop(key, None)
            return response
        except BaseException as exc:
            if future is not None and not future.done():
                future.set_exception(exc)
            with self._lock:
                self._in_flight.pop(key, None)
            raise

    def _record(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        response: ToolResponse,
        started: float,
        status: str | None = None,
        *,
        call_id: str | None = None,
        reused_from_call_id: str = "",
    ) -> None:
        canonical_arguments = _canonical_arguments(arguments)
        key = canonical_tool_key(tool_name, canonical_arguments)
        effective_status = status or _response_status(response)
        with self._lock:
            effective_call_id = call_id or f"discovery-tool-{uuid4()}"
            first_call_id = (
                reused_from_call_id or self._first_call_ids.get(key, "")
            )
            if not first_call_id and effective_status != "reused":
                self._first_call_ids[key] = effective_call_id
            record = DiscoveryToolRecord(
                call_id=effective_call_id,
                tool=tool_name,
                arguments=canonical_arguments,
                output=response.as_tool_output(),
                duration_ms=(
                    0.0
                    if effective_status == "reused"
                    else (perf_counter() - started) * 1000
                ),
                status=effective_status,
                reuse_key=f"{key[0]}:{key[1]}",
                reused_from_call_id=(
                    first_call_id
                    if effective_status == "reused"
                    else ""
                ),
                resolved_output=(
                    self._coordinator.first_payload_for(key)
                    if effective_status == "reused"
                    else response.as_tool_output()
                ),
            )
            self._records.append(record)

    @property
    def trace_records(self) -> tuple[DiscoveryToolRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def get_file_content(self, file_path: str) -> ToolResponse:
        key = canonical_tool_key("get_file_content", {"file_path": file_path})
        if key in self._complete_patch_keys:
            response = ToolResponse(success=True, result=COMPLETE_PATCH_RESULT)
            self._record(
                "get_file_content",
                {"file_path": file_path},
                response,
                perf_counter(),
                "reused",
                reused_from_call_id="task_patch",
            )
            return response
        return self._invoke(
            "get_file_content",
            {"file_path": file_path},
            lambda: self._delegate.get_file_content(file_path),
        )

    def inspect_security_path(self, symbol_id: str) -> ToolResponse:
        return self._invoke(
            "inspect_security_path",
            {"symbol_id": symbol_id},
            lambda: self._delegate.inspect_security_path(symbol_id),
        )

    def inspect_change_impact(self, symbol_id: str) -> ToolResponse:
        return self._invoke(
            "inspect_change_impact",
            {"symbol_id": symbol_id},
            lambda: self._delegate.inspect_change_impact(symbol_id),
        )

    def inspect_structure(self, symbol_id: str) -> ToolResponse:
        return self._invoke(
            "inspect_structure",
            {"symbol_id": symbol_id},
            lambda: self._delegate.inspect_structure(symbol_id),
        )
