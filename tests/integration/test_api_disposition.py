"""Disposition end-to-end: persisted, read back, and invariant 8.

Before migration 0010 Conclair dropped ``disposition`` silently (no field on
``PostMessageRequest``, pydantic's default ``extra='ignore'``). The first
tests pin that it now survives a post *and* a later ``GET``, which is the
read-back Magickit's Slice C compares against.

The last tests insert through the ORM, like ``test_api_next_participant``:
they prove the CHECKs hold for a writer that never calls the assert.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.models import Message

BLOCKED: dict[str, Any] = {
    "kind": "blocked_on",
    "trigger": {"arm": "pr", "ref": "SpirrowGames/spirrow-conclair#32"},
    "wake": "Heisenberg",
}


async def _open(client: AsyncClient, thread_id: str = "T-1", owner: str = "alice") -> None:
    r = await client.post(
        "/v1/projects/p/threads",
        json={"thread_id": thread_id, "title": "t", "owner": owner, "propose_content": "s"},
    )
    assert r.status_code == 201, r.text


async def _messages(client: AsyncClient, thread_id: str = "T-1") -> list[dict[str, Any]]:
    r = await client.get(f"/v1/projects/p/threads/{thread_id}")
    assert r.status_code == 200, r.text
    return list(r.json()["messages"])


async def _post(client: AsyncClient, thread_id: str = "T-1", **body: Any) -> Any:
    return await client.post(
        f"/v1/projects/p/threads/{thread_id}/messages",
        json={"author": "alice", "content": "c", **body},
    )


async def test_blocked_on_is_persisted_and_read_back(client: AsyncClient) -> None:
    await _open(client)
    r = await _post(client, type="handoff", disposition=BLOCKED)
    assert r.status_code == 201, r.text
    assert r.json()["msg"]["disposition"] == {**BLOCKED, "fallback": None}
    assert (await _messages(client))[-1]["disposition"] == {**BLOCKED, "fallback": None}


async def test_fallback_is_persisted(client: AsyncClient) -> None:
    await _open(client)
    value = {
        "kind": "blocked_on",
        "trigger": {"arm": "human", "ref": "T-1"},
        "wake": "Takahito",
        "fallback": {"reason": "owner_unresolved", "owner": "ghost-bot", "declared": "done"},
    }
    r = await _post(client, type="report", disposition=value)
    assert r.status_code == 201, r.text
    assert (await _messages(client))[-1]["disposition"] == value


async def test_done_on_a_non_closing_msg(client: AsyncClient) -> None:
    await _open(client)
    r = await _post(client, type="report", disposition={"kind": "done"})
    assert r.status_code == 201, r.text
    assert (await _messages(client))[-1]["disposition"] == {"kind": "done"}


async def test_omitted_stays_null(client: AsyncClient) -> None:
    await _open(client)
    r = await _post(client, type="report")
    assert r.status_code == 201, r.text
    assert all(m["disposition"] is None for m in await _messages(client))


async def test_unknown_key_is_422_and_writes_nothing(client: AsyncClient) -> None:
    await _open(client)
    before = len(await _messages(client))
    r = await _post(client, type="handoff", disposition={**BLOCKED, "wkae": "typo"})
    assert r.status_code == 422, r.text
    assert len(await _messages(client)) == before


async def test_closing_decide_with_done(client: AsyncClient) -> None:
    await _open(client)
    r = await _post(client, type="decide", closes_thread="T-1", disposition={"kind": "done"})
    assert r.status_code == 201, r.text
    assert (await _messages(client))[-1]["disposition"] == {"kind": "done"}


async def test_closing_decide_with_blocked_on_is_409_and_writes_nothing(
    client: AsyncClient,
) -> None:
    await _open(client)
    before = len(await _messages(client))
    r = await _post(client, type="decide", closes_thread="T-1", disposition=BLOCKED)
    assert r.status_code == 409, r.text
    assert len(await _messages(client)) == before
    view = await client.get("/v1/projects/p/threads/T-1")
    assert view.json()["thread"]["status"] != "resolved"


async def test_close_route_does_not_accept_disposition(client: AsyncClient) -> None:
    # CloseThreadRequest has no field (DESIGN v1 §1: close carries no
    # disposition); the key is ignored like any unknown key on that route,
    # and the decide msg is stored with NULL.
    await _open(client)
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "done", "author": "alice", "disposition": BLOCKED},
    )
    assert r.status_code == 201, r.text
    assert r.json()["decide_msg"]["disposition"] is None


# --- the layer under the API ---------------------------------------------


def _row(msg_id: str, **over: Any) -> Message:
    fields: dict[str, Any] = {
        "project": "p",
        "msg_id": msg_id,
        "thread_id": "T-1",
        "author": "alice",
        "timestamp": datetime.now(UTC),
        "type": "decide",
        "content": "written around the API",
    }
    fields.update(over)
    return Message(**fields)


async def test_db_check_refuses_blocked_on_on_a_closing_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _open(client)
    db_session.add(_row("msg-900", closes_thread="T-1", disposition=BLOCKED))
    with pytest.raises(IntegrityError) as ei:
        await db_session.flush()
    assert "messages_disposition_close_check" in str(ei.value)


async def test_db_check_refuses_unknown_kind(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _open(client)
    db_session.add(_row("msg-901", type="report", disposition={"kind": "malformed"}))
    with pytest.raises(IntegrityError) as ei:
        await db_session.flush()
    assert "messages_disposition_shape_check" in str(ei.value)


async def test_db_check_allows_done_on_a_closing_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Proves the close check is not simply refusing every closing row.
    await _open(client)
    db_session.add(_row("msg-902", closes_thread="T-1", disposition={"kind": "done"}))
    await db_session.flush()
