"""End-to-end tests for the messages endpoint and status transitions."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _open(client: AsyncClient, project: str, thread_id: str) -> None:
    r = await client.post(
        f"/v1/projects/{project}/threads",
        json={
            "thread_id": thread_id,
            "title": "t",
            "owner": "alice",
            "propose_content": "start",
        },
    )
    assert r.status_code == 201


async def _post(client: AsyncClient, project: str, thread_id: str, **body) -> dict:
    r = await client.post(
        f"/v1/projects/{project}/threads/{thread_id}/messages", json=body
    )
    return r.status_code, r.json()


async def test_handoff_then_ack_round_trip(client: AsyncClient) -> None:
    await _open(client, "p", "T-1")

    # plain question -> no transition
    code, body = await _post(client, "p", "T-1", type="question", author="bob", content="q?")
    assert code == 201
    assert body["thread_status_changed_to"] is None

    # handoff -> awaiting_reply
    code, body = await _post(client, "p", "T-1", type="handoff", author="alice", content="over to you")
    assert code == 201
    assert body["thread_status_changed_to"] == "awaiting_reply"

    # ack -> active
    code, body = await _post(client, "p", "T-1", type="ack", author="bob", content="got it")
    assert code == 201
    assert body["thread_status_changed_to"] == "active"

    # verify thread state
    r = await client.get("/v1/projects/p/threads/T-1")
    assert r.json()["thread"]["status"] == "active"


async def test_post_propose_into_existing_thread_is_409(client: AsyncClient) -> None:
    await _open(client, "p", "T-1")
    code, body = await _post(client, "p", "T-1", type="propose", author="alice", content="2nd propose")
    assert code == 409
    assert body["error_type"] == "ChatroomIntegrityError"


async def test_reply_to_unknown_msg_is_409(client: AsyncClient) -> None:
    await _open(client, "p", "T-1")
    code, body = await _post(
        client, "p", "T-1",
        type="answer", author="alice", content="ans", reply_to="msg-999",
    )
    assert code == 409
    assert body["error_type"] == "ChatroomIntegrityError"
    assert "msg-999" in body["error"]


async def test_closes_thread_by_non_owner_is_409(client: AsyncClient) -> None:
    """Posting decide+closes_thread as non-owner via /messages — 409 IntegrityError.

    The /close endpoint surfaces the same condition as 403 PermissionError.
    """
    await _open(client, "p", "T-1")
    code, body = await _post(
        client, "p", "T-1",
        type="decide", author="bob", content="close", closes_thread="T-1",
    )
    assert code == 409
    assert body["error_type"] == "ChatroomIntegrityError"


async def test_post_to_unknown_thread_is_404(client: AsyncClient) -> None:
    code, body = await _post(
        client, "p", "T-no-such", type="question", author="x", content="q",
    )
    assert code == 404
    assert body["error_type"] == "ChatroomNotFoundError"


async def test_references_threads_must_exist(client: AsyncClient) -> None:
    await _open(client, "p", "T-1")
    code, body = await _post(
        client, "p", "T-1",
        type="report", author="alice", content="r",
        references_threads=["T-bogus"],
    )
    assert code == 409
    assert body["error_type"] == "ChatroomIntegrityError"
    assert "T-bogus" in body["error"]


async def test_sequential_msg_id_allocation(client: AsyncClient) -> None:
    """30 sequential posts -> msg-001..msg-031 contiguous."""
    await _open(client, "p", "T-1")
    for i in range(30):
        code, _ = await _post(client, "p", "T-1", type="report", author="alice", content=f"#{i}")
        assert code == 201

    r = await client.get("/v1/projects/p/threads/T-1?mode=full")
    msg_ids = [m["msg_id"] for m in r.json()["messages"]]
    assert msg_ids == [f"msg-{i:03d}" for i in range(1, 32)]


async def test_embodiment_persists_on_post_message(client: AsyncClient) -> None:
    """ADR-2026-05-29-12: embodiment supplied on the body is persisted on
    the resulting msg row and surfaced on the GET /threads/{tid} fetch.
    Conclair does not validate the value (validation lives in Magickit)
    so any string is round-tripped; a missing field stays null."""
    await _open(client, "p", "T-1")

    code, body = await _post(
        client, "p", "T-1",
        type="report", author="alice", content="declared",
        embodiment="terminal_coding_agent",
    )
    assert code == 201
    assert body["msg"]["embodiment"] == "terminal_coding_agent"

    # Round-trip via the thread fetch.
    r = await client.get("/v1/projects/p/threads/T-1?mode=full")
    msgs = {m["msg_id"]: m for m in r.json()["messages"]}
    declared = next(m for m in msgs.values() if m["author"] == "alice" and m["type"] == "report")
    assert declared["embodiment"] == "terminal_coding_agent"

    # A second post without embodiment stays null.
    code, body = await _post(
        client, "p", "T-1",
        type="report", author="alice", content="undeclared",
    )
    assert code == 201
    assert body["msg"]["embodiment"] is None


async def test_role_persists_on_post_message(client: AsyncClient) -> None:
    """ADR-2026-05-27-09 / msg-002 §2: role supplied on the body is persisted
    on the resulting msg row and surfaced on the GET /threads/{tid} fetch.
    Conclair does not validate role × allowed_roles (Magickit enforces) so
    any string is round-tripped; a missing field stays null.
    """
    await _open(client, "p", "T-1")

    code, body = await _post(
        client, "p", "T-1",
        type="report", author="alice", content="declared",
        role="implementer",
    )
    assert code == 201
    assert body["msg"]["role"] == "implementer"

    r = await client.get("/v1/projects/p/threads/T-1?mode=full")
    msgs = r.json()["messages"]
    declared = next(m for m in msgs if m["author"] == "alice" and m["type"] == "report")
    assert declared["role"] == "implementer"

    # Round-trip an arbitrary, unknown role string (Conclair is value-agnostic).
    code, body = await _post(
        client, "p", "T-1",
        type="report", author="alice", content="exotic",
        role="some-future-role-not-in-any-allowlist",
    )
    assert code == 201
    assert body["msg"]["role"] == "some-future-role-not-in-any-allowlist"

    # A post without role stays null.
    code, body = await _post(
        client, "p", "T-1",
        type="report", author="alice", content="undeclared",
    )
    assert code == 201
    assert body["msg"]["role"] is None


async def test_concurrent_msg_id_allocation(client: AsyncClient) -> None:
    """Concurrent posts must still produce unique, contiguous msg_ids
    thanks to pg_advisory_xact_lock.
    """
    import asyncio

    await _open(client, "p", "T-1")

    async def one(i: int):
        return await _post(client, "p", "T-1", type="report", author="alice", content=f"#{i}")

    results = await asyncio.gather(*[one(i) for i in range(20)])
    codes = [code for code, _ in results]
    assert all(c == 201 for c in codes), codes

    r = await client.get("/v1/projects/p/threads/T-1?mode=full")
    msg_ids = sorted(m["msg_id"] for m in r.json()["messages"])
    expected = sorted([f"msg-{i:03d}" for i in range(1, 22)])
    assert msg_ids == expected


# ---- resolved-terminal refusal (msg-406 §5.1) ----------------------------
#
# The pair these tests refuse is a settled thread taking a new message,
# for every ``type``. Refusing decide alone would leave open the two-msg
# variant of invariant 7's paired state (close-turns-1 + handoff-turns-2)
# -- msg-406 §2 is explicit that the point of these tests is to pin the
# refusal on *every* type, not just decide.


async def _close(client: AsyncClient, project: str, thread_id: str) -> str:
    r = await client.post(
        f"/v1/projects/{project}/threads/{thread_id}/close",
        json={"summary_content": "done", "author": "alice"},
    )
    assert r.status_code == 201, r.text
    return r.json()["decide_msg"]["msg_id"]


@pytest.mark.parametrize(
    "msg_type,extra",
    [
        ("question", {}),
        ("answer", {}),
        ("report", {}),
        ("handoff", {}),
        ("ack", {}),
        # decide *without* closes_thread also refused: the write path is
        # `post_message_in_session`, and the assert runs before any type
        # branch. The decide-with-closes_thread case is separately covered
        # by `test_re_close_returns_409_state_error` (test_api_close.py).
        ("decide", {}),
    ],
)
async def test_post_to_resolved_thread_is_refused_regardless_of_type(
    client: AsyncClient, msg_type: str, extra: dict
) -> None:
    await _open(client, "p", "T-1")
    decide_id = await _close(client, "p", "T-1")

    code, body = await _post(
        client, "p", "T-1",
        type=msg_type, author="bob", content="after-decide", **extra,
    )
    assert code == 409, body
    assert body["error_type"] == "ChatroomStateError"
    assert "resolved" in body["error"]

    # Machine-readable pointer: "the settled thread is *this* msg, and if
    # you want to continue the topic, open a new thread and reference it".
    assert body["details"]["thread_id"] == "T-1"
    assert body["details"]["status"] == "resolved"
    assert body["details"]["resolved_by_msg"] == decide_id


async def test_refused_post_writes_nothing(client: AsyncClient) -> None:
    """The refusal has to leave no trace. A rejected write that still
    allocated a msg_id, incremented ``last_msg_num``, or emitted a
    ``post_message`` event would be worse than the acceptance the rule
    exists to prevent -- the ordering property downstream reads (§5.2)
    would break where a future R3-style filter cannot repair it.
    """
    await _open(client, "p", "T-1")
    decide_id = await _close(client, "p", "T-1")

    before = await client.get("/v1/projects/p/threads/T-1?mode=full")
    before_body = before.json()
    events_before = await client.get("/v1/projects/p/events?thread_id=T-1")
    n_events_before = len(events_before.json()["items"])

    code, _ = await _post(
        client, "p", "T-1",
        type="report", author="bob", content="ignored",
    )
    assert code == 409

    after = await client.get("/v1/projects/p/threads/T-1?mode=full")
    after_body = after.json()
    # No new msg row, and last_msg_id is still the decide.
    assert after_body["thread"]["last_msg_id"] == decide_id
    assert after_body["thread"]["msg_count"] == before_body["thread"]["msg_count"]
    assert [m["msg_id"] for m in after_body["messages"]] == [
        m["msg_id"] for m in before_body["messages"]
    ]

    # No new event row either -- a post_message event would have been the
    # smoking gun for a half-applied write.
    events_after = await client.get("/v1/projects/p/events?thread_id=T-1")
    assert len(events_after.json()["items"]) == n_events_before


# msg-406 §5.3 non-goal: superseded and parked are deliberately not
# refused. These two tests pin that the msg-406 disposition did not
# accidentally widen the terminal-for-writes set. If a later contributor
# adds superseded or parked to `_WRITE_TERMINAL_STATUS`, they will fire
# here and force the reasoning back into review.


async def test_post_to_parked_thread_is_still_accepted(
    client: AsyncClient, db_session
) -> None:
    from sqlalchemy import text as sql_text

    await _open(client, "p", "T-1")
    # ``parked`` is not writable through any API today, so poke the row
    # directly. This is the same shape ``test_api_close_sanction.py`` uses
    # to seed statuses that no HTTP path constructs.
    await db_session.execute(
        sql_text("UPDATE threads SET status = 'parked' WHERE thread_id = 'T-1'")
    )
    await db_session.commit()

    code, body = await _post(
        client, "p", "T-1", type="report", author="alice", content="on parked",
    )
    assert code == 201, body


async def test_post_to_superseded_thread_is_still_accepted(
    client: AsyncClient, db_session
) -> None:
    from sqlalchemy import text as sql_text

    await _open(client, "p", "T-1")
    await db_session.execute(
        sql_text("UPDATE threads SET status = 'superseded' WHERE thread_id = 'T-1'")
    )
    await db_session.commit()

    code, body = await _post(
        client, "p", "T-1", type="report", author="alice", content="on superseded",
    )
    assert code == 201, body
