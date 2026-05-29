"""End-to-end tests for the messages endpoint and status transitions."""

from __future__ import annotations

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
