"""`tests.task_containment.tasks_contained`: the failure path, without a DB.

The race tests in `tests/integration/test_settled_disposition_cas_race.py`
start their HTTP request tasks through this helper so that a failed probe does
not leave a request running into fixture teardown. Those tests only take the
passing path, so they cannot tell a working handler from a missing one
(PR-gate on SpirrowGames/spirrow-conclair#35 @ e0ee674, objection class
`untested`). These tests pin the handler directly: a task kept pending by an
event, a body that raises a sentinel, and the order of what happens next.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.task_containment import tasks_contained


class _SentinelError(Exception):
    """Raised by the context body; must be the exception that comes out."""


async def test_failure_cancels_and_awaits_pending_task_before_reraising() -> None:
    started = asyncio.Event()
    never_set = asyncio.Event()
    events: list[str] = []

    async def pending() -> str:
        started.set()
        try:
            await never_set.wait()
        except asyncio.CancelledError:
            # Yield once while handling the cancel: if the helper re-raised
            # before awaiting the task, the sentinel would surface before this
            # line runs and "cleanup-finished" would be missing below.
            await asyncio.sleep(0)
            events.append("cleanup-finished")
            raise
        return "unreachable"

    sentinel = _SentinelError("probe failed")
    task: asyncio.Task[str] | None = None
    with pytest.raises(_SentinelError) as excinfo:
        async with tasks_contained() as start:
            task = start(pending())
            await started.wait()
            assert not task.done()
            raise sentinel
    events.append("sentinel-propagated")

    # The very same exception object, not one raised by the cleanup.
    assert excinfo.value is sentinel
    assert task is not None
    assert task.cancelled()
    assert events == ["cleanup-finished", "sentinel-propagated"]


async def test_failure_awaits_task_that_already_finished_without_cancelling_it() -> None:
    async def quick() -> str:
        return "done"

    sentinel = _SentinelError("assertion failed after the request returned")
    with pytest.raises(_SentinelError) as excinfo:
        async with tasks_contained() as start:
            task = start(quick())
            await asyncio.sleep(0)
            assert task.done()
            raise sentinel

    assert excinfo.value is sentinel
    assert not task.cancelled()
    assert task.result() == "done"


async def test_failure_reports_body_exception_not_the_task_failure() -> None:
    release = asyncio.Event()

    async def fails_on_cancel() -> str:
        try:
            await release.wait()
        except asyncio.CancelledError:
            raise RuntimeError("task failed while being cleaned up") from None
        return "unreachable"

    sentinel = _SentinelError("body")
    with pytest.raises(_SentinelError) as excinfo:
        async with tasks_contained() as start:
            task = start(fails_on_cancel())
            await asyncio.sleep(0)
            raise sentinel

    assert excinfo.value is sentinel
    assert task.done()
    assert isinstance(task.exception(), RuntimeError)


async def test_success_path_leaves_tasks_alone() -> None:
    release = asyncio.Event()

    async def waits() -> str:
        await release.wait()
        return "ok"

    async with tasks_contained() as start:
        task = start(waits())
        await asyncio.sleep(0)
        assert not task.done()

    # Leaving the context without an exception neither cancels nor awaits.
    assert not task.done()
    assert not task.cancelled()
    release.set()
    assert await task == "ok"
