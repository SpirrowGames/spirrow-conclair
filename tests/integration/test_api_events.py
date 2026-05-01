"""End-to-end tests for the events endpoint (audit log filters)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from httpx import AsyncClient


async def _scenario(client: AsyncClient) -> None:
    """Build a small scenario: open + question + handoff + ack + close."""
    await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": "T-1",
            "title": "t",
            "owner": "alice",
            "propose_content": "start",
        },
    )
    # tiny sleeps so timestamps definitely differ when ordered DESC
    await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={"type": "question", "author": "bob", "content": "q?"},
    )
    await asyncio.sleep(0.01)
    await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={"type": "handoff", "author": "alice", "content": "to you"},
    )
    await asyncio.sleep(0.01)
    await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={"type": "ack", "author": "bob", "content": "got"},
    )
    await asyncio.sleep(0.01)
    await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "done", "author": "alice"},
    )


async def test_list_returns_all_actions_in_desc_order(client: AsyncClient) -> None:
    await _scenario(client)
    r = await client.get("/v1/projects/p/events")
    items = r.json()["items"]
    # We should see: 1 open + 4 post_message + 3 status_transition = 8
    actions = [e["action"] for e in items]
    assert actions.count("open_thread") == 1
    assert actions.count("post_message") == 4
    assert actions.count("status_transition") == 3
    # newest first
    timestamps = [e["timestamp"] for e in items]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_filter_by_action(client: AsyncClient) -> None:
    await _scenario(client)
    r = await client.get("/v1/projects/p/events?action=status_transition")
    items = r.json()["items"]
    assert all(e["action"] == "status_transition" for e in items)
    transitions = sorted((e["details"]["from"], e["details"]["to"]) for e in items)
    assert transitions == [
        ("active", "awaiting_reply"),
        ("active", "resolved"),
        ("awaiting_reply", "active"),
    ]


async def test_filter_by_thread_id(client: AsyncClient) -> None:
    await _scenario(client)
    # second project + thread to make sure filter works
    await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": "T-2",
            "title": "t2",
            "owner": "alice",
            "propose_content": "x",
        },
    )

    r = await client.get("/v1/projects/p/events?thread_id=T-1")
    assert all(e["thread_id"] == "T-1" for e in r.json()["items"])

    r = await client.get("/v1/projects/p/events?thread_id=T-2")
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "open_thread"


async def test_filter_by_since_until(client: AsyncClient) -> None:
    from datetime import datetime, timezone

    await _scenario(client)
    r = await client.get("/v1/projects/p/events")
    items = r.json()["items"]
    # pick a timestamp in the middle of the timeline
    middle = items[len(items) // 2]["timestamp"]
    middle_dt = datetime.fromisoformat(middle.replace("Z", "+00:00"))

    r = await client.get(
        f"/v1/projects/p/events?since={middle}"
    )
    after_or_equal = r.json()["items"]
    for e in after_or_equal:
        ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        assert ts >= middle_dt - timedelta(microseconds=1)

    r = await client.get(f"/v1/projects/p/events?until={middle}")
    strictly_before = r.json()["items"]
    for e in strictly_before:
        ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        assert ts < middle_dt


async def test_pagination(client: AsyncClient) -> None:
    await _scenario(client)
    r = await client.get("/v1/projects/p/events?limit=3&offset=0")
    body = r.json()
    assert body["limit"] == 3 and body["offset"] == 0
    assert len(body["items"]) == 3
    assert body["total"] == 8  # full event count
