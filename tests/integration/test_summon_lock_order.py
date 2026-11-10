"""Summon lock order under concurrency (msg-1439 v1.2.2, as applied in r1/r2).

PUT takes the thread row FOR SHARE, then the project's control row FOR
UPDATE. The triggers run while a status UPDATE / DELETE holds the thread row
and ``clear_active_summon`` then takes the control row FOR UPDATE: the same
order. These tests pin the outcome of each interleaving between operations
on the SAME thread.

How the order is fixed. One connection holds a lock inside an open
transaction; the operation under test is started and the test waits until
PostgreSQL itself reports it as blocked by that holder
(``pg_blocking_pids``). Only then does the holder commit. The poll is a
condition, not a timer: there is no ``sleep`` -- each probe query is itself
the await that lets the waiting request run. The deadline only turns a hang
into a failure.

Scope of the guarantee (Einstein msg-005 obj 2): this is about one thread.
Transactions that end several threads can still deadlock with each other;
PostgreSQL's detector then rolls one back. That is not tested here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tests.task_containment import tasks_contained

P = "lk-proj"
PROBE_DEADLINE_SEC = 15.0

SessionFactory = async_sessionmaker[AsyncSession]


@pytest.fixture
async def rr_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url, isolation_level="REPEATABLE READ")
    try:
        yield engine
    finally:
        await engine.dispose()


async def _open(client: AsyncClient, thread_id: str) -> None:
    r = await client.post(
        f"/v1/projects/{P}/threads",
        json={"thread_id": thread_id, "title": "t", "owner": "alice", "propose_content": "s"},
    )
    assert r.status_code == 201, r.text


async def _ensure_control_row(client: AsyncClient) -> None:
    """Create the control row without a desired state or a mark."""
    r = await client.post(
        f"/v1/projects/{P}/control/observed", json={"state": "run", "actor": "loop"}
    )
    assert r.status_code == 200, r.text


async def _pid(session: AsyncSession) -> int:
    pid: int = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    return pid


async def _wait_blocked_by(sf: SessionFactory, holder_pid: int, waiter: asyncio.Task[Any]) -> int:
    """The pid PostgreSQL reports as blocked by ``holder_pid``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PROBE_DEADLINE_SEC
    async with sf() as probe:
        while True:
            if waiter.done():
                pytest.fail(f"the operation finished without waiting: {waiter.result()!r}")
            pids = (
                await probe.execute(
                    text(
                        "SELECT pid FROM pg_stat_activity "
                        "WHERE :holder = ANY(pg_blocking_pids(pid))"
                    ),
                    {"holder": holder_pid},
                )
            ).scalars().all()
            await probe.rollback()
            if pids:
                (pid,) = pids
                return int(pid)
            if loop.time() > deadline:
                pytest.fail(f"nothing reported as blocked by pid {holder_pid}")


async def _mark(sf: SessionFactory) -> tuple[Any, ...] | None:
    async with sf() as s:
        row = (
            await s.execute(
                text(
                    "SELECT active_summon_thread_id, active_summon_since, active_summon_by "
                    "FROM project_control WHERE project = :p"
                ),
                {"p": P},
            )
        ).first()
    return None if row is None else tuple(row)


async def _history(sf: SessionFactory) -> list[tuple[str, str, str]]:
    async with sf() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT action, thread_id, actor FROM project_summon_history "
                    "WHERE project = :p ORDER BY id"
                ),
                {"p": P},
            )
        ).all()
    return [tuple(r) for r in rows]  # type: ignore[misc]


async def _thread_status(sf: SessionFactory, thread_id: str) -> str:
    async with sf() as s:
        status: str = (
            await s.execute(
                text("SELECT status FROM threads WHERE project = :p AND thread_id = :t"),
                {"p": P, "t": thread_id},
            )
        ).scalar_one()
    return status


# 1 ---------------------------------------------------------------------------


async def test_close_queued_behind_a_put_clears_the_mark_the_put_set(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """PUT holds the thread (FOR SHARE) and waits on control -> a close of the
    same thread queues behind PUT -> PUT commits its mark -> the close's
    trigger then sees that mark and clears it. No mark survives on a
    resolved thread."""
    await _open(client, "T-a")
    await _ensure_control_row(client)

    async with tasks_contained() as start:
        async with session_factory() as holder:
            async with holder.begin():
                holder_pid = await _pid(holder)
                await holder.execute(
                    text("SELECT 1 FROM project_control WHERE project = :p FOR UPDATE"),
                    {"p": P},
                )
                put = start(
                    client.put(
                        f"/v1/projects/{P}/control/summon",
                        json={"thread_id": "T-a", "by": "takahito"},
                    )
                )
                put_pid = await _wait_blocked_by(session_factory, holder_pid, put)
                close = start(
                    client.post(
                        f"/v1/projects/{P}/threads/T-a/close",
                        json={"summary_content": "done", "author": "alice"},
                    )
                )
                await _wait_blocked_by(session_factory, put_pid, close)
            # holder commits: PUT proceeds, then the close.

        put_resp = await asyncio.wait_for(put, PROBE_DEADLINE_SEC)
        close_resp = await asyncio.wait_for(close, PROBE_DEADLINE_SEC)

    assert put_resp.status_code == 200, put_resp.text
    assert close_resp.status_code == 201, close_resp.text
    assert await _thread_status(session_factory, "T-a") == "resolved"
    assert await _mark(session_factory) == (None, None, None)
    assert await _history(session_factory) == [
        ("set", "T-a", "takahito"),
        ("clear", "T-a", "system:status:resolved"),
    ]


# 2 ---------------------------------------------------------------------------


async def test_put_queued_behind_an_uncommitted_close_is_refused_not_open(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """A status change holds the thread row -> PUT waits on it -> the change
    commits -> PUT reads the committed status and refuses (409); nothing is
    marked."""
    await _open(client, "T-a")

    async with tasks_contained() as start:
        async with session_factory() as holder:
            async with holder.begin():
                holder_pid = await _pid(holder)
                await holder.execute(
                    text(
                        "UPDATE threads SET status = 'resolved' "
                        "WHERE project = :p AND thread_id = 'T-a'"
                    ),
                    {"p": P},
                )
                put = start(
                    client.put(
                        f"/v1/projects/{P}/control/summon",
                        json={"thread_id": "T-a", "by": "takahito"},
                    )
                )
                await _wait_blocked_by(session_factory, holder_pid, put)

        put_resp = await asyncio.wait_for(put, PROBE_DEADLINE_SEC)

    assert put_resp.status_code == 409, put_resp.text
    assert put_resp.json()["details"]["code"] == "summon_thread_not_open"
    assert (await _mark(session_factory)) in (None, (None, None, None))
    assert await _history(session_factory) == []


# 3 ---------------------------------------------------------------------------


async def test_clear_queued_behind_an_uncommitted_delete_finds_nothing_to_clear(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """DELETE of the marked thread clears it in its trigger and holds the
    control row -> a clear of the same thread waits -> DELETE commits -> the
    clear sees no mark: 200 no-op, and exactly one clear is recorded."""
    await _open(client, "T-a")
    r = await client.put(
        f"/v1/projects/{P}/control/summon", json={"thread_id": "T-a", "by": "takahito"}
    )
    assert r.status_code == 200, r.text

    async with tasks_contained() as start:
        async with session_factory() as holder:
            async with holder.begin():
                holder_pid = await _pid(holder)
                for table in ("actor_read_cursors", "thread_digests", "messages", "threads"):
                    await holder.execute(
                        text(f"DELETE FROM {table} WHERE project = :p AND thread_id = 'T-a'"),
                        {"p": P},
                    )
                clear = start(
                    client.post(
                        f"/v1/projects/{P}/control/summon/clear",
                        json={"thread_id": "T-a", "by": "takahito"},
                    )
                )
                await _wait_blocked_by(session_factory, holder_pid, clear)

        clear_resp = await asyncio.wait_for(clear, PROBE_DEADLINE_SEC)

    assert clear_resp.status_code == 200, clear_resp.text
    assert clear_resp.json()["active_summon"] is None
    assert await _mark(session_factory) == (None, None, None)
    assert await _history(session_factory) == [
        ("set", "T-a", "takahito"),
        ("clear", "T-a", "system:thread_deleted"),
    ]


# 4 ---------------------------------------------------------------------------


async def test_repeatable_read_close_after_a_newer_mark_fails_instead_of_leaving_it(
    client: AsyncClient, session_factory: SessionFactory, rr_engine: AsyncEngine
) -> None:
    """A REPEATABLE READ transaction takes its snapshot -> a PUT commits a
    mark -> that transaction resolves the thread. The trigger's FOR UPDATE
    meets a control row changed after the snapshot: serialization failure,
    rollback. The thread stays open with its mark -- never resolved with a
    mark silently left on it.

    Precondition, deliberately set up: the control row already exists when
    the snapshot is taken. A row created after the snapshot is invisible to
    it, and FOR UPDATE then finds nothing to conflict with (recorded as a
    known limit in the PR body; Conclair's own writes are READ COMMITTED).
    """
    await _open(client, "T-a")
    await _ensure_control_row(client)

    async with rr_engine.connect() as conn:
        trans = await conn.begin()
        try:
            # Take the snapshot now.
            await conn.execute(text("SELECT count(*) FROM project_control"))

            r = await client.put(
                f"/v1/projects/{P}/control/summon",
                json={"thread_id": "T-a", "by": "takahito"},
            )
            assert r.status_code == 200, r.text

            with pytest.raises(DBAPIError) as raised:
                await conn.execute(
                    text(
                        "UPDATE threads SET status = 'resolved' "
                        "WHERE project = :p AND thread_id = 'T-a'"
                    ),
                    {"p": P},
                )
        finally:
            await trans.rollback()

    assert "could not serialize access" in str(raised.value), raised.value
    assert await _thread_status(session_factory, "T-a") == "active"
    mark = await _mark(session_factory)
    assert mark is not None and mark[0] == "T-a"
    assert await _history(session_factory) == [("set", "T-a", "takahito")]
