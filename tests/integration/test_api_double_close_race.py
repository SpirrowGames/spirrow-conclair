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


async def test_parallel_handoff_vs_close_close_first_refuses_the_handoff(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First: handoff (paused). Second: close (runs first). Release handoff.

    Ordering seen: close wins and commits (active -> resolved). Handoff
    then refreshes, reads ``status='resolved'``, and
    ``assert_thread_writable`` (msg-406 §5.1) refuses the write with
    ``ChatroomStateError`` -> 409. This is the TOCTOU pin Einstein asked
    for (msg-405): the handoff request's initial read saw ``active``, but
    the read that decides is the one under the row lock, and it sees the
    committed close.

    This test **replaces** the earlier form of the same fixture, which
    used to pin ``handoff appended, thread still resolved, no inconsistent
    row`` -- the "未測定 3" behaviour Bohr's msg-348 §3 called out and
    msg-406 §5.1 refused. The refusal target is *any* type, not only
    ``decide``: refusing decide alone would leave open the two-msg
    variant of invariant 7's paired state (msg-406 §2).
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
    assert resp_handoff.status_code == 409, resp_handoff.text
    body_handoff = resp_handoff.json()
    assert body_handoff["error_type"] == "ChatroomStateError"
    assert "resolved" in body_handoff["error"]
    # Machine-readable pointer to where the decision was recorded, so the
    # refused client (typically an agent) can open a new thread and
    # reference this one instead of retrying blindly.
    assert body_handoff["details"]["thread_id"] == "T-1"
    assert body_handoff["details"]["status"] == "resolved"
    assert body_handoff["details"]["resolved_by_msg"] is not None

    r = await client.get("/v1/projects/p/threads/T-1?mode=full")
    body = r.json()
    assert body["thread"]["status"] == "resolved"
    closing = [m for m in body["messages"] if m.get("closes_thread")]
    assert len(closing) == 1
    assert body["thread"]["resolved_by_msg"] == closing[0]["msg_id"]
    # The refused handoff wrote nothing. The propose + the close's decide
    # are the only rows.
    assert [m["type"] for m in body["messages"]] == ["propose", "decide"]

    audit = await client.get("/v1/projects/p/integrity")
    types = {i["type"] for i in audit.json()["issues"]}
    assert "inconsistent_resolved" not in types


# ---------------------------------------------------------------------------
# 受入 4: sibling threads must not serialise on each other. A held FOR
# UPDATE lock on T-A's row must not block a write on T-B. If the lock
# had been on the ``threads`` table as a whole (LOCK TABLE), or on a
# broader partition than the row PK, the T-B write would block until
# the T-A lock is released.
#
# Using the barrier fixture above would *not* prove this: the barrier
# pauses the first entrant *before* the refresh call, so the paused
# task holds no DB lock at all, and the second task sails through
# regardless of what the fix's lock scope is. Even a hypothetical
# table-wide LOCK TABLE would pass a barrier-before-refresh test,
# because the barrier's paused task has not yet reached the LOCK
# statement. So this test opens a raw session and takes the same
# per-row FOR UPDATE the fix takes, held for the duration of the T-B
# request; that is the setup a scope regression could actually fail
# under. ``asyncio.wait_for`` distinguishes "T-B completed" from "T-B
# blocked waiting on T-A's lock" -- a per-row fix completes in
# milliseconds; a per-table lock would keep T-B waiting until the
# outer session releases at commit.
# ---------------------------------------------------------------------------


async def test_row_lock_on_one_thread_does_not_block_writes_to_another(
    client: AsyncClient,
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    from sqlalchemy import text

    await _open(client, "T-A")
    await _open(client, "T-B")

    async with session_factory() as blocker:
        async with blocker.begin():
            # Take the same lock the production fix takes, on T-A's row.
            # A ``SELECT ... FOR UPDATE`` here holds until the enclosing
            # ``begin()`` block commits, which we do at scope exit.
            row = (
                await blocker.execute(
                    text(
                        "SELECT thread_id FROM threads "
                        "WHERE project = :p AND thread_id = :t FOR UPDATE"
                    ),
                    {"p": "p", "t": "T-A"},
                )
            ).scalar_one()
            assert row == "T-A"

            # Meanwhile, a write on T-B must complete without waiting on
            # the T-A row lock. Timeout is generous enough that a slow CI
            # runner does not false-positive (a per-row fix answers in
            # milliseconds), but short enough that a scope regression
            # surfaces as a test failure rather than a hung suite.
            resp_b = await asyncio.wait_for(
                client.post(
                    "/v1/projects/p/threads/T-B/messages",
                    json={"type": "report", "author": "alice", "content": "b"},
                ),
                timeout=5.0,
            )
            assert resp_b.status_code == 201, resp_b.text
        # blocker commits and releases the T-A lock here.

    # Sanity: a subsequent T-A write is not stuck (i.e. the blocker's lock
    # really did release, and no ambient state was left behind).
    resp_a = await client.post(
        "/v1/projects/p/threads/T-A/messages",
        json={"type": "report", "author": "alice", "content": "a"},
    )
    assert resp_a.status_code == 201, resp_a.text


# ---------------------------------------------------------------------------
# Bohr msg-409 §2.2 discrimination pin: the barrier-based tests above do
# NOT discriminate for the row lock. At READ COMMITTED with the barrier
# placed BEFORE the refresh (which it must be, or the runner deadlocks
# per the module docstring), task-A pauses without holding a lock,
# task-B refreshes and commits, and task-A's post-release refresh reads
# task-B's committed state via READ COMMITTED whether or not that
# refresh acquires a lock. So the state-machine outcome is the same
# with and without ``with_for_update=True``, and those tests pin
# state-machine correctness (``assert_thread_writable`` + the transition
# table), not the lock's serialization value.
#
# The lock's serialization value is what this test pins: hold a
# ``SELECT ... FOR UPDATE`` on T-A's row from a raw session, then issue
# a write to T-A via the API and confirm the API request blocks (times
# out under ``asyncio.wait_for``). Together with
# ``test_row_lock_on_one_thread_does_not_block_writes_to_another``, this
# pins the two orthogonal properties of the fix:
#
#     - lock is per-ROW (sibling-thread test above completes fast)
#     - lock SERIALISES on that row (this test blocks and times out)
#
# Bohr's original ask was a repeated experiment ("N of M times remove
# the kwarg and re-run the barrier test") reported to the thread. That
# form was chosen when we still believed the barrier test discriminated;
# the analysis in msg-455's context showed it does not, so a repeated
# ad-hoc measurement would report 0/N and teach us nothing. This test
# is the standing pin for the property Bohr wanted evidence of, and it
# runs on every CI push.
# ---------------------------------------------------------------------------


async def test_row_lock_serialises_two_writes_on_the_same_thread(
    client: AsyncClient,
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    """A held ``SELECT ... FOR UPDATE`` on T-A's row blocks a concurrent
    write to T-A. Mirror of the sibling-thread test above; together the
    two pin lock scope AND lock serialization.

    Sequence:
    1. Open T-A.
    2. Open a raw session, ``BEGIN``, ``SELECT ... FOR UPDATE`` on T-A's
       row. Do not commit yet.
    3. Fire an HTTP write to T-A. The write's ``session.refresh(...,
       with_for_update=True)`` inside ``post_message_in_session`` must
       queue behind the raw session's lock -- which is held.
    4. ``asyncio.wait_for`` times out because the write is blocked.
    5. On timeout, we cancel the pending write task, release the raw
       session (rollback), and confirm a subsequent write to T-A
       completes fast (sanity: the lock really did release).

    Failing verdict (regression -- the production ``refresh`` is no
    longer a locking one): the HTTP write completes before the timeout
    because ``refresh`` did not queue. That is exactly the failure mode
    Bohr msg-409 §2.2 wanted evidence of, and this test would flip red
    on that regression on every CI push.

    Note on cancellation: ``asyncio.wait_for`` cancels the wrapped task
    on timeout, which is enough because ``httpx.AsyncClient`` releases
    its connection and the ASGI request coroutine is aborted. The raw
    session's lock releases at scope exit.
    """
    from sqlalchemy import text

    await _open(client, "T-A")

    async with session_factory() as blocker:
        async with blocker.begin():
            row = (
                await blocker.execute(
                    text(
                        "SELECT thread_id FROM threads "
                        "WHERE project = :p AND thread_id = :t FOR UPDATE"
                    ),
                    {"p": "p", "t": "T-A"},
                )
            ).scalar_one()
            assert row == "T-A"

            # Write to T-A must block on the raw session's lock. Timeout
            # short enough to fail the test if the lock is not doing its
            # job (a regression would answer in milliseconds), long enough
            # that the "did it queue?" signal is unambiguous on slow CI.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    client.post(
                        "/v1/projects/p/threads/T-A/messages",
                        json={
                            "type": "report",
                            "author": "alice",
                            "content": "queued",
                        },
                    ),
                    timeout=2.0,
                )
        # blocker rolls back and releases the T-A lock here (no commit
        # because the ``begin()`` scope is exiting via exception -- the
        # inner ``pytest.raises`` catches the TimeoutError, so control
        # exits ``blocker.begin()`` normally; the row lock is released
        # at commit time in either case).

    # Sanity: the lock really did release; a subsequent T-A write is fast.
    resp_a = await asyncio.wait_for(
        client.post(
            "/v1/projects/p/threads/T-A/messages",
            json={"type": "report", "author": "alice", "content": "after"},
        ),
        timeout=5.0,
    )
    assert resp_a.status_code == 201, resp_a.text


# ---------------------------------------------------------------------------
# TOCTOU defense: the caller-level ``assert_owner_can_close`` in the
# ``/close`` route runs on the stale, unlocked ``thread`` read, so a
# concurrent ownership mutation between that check and the row-lock
# refresh could otherwise slip through the caller-level gate. The
# invariant that a non-owner cannot close is not enforced only by the
# early 403 gate; ``assert_closes_thread_rule`` runs again inside
# ``post_message_in_session`` *after* the ``session.refresh(...,
# with_for_update=True)``, and *that* check is the load-bearing one.
#
# ``thread.owner`` has no API-surface mutation today (verified by grep:
# it is written only at ``open_thread``; no route or service writes it
# afterwards), so this TOCTOU window has no live exploit path. This
# test simulates the missing mutation via a raw ``UPDATE threads SET
# owner`` while the close request is paused on the barrier -- the same
# window a hypothetical future owner-transfer endpoint would open --
# and pins that the close is *rejected*, not that it *succeeds*.
# ---------------------------------------------------------------------------


async def test_owner_change_between_caller_check_and_refresh_is_rejected(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    """The pre-refresh ``assert_owner_can_close`` is UX (403) only; the
    load-bearing check is ``assert_closes_thread_rule`` under the lock.

    Sequence, held deterministic by the barrier:
    1. Alice opens T-1 (owner=alice).
    2. Alice sends /close. In the route: ``assert_owner_can_close`` runs
       on the stale read and passes (author=alice, owner=alice).
    3. Alice enters ``post_message_in_session`` and pauses on the
       barrier -- above the refresh, no lock held yet.
    4. A raw session UPDATEs ``threads.owner`` to 'bob' and commits.
    5. The barrier releases Alice. Her refresh reads owner='bob'.
    6. ``assert_closes_thread_rule`` fires: author=alice != owner=bob,
       and ``owner_override`` is not set -- raises
       ``ChatroomIntegrityError`` -> 409.

    Passing verdict: response is 409 (not 201), no decide msg is
    appended, thread stays active. The auth is *re-checked* under the
    lock and the close is *rejected*, so the TOCTOU window closes with
    a domain error rather than a bypass.

    Failing verdict (regression): response is 201, a decide msg is
    written for a thread the caller no longer owns. That is the
    scenario the reviewer flagged as ``security`` on PR #19; this test
    turns "the fix survives that scenario" into a runnable assertion.
    """
    from sqlalchemy import text

    await _open(client, "T-1", owner="alice")
    barrier = _install_barrier(monkeypatch)

    async def close_as_alice() -> Response:
        return await client.post(
            "/v1/projects/p/threads/T-1/close",
            json={"summary_content": "close by alice", "author": "alice"},
        )

    task = asyncio.create_task(close_as_alice())
    await barrier.first_entered.wait()

    # While Alice is paused, mutate ownership from a raw session --
    # simulating a concurrent transfer that the caller-level check
    # cannot see.
    async with session_factory() as mutator:
        async with mutator.begin():
            await mutator.execute(
                text(
                    "UPDATE threads SET owner = :new_owner "
                    "WHERE project = :p AND thread_id = :t"
                ),
                {"new_owner": "bob", "p": "p", "t": "T-1"},
            )

    barrier.release()
    resp = await task

    # Under-the-lock re-check rejects the close.
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_type"] == "ChatroomIntegrityError"
    assert "owner" in body["error"].lower()

    # No decide msg landed; thread stayed active.
    r = await client.get("/v1/projects/p/threads/T-1?mode=full")
    view = r.json()
    assert view["thread"]["status"] == "active", view["thread"]
    assert view["thread"]["resolved_by_msg"] is None
    assert all(m["type"] != "decide" for m in view["messages"]), view["messages"]

    # Audit stays green: no inconsistent_resolved, and the mutated owner
    # is not reported as an issue by itself (owner mutation is not an
    # invariant this audit tracks).
    audit = await client.get("/v1/projects/p/integrity")
    types = {i["type"] for i in audit.json()["issues"]}
    assert "inconsistent_resolved" not in types


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
