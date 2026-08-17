"""Invariant 7 end-to-end: `next_participant='none'` requires closing the thread.

Covers all three write routes, because they do not share one code path:
``POST /messages`` and ``/close`` both go through ``post_message_in_session``,
but ``POST /threads`` (open_thread) builds its propose msg itself — which is
how a rule can be enforced on two routes and silently absent on the third.

The last test bypasses the API entirely and inserts through the ORM. That one
is not redundant with the others: they prove the pre-write assert produces a
409 a caller can act on, and it proves the state is *unrepresentable* even for
a writer that never asks. ``messages`` is append-only, so a row written wrong
cannot be repaired — the second layer is what makes that acceptable.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.models import Message


async def _open(
    client: AsyncClient,
    thread_id: str = "T-1",
    owner: str = "alice",
    **extra: object,
) -> None:
    r = await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": thread_id,
            "title": "t",
            "owner": owner,
            "propose_content": "start",
            **extra,
        },
    )
    assert r.status_code == 201, r.text


async def _msg_count(client: AsyncClient, thread_id: str = "T-1") -> int:
    r = await client.get(f"/v1/projects/p/threads/{thread_id}")
    assert r.status_code == 200, r.text
    return len(r.json()["messages"])


# --- POST /messages -------------------------------------------------------


async def test_none_with_close_is_accepted_and_read_back(client: AsyncClient) -> None:
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={
            "type": "decide",
            "author": "alice",
            "content": "settled",
            "closes_thread": "T-1",
            "next_participant": "none",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["msg"]["next_participant"] == "none"

    # Read back through a second route: the value is stored, not just echoed.
    view = await client.get("/v1/projects/p/threads/T-1")
    assert view.json()["thread"]["status"] == "resolved"
    assert view.json()["messages"][-1]["next_participant"] == "none"


async def test_none_without_close_is_refused_and_writes_nothing(
    client: AsyncClient,
) -> None:
    await _open(client)
    before = await _msg_count(client)

    r = await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={
            "type": "report",
            "author": "alice",
            "content": "just recording a measurement",
            "next_participant": "none",
        },
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error_type"] == "ChatroomIntegrityError"

    # The refusal has to leave no trace. A rejected write that still allocated
    # a msg_id, or half-applied a status transition, would be worse than the
    # divergence this invariant exists to prevent.
    assert await _msg_count(client) == before
    view = await client.get("/v1/projects/p/threads/T-1")
    assert view.json()["thread"]["status"] == "active"


@pytest.mark.parametrize("name", ["Heisenberg", "human", "orchestrator"])
async def test_participant_names_need_no_close_and_are_stored_verbatim(
    client: AsyncClient, name: str
) -> None:
    # Conclair does not know who may act; it stores what it was told. 'human'
    # is included deliberately — it is reserved to Magickit's vocabulary, not
    # to the archive, so it gets no special handling here.
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={
            "type": "handoff",
            "author": "alice",
            "content": "over to you",
            "next_participant": name,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["msg"]["next_participant"] == name


async def test_omitted_stays_null(client: AsyncClient) -> None:
    # The pre-existing shape of every caller that has not been updated yet.
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={"type": "report", "author": "alice", "content": "no field supplied"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["msg"]["next_participant"] is None


# --- POST /close ----------------------------------------------------------


async def test_close_shortcut_carries_none(client: AsyncClient) -> None:
    # The canonical case: this route always sets closes_thread, so it is where
    # 'none' is both legal and meant.
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={
            "summary_content": "## Resolution\ndone",
            "author": "alice",
            "next_participant": "none",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["decide_msg"]["next_participant"] == "none"
    assert r.json()["thread"]["status"] == "resolved"


async def test_close_shortcut_does_not_invent_none(client: AsyncClient) -> None:
    # Not defaulted. A recorded 'none' must mean "someone declared no
    # successor", not "someone closed the thread" — otherwise the field stops
    # being a declaration and starts being a restatement of thread.status.
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "done", "author": "alice"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["decide_msg"]["next_participant"] is None


# --- POST /threads (open_thread) -----------------------------------------


async def test_open_thread_refuses_none_and_creates_no_thread(
    client: AsyncClient,
) -> None:
    # A propose msg cannot carry closes_thread, so 'none' can never be true on
    # this route: a thread is not settled by being opened.
    r = await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": "T-new",
            "title": "t",
            "owner": "alice",
            "propose_content": "start",
            "next_participant": "none",
        },
    )
    assert r.status_code == 409, r.text
    assert r.json()["error_type"] == "ChatroomIntegrityError"

    missing = await client.get("/v1/projects/p/threads/T-new")
    assert missing.status_code == 404


async def test_open_thread_stamps_participant_on_propose(client: AsyncClient) -> None:
    await _open(client, thread_id="T-2", next_participant="Einstein")
    view = await client.get("/v1/projects/p/threads/T-2")
    assert view.json()["messages"][0]["next_participant"] == "Einstein"


# --- the layer under the API ---------------------------------------------


async def test_db_check_refuses_none_without_close(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The constraint holds for a writer that never calls the assert."""
    await _open(client)

    db_session.add(
        Message(
            project="p",
            msg_id="msg-900",
            thread_id="T-1",
            author="alice",
            timestamp=datetime.now(timezone.utc),
            type="report",
            content="written around the API",
            closes_thread=None,
            next_participant="none",
        )
    )
    with pytest.raises(IntegrityError) as ei:
        await db_session.flush()
    assert "messages_next_participant_close_check" in str(ei.value)


async def test_db_check_allows_none_with_close(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Same route in, valid row: proves the constraint is not simply rejecting
    # every 'none', which is what a mis-specified predicate would also do.
    await _open(client)

    db_session.add(
        Message(
            project="p",
            msg_id="msg-901",
            thread_id="T-1",
            author="alice",
            timestamp=datetime.now(timezone.utc),
            type="decide",
            content="written around the API, but closing",
            closes_thread="T-1",
            next_participant="none",
        )
    )
    await db_session.flush()
