"""通用有界并行派发(阶段3引入,供 ContextProvider Level1 工具调用复用)。

只做"有界线程池 + 单项失败隔离 + 按输入顺序回收结果"这一件事,不为假设中的 async
迁移预留双接口——若后续阶段的并发形状变成需要全局限流的二维 fan-out,那是 ROADMAP
"chunking 前不切 async"岔路口登记的切换时机,届时另行设计(见
docs/superpowers/specs/2026-07-11-risk-aware-context-provider-design.md §"不是为后续
阶段的 async 迁移预留双接口")。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def run_bounded_parallel(
    items: list[T],
    fn: Callable[[T], R],
    max_workers: int = 8,
) -> list[R | None]:
    """有界线程池并发执行 fn(item),按输入顺序返回结果。

    单项抛异常时该项结果为 None,不影响其它项。
    """
    if not items:
        return []

    results: list[R | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        futures = {pool.submit(fn, item): idx for idx, item in enumerate(items)}
        for future, idx in futures.items():
            try:
                results[idx] = future.result()
            except Exception:  # noqa: BLE001 单项失败隔离,不让其它项失败
                results[idx] = None

    return results
