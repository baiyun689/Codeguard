from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock

from codeguard_agent.pipeline.discovery_tools import (
    COMPLETE_PATCH_RESULT,
    REPEATED_TOOL_RESULT,
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
    canonical_tool_key,
)
from codeguard_agent.tools.tool_client import ToolResponse


class _FakeClient:
    def __init__(self, responses: list[ToolResponse] | None = None) -> None:
        self.calls = 0
        self._responses = list(responses or [ToolResponse(True, "FULL BODY")])
        self._lock = Lock()

    def get_file_content(self, file_path: str) -> ToolResponse:
        with self._lock:
            index = self.calls
            self.calls += 1
        return self._responses[min(index, len(self._responses) - 1)]


def test_canonical_key_normalizes_slashes_and_dot_segments_without_lowercasing() -> None:
    left = canonical_tool_key("get_file_content", {"file_path": "src\\.\\A.java"})
    right = canonical_tool_key("get_file_content", {"file_path": "src/A.java"})
    lower = canonical_tool_key("get_file_content", {"file_path": "src/a.java"})
    assert left == right
    assert left != lower


def test_same_conversation_repeated_read_returns_short_marker() -> None:
    raw = _FakeClient()
    client = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    first = client.get_file_content("src/A.java")
    second = client.get_file_content("src/A.java")
    assert first.result == "FULL BODY"
    assert second.result == REPEATED_TOOL_RESULT
    assert raw.calls == 1


def test_complete_patch_file_read_returns_marker_without_delegate_call() -> None:
    raw = _FakeClient()
    client = CoordinatedDiscoveryToolClient(
        raw,
        DiscoveryToolCoordinator(),
        complete_patch_files={"src/A.java"},
    )

    response = client.get_file_content("src\\.\\A.java")

    assert response.success is True
    assert response.result == COMPLETE_PATCH_RESULT
    assert raw.calls == 0


def test_parallel_task_clients_share_single_flight_but_both_receive_full_result() -> None:
    raw = _FakeClient()
    coordinator = DiscoveryToolCoordinator()
    clients = [CoordinatedDiscoveryToolClient(raw, coordinator) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: c.get_file_content("src/A.java"), clients))
    assert [result.result for result in results] == ["FULL BODY", "FULL BODY"]
    assert raw.calls == 1


def test_same_conversation_parallel_duplicate_returns_one_short_marker() -> None:
    started = Event()
    release = Event()

    class _BlockingSuccessClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_file_content(self, file_path: str) -> ToolResponse:  # noqa: ARG002
            self.calls += 1
            started.set()
            assert release.wait(timeout=2)
            return ToolResponse(True, "FULL BODY")

    raw = _BlockingSuccessClient()
    client = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.get_file_content, "src/A.java")
        assert started.wait(timeout=2)
        second = pool.submit(client.get_file_content, "src/A.java")
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert raw.calls == 1
    assert [result.result for result in results].count("FULL BODY") == 1
    assert [result.result for result in results].count(REPEATED_TOOL_RESULT) == 1


def test_empty_success_is_not_cached() -> None:
    raw = _FakeClient([ToolResponse(True, ""), ToolResponse(True, "RECOVERED")])
    coordinator = DiscoveryToolCoordinator()
    first = CoordinatedDiscoveryToolClient(raw, coordinator)
    second = CoordinatedDiscoveryToolClient(raw, coordinator)
    assert first.get_file_content("src/A.java").result == ""
    assert second.get_file_content("src/A.java").result == "RECOVERED"
    assert raw.calls == 2


def test_different_arguments_execute_separately() -> None:
    raw = _FakeClient()
    coordinator = DiscoveryToolCoordinator()
    client = CoordinatedDiscoveryToolClient(raw, coordinator)
    client.get_file_content("src/A.java")
    client.get_file_content("src/B.java")
    assert raw.calls == 2


def test_parameterless_tool_key_is_stable() -> None:
    assert canonical_tool_key("find_sensitive_apis", {}) == (
        "find_sensitive_apis",
        "{}",
    )


def test_find_callers_normalizes_only_query_path() -> None:
    left = canonical_tool_key("find_callers", {"query": "src\\.\\A.java#Run"})
    right = canonical_tool_key("find_callers", {"query": "src/A.java#Run"})
    different_method_case = canonical_tool_key(
        "find_callers", {"query": "src/A.java#run"}
    )
    assert left == right
    assert left != different_method_case


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

        def get_file_content(self, file_path: str) -> ToolResponse:  # noqa: ARG002
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
        first = pool.submit(clients[0].get_file_content, "src/A.java")
        assert started.wait(timeout=2)
        second = pool.submit(clients[1].get_file_content, "src/A.java")
        assert waiter_entered.wait(timeout=2)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert all(result.success is False for result in results)
    assert raw.calls == 1

    retry = CoordinatedDiscoveryToolClient(raw, coordinator)
    assert retry.get_file_content("src/A.java").result == "RECOVERED"
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

        def get_file_content(self, file_path: str) -> ToolResponse:  # noqa: ARG002
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
        first = pool.submit(clients[0].get_file_content, "src/A.java")
        assert publishing.wait(timeout=2)
        second = pool.submit(clients[1].get_file_content, "src/A.java")
        assert not second_raw_call.wait(timeout=0.2)
        allow_publish.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert all(result.success is False for result in results)
    assert raw.calls == 1
    retry = CoordinatedDiscoveryToolClient(raw, coordinator)
    assert retry.get_file_content("src/A.java").result == "RECOVERED"
    assert raw.calls == 2


def test_separate_coordinators_do_not_share_cache() -> None:
    raw = _FakeClient()
    one = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    two = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    assert one.get_file_content("src/A.java").success
    assert two.get_file_content("src/A.java").success
    assert raw.calls == 2
