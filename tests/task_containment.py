"""Start asyncio tasks from a test body without leaking them on a failure.

Shared by tests, not collected by pytest (no `test_` prefix). It lives outside
`tests/integration/` so that its own pin, `tests/unit/test_task_containment.py`,
runs in the unit suite without Postgres.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


@contextlib.asynccontextmanager
async def tasks_contained() -> AsyncIterator[Callable[[Coroutine[Any, Any, T]], asyncio.Task[T]]]:
    """Yield a task starter; on a failure, cancel and await what it started.

    Only the failure path does anything: if the body raises, each started task
    that is still running is cancelled, and every started task is awaited
    (its own outcome swallowed) before the body's exception propagates. The
    body's exception is the one reported, not a side effect of the cleanup.
    """
    tasks: list[asyncio.Task[T]] = []

    def start(coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    try:
        yield start
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
