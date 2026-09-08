from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
from threading import Event, Lock

from codeguard_agent.pipeline.execution.discovery import (
    COMPLETE_PATCH_RESULT,
    REPEATED_TOOL_RESULT,
    SUBTASK_NO_PROGRESS_TERMINAL_RESULT,
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
    canonical_tool_key,
)
from codeguard_agent.pipeline.evidence.projection import GraphProjectionFocus
from codeguard_agent.tools.tool_client import ToolResponse


class _FakeClient:
    def __init__(self, responses: list[ToolResponse] | None = None) -> None:
        self.calls = 0
        self._responses = list(responses or [ToolResponse(True, "FULL BODY")])
        self._lock = Lock()

    def get_file_content(self, symbol_id: str) -> ToolResponse:  # noqa: ARG002
        with self._lock:
            index = self.calls
            self.calls += 1
        return self._responses[min(index, len(self._responses) - 1)]


class _FakeGraphClient:
    def __init__(self) -> None:
        self.path_calls: list[tuple[str, str, int]] = []
        self.impact_calls: list[str] = []
        self.structure_calls: list[str] = []

    def inspect_path(
        self, symbol_id: str, path_kind: str, max_depth: int = 3
    ) -> ToolResponse:
        self.path_calls.append((symbol_id, path_kind, max_depth))
        return ToolResponse(True, "PATH")

    def inspect_change_impact(self, symbol_id: str) -> ToolResponse:
        self.impact_calls.append(symbol_id)
        return ToolResponse(True, "IMPACT")

    def inspect_structure(self, symbol_id: str) -> ToolResponse:
        self.structure_calls.append(symbol_id)
        return ToolResponse(True, "STRUCTURE")


class _ResolvedGraphClient(_FakeClient, _FakeGraphClient):
    def __init__(self) -> None:
        _FakeClient.__init__(self)
        self.structure_calls: list[str] = []

    def inspect_structure(self, symbol_id: str) -> ToolResponse:
        self.structure_calls.append(symbol_id)
        return ToolResponse(
            True,
            '{"schema_version":2,"outcome":"found","coverage":"complete",'
            '"source_scope":"MAIN","subject_symbol_id":"java:demo.A#m()",'
            '"symbols":[{"id":"java:demo.A#m()","kind":"METHOD",'
            '"file":"src/A.java","startLine":1,"endLine":2,"source_set":"MAIN"},'
            '{"id":"java:demo.B#n()","kind":"METHOD","file":"src/B.java",'
            '"startLine":1,"endLine":2,"source_set":"MAIN"}],'
            '"relationships":[{"sourceId":"java:demo.A#m()",'
            '"targetId":"java:demo.B#n()","kind":"CALLS","file":"src/A.java",'
            '"line":2,"source_set":"MAIN","resolution":"RESOLVED"}],'
            '"unresolved_relationships":[],"unresolved_count":0,"limitations":[]}',
        )


class _StableRelationClient:
    def __init__(self) -> None:
        self.calls = 0

    def query_relations(self, subject_symbol_id: str, relation: str, **_kwargs):
        self.calls += 1
        return ToolResponse(True, json.dumps({
            "schema_version": 2,
            "outcome": "found",
            "coverage": "complete",
            "source_scope": "MAIN",
            "subject_symbol_id": subject_symbol_id,
            "symbols": [
                {"id": subject_symbol_id, "kind": "METHOD", "source_set": "MAIN"},
                {"id": "java:demo.B#run()", "kind": "METHOD", "source_set": "MAIN"},
            ],
            "relationships": [{
                "sourceId": subject_symbol_id,
                "targetId": "java:demo.B#run()",
                "kind": "CALLS",
                "file": "src/A.java",
                "line": 2,
                "source_set": "MAIN",
                "resolution": "RESOLVED",
            }],
            "unresolved_relationships": [],
            "unresolved_count": 0,
            "limitations": [],
            "next_cursor": None,
        }))


class _EmptyRelationClient:
    def __init__(self) -> None:
        self.calls = 0

    def query_relations(self, subject_symbol_id: str, relation: str, **_kwargs):
        self.calls += 1
        return ToolResponse(True, json.dumps({
            "schema_version": 2,
            "outcome": "found",
            "coverage": "complete",
            "source_scope": "MAIN",
            "subject_symbol_id": subject_symbol_id,
            "symbols": [],
            "relationships": [],
            "unresolved_relationships": [],
            "unresolved_count": 0,
            "limitations": [],
            "next_cursor": None,
        }))


class _StableReadClient:
    def __init__(self) -> None:
        self.calls = 0

    def read_symbol(self, symbol_id: str, **kwargs):
        self.calls += 1
        start = kwargs.get("start_line", 1)
        end = kwargs.get("end_line", 4)
        return ToolResponse(
            True,
            "\n".join([
                f"symbol_id: {symbol_id}",
                "kind: METHOD",
                "file: src/A.java",
                f"lines: {start}-{end}",
                "truncated: true",
                "",
                "return value;",
            ]),
        )


class _FailedRelationClient:
    def __init__(self) -> None:
        self.calls = 0

    def query_relations(self, subject_symbol_id: str, relation: str, **_kwargs):
        self.calls += 1
        return ToolResponse(False, error="graph_unavailable: test")


class _StableJsonGraphClient:
    def __init__(self) -> None:
        self.calls = 0

    def inspect_path(self, symbol_id: str, path_kind: str, max_depth: int = 3, **_kwargs):
        self.calls += 1
        return ToolResponse(True, json.dumps({
            "schema_version": 2,
            "outcome": "found",
            "coverage": "complete",
            "path_kind": path_kind,
            "max_depth": max_depth,
            "next_cursor": self.calls,
            "symbols": [{"id": symbol_id}],
            "relationships": [{
                "sourceId": symbol_id,
                "targetId": "java:demo.B#run()",
                "kind": "CALLS",
            }],
            "unresolved_relationships": [],
        }))


class _GraphWithHiddenSymbolClient(_FakeClient):
    def inspect_structure(self, symbol_id: str) -> ToolResponse:
        return ToolResponse(
            True,
            '{"schema_version":2,"outcome":"found","coverage":"complete",'
            '"source_scope":"MAIN","subject_symbol_id":"java:demo.A#m()",'
            '"symbols":['
            '{"id":"java:demo.A#m()","kind":"METHOD",'
            '"file":"src/A.java","startLine":1,"endLine":2,"source_set":"MAIN"},'
            '{"id":"java:demo.Hidden#n()","kind":"METHOD",'
            '"file":"src/Hidden.java","startLine":1,"endLine":2,"source_set":"MAIN"}'
            '],"relationships":[],"unresolved_relationships":[],"unresolved_count":0,"limitations":[]}',
        )


def test_graph_tools_decode_html_entities_in_symbol_id_before_gateway_call() -> None:
    raw = _FakeGraphClient()
    client = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    escaped = "java:demo.Retry#run(java.util.List&lt;T&gt;)"
    canonical = "java:demo.Retry#run(java.util.List<T>)"

    client.inspect_path(escaped, "behavior")
    client.inspect_change_impact(escaped)
    client.inspect_structure(escaped)

    assert raw.path_calls == [(canonical, "behavior", 3)]
    assert raw.impact_calls == [canonical]
    assert raw.structure_calls == [canonical]


def test_canonical_key_normalizes_symbol_entities_without_lowercasing() -> None:
    left = canonical_tool_key(
        "get_file_content",
        {"symbol_id": "java:demo.Retry#run(java.util.List&lt;T&gt;)"},
    )
    right = canonical_tool_key(
        "get_file_content",
        {"symbol_id": "java:demo.Retry#run(java.util.List<T>)"},
    )
    lower = canonical_tool_key(
        "get_file_content",
        {"symbol_id": "java:demo.retry#run(java.util.List<T>)"},
    )
    assert left == right
    assert left != lower


def test_same_conversation_repeated_read_returns_short_marker() -> None:
    raw = _FakeClient()
    client = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    first = client.get_file_content("java:demo.A#run()")
    second = client.get_file_content("java:demo.A#run()")
    # 首次真实结果回显证据编号(Evidence Ledger 修正④),重复调用仍是短标记。
    assert first.result == "FULL BODY\n\n[证据编号 T01]"
    assert second.result == REPEATED_TOOL_RESULT
    assert raw.calls == 1
    records = client.trace_records
    assert [record.status for record in records] == ["complete", "reused"]
    assert records[1].reuse_key == records[0].reuse_key
    assert records[1].reused_from_call_id == records[0].call_id


def test_repeated_relation_probes_close_subtask_at_same_frontier() -> None:
    raw = _StableRelationClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        initial_symbol_ids={"java:demo.A#run()"},
        symbol_catalog_ids=("java:demo.A#run()",),
        lossless_payload=True,
        max_no_progress_calls=2,
    )

    first = client.query_relations("S01", "callees", depth=1, limit=20)
    second = client.query_relations("S01", "callees", depth=2, limit=50)
    third = client.query_relations("S01", "callees", depth=3, limit=200)

    assert first.success is True
    assert second.success is True
    assert third.success is True
    assert SUBTASK_NO_PROGRESS_TERMINAL_RESULT in (third.result or "")
    assert client.no_progress_exhausted is True
    assert client.termination_reason == "no_progress"
    # A fourth probe is rejected before it can reach the Gateway.
    fourth = client.query_relations("S01", "callees", depth=3, limit=200, cursor=1)
    assert fourth.success is False
    assert "subtask_no_progress" in (fourth.error or "")
    assert raw.calls == 3


def test_empty_relation_response_counts_as_no_progress() -> None:
    raw = _EmptyRelationClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        initial_symbol_ids={"java:demo.A#run()"},
        symbol_catalog_ids=("java:demo.A#run()",),
        lossless_payload=True,
        max_no_progress_calls=2,
    )

    first = client.query_relations("S01", "callees")
    second = client.query_relations("S01", "callees", depth=2)

    assert first.success is True
    assert second.success is True
    assert SUBTASK_NO_PROGRESS_TERMINAL_RESULT in (second.result or "")
    assert client.no_progress_exhausted is True
    assert raw.calls == 2


def test_no_progress_isolated_per_relation_frontier() -> None:
    raw = _EmptyRelationClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        initial_symbol_ids={"java:demo.A#run()", "java:demo.B#run()"},
        symbol_catalog_ids=("java:demo.A#run()", "java:demo.B#run()"),
        lossless_payload=True,
        max_no_progress_calls=2,
    )

    first = client.query_relations("S01", "callees")
    other_frontier = client.query_relations("S02", "callees")
    repeated = client.query_relations("S01", "callees", depth=2)

    assert first.success is True
    assert other_frontier.success is True
    assert SUBTASK_NO_PROGRESS_TERMINAL_RESULT not in (other_frontier.result or "")
    assert SUBTASK_NO_PROGRESS_TERMINAL_RESULT in (repeated.result or "")
    assert client.no_progress_exhausted is True
    assert raw.calls == 3


def test_failed_tool_response_does_not_trigger_no_progress_close() -> None:
    raw = _FailedRelationClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        initial_symbol_ids={"java:demo.A#run()"},
        symbol_catalog_ids=("java:demo.A#run()",),
        lossless_payload=True,
        max_no_progress_calls=2,
    )

    first = client.query_relations("S01", "callees")
    second = client.query_relations("S01", "callees", depth=2)

    assert first.success is False
    assert second.success is False
    assert client.no_progress_exhausted is False
    assert raw.calls == 2


def test_graph_page_metadata_does_not_count_as_new_progress() -> None:
    raw = _StableJsonGraphClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        initial_symbol_ids={"java:demo.A#run()"},
        symbol_catalog_ids=("java:demo.A#run()",),
        lossless_payload=True,
        max_no_progress_calls=2,
    )

    first = client.inspect_path("S01", "behavior", 1)
    second = client.inspect_path("S01", "behavior", 2)
    third = client.inspect_path("S01", "behavior", 3)

    assert first.success is True
    assert second.success is True
    assert third.success is True
    assert SUBTASK_NO_PROGRESS_TERMINAL_RESULT in (third.result or "")
    assert client.no_progress_exhausted is True
    assert raw.calls == 3


def test_source_range_metadata_does_not_count_as_new_progress() -> None:
    raw = _StableReadClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        initial_symbol_ids={"java:demo.A#run()"},
        symbol_catalog_ids=("java:demo.A#run()",),
        lossless_payload=True,
        max_no_progress_calls=2,
    )

    first = client.read_symbol("S01", start_line=1, end_line=4)
    second = client.read_symbol("S01", start_line=2, end_line=5)
    third = client.read_symbol("S01", start_line=3, end_line=6)

    assert first.success is True
    assert second.success is True
    assert third.success is True
    assert SUBTASK_NO_PROGRESS_TERMINAL_RESULT in (third.result or "")
    assert client.no_progress_exhausted is True
    assert raw.calls == 3


def test_complete_patch_file_read_hides_internal_alias_without_delegate_call() -> None:
    raw = _FakeClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        complete_patch_symbol_ids={"java:demo.A#run()"},
    )

    response = client.get_file_content("java:demo.A#run()")

    assert response.success is True
    assert response.result == COMPLETE_PATCH_RESULT
    assert "P01" not in (response.result or "")
    assert raw.calls == 0


def test_source_read_rejects_symbol_outside_review_context() -> None:
    raw = _FakeClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        projection_focus=GraphProjectionFocus(
            changed_file="src/A.java",
            changed_lines=(1,),
            changed_symbol_ids=("java:demo.A#m()",),
        ),
    )

    response = client.get_file_content("java:demo.B#n()")

    assert response.success is False
    assert (response.error or "").startswith("symbol_not_in_review_context")
    assert raw.calls == 0
    assert client.trace_records[-1].status == "failed"


def test_graph_resolved_symbol_can_be_read_after_query() -> None:
    raw = _ResolvedGraphClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        projection_focus=GraphProjectionFocus(
            changed_file="src/A.java",
            changed_lines=(1,),
            changed_symbol_ids=("java:demo.A#m()",),
        ),
    )

    client.inspect_structure("java:demo.A#m()")
    response = client.get_file_content("java:demo.B#n()")

    assert response.success is True
    assert raw.calls == 1


def test_source_read_allowlist_uses_symbols_visible_in_graph_projection() -> None:
    raw = _GraphWithHiddenSymbolClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        projection_focus=GraphProjectionFocus(
            changed_file="src/A.java",
            changed_lines=(1,),
            changed_symbol_ids=("java:demo.A#m()",),
        ),
    )

    client.inspect_structure("java:demo.A#m()")
    response = client.get_file_content("java:demo.Hidden#n()")

    assert response.success is False
    assert (response.error or "").startswith("symbol_not_in_review_context")
    assert raw.calls == 0


def test_parallel_task_clients_share_single_flight_but_both_receive_full_result() -> None:
    raw = _FakeClient()
    coordinator = DiscoveryToolCoordinator()
    clients = [CoordinatedDiscoveryToolClient(raw, coordinator) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: c.get_file_content("java:demo.A#run()"), clients))
    # 每个 task 都收到完整内容和自己账本中的 T01，复用不丢引用能力。
    assert [result.result for result in results] == [
        "FULL BODY\n\n[证据编号 T01]",
        "FULL BODY\n\n[证据编号 T01]",
    ]
    assert raw.calls == 1
    records = [record for client in clients for record in client.trace_records]
    assert sorted(record.status for record in records) == ["complete", "reused"]
    first = next(record for record in records if record.status == "complete")
    reused = next(record for record in records if record.status == "reused")
    assert reused.duration_ms == 0.0
    assert reused.reused_from_call_id == first.call_id


def test_same_conversation_parallel_duplicate_returns_one_short_marker() -> None:
    started = Event()
    release = Event()

    class _BlockingSuccessClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_file_content(self, symbol_id: str) -> ToolResponse:  # noqa: ARG002
            self.calls += 1
            started.set()
            assert release.wait(timeout=2)
            return ToolResponse(True, "FULL BODY")

    raw = _BlockingSuccessClient()
    client = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.get_file_content, "java:demo.A#run()")
        assert started.wait(timeout=2)
        second = pool.submit(client.get_file_content, "java:demo.A#run()")
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert raw.calls == 1
    assert sum(
        result.result.startswith("FULL BODY") for result in results
    ) == 1
    assert [result.result for result in results].count(REPEATED_TOOL_RESULT) == 1


def test_empty_success_is_not_cached() -> None:
    raw = _FakeClient([ToolResponse(True, ""), ToolResponse(True, "RECOVERED")])
    coordinator = DiscoveryToolCoordinator()
    first = CoordinatedDiscoveryToolClient(raw, coordinator)
    second = CoordinatedDiscoveryToolClient(raw, coordinator)
    assert first.get_file_content("java:demo.A#run()").result == ""  # 空结果不附加编号
    # 第二个客户端各自的记录独立编号(T01),与 per-task 目录一致。
    assert second.get_file_content("java:demo.A#run()").result == "RECOVERED\n\n[证据编号 T01]"
    assert raw.calls == 2


def test_source_failures_have_domain_statuses() -> None:
    raw = _FakeClient(
        [
            ToolResponse(False, error="symbol_not_in_review_context"),
            ToolResponse(False, error="symbol_not_found: java:demo.Deleted#run()"),
        ]
    )
    client = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())

    rejected = client.get_file_content("java:demo.Guessed#run()")
    missing = client.get_file_content("java:demo.Deleted#run()")

    assert [record.status for record in client.trace_records] == [
        "failed",
        "failed",
    ]
    assert "[证据编号 T01]" in (rejected.error or "")
    assert "[证据编号 T02]" in (missing.error or "")


def test_transport_or_protocol_failure_keeps_failed_status() -> None:
    raw = _FakeClient([ToolResponse(False, error="HTTP 503")])
    client = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())

    response = client.get_file_content("java:demo.A#run()")

    assert client.trace_records[0].status == "failed"
    assert "[证据编号 T01]" in (response.error or "")


def test_different_arguments_execute_separately() -> None:
    raw = _FakeClient()
    coordinator = DiscoveryToolCoordinator()
    client = CoordinatedDiscoveryToolClient(raw, coordinator)
    client.get_file_content("java:demo.A#run()")
    client.get_file_content("java:demo.B#run()")
    assert raw.calls == 2


def test_parameterless_tool_key_is_stable() -> None:
    assert canonical_tool_key("inspect_path", {}) == (
        "inspect_path",
        "{}",
    )


def test_parallel_failure_is_shared_then_later_call_retries(monkeypatch) -> None:
    started = Event()
    release = Event()
    waiter_entered = Event()
    original_future_result = Future.result

    def _observed_result(self, *args, **kwargs):
        waiter_entered.set()
        return original_future_result(self, *args, **kwargs)

    monkeypatch.setattr(Future, "result", _observed_result)

    class _BlockingFailureClient:
        def __init__(self) -> None:
            self.calls = 0
            self.lock = Lock()

        def get_file_content(self, symbol_id: str) -> ToolResponse:  # noqa: ARG002
            with self.lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                started.set()
                assert release.wait(timeout=2)
                return ToolResponse(False, error="temporary")
            return ToolResponse(True, "RECOVERED")

    raw = _BlockingFailureClient()
    coordinator = DiscoveryToolCoordinator()
    clients = [CoordinatedDiscoveryToolClient(raw, coordinator) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(clients[0].get_file_content, "java:demo.A#run()")
        assert started.wait(timeout=2)
        second = pool.submit(clients[1].get_file_content, "java:demo.A#run()")
        assert waiter_entered.wait(timeout=2)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert all(result.success is False for result in results)
    assert raw.calls == 1

    retry = CoordinatedDiscoveryToolClient(raw, coordinator)
    assert retry.get_file_content("java:demo.A#run()").result == "RECOVERED\n\n[证据编号 T01]"
    assert raw.calls == 2


def test_failure_remains_in_flight_until_waiters_receive_it(monkeypatch) -> None:
    publishing = Event()
    allow_publish = Event()
    second_raw_call = Event()
    original_set_result = Future.set_result
    set_result_calls = 0
    set_result_lock = Lock()

    def _blocked_first_publish(self, result):
        nonlocal set_result_calls
        with set_result_lock:
            set_result_calls += 1
            call_number = set_result_calls
        if call_number == 1:
            publishing.set()
            assert allow_publish.wait(timeout=2)
        return original_set_result(self, result)

    monkeypatch.setattr(Future, "set_result", _blocked_first_publish)

    class _FailureThenSuccessClient:
        def __init__(self) -> None:
            self.calls = 0
            self.lock = Lock()

        def get_file_content(self, symbol_id: str) -> ToolResponse:  # noqa: ARG002
            with self.lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                return ToolResponse(False, error="temporary")
            second_raw_call.set()
            return ToolResponse(True, "RECOVERED")

    raw = _FailureThenSuccessClient()
    coordinator = DiscoveryToolCoordinator()
    clients = [CoordinatedDiscoveryToolClient(raw, coordinator) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(clients[0].get_file_content, "java:demo.A#run()")
        assert publishing.wait(timeout=2)
        second = pool.submit(clients[1].get_file_content, "java:demo.A#run()")
        assert not second_raw_call.wait(timeout=0.2)
        allow_publish.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert all(result.success is False for result in results)
    assert raw.calls == 1
    retry = CoordinatedDiscoveryToolClient(raw, coordinator)
    assert retry.get_file_content("java:demo.A#run()").result == "RECOVERED\n\n[证据编号 T01]"
    assert raw.calls == 2


def test_separate_coordinators_do_not_share_cache() -> None:
    raw = _FakeClient()
    one = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    two = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    assert one.get_file_content("java:demo.A#run()").success
    assert two.get_file_content("java:demo.A#run()").success
    assert raw.calls == 2


def test_dynamic_aliases_never_pollute_shared_symbol_cache() -> None:
    """R01 is local to each subtask; cache keys must use the raw symbol."""

    class Delegate:
        def __init__(self) -> None:
            self.relation_calls = 0
            self.source_calls = []

        def query_relations(self, subject, relation, **_kwargs):
            self.relation_calls += 1
            target = "java:B#run()" if subject == "java:RootA#run()" else "java:C#run()"
            return ToolResponse(True, json.dumps({
                "schema_version": 2,
                "outcome": "found",
                "coverage": "complete",
                "source_scope": "MAIN",
                "subject_symbol_id": subject,
                "symbols": [
                    {"id": subject, "kind": "method", "source_set": "MAIN"},
                    {"id": target, "kind": "method", "source_set": "MAIN"},
                ],
                "relationships": [{
                    "sourceId": subject, "targetId": target, "kind": "CALLS",
                    "file": "A.java", "line": 1, "source_set": "MAIN",
                    "resolution": "RESOLVED",
                }],
                "unresolved_relationships": [], "unresolved_count": 0,
                "limitations": [], "next_cursor": None,
            }))

        def read_symbol(self, symbol_id, **_kwargs):
            self.source_calls.append(symbol_id)
            return ToolResponse(True, f"source for {symbol_id}")

    delegate = Delegate()
    coordinator = DiscoveryToolCoordinator()
    client_a = CoordinatedDiscoveryToolClient(
        delegate, coordinator,
        initial_symbol_ids={"java:RootA#run()"},
        symbol_catalog_ids=("java:RootA#run()",),
        lossless_payload=True,
    )
    client_b = CoordinatedDiscoveryToolClient(
        delegate, coordinator,
        initial_symbol_ids={"java:RootB#run()"},
        symbol_catalog_ids=("java:RootB#run()",),
        lossless_payload=True,
    )

    client_a.query_relations("S01", "callees")
    client_b.query_relations("S01", "callees")
    assert client_a.read_symbol("R01").result.startswith("source for java:B#run()")
    assert client_b.read_symbol("R01").result.startswith("source for java:C#run()")

    # A later page/query may contain endpoints that are already rendered as
    # local aliases.  They must resolve back to B/C rather than creating a
    # chained alias such as R02 -> R01.
    client_a.query_relations("S01", "callees", cursor=0)
    assert client_a.symbol_aliases.get("R01") == "java:B#run()"
    assert "R02" not in client_a.symbol_aliases
    assert delegate.source_calls == ["java:B#run()", "java:C#run()"]
    assert delegate.source_calls == ["java:B#run()", "java:C#run()"]
