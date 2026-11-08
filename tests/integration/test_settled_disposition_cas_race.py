"""The settled_disposition head comparison is a compare-and-set, under contention.

`test_api_close_sanction.py` pins the comparison *sequentially*: the reply has
committed before the close starts, so the close would read the new head with
or without a lock. That does not exercise the property the sanction rests on
(PR-gate on SpirrowGames/spirrow-conclair#34 @ fdcaacb, objection class
`untested`): a close that is *waiting* while a reply is being written must,
once the reply commits, compare against the head the reply produced -- not
against the head it saw before it started waiting.

So these tests run two real transactions on two connections and interleave
them with a lock probe, not a timer:

- The transaction that must win is opened on its own session and driven
  through the real choke point, `post_message_in_session`, then held open.
  At that point it holds the thread row (its `refresh(with_for_update=True)`
  and its `last_msg_num` UPDATE) and the project advisory lock.
- The other party is sent through the HTTP route as a task.
- A third session asks Postgres whether that request's backend is blocked by
  the holder's pid (`pg_blocking_pids`), and on which wait event. Only once
  Postgres says so does the holder commit.

Why the probe asserts the wait event and the statement, too: without
`with_for_update`, the close's refresh would not wait at all. It would read
the committed (old) head, pass the comparison, and only then block -- on the
advisory lock in `allocate_next_msg_id` (wait event `advisory`). Then it would
write a close on top of the reply. The probe therefore requires the waiter to
be in a row-lock wait (`transactionid` / `tuple`) on a `FOR UPDATE`
statement. That is the wait the compare-and-set depends on. If the lock is
dropped, this test fails at the probe with a message naming the wait it saw,
and fails again at the 422 assertion.

Deadlock note: unlike the barrier tests in `test_api_double_close_race.py`,
the holder here is not a task waiting on the other task -- it is this test
body, which releases by committing. The waiter blocks inside Postgres, not
inside the event loop, so nothing is circular.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from spirrow_conclair.api.messages import post_message_in_session
from spirrow_conclair.schemas.message import CloseSanction
from spirrow_conclair.services.integrity import fetch_thread_or_raise
from spirrow_conclair.services.msg_id_allocator import parse_msg_id

CUTOVER = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

#: Upper bound on how long the probe waits for Postgres to report the waiter.
#: It is a failure deadline, not a synchronisation point: the interleaving is
#: decided by the probe's answer, and on a healthy runner the answer arrives
#: within a few polls.
PROBE_DEADLINE_SEC = 15.0

#: Row-lock waits. A `SELECT ... FOR UPDATE` on a row another transaction has
#: updated waits on that transaction's id. `tuple` is the wait a second
#: waiter queues on behind the first.
ROW_LOCK_WAIT_EVENTS = frozenset({"transactionid", "tuple"})


@pytest.fixture(autouse=True)
def _cutover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same recorder cutover as `test_api_close_sanction.py`, so the audit's
    sanctioned bucket is the one these closes land in."""
    monkeypatch.setenv("SANCTION_RECORDING_SINCE", CUTOVER.isoformat())


async def _open(client: AsyncClient, thread_id: str) -> None:
    r = await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": thread_id,
            "title": "t",
            "owner": "orchestrator",
            "propose_content": "start",
        },
    )
    assert r.status_code == 201, r.text


async def _post_done(client: AsyncClient, thread_id: str) -> str:
    r = await client.post(
        f"/v1/projects/p/threads/{thread_id}/messages",
        json={
            "type": "report",
            "author": "Bohr",
            "role": "proposer",
            "content": "settled",
            "disposition": {"kind": "done"},
        },
    )
    assert r.status_code == 201, r.text
    msg_id: str = r.json()["msg"]["msg_id"]
    return msg_id


def _settled_close_payload(head_msg_id: str) -> dict[str, Any]:
    return {
        "summary_content": f"settled_close: carried out the done at {head_msg_id}",
        "author": "settled-closer",
        "owner_override": True,
        "close_sanction": {"kind": "settled_disposition", "head_msg_id": head_msg_id},
    }


async def _backend_pid(session: AsyncSession) -> int:
    pid: int = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    return pid


async def _wait_until_blocked_by(
    session_factory: async_sessionmaker[AsyncSession],
    holder_pid: int,
    waiter: asyncio.Task[Response],
) -> dict[str, Any]:
    """Return the backend Postgres reports as blocked by `holder_pid`.

    Polls `pg_stat_activity` from a third connection. The short sleep between
    polls only yields the loop so the waiter's request can make progress; the
    decision to proceed is Postgres's answer, not elapsed time. If the waiter
    finishes instead of blocking, that is the failure, and its response is
    put in the message.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PROBE_DEADLINE_SEC
    async with session_factory() as probe:
        while True:
            if waiter.done():
                resp = waiter.result()
                pytest.fail(
                    "the request finished without ever waiting on the holder's "
                    f"lock: {resp.status_code} {resp.text}"
                )
            rows = (
                (
                    await probe.execute(
                        text(
                            "SELECT pid, wait_event_type, wait_event, query "
                            "FROM pg_stat_activity "
                            "WHERE :holder = ANY(pg_blocking_pids(pid))"
                        ),
                        {"holder": holder_pid},
                    )
                )
                .mappings()
                .all()
            )
            # Each poll is its own snapshot of pg_stat_activity.
            await probe.rollback()
            if rows:
                (row,) = rows
                return dict(row)
            if loop.time() > deadline:
                pytest.fail(
                    f"no backend reported as blocked by pid {holder_pid} within "
                    f"{PROBE_DEADLINE_SEC}s"
                )
            await asyncio.sleep(0.01)


def _assert_row_lock_wait(blocked: dict[str, Any]) -> None:
    assert blocked["wait_event_type"] == "Lock", blocked
    assert blocked["wait_event"] in ROW_LOCK_WAIT_EVENTS, (
        "the waiter is blocked, but not on the thread row: the deciding read "
        f"did not wait for the row lock ({blocked['wait_event']!r})",
        blocked,
    )
    assert "FOR UPDATE" in blocked["query"].upper(), blocked


async def _thread_view(client: AsyncClient, thread_id: str) -> dict[str, Any]:
    r = await client.get(f"/v1/projects/p/threads/{thread_id}?mode=full")
    assert r.status_code == 200, r.text
    view: dict[str, Any] = r.json()
    return view


async def test_a_close_waiting_behind_an_uncommitted_reply_sees_head_moved(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reply holds the row -> close starts and blocks -> reply commits ->
    close compares against the reply's head and is refused with head_moved.
    """
    await _open(client, "T-1")
    head = await _post_done(client, "T-1")

    async with session_factory() as reply_session:
        async with reply_session.begin():
            holder_pid = await _backend_pid(reply_session)
            thread = await fetch_thread_or_raise(reply_session, project="p", thread_id="T-1")
            reply, _ = await post_message_in_session(
                reply_session,
                project="p",
                thread=thread,
                msg_type="report",
                author="Einstein",
                role="naysayer",
                content="objection to the settlement",
            )

            close_task = asyncio.create_task(
                client.post(
                    "/v1/projects/p/threads/T-1/close",
                    json=_settled_close_payload(head),
                )
            )
            blocked = await _wait_until_blocked_by(session_factory, holder_pid, close_task)
            _assert_row_lock_wait(blocked)
            # Still blocked: nothing has been decided yet.
            assert not close_task.done()
        # The reply commits here, releasing the row.

    resp = await asyncio.wait_for(close_task, timeout=PROBE_DEADLINE_SEC)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error_type"] == "ChatroomUnprocessableError"
    assert body["details"]["reason"] == "head_moved"
    assert body["details"]["head_msg_id"] == head
    # The head it compared against is the committed reply's, i.e. the refresh
    # read the row version written while it waited.
    assert body["details"]["last_msg_num"] == parse_msg_id(reply.msg_id)

    view = await _thread_view(client, "T-1")
    assert view["thread"]["status"] != "resolved", view["thread"]
    assert view["thread"]["resolved_by_msg"] is None
    assert [m["msg_id"] for m in view["messages"]][-2:] == [head, reply.msg_id]
    assert all(not m.get("closes_thread") for m in view["messages"])

    audit = (await client.get("/v1/projects/p/integrity")).json()
    assert audit["sanctioned_counts"]["settled_disposition"] == 0
    assert audit["issue_count"] == 0, audit["issues"]


async def test_a_reply_waiting_behind_an_uncommitted_close_is_refused(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The opposite order. Close holds the row -> reply starts and blocks ->
    close commits -> the reply re-reads the row, finds it resolved, and is
    refused (409) -- the reply lands after the close as a refusal, not as a
    msg appended to a resolved thread.
    """
    await _open(client, "T-1")
    head = await _post_done(client, "T-1")

    async with session_factory() as close_session:
        async with close_session.begin():
            holder_pid = await _backend_pid(close_session)
            thread = await fetch_thread_or_raise(close_session, project="p", thread_id="T-1")
            close_msg, transition = await post_message_in_session(
                close_session,
                project="p",
                thread=thread,
                msg_type="decide",
                author="settled-closer",
                content=f"settled_close: carried out the done at {head}",
                closes_thread="T-1",
                owner_override=True,
                close_sanction=CloseSanction(kind="settled_disposition", head_msg_id=head),
            )
            # The comparison ran under the lock and passed: head is current.
            assert transition == "resolved"

            reply_task = asyncio.create_task(
                client.post(
                    "/v1/projects/p/threads/T-1/messages",
                    json={
                        "type": "report",
                        "author": "Einstein",
                        "role": "naysayer",
                        "content": "objection, too late",
                    },
                )
            )
            blocked = await _wait_until_blocked_by(session_factory, holder_pid, reply_task)
            _assert_row_lock_wait(blocked)
            assert not reply_task.done()
        # The close commits here.

    resp = await asyncio.wait_for(reply_task, timeout=PROBE_DEADLINE_SEC)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_type"] == "ChatroomThreadResolvedError"
    assert body["details"]["resolved_by_msg"] == close_msg.msg_id

    view = await _thread_view(client, "T-1")
    assert view["thread"]["status"] == "resolved"
    assert view["thread"]["resolved_by_msg"] == close_msg.msg_id
    # Nothing landed after the close.
    assert [m["msg_id"] for m in view["messages"]][-2:] == [head, close_msg.msg_id]

    audit = (await client.get("/v1/projects/p/integrity")).json()
    assert audit["sanctioned_counts"]["settled_disposition"] == 1
    assert audit["issue_count"] == 0, audit["issues"]
    assert "inconsistent_resolved" not in {i["type"] for i in audit["issues"]}
