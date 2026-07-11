"""pipeline/concurrency.py 的单测。"""

from __future__ import annotations

from codeguard_agent.pipeline.concurrency import run_bounded_parallel


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
