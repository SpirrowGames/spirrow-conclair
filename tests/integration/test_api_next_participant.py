"""Invariant 7 end-to-end: a msg that closes its thread names no successor.

Covers both write paths that can close a thread — ``POST /messages`` with
``closes_thread`` set, and the ``/close`` shortcut — plus ``POST /threads``,
where the rule has nothing to say (a propose msg cannot close) and the field
must therefore pass through untouched.

Several tests pin the *absence* of a sentinel. "Nobody is next" has exactly one
encoding, closing the thread, and the literal string ``'none'`` is a
participant name like any other here. An earlier draft reserved it; tied to a
close it could say nothing ``closes_thread`` did not already say. (Tier B
naysayer, PR #13.)

The last two tests bypass the API and insert through the ORM. They are not
redundant with the rest: those prove the pre-write assert produces a 409 a
caller can act on, and these prove the state is *unrepresentable* even for a
writer that never asks. ``messages`` is append-only, so a row written wrong
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


async def test_handoff_names_a_successor(client: AsyncClient) -> None:
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={
            "type": "handoff",
            "author": "alice",
            "content": "over to you",
            "next_participant": "Heisenberg",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["msg"]["next_participant"] == "Heisenberg"

    # Read back through a second route: stored, not merely echoed.
    view = await client.get("/v1/projects/p/threads/T-1")
    assert view.json()["messages"][-1]["next_participant"] == "Heisenberg"


async def test_closing_decide_leaves_the_field_null(client: AsyncClient) -> None:
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={
            "type": "decide",
            "author": "alice",
            "content": "settled",
            "closes_thread": "T-1",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["msg"]["next_participant"] is None
    assert r.json()["thread_status_changed_to"] == "resolved"


async def test_close_with_successor_is_refused_and_writes_nothing(
    client: AsyncClient,
) -> None:
    await _open(client)
    before = await _msg_count(client)

    r = await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={
            "type": "decide",
            "author": "alice",
            "content": "settled, but Heisenberg is up?",
            "closes_thread": "T-1",
            "next_participant": "Heisenberg",
        },
    )
    assert r.status_code == 409, r.text
    assert r.json()["error_type"] == "ChatroomIntegrityError"

    # The refusal has to leave no trace. A rejected write that still allocated
    # a msg_id, or half-applied the resolve transition, would be worse than the
    # contradiction this invariant exists to prevent.
    assert await _msg_count(client) == before
    view = await client.get("/v1/projects/p/threads/T-1")
    assert view.json()["thread"]["status"] == "active"
    assert view.json()["thread"]["resolved_by_msg"] is None


async def test_omitted_stays_null(client: AsyncClient) -> None:
    # The pre-existing shape of every caller that has not been updated yet.
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={"type": "report", "author": "alice", "content": "no field supplied"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["msg"]["next_participant"] is None


@pytest.mark.parametrize("name", ["none", "human", "orchestrator"])
async def test_no_string_is_reserved_on_an_open_thread(
    client: AsyncClient, name: str
) -> None:
    # 'none' included: to Conclair it is a name like any other, stored verbatim
    # and never interpreted. Whether it is a *legal* name is Magickit's
    # question — answering it needs the identity record Conclair must not read.
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={
            "type": "handoff",
            "author": "alice",
            "content": "x",
            "next_participant": name,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["msg"]["next_participant"] == name


async def test_none_on_a_closing_msg_is_refused_like_any_name(
    client: AsyncClient,
) -> None:
    # The counterpart of the test above, and the reason the sentinel was
    # dropped: 'none' gets no exemption, because the rule is about the field
    # being set at all rather than about which string it holds.
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
    assert r.status_code == 409, r.text


# --- POST /close ----------------------------------------------------------


async def test_close_shortcut_records_null(client: AsyncClient) -> None:
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "## Resolution\ndone", "author": "alice"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["decide_msg"]["next_participant"] is None
    assert r.json()["thread"]["status"] == "resolved"


async def test_close_shortcut_refuses_a_successor(client: AsyncClient) -> None:
    # The field is accepted by the schema only so this answer is a 409 rather
    # than a silent discard: a caller supplying it has misunderstood closing.
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={
            "summary_content": "done",
            "author": "alice",
            "next_participant": "Bohr",
        },
    )
    assert r.status_code == 409, r.text
    view = await client.get("/v1/projects/p/threads/T-1")
    assert view.json()["thread"]["status"] == "active"


# --- POST /threads (open_thread) -----------------------------------------


async def test_open_thread_stamps_participant_on_propose(client: AsyncClient) -> None:
    # A proposal has an addressee like any other handoff.
    await _open(client, thread_id="T-2", next_participant="Einstein")
    view = await client.get("/v1/projects/p/threads/T-2")
    assert view.json()["messages"][0]["next_participant"] == "Einstein"


async def test_open_thread_does_not_constrain_the_field(client: AsyncClient) -> None:
    # Invariant 7 has nothing to say here -- a propose msg cannot close its
    # thread -- so even 'none' passes through as the plain string it is.
    await _open(client, thread_id="T-3", next_participant="none")
    view = await client.get("/v1/projects/p/threads/T-3")
    assert view.json()["messages"][0]["next_participant"] == "none"


# --- the layer under the API ---------------------------------------------


async def test_db_check_refuses_successor_on_a_closing_row(
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
            type="decide",
            content="written around the API",
            closes_thread="T-1",
            next_participant="Heisenberg",
        )
    )
    with pytest.raises(IntegrityError) as ei:
        await db_session.flush()
    assert "messages_next_participant_close_check" in str(ei.value)


async def test_db_check_allows_a_closing_row_with_no_successor(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Same route in, valid row: proves the constraint is not simply rejecting
    # every row that closes, which a mis-specified predicate would also do.
    await _open(client)

    db_session.add(
        Message(
            project="p",
            msg_id="msg-901",
            thread_id="T-1",
            author="alice",
            timestamp=datetime.now(timezone.utc),
            type="decide",
            content="written around the API, closing cleanly",
            closes_thread="T-1",
            next_participant=None,
        )
    )
    await db_session.flush()
