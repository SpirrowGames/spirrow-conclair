"""End-to-end tests for the thread endpoints (open / list / get)."""

from __future__ import annotations

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
) -> dict:
    payload = {
        "thread_id": thread_id,
        "title": title,
        "owner": owner,
        "propose_content": propose_content,
    }
    if tags:
        payload["tags"] = tags
    r = await client.post(f"/v1/projects/{project}/threads", json=payload)
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
