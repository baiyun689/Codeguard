from __future__ import annotations

from html import unescape
import json
import posixpath
from dataclasses import dataclass
from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from codeguard_agent.pipeline.evidence.projection import (
    GraphProjectionFocus,
    ProjectionAudience,
    project_tool_payload,
)
from codeguard_agent.tools.tool_client import ToolResponse

DISCOVERY_GATEWAY_TOOLS = frozenset({
    "read_symbol",
    "query_relations",
    "get_file_content",
    "inspect_change_impact",
    "inspect_structure",
    "inspect_path",
})
GRAPH_DISCOVERY_TOOLS = frozenset({
    "query_relations",
    "inspect_change_impact",
    "inspect_structure",
    "inspect_path",
})
REPEATED_TOOL_RESULT = (
    "该工具和参数已经在当前对话中成功返回；请复用前述结果，不要重复读取。"
)
SUBTASK_BUDGET_TERMINAL_RESULT = (
    "这是该子任务允许的最后一个工具窗口。立即停止调用任何工具并输出"
    " InvestigationResult：只有已返回的 observation 能直接支持时才输出 findings；"
    "否则输出 inconclusive。不要把工具预算不足当作 no_finding。"
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
    lossless: bool = False,
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
        (response.result if lossless else _reviewer_response(tool, response, arguments, focus).result)
        or ""
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
        lossless_payload: bool = False,
        max_tool_calls: int | None = None,
        max_path_depth: int = 3,
        allowed_path_kind: Literal["behavior", "security"] | None = None,
        allowed_direction: Literal["downstream", "upstream"] | None = None,
        allowed_relations: tuple[str, ...] | frozenset[str] | set[str] = (),
        initial_symbol_ids: set[str] | frozenset[str] = frozenset(),
        symbol_catalog_ids: tuple[str, ...] = (),
    ) -> None:
        self._delegate = delegate
        self._coordinator = coordinator
        self._lock = Lock()
        self._seen: set[ToolKey] = set()
        self._in_flight: dict[ToolKey, Future[ToolResponse]] = {}
        self._records: list[DiscoveryToolRecord] = []
        self._observation_aliases: dict[str, str] = {}
        self._first_call_ids: dict[ToolKey, str] = {}
        self._alias_counter = 0
        # 源码工具现在只接受 symbol_id。完整新增文件的 shortcut 仍由调用方
        # 显式传入对应的 resolved symbol IDs，避免根据 LLM 提供的路径猜测。
        self._complete_patch_keys = {
            canonical_tool_key(tool_name, {"symbol_id": unescape(symbol_id)})
            for tool_name in ("get_file_content", "read_symbol")
            for symbol_id in complete_patch_symbol_ids
        }
        self._projection_focus = projection_focus
        self._lossless_payload = lossless_payload
        self._max_tool_calls = (
            max(0, max_tool_calls) if max_tool_calls is not None else None
        )
        self._tool_calls = 0
        self._budget_exhausted = False
        self._closed = False
        self._max_path_depth = max(1, min(3, max_path_depth))
        if allowed_path_kind not in {None, "behavior", "security"}:
            raise ValueError(
                "allowed_path_kind must be 'behavior', 'security', or None"
            )
        self._allowed_path_kind = allowed_path_kind
        if allowed_direction not in {None, "downstream", "upstream"}:
            raise ValueError(
                "allowed_direction must be 'downstream', 'upstream', or None"
            )
        self._allowed_direction = allowed_direction
        self._allowed_relations = frozenset(str(item) for item in allowed_relations)
        initial_ids = {
            unescape(symbol_id).strip()
            for symbol_id in initial_symbol_ids
            if symbol_id.strip()
        }
        catalog_ids = set(symbol_catalog_ids) | initial_ids
        self._initial_allowed_symbol_ids = set(initial_ids)
        self._raw_by_symbol_alias: dict[str, str] = {
            f"S{index:02d}": symbol_id
            for index, symbol_id in enumerate(sorted(catalog_ids), start=1)
            if symbol_id
        }
        self._symbol_alias_by_raw = {
            raw: alias for alias, raw in self._raw_by_symbol_alias.items()
        }
        self._alias_mode = bool(self._raw_by_symbol_alias)
        # Sxx and Rxx are separate namespaces; a dynamic result always starts
        # at R01 even when the initial catalog contains many Sxx symbols.
        self._next_symbol_alias = 1
        # In a real reviewer run, source reads are limited to symbols exposed by
        # SymbolResolution or returned by an earlier graph query.  A missing
        # focus is retained for small, isolated clients/tests that do not have a
        # task context; production reviewer clients always carry one.
        focus_ids = {
            unescape(symbol_id).strip()
            for symbol_id in (
                projection_focus.changed_symbol_ids
                if projection_focus is not None else ()
            )
            if symbol_id.strip()
        }
        self._allowed_symbol_ids: set[str] | None = (
            (
                self._initial_allowed_symbol_ids
                if self._alias_mode
                else focus_ids | initial_ids
            )
            if (projection_focus is not None or initial_ids)
            else None
        )

    @property
    def projection_focus(self) -> GraphProjectionFocus | None:
        return self._projection_focus

    @property
    def lossless_payload(self) -> bool:
        return self._lossless_payload

    @property
    def symbol_aliases(self) -> dict[str, str]:
        with self._lock:
            return dict(self._raw_by_symbol_alias)

    def symbol_alias_for(self, symbol_id: str) -> str:
        with self._lock:
            return self._symbol_alias_by_raw.get(symbol_id, symbol_id)

    def _resolve_symbol_ref(self, symbol_ref: str) -> str | None:
        value = unescape(symbol_ref).strip()
        with self._lock:
            if self._alias_mode:
                raw = self._raw_by_symbol_alias.get(value)
                if raw is None or raw not in self._allowed_symbol_ids:
                    return None
                return raw
        return value

    def _alias_payload(self, tool: str, response: ToolResponse) -> ToolResponse:
        if not self._alias_mode or not response.success or not response.result:
            return response
        text = response.result
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            if tool in {"get_file_content", "read_symbol"}:
                with self._lock:
                    for raw, alias in self._symbol_alias_by_raw.items():
                        text = text.replace(f"symbol_id: {raw}", f"symbol_id: {alias}")
            return ToolResponse(success=True, result=text)
        if not isinstance(payload, dict):
            return response
        with self._lock:
            aliases = dict(self._symbol_alias_by_raw)

        def replace(value: Any, key: str = "") -> Any:
            if isinstance(value, dict):
                return {name: replace(item, name) for name, item in value.items()}
            if isinstance(value, list):
                return [replace(item, key) for item in value]
            if isinstance(value, str) and key in {
                "id", "symbol_id", "owner_id", "sourceId", "targetId",
                "source_id", "target_id", "subject_symbol_id",
            }:
                return aliases.get(value, value)
            return value

        try:
            return ToolResponse(success=True, result=json.dumps(
                replace(payload), ensure_ascii=False, separators=(",", ":")
            ))
        except (TypeError, ValueError):
            return response

    def _visible_response(
        self,
        tool: str,
        response: ToolResponse,
        arguments: dict[str, Any] | None = None,
    ) -> ToolResponse:
        if response.success and self._lossless_payload:
            return self._alias_payload(tool, response)
        if not response.success:
            return response
        return self._alias_payload(
            tool,
            _reviewer_response(tool, response, arguments, self._projection_focus),
        )

    def _invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call: Callable[[], ToolResponse],
    ) -> ToolResponse:
        key = canonical_tool_key(tool_name, arguments)
        started = perf_counter()
        budget_exceeded = False
        with self._lock:
            if self._closed:
                budget_exceeded = True
                close_error = "subtask_execution_closed"
            elif self._max_tool_calls is not None and self._tool_calls >= self._max_tool_calls:
                budget_exceeded = True
                close_error = "subtask_tool_budget_exceeded"
                self._budget_exhausted = True
            else:
                self._tool_calls += 1
                close_error = ""
            already_seen = False if budget_exceeded else key in self._seen
            if already_seen:
                future = None
                leader = False
            elif not budget_exceeded:
                future = self._in_flight.get(key)
                leader = future is None
                if future is None:
                    future = Future()
                    self._in_flight[key] = future

        if budget_exceeded:
            response = ToolResponse(
                success=False,
                error=close_error or "subtask_tool_budget_exceeded",
                result=(
                    SUBTASK_BUDGET_TERMINAL_RESULT
                    if close_error == "subtask_tool_budget_exceeded"
                    else None
                ),
            )
            self._record(
                tool_name,
                arguments,
                response,
                started,
                "rejected",
            )
            return response

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
            record_call_id = self._record(tool_name, arguments, response, started)
            return _alias_echo(
                tool_name,
                self._visible_response(tool_name, response, arguments),
                self._next_t_alias(record_call_id),
                arguments,
                self._projection_focus,
                True,
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
            record_call_id = self._record(
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
                    self._visible_response(tool_name, response, arguments),
                    self._next_t_alias(record_call_id),
                    arguments,
                    self._projection_focus,
                    True,
                )
            return _alias_echo(
                tool_name,
                self._visible_response(tool_name, response, arguments),
                self._next_t_alias(record_call_id),
                arguments,
                self._projection_focus,
                True,
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
        visible_response = (
            response
            if self._alias_mode and self._lossless_payload
            else _reviewer_response(
                tool_name, response, arguments, self._projection_focus
            )
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
            # ``_alias_payload`` may already have rewritten known endpoints to
            # local Sxx/Rxx presentation aliases.  Never treat those aliases
            # as graph symbols: resolving them here prevents a second query
            # from creating R02 -> R01 (or poisoning the source-read allowlist).
            raw_ids = {
                self._raw_by_symbol_alias.get(symbol_id, symbol_id)
                for symbol_id in ids
            }
            self._allowed_symbol_ids.update(raw_ids)
            if not self._alias_mode:
                return
            for symbol_id in sorted(raw_ids):
                if symbol_id in self._symbol_alias_by_raw:
                    continue
                alias = f"R{self._next_symbol_alias:02d}"
                self._next_symbol_alias += 1
                self._raw_by_symbol_alias[alias] = symbol_id
                self._symbol_alias_by_raw[symbol_id] = alias

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
    ) -> str:
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
            return effective_call_id

    @property
    def trace_records(self) -> tuple[DiscoveryToolRecord, ...]:
        with self._lock:
            return tuple(self._records)

    @property
    def tool_calls(self) -> int:
        with self._lock:
            return self._tool_calls

    @property
    def budget_exhausted(self) -> bool:
        """Whether a tool call was rejected by this subtask's hard budget."""

        with self._lock:
            return self._budget_exhausted

    @property
    def observation_aliases(self) -> dict[str, str]:
        with self._lock:
            return dict(self._observation_aliases)

    def close(self) -> None:
        """Prevent late React turns from issuing new Gateway calls after timeout."""

        with self._lock:
            self._closed = True

    def _next_t_alias(self, call_id: str = "") -> str:
        """本客户端目录的下一个 T 编号(与 ledger 的 append_tool_records 同序)。

        编号 = 非短标记记录数(短标记不建 Artifact,跨任务复用建 REUSED
        Artifact),与目录追加顺序一致,保证回显编号可被合成期引用。
        """
        with self._lock:
            self._alias_counter += 1
            alias = f"T{self._alias_counter:02d}"
            if call_id:
                self._observation_aliases[alias] = call_id
            return alias

    def get_file_content(
        self,
        symbol_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        cursor: str | None = None,
    ) -> ToolResponse:
        raw_symbol_id = self._resolve_symbol_ref(symbol_id)
        # Cache and evidence keys always use the canonical raw symbol.  Sxx/Rxx
        # aliases are presentation-only and are local to a subtask.
        arguments: dict[str, Any] = {"symbol_id": raw_symbol_id}
        if raw_symbol_id is None:
            return ToolResponse(success=False, error="symbol_ref_not_in_review_context")
        symbol_id = raw_symbol_id
        if start_line is not None:
            arguments["start_line"] = start_line
        if end_line is not None:
            arguments["end_line"] = end_line
        if cursor is not None:
            arguments["cursor"] = cursor
        if (
            self._allowed_symbol_ids is not None
            and symbol_id not in self._allowed_symbol_ids
        ):
            return self._invoke(
                "get_file_content",
                arguments,
                lambda: ToolResponse(
                    success=False,
                    error="symbol_not_in_review_context",
                ),
            )
        key = canonical_tool_key("get_file_content", arguments)
        if key in self._complete_patch_keys:
            response = ToolResponse(success=True, result=COMPLETE_PATCH_RESULT)
            self._record(
                "get_file_content",
                arguments,
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
            arguments,
            lambda: self._delegate.get_file_content(symbol_id)
            if not any(value is not None for value in (start_line, end_line, cursor))
            else self._delegate.get_file_content(
                symbol_id, start_line=start_line, end_line=end_line, cursor=cursor
            ),
        )

    def read_symbol(
        self,
        symbol_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        cursor: str | None = None,
    ) -> ToolResponse:
        """Stable alias for the source reader with canonical cache arguments."""
        raw_symbol_id = self._resolve_symbol_ref(symbol_id)
        if raw_symbol_id is None:
            return ToolResponse(success=False, error="symbol_ref_not_in_review_context")
        arguments: dict[str, Any] = {"symbol_id": raw_symbol_id}
        if start_line is not None:
            arguments["start_line"] = start_line
        if end_line is not None:
            arguments["end_line"] = end_line
        if cursor is not None:
            arguments["cursor"] = cursor
        key = canonical_tool_key("read_symbol", arguments)
        if key in self._complete_patch_keys:
            response = ToolResponse(success=True, result=COMPLETE_PATCH_RESULT)
            self._record(
                "read_symbol",
                arguments,
                response,
                perf_counter(),
                "reused",
                reused_from_call_id="task_patch",
            )
            return response
        return self._invoke(
            "read_symbol",
            arguments,
            lambda: self._delegate.read_symbol(
                raw_symbol_id,
                start_line=start_line,
                end_line=end_line,
                cursor=cursor,
            ),
        )

    def query_relations(
        self,
        subject_symbol_id: str,
        relation: str,
        *,
        depth: int = 1,
        limit: int = 20,
        cursor: int | None = None,
        include_callsite: bool = True,
        include_context: bool = True,
    ) -> ToolResponse:
        """Typed relation navigation; returned resolved symbols extend this subtask scope."""
        raw_symbol_id = self._resolve_symbol_ref(subject_symbol_id)
        if raw_symbol_id is None:
            return ToolResponse(success=False, error="symbol_ref_not_in_review_context")
        # Compatible tool-calling models occasionally emit an exploratory
        # limit/depth outside the Gateway contract.  Clamp those values at
        # the Python boundary so a harmless over-request cannot consume a
        # budget slot as an infrastructure failure; the effective values are
        # also what enters the canonical cache/evidence key.
        try:
            depth = max(1, min(3, int(depth)))
            limit = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            return ToolResponse(success=False, error="invalid_relation_page")
        if relation not in {
            "callers", "callees", "field_readers", "field_writers",
            "implementations", "overrides",
        }:
            return ToolResponse(success=False, error="unsupported_relation")
        if self._allowed_relations and relation not in self._allowed_relations:
            return ToolResponse(success=False, error="relation_not_allowed")
        if (
            self._allowed_direction == "downstream"
            and relation == "callers"
        ) or (
            self._allowed_direction == "upstream"
            and relation == "callees"
        ):
            return ToolResponse(success=False, error="relation_direction_not_allowed")
        arguments: dict[str, Any] = {
            "subject_symbol_id": raw_symbol_id,
            "relation": relation,
            "depth": depth,
            "limit": limit,
            "include_callsite": include_callsite,
            "include_context": include_context,
        }
        if cursor is not None:
            arguments["cursor"] = cursor
        return self._invoke(
            "query_relations",
            arguments,
            lambda: self._delegate.query_relations(
                raw_symbol_id,
                relation,
                depth=depth,
                limit=limit,
                cursor=cursor,
                include_callsite=include_callsite,
                include_context=include_context,
            ),
        )

    def inspect_path(
        self,
        symbol_id: str,
        path_kind: str,
        max_depth: int = 3,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ToolResponse:
        raw_symbol_id = self._resolve_symbol_ref(symbol_id)
        if raw_symbol_id is None:
            return ToolResponse(success=False, error="symbol_ref_not_in_review_context")
        symbol_id = raw_symbol_id
        if path_kind not in {"behavior", "security"}:
            return ToolResponse(success=False, error="invalid_path_kind")
        if (
            self._allowed_path_kind is not None
            and path_kind != self._allowed_path_kind
        ):
            return ToolResponse(success=False, error="path_kind_not_allowed")
        if (
            not isinstance(max_depth, int)
            or isinstance(max_depth, bool)
            or not 1 <= max_depth <= self._max_path_depth
        ):
            return ToolResponse(success=False, error="invalid_max_depth")
        arguments: dict[str, Any] = {
            "symbol_id": symbol_id,
            "path_kind": path_kind,
            "max_depth": max_depth,
        }
        if limit is not None:
            arguments["limit"] = limit
        if cursor is not None:
            arguments["cursor"] = cursor
        return self._invoke(
            "inspect_path",
            arguments,
            lambda: self._delegate.inspect_path(symbol_id, path_kind, max_depth)
            if limit is None and cursor is None
            else self._delegate.inspect_path(
                symbol_id, path_kind, max_depth, limit=limit, cursor=cursor
            ),
        )

    def inspect_change_impact(
        self,
        symbol_id: str,
        *,
        max_depth: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ToolResponse:
        requested_ref = unescape(symbol_id)
        raw_symbol_id = self._resolve_symbol_ref(requested_ref)
        if raw_symbol_id is None:
            return ToolResponse(success=False, error="symbol_ref_not_in_review_context")
        symbol_id = raw_symbol_id
        arguments: dict[str, Any] = {"symbol_id": raw_symbol_id}
        if max_depth is not None:
            arguments["max_depth"] = max_depth
        if limit is not None:
            arguments["limit"] = limit
        if cursor is not None:
            arguments["cursor"] = cursor
        return self._invoke(
            "inspect_change_impact",
            arguments,
            lambda: self._delegate.inspect_change_impact(symbol_id)
            if not any(value is not None for value in (max_depth, limit, cursor))
            else self._delegate.inspect_change_impact(
                symbol_id, max_depth=max_depth, limit=limit, cursor=cursor
            ),
        )

    def inspect_structure(
        self,
        symbol_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ToolResponse:
        requested_ref = unescape(symbol_id)
        raw_symbol_id = self._resolve_symbol_ref(requested_ref)
        if raw_symbol_id is None:
            return ToolResponse(success=False, error="symbol_ref_not_in_review_context")
        symbol_id = raw_symbol_id
        arguments: dict[str, Any] = {"symbol_id": raw_symbol_id}
        if limit is not None:
            arguments["limit"] = limit
        if cursor is not None:
            arguments["cursor"] = cursor
        return self._invoke(
            "inspect_structure",
            arguments,
            lambda: self._delegate.inspect_structure(symbol_id)
            if limit is None and cursor is None
            else self._delegate.inspect_structure(
                symbol_id, limit=limit, cursor=cursor
            ),
        )
