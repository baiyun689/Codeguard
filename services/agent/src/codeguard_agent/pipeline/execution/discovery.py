from __future__ import annotations

from html import unescape
import json
import posixpath
from dataclasses import dataclass
from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import uuid4

from codeguard_agent.pipeline.evidence.projection import (
    GraphProjectionFocus,
    ProjectionAudience,
    project_tool_payload,
)
from codeguard_agent.tools.tool_client import ToolResponse

DISCOVERY_GATEWAY_TOOLS = frozenset({
    "get_file_content",
    "inspect_change_impact",
    "inspect_structure",
    "inspect_path",
})
GRAPH_DISCOVERY_TOOLS = frozenset({
    "inspect_change_impact",
    "inspect_structure",
    "inspect_path",
})
REPEATED_TOOL_RESULT = (
    "该工具和参数已经在当前对话中成功返回；请复用前述结果，不要重复读取。"
)
COMPLETE_PATCH_RESULT = (
    "当前 task patch 已包含该新增文件的完整内容；请直接复用 patch，不要重复读取。"
)
ALIAS_TAG = "[证据编号 {alias}]"  # 证据目录短别名回显(Evidence Ledger 修正④)
ToolKey = tuple[str, str]


def _reviewer_response(
    tool: str,
    response: ToolResponse,
    arguments: dict[str, Any] | None = None,
    focus: GraphProjectionFocus | None = None,
) -> ToolResponse:
    """把已捕获的原始工具响应投影成 Reviewer 所需视图。"""
    if not response.success:
        return response
    raw = response.result or ""
    projection = project_tool_payload(
        tool,
        raw,
        ProjectionAudience.REVIEWER,
        arguments=_canonical_arguments(arguments or {}),
        focus=focus,
    )
    return ToolResponse(success=True, result=projection.content)


def _alias_echo(
    tool: str,
    response: ToolResponse,
    alias: str,
    arguments: dict[str, Any] | None = None,
    focus: GraphProjectionFocus | None = None,
) -> ToolResponse:
    """把证据编号回显进返回给 LLM 的文本;record 保留原始 payload 不污染。

    空结果不附加编号(无内容可引用,不占 T 编号语义)。"""
    if not response.success:
        error = (response.error or "tool_failed").strip()
        return ToolResponse(
            success=False,
            error=f"{error}\n\n{ALIAS_TAG.format(alias=alias)}",
        )
    text = (
        _reviewer_response(tool, response, arguments, focus).result or ""
    ).strip()
    if not text:
        return response
    return ToolResponse(success=True, result=f"{text}\n\n{ALIAS_TAG.format(alias=alias)}")


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
    symbol_id = normalized.get("symbol_id")
    if isinstance(symbol_id, str):
        normalized["symbol_id"] = unescape(symbol_id).strip()
    file_path = normalized.get("file_path")
    if isinstance(file_path, str):
        normalized["file_path"] = _normalize_path(file_path)
    query = normalized.get("query")
    if isinstance(query, str) and "#" in query:
        path, method = query.split("#", 1)
        normalized["query"] = f"{_normalize_path(path)}#{method}"
    max_depth = normalized.get("max_depth")
    if isinstance(max_depth, str) and max_depth.strip().isdigit():
        normalized["max_depth"] = int(max_depth.strip())
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
        complete_patch_symbol_ids: set[str] | frozenset[str] = frozenset(),
        projection_focus: GraphProjectionFocus | None = None,
    ) -> None:
        self._delegate = delegate
        self._coordinator = coordinator
        self._lock = Lock()
        self._seen: set[ToolKey] = set()
        self._in_flight: dict[ToolKey, Future[ToolResponse]] = {}
        self._records: list[DiscoveryToolRecord] = []
        self._first_call_ids: dict[ToolKey, str] = {}
        # 源码工具现在只接受 symbol_id。完整新增文件的 shortcut 仍由调用方
        # 显式传入对应的 resolved symbol IDs，避免根据 LLM 提供的路径猜测。
        self._complete_patch_keys = {
            canonical_tool_key(
                "get_file_content", {"symbol_id": unescape(symbol_id)}
            )
            for symbol_id in complete_patch_symbol_ids
        }
        self._projection_focus = projection_focus
        # In a real reviewer run, source reads are limited to symbols exposed by
        # SymbolResolution or returned by an earlier graph query.  A missing
        # focus is retained for small, isolated clients/tests that do not have a
        # task context; production reviewer clients always carry one.
        self._allowed_symbol_ids: set[str] | None = (
            {
                unescape(symbol_id).strip()
                for symbol_id in projection_focus.changed_symbol_ids
                if symbol_id.strip()
            }
            if projection_focus is not None
            else None
        )

    @property
    def projection_focus(self) -> GraphProjectionFocus | None:
        return self._projection_focus

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
            return _alias_echo(
                tool_name,
                response,
                self._next_t_alias(),
                arguments,
                self._projection_focus,
            )

        try:
            assert future is not None
            response, coordinator_reused, first_call_id = (
                self._coordinator.execute_with_trace(key, call)
            )
            self._remember_graph_symbols(tool_name, response, arguments)
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
            if not coordinator_reused:
                return _alias_echo(
                    tool_name,
                    response,
                    self._next_t_alias(),
                    arguments,
                    self._projection_focus,
                )
            return _alias_echo(
                tool_name,
                response,
                self._next_t_alias(),
                arguments,
                self._projection_focus,
            )
        except BaseException as exc:
            if future is not None and not future.done():
                future.set_exception(exc)
            with self._lock:
                self._in_flight.pop(key, None)
            raise

    def _remember_graph_symbols(
        self,
        tool_name: str,
        response: ToolResponse,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        """Extend the source-read allowlist with symbols visible to the reviewer.

        The Gateway payload is retained separately as an Evidence Artifact, but
        it is not the LLM-facing contract.  Only symbols that survive the same
        deterministic projection shown to the reviewer may unlock a subsequent
        source read.  In particular, a symbol present only in a truncated or
        otherwise hidden raw ``symbols`` array must not become an implicit
        source-read capability, and relationship endpoints without a resolved
        symbol remain fail-closed.
        """
        if tool_name not in GRAPH_DISCOVERY_TOOLS or not response.success:
            return
        visible_response = _reviewer_response(
            tool_name,
            response,
            arguments,
            self._projection_focus,
        )
        try:
            payload = json.loads(visible_response.result or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            return
        ids = {
            unescape(str(item.get("id", ""))).strip()
            for item in symbols
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
        if not ids or self._allowed_symbol_ids is None:
            return
        with self._lock:
            self._allowed_symbol_ids.update(ids)

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

    def _next_t_alias(self) -> str:
        """本客户端目录的下一个 T 编号(与 ledger 的 append_tool_records 同序)。

        编号 = 非短标记记录数(短标记不建 Artifact,跨任务复用建 REUSED
        Artifact),与目录追加顺序一致,保证回显编号可被合成期引用。
        """
        with self._lock:
            count = sum(
                1
                for record in self._records
                if record.output
                not in {COMPLETE_PATCH_RESULT, REPEATED_TOOL_RESULT}
            )
        return f"T{count:02d}"

    def get_file_content(self, symbol_id: str) -> ToolResponse:
        symbol_id = unescape(symbol_id)
        if (
            self._allowed_symbol_ids is not None
            and symbol_id not in self._allowed_symbol_ids
        ):
            return self._invoke(
                "get_file_content",
                {"symbol_id": symbol_id},
                lambda: ToolResponse(
                    success=False,
                    error="symbol_not_in_review_context",
                ),
            )
        key = canonical_tool_key("get_file_content", {"symbol_id": symbol_id})
        if key in self._complete_patch_keys:
            response = ToolResponse(success=True, result=COMPLETE_PATCH_RESULT)
            self._record(
                "get_file_content",
                {"symbol_id": symbol_id},
                response,
                perf_counter(),
                "reused",
                reused_from_call_id="task_patch",
            )
            # Patch is bound internally as P01; never expose that implementation
            # alias to the reviewer.  The LLM-facing contract only allows Cxx/Txx.
            return response
        return self._invoke(
            "get_file_content",
            {"symbol_id": symbol_id},
            lambda: self._delegate.get_file_content(symbol_id),
        )

    def inspect_path(
        self,
        symbol_id: str,
        path_kind: str,
        max_depth: int = 3,
    ) -> ToolResponse:
        symbol_id = unescape(symbol_id)
        if path_kind not in {"behavior", "security"}:
            return ToolResponse(success=False, error="invalid_path_kind")
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not 1 <= max_depth <= 3:
            return ToolResponse(success=False, error="invalid_max_depth")
        return self._invoke(
            "inspect_path",
            {
                "symbol_id": symbol_id,
                "path_kind": path_kind,
                "max_depth": max_depth,
            },
            lambda: self._delegate.inspect_path(symbol_id, path_kind, max_depth),
        )

    def inspect_change_impact(self, symbol_id: str) -> ToolResponse:
        symbol_id = unescape(symbol_id)
        return self._invoke(
            "inspect_change_impact",
            {"symbol_id": symbol_id},
            lambda: self._delegate.inspect_change_impact(symbol_id),
        )

    def inspect_structure(self, symbol_id: str) -> ToolResponse:
        symbol_id = unescape(symbol_id)
        return self._invoke(
            "inspect_structure",
            {"symbol_id": symbol_id},
            lambda: self._delegate.inspect_structure(symbol_id),
        )
