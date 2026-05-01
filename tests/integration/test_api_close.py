"""End-to-end tests for the close_thread endpoint."""

from __future__ import annotations

from httpx import AsyncClient


async def _open(client: AsyncClient, thread_id: str, owner: str = "alice") -> None:
    r = await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": thread_id,
            "title": "t",
            "owner": owner,
            "propose_content": "start",
        },
    )
    assert r.status_code == 201


async def test_owner_can_close(client: AsyncClient) -> None:
    await _open(client, "T-1", owner="alice")
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={
            "summary_content": "## Resolution\n結論",
            "author": "alice",
            "affects_threads": ["T-x", "T-y"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["thread"]["status"] == "resolved"
    assert body["thread"]["resolved_by_msg"] == body["decide_msg"]["msg_id"]
    assert body["thread"]["affects_threads"] == ["T-x", "T-y"]
    assert body["decide_msg"]["type"] == "decide"
    assert body["decide_msg"]["closes_thread"] == "T-1"


async def test_non_owner_close_returns_403(client: AsyncClient) -> None:
    await _open(client, "T-1", owner="alice")
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "x", "author": "bob"},
    )
    assert r.status_code == 403
    body = r.json()
    assert body["error_type"] == "ChatroomPermissionError"


async def test_close_unknown_thread_returns_404(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/projects/p/threads/T-nope/close",
        json={"summary_content": "x", "author": "alice"},
    )
    assert r.status_code == 404


async def test_re_close_returns_409_state_error(client: AsyncClient) -> None:
    await _open(client, "T-1")
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "first close", "author": "alice"},
    )
    assert r.status_code == 201

    r2 = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "second close", "author": "alice"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["error_type"] == "ChatroomStateError"
    assert "resolved" in body["error"]


async def test_close_emits_post_message_and_status_transition_events(
    client: AsyncClient,
) -> None:
    await _open(client, "T-1")
    await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "done", "author": "alice"},
    )
    r = await client.get("/v1/projects/p/events?thread_id=T-1")
    actions = sorted(e["action"] for e in r.json()["items"])
    # open_thread + post_message (decide) + status_transition
    assert actions == ["open_thread", "post_message", "status_transition"]

    # status_transition event details should record the from/to
    transitions = [e for e in r.json()["items"] if e["action"] == "status_transition"]
    assert len(transitions) == 1
    assert transitions[0]["details"] == {"from": "active", "to": "resolved"}


async def test_summary_mode_after_close_returns_only_decide(
    client: AsyncClient,
) -> None:
    await _open(client, "T-1")
    # post a question first so the thread has 2 msgs before close
    await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={"type": "question", "author": "bob", "content": "?"},
    )
    await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "done", "author": "alice"},
    )
    r = await client.get("/v1/projects/p/threads/T-1?mode=summary")
    body = r.json()
    msgs = body["messages"]
    assert len(msgs) == 1
    assert msgs[0]["type"] == "decide"
