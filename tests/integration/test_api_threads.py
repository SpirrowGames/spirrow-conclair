"""End-to-end tests for the thread endpoints (open / list / get)."""

from __future__ import annotations

from datetime import datetime

from httpx import AsyncClient


async def _open_thread(
    client: AsyncClient,
    project: str,
    thread_id: str,
    *,
    title: str = "t",
    owner: str = "alice",
    propose_content: str = "start",
    tags: list[str] | None = None,
    timestamp: str | None = None,
) -> dict:
    payload = {
        "thread_id": thread_id,
        "title": title,
        "owner": owner,
        "propose_content": propose_content,
    }
    if tags:
        payload["tags"] = tags
    if timestamp:
        payload["timestamp"] = timestamp
    r = await client.post(f"/v1/projects/{project}/threads", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def _post_msg(
    client: AsyncClient,
    project: str,
    thread_id: str,
    *,
    author: str = "bob",
    content: str = "x",
    timestamp: str | None = None,
) -> dict:
    payload: dict = {"type": "report", "author": author, "content": content}
    if timestamp:
        payload["timestamp"] = timestamp
    r = await client.post(
        f"/v1/projects/{project}/threads/{thread_id}/messages", json=payload
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_open_creates_thread_propose_msg_and_event(client: AsyncClient) -> None:
    body = await _open_thread(client, "p", "T-1", tags=["smoke"])
    assert body["thread"]["status"] == "active"
    assert body["thread"]["created_by_msg"] == "msg-001"
    assert body["thread"]["tags"] == ["smoke"]
    assert body["msg"]["msg_id"] == "msg-001"
    assert body["msg"]["type"] == "propose"
    assert body["msg"]["author"] == "alice"

    # event log should have an open_thread row
    r = await client.get("/v1/projects/p/events")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "open_thread"
    assert items[0]["thread_id"] == "T-1"
    assert items[0]["msg_id"] == "msg-001"
    assert items[0]["actor"] == "alice"


async def test_open_duplicate_returns_409(client: AsyncClient) -> None:
    await _open_thread(client, "p", "T-1")
    r = await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": "T-1",
            "title": "x",
            "owner": "alice",
            "propose_content": "x",
        },
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error_type"] == "ChatroomIntegrityError"
    assert "already exists" in body["error"]


async def test_open_validation_error_returns_422(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/projects/p/threads",
        json={"thread_id": "", "title": "", "owner": "", "propose_content": ""},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error_type"] == "ValidationError"
    assert "errors" in body["details"]


async def test_list_filters_and_pagination(client: AsyncClient) -> None:
    await _open_thread(client, "p", "T-1", owner="alice")
    await _open_thread(client, "p", "T-2", owner="bob")
    await _open_thread(client, "p", "T-3", owner="alice")

    # all
    r = await client.get("/v1/projects/p/threads")
    body = r.json()
    assert body["total"] == 3
    assert {t["thread_id"] for t in body["items"]} == {"T-1", "T-2", "T-3"}

    # owner filter
    r = await client.get("/v1/projects/p/threads?owner=alice")
    body = r.json()
    assert body["total"] == 2
    assert all(t["owner"] == "alice" for t in body["items"])

    # status filter (multiple)
    r = await client.get("/v1/projects/p/threads?status=active&status=resolved")
    assert r.json()["total"] == 3

    # pagination
    r = await client.get("/v1/projects/p/threads?limit=2&offset=0")
    body = r.json()
    assert body["limit"] == 2 and body["offset"] == 0
    assert len(body["items"]) == 2
    assert body["total"] == 3


async def test_get_thread_full_mode_returns_messages(client: AsyncClient) -> None:
    await _open_thread(client, "p", "T-1")
    r = await client.get("/v1/projects/p/threads/T-1?mode=full")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "full"
    assert body["thread"]["thread_id"] == "T-1"
    assert len(body["messages"]) == 1
    assert body["messages"][0]["type"] == "propose"


async def test_get_thread_404_when_missing(client: AsyncClient) -> None:
    r = await client.get("/v1/projects/p/threads/T-no-such")
    assert r.status_code == 404
    body = r.json()
    assert body["error_type"] == "ChatroomNotFoundError"


async def test_summary_mode_on_active_thread_shows_full(client: AsyncClient) -> None:
    """summary mode is only "filtering" for resolved threads."""
    await _open_thread(client, "p", "T-1")
    await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={"type": "question", "author": "bob", "content": "q?"},
    )
    r = await client.get("/v1/projects/p/threads/T-1?mode=summary")
    body = r.json()
    assert body["mode"] == "summary"
    assert len(body["messages"]) == 2  # propose + question (not yet resolved)


# ---- activity rollup (last_msg_id / msg_count / last_activity_at) ---------


async def test_list_reports_per_thread_activity(client: AsyncClient) -> None:
    """A busy thread and an untouched one must be distinguishable from the
    listing alone -- the affordance whose absence produced the 2026-08-15
    near-miss (a live thread read as a one-msg leftover)."""
    await _open_thread(client, "p", "T-busy")  # msg-001
    await _open_thread(client, "p", "T-quiet")  # msg-002
    await _post_msg(client, "p", "T-busy", content="r1")  # msg-003
    await _post_msg(client, "p", "T-busy", content="r2")  # msg-004

    body = (await client.get("/v1/projects/p/threads")).json()
    rows = {t["thread_id"]: t for t in body["items"]}

    busy, quiet = rows["T-busy"], rows["T-quiet"]

    # msg_id is allocated project-wide, so these values are only right if
    # the rollup is grouped by thread_id: T-busy's latest is msg-004 even
    # though msg-002 (a sibling thread's propose) sits between its msgs.
    assert busy["msg_count"] == 3
    assert busy["last_msg_id"] == "msg-004"
    assert quiet["msg_count"] == 1
    assert quiet["last_msg_id"] == "msg-002"

    # The first msg and the last msg are different fields with different
    # values; `created_by_msg` never moves.
    assert busy["created_by_msg"] == "msg-001"
    assert quiet["last_msg_id"] == quiet["created_by_msg"]

    assert datetime.fromisoformat(busy["last_activity_at"]) >= datetime.fromisoformat(
        busy["created_at"]
    )
    assert datetime.fromisoformat(quiet["last_activity_at"]) == datetime.fromisoformat(
        quiet["created_at"]
    )


async def test_list_orders_by_activity_not_creation(client: AsyncClient) -> None:
    """Creation order and activity order are deliberately opposed here.

    Explicit timestamps rather than wall-clock ordering: the point of the
    fixture is that an old thread posted to today outranks a young silent
    one, and that only shows if the two orders actually disagree.
    """
    await _open_thread(client, "p", "T-old", timestamp="2026-06-01T00:00:00Z")
    await _open_thread(client, "p", "T-young", timestamp="2026-08-01T00:00:00Z")
    await _post_msg(client, "p", "T-old", timestamp="2026-08-15T00:00:00Z")

    items = (await client.get("/v1/projects/p/threads")).json()["items"]

    assert [t["thread_id"] for t in items] == ["T-old", "T-young"]
    # ... and the list is NOT in created_at DESC order, which is what it
    # used to be. Without this the assertion above could pass by accident.
    assert datetime.fromisoformat(items[0]["created_at"]) < datetime.fromisoformat(
        items[1]["created_at"]
    )


async def test_list_pagination_is_stable_across_pages(client: AsyncClient) -> None:
    """Same activity timestamp on every thread -- the tiebreakers have to
    produce one total order, or a row can appear on both pages or neither."""
    ts = "2026-07-01T00:00:00Z"
    for tid in ("T-a", "T-b", "T-c", "T-d"):
        await _open_thread(client, "p", tid, timestamp=ts)

    page1 = (await client.get("/v1/projects/p/threads?limit=2&offset=0")).json()
    page2 = (await client.get("/v1/projects/p/threads?limit=2&offset=2")).json()

    seen = [t["thread_id"] for t in page1["items"] + page2["items"]]
    assert sorted(seen) == ["T-a", "T-b", "T-c", "T-d"]


async def test_open_response_carries_its_own_rollup(client: AsyncClient) -> None:
    body = await _open_thread(client, "p", "T-1")

    assert body["thread"]["msg_count"] == 1
    assert body["thread"]["last_msg_id"] == body["msg"]["msg_id"]
    assert body["thread"]["last_msg_id"] == body["thread"]["created_by_msg"]
    assert body["thread"]["last_activity_at"] == body["msg"]["timestamp"]


async def test_get_thread_rollup_counts_all_msgs_in_summary_mode(
    client: AsyncClient,
) -> None:
    """`summary` returns only the decide msg, so the count cannot come from
    the returned list -- it would under-report exactly where the list is
    shortest."""
    await _open_thread(client, "p", "T-1")
    await _post_msg(client, "p", "T-1")
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "done", "author": "alice"},
    )
    assert r.status_code == 201, r.text
    # The close response already reports the decide msg it just wrote.
    assert r.json()["thread"]["msg_count"] == 3
    assert r.json()["thread"]["last_msg_id"] == r.json()["decide_msg"]["msg_id"]

    body = (await client.get("/v1/projects/p/threads/T-1?mode=summary")).json()

    assert len(body["messages"]) == 1  # decide only
    assert body["thread"]["msg_count"] == 3
    assert body["thread"]["last_msg_id"] == "msg-003"
