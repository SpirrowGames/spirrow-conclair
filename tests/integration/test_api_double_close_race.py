"""Concurrent write races on one thread must not produce two closing msgs
or leave the row half-transitioned.

`post_message_in_session` holds every write path a msg-into-existing-thread
takes (`POST .../messages` and `POST .../close`; `open_thread` builds a
fresh row and has no competitor). Before the row-level lock was added,
two concurrent close requests to the same active thread would both see
`status='active'` in memory, both compute the transition, and both write
a closing msg -- and the more insidious case, close × handoff in
parallel, would let the handoff's `UPDATE` land after the close's,
producing `status='awaiting_reply'` alongside `resolved_by_msg` set, an
`inconsistent_resolved` state `messages` (append-only) cannot repair.

These tests pin the fix from both sides: the write route must serialise
against another writer on the same thread, but writers against
*different* threads must still proceed independently. They are
interleaved deterministically -- one task is held on an asyncio barrier
placed *at the entry to* `post_message_in_session`, before any DB lock
is acquired, so a paused task holds no row lock and a second task can
sail through without a test-runner deadlock. Seams past the lock would
livelock the runner: the paused task would already hold the row, and
the second task's `refresh` would block on it, waiting for a release
the runner never issues.

The concurrency comes from `asyncio.create_task` + `AsyncClient` +
`ASGITransport`, which delivers requests to the app in-process against
a real Postgres via `testcontainers`. Each request opens its own
transaction on its own connection from the SQLAlchemy pool, so the
race is real (same as `test_concurrent_msg_id_allocation`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from httpx import AsyncClient, Response

from spirrow_conclair.api import messages as messages_mod
from spirrow_conclair.api import threads as threads_mod

# ---------------------------------------------------------------------------
# Barrier: pause the first entrant to post_message_in_session, let the
# second sail through to commit, then release the first. Placed BEFORE the
# refresh() call, so a paused task holds no DB lock -- see module docstring.
# ---------------------------------------------------------------------------


@dataclass
class _Barrier:
    """One-shot barrier: first entrant blocks until `release()`; others pass."""

    first_entered: asyncio.Event
    release_first: asyncio.Event
    entry_count: int = 0

    def release(self) -> None:
        self.release_first.set()


def _install_barrier(monkeypatch: pytest.MonkeyPatch) -> _Barrier:
    """Wrap `post_message_in_session` at BOTH call sites (threads.py imports it
    by name, so the module-local binding is what routes look up).

    Wrapping the module-level function -- not an inner call it makes -- keeps
    the pause point above `session.refresh(with_for_update=True)`, which is
    the load-bearing property of this test type: no DB lock is held while a
    task waits on `release_first`, so a second concurrent task can complete
    its own transaction and commit. If the pause were below the refresh, the
    second task's own refresh would block on the paused task's row lock and
    the runner would deadlock (there is no third actor to release the first).
    """
    real = messages_mod.post_message_in_session

    barrier = _Barrier(
        first_entered=asyncio.Event(), release_first=asyncio.Event()
    )

    async def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
        barrier.entry_count += 1
        if barrier.entry_count == 1:
            barrier.first_entered.set()
            await barrier.release_first.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(messages_mod, "post_message_in_session", wrapped)
    monkeypatch.setattr(threads_mod, "post_message_in_session", wrapped)
    return barrier


async def _open(
    client: AsyncClient, thread_id: str, *, project: str = "p", owner: str = "alice"
) -> None:
    r = await client.post(
        f"/v1/projects/{project}/threads",
        json={
            "thread_id": thread_id,
            "title": "t",
            "owner": owner,
            "propose_content": "start",
        },
    )
    assert r.status_code == 201, r.text


async def _run_interleaved(
    barrier: _Barrier,
    first: Callable[[], Awaitable[Response]],
    second: Callable[[], Awaitable[Response]],
) -> tuple[Response, Response]:
    """Kick off `first`, wait until it is paused inside the barrier, run
    `second` end to end, then release `first` and collect both responses.

    Deterministic: the ordering `second wins, first sees the committed
    result` is what the test asserts against, so it must not depend on the
    scheduler's fairness.
    """
    task_first = asyncio.create_task(first())
    await barrier.first_entered.wait()
    resp_second = await second()
    barrier.release()
    resp_first = await task_first
    return resp_first, resp_second


# ---------------------------------------------------------------------------
# 受入 1: parallel close × close -> exactly one closing msg, second is
# refused by the existing ChatroomStateError (no new error type added).
# ---------------------------------------------------------------------------


async def test_parallel_close_close_writes_exactly_one_closing_msg(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _open(client, "T-1")
    barrier = _install_barrier(monkeypatch)

    async def close(tag: str) -> Response:
        return await client.post(
            "/v1/projects/p/threads/T-1/close",
            json={"summary_content": f"close-{tag}", "author": "alice"},
        )

    resp_first, resp_second = await _run_interleaved(
        barrier,
        first=lambda: close("first"),
        second=lambda: close("second"),
    )

    # Second entered post_message_in_session after first was paused, so
    # second wins (commits first). The first is released, its refresh
    # reads status='resolved', and compute_transition raises
    # ChatroomStateError -> 409.
    assert resp_second.status_code == 201, resp_second.text
    assert resp_first.status_code == 409, resp_first.text
    body_first = resp_first.json()
    assert body_first["error_type"] == "ChatroomStateError"
    assert "resolved" in body_first["error"]

    # Ground truth: only one closing msg exists in the thread.
    r = await client.get("/v1/projects/p/threads/T-1?mode=full")
    closing = [m for m in r.json()["messages"] if m.get("closes_thread")]
    assert len(closing) == 1
    assert r.json()["thread"]["status"] == "resolved"
    assert r.json()["thread"]["resolved_by_msg"] == closing[0]["msg_id"]

    # The audit must be green: no closes_thread_by_non_owner (both requests
    # were the owner), and specifically no inconsistent_resolved.
    audit = await client.get("/v1/projects/p/integrity")
    assert audit.status_code == 200
    types = {i["type"] for i in audit.json()["issues"]}
    assert "inconsistent_resolved" not in types
    assert "closes_thread_by_non_owner" not in types


# ---------------------------------------------------------------------------
# 受入 2: parallel close × handoff -- both orderings must be linearizable
# and neither must leave `inconsistent_resolved` behind.
# ---------------------------------------------------------------------------


async def test_parallel_close_vs_handoff_close_first_leaves_thread_resolved(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First: close (paused). Second: handoff (runs first). Release close.

    Ordering seen: handoff wins the lock and commits (active ->
    awaiting_reply). Close then refreshes, reads awaiting_reply, and the
    existing transition table permits closing from awaiting_reply too, so
    it succeeds -> resolved.
    """
    await _open(client, "T-1")
    barrier = _install_barrier(monkeypatch)

    async def close() -> Response:
        return await client.post(
            "/v1/projects/p/threads/T-1/close",
            json={"summary_content": "done", "author": "alice"},
        )

    async def handoff() -> Response:
        return await client.post(
            "/v1/projects/p/threads/T-1/messages",
            json={"type": "handoff", "author": "alice", "content": "over"},
        )

    resp_close, resp_handoff = await _run_interleaved(
        barrier, first=close, second=handoff
    )

    assert resp_handoff.status_code == 201, resp_handoff.text
    assert resp_handoff.json()["thread_status_changed_to"] == "awaiting_reply"
    assert resp_close.status_code == 201, resp_close.text

    r = await client.get("/v1/projects/p/threads/T-1?mode=full")
    body = r.json()
    assert body["thread"]["status"] == "resolved"
    # resolved_by_msg is the decide msg, not the handoff.
    closing = [m for m in body["messages"] if m.get("closes_thread")]
    assert len(closing) == 1
    assert body["thread"]["resolved_by_msg"] == closing[0]["msg_id"]

    # Every state transition the write path took is recorded consistently.
    audit = await client.get("/v1/projects/p/integrity")
    types = {i["type"] for i in audit.json()["issues"]}
    assert "inconsistent_resolved" not in types


async def test_parallel_handoff_vs_close_close_first_leaves_thread_resolved(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First: handoff (paused). Second: close (runs first). Release handoff.

    Ordering seen: close wins and commits (active -> resolved). Handoff
    then refreshes, reads resolved, and compute_transition returns
    (None, {}) for handoff-on-resolved (existing behaviour, `status_transition`
    table's "anything else" branch). The handoff msg is appended; the
    thread stays resolved; nothing about `inconsistent_resolved` fires.

    This test pins the existing behaviour (未測定 3): non-close posts to a
    resolved thread are accepted with no state change. Changing that is
    outside this thread's scope.
    """
    await _open(client, "T-1")
    barrier = _install_barrier(monkeypatch)

    async def handoff() -> Response:
        return await client.post(
            "/v1/projects/p/threads/T-1/messages",
            json={"type": "handoff", "author": "alice", "content": "over"},
        )

    async def close() -> Response:
        return await client.post(
            "/v1/projects/p/threads/T-1/close",
            json={"summary_content": "done", "author": "alice"},
        )

    resp_handoff, resp_close = await _run_interleaved(
        barrier, first=handoff, second=close
    )

    assert resp_close.status_code == 201, resp_close.text
    # Handoff succeeds but changes nothing (thread is already resolved).
    assert resp_handoff.status_code == 201, resp_handoff.text
    assert resp_handoff.json()["thread_status_changed_to"] is None

    r = await client.get("/v1/projects/p/threads/T-1?mode=full")
    body = r.json()
    assert body["thread"]["status"] == "resolved"
    closing = [m for m in body["messages"] if m.get("closes_thread")]
    assert len(closing) == 1
    assert body["thread"]["resolved_by_msg"] == closing[0]["msg_id"]

    audit = await client.get("/v1/projects/p/integrity")
    types = {i["type"] for i in audit.json()["issues"]}
    assert "inconsistent_resolved" not in types


# ---------------------------------------------------------------------------
# 受入 4: sibling threads must not serialise on each other. The row lock
# is per-thread, so a paused write on T-A must not block a write on T-B.
# The advisory (project) lock already existed and serialises the allocator;
# this fix must not widen that.
# ---------------------------------------------------------------------------


async def test_parallel_writes_on_different_threads_do_not_serialise(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threads, one is paused inside the barrier. A write on the other
    thread must still be able to enter post_message_in_session, take its
    own row lock (a different row), and commit -- proving the added lock
    is per-thread, not per-project.

    ``entry_count`` on the barrier gates only the *first* entrant; the
    second call sails through. If the added row lock had been on the
    threads table as a whole, this test would time out (second call would
    wait on the paused task's lock).
    """
    await _open(client, "T-A")
    await _open(client, "T-B")
    barrier = _install_barrier(monkeypatch)

    async def write_a() -> Response:
        return await client.post(
            "/v1/projects/p/threads/T-A/messages",
            json={"type": "report", "author": "alice", "content": "a"},
        )

    async def write_b() -> Response:
        return await client.post(
            "/v1/projects/p/threads/T-B/messages",
            json={"type": "report", "author": "alice", "content": "b"},
        )

    # First pauses on the barrier; second must complete without waiting on
    # the first's row lock (the row is a different one).
    resp_a, resp_b = await _run_interleaved(
        barrier, first=write_a, second=write_b
    )

    assert resp_a.status_code == 201, resp_a.text
    assert resp_b.status_code == 201, resp_b.text


# ---------------------------------------------------------------------------
# 受入 5 (regression): sequential re-close still returns 409 -- the fix
# must not change any single-writer behaviour. `test_api_close.py` already
# holds `test_re_close_returns_409_state_error`; the redundant assertion
# here is deliberate belt-and-braces so this file, read on its own, shows
# the property the two tests together pin.
# ---------------------------------------------------------------------------


async def test_sequential_re_close_still_returns_409(client: AsyncClient) -> None:
    await _open(client, "T-1")
    r1 = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "first", "author": "alice"},
    )
    assert r1.status_code == 201, r1.text

    r2 = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "second", "author": "alice"},
    )
    assert r2.status_code == 409
    assert r2.json()["error_type"] == "ChatroomStateError"
