"""pipeline/execution/concurrency.py 的单测。"""

from __future__ import annotations

import codeguard_agent.pipeline.execution.concurrency as concurrency
from codeguard_agent.pipeline.execution.concurrency import run_bounded_parallel


def test_run_bounded_parallel_returns_results_in_input_order():
    items = [3, 1, 4, 1, 5]
    results = run_bounded_parallel(items, lambda x: x * 10)
    assert results == [30, 10, 40, 10, 50]


def test_run_bounded_parallel_isolates_single_failure():
    def _maybe_fail(x: int) -> int:
        if x == 2:
            raise ValueError("boom")
        return x * 10

    results = run_bounded_parallel([1, 2, 3], _maybe_fail)
    assert results == [10, None, 30]


def test_run_bounded_parallel_empty_items_returns_empty_list():
    assert run_bounded_parallel([], lambda x: x) == []


def test_run_bounded_parallel_caps_workers_to_item_count():
    results = run_bounded_parallel([1], lambda x: x + 1, max_workers=8)
    assert results == [2]


def test_run_bounded_parallel_caps_workers_at_global_limit(monkeypatch):
    observed_worker_counts: list[int] = []

    class _ImmediateFuture:
        def __init__(self, result: int) -> None:
            self._result = result

        def result(self) -> int:
            return self._result

    class _CapturingExecutor:
        def __init__(self, max_workers: int) -> None:
            observed_worker_counts.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def submit(self, fn, *args) -> _ImmediateFuture:
            return _ImmediateFuture(fn(*args))

    monkeypatch.setattr(concurrency, "ThreadPoolExecutor", _CapturingExecutor)

    assert run_bounded_parallel(list(range(9)), lambda x: x, max_workers=99) == list(
        range(9)
    )
    assert observed_worker_counts == [8]


def test_parallel_work_inherits_trace_context_without_leaking_worker_changes():
    from contextvars import ContextVar

    trace_id = ContextVar("trace_id", default="unset")
    token = trace_id.set("review-1")
    try:
        def worker(item):
            inherited = trace_id.get()
            trace_id.set(str(item))
            return inherited

        assert run_bounded_parallel([1, 2], worker) == ["review-1", "review-1"]
        assert trace_id.get() == "review-1"
    finally:
        trace_id.reset(token)
