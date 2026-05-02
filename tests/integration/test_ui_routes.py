"""UI smoke tests — landing page, static assets, page + fragment routes."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

PROJECT = "ui-test-proj"


# ---- landing & static -----------------------------------------------------


@pytest.mark.asyncio
async def test_landing_renders(client: AsyncClient) -> None:
    resp = await client.get("/ui/")
    assert resp.status_code == 200
    body = resp.text.lower()
    assert "conclair" in body
    assert "htmx" in body
    assert "/static/css/conclair.css" in body
    assert "/static/js/conclair.js" in body


@pytest.mark.asyncio
async def test_landing_has_recent_projects_anchor(client: AsyncClient) -> None:
    resp = await client.get("/ui/")
    assert resp.status_code == 200
    assert 'id="recent-projects"' in resp.text


@pytest.mark.asyncio
async def test_static_css_served(client: AsyncClient) -> None:
    resp = await client.get("/static/css/conclair.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert "--bg" in resp.text


@pytest.mark.asyncio
async def test_static_js_served(client: AsyncClient) -> None:
    resp = await client.get("/static/js/conclair.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "conclairOpenProject" in resp.text


# ---- threads page + fragment ---------------------------------------------


@pytest.mark.asyncio
async def test_threads_page_empty(client: AsyncClient) -> None:
    resp = await client.get(f"/ui/projects/{PROJECT}/threads")
    assert resp.status_code == 200
    body = resp.text
    assert "threads" in body.lower()
    assert PROJECT in body
    assert "no threads match these filters" in body
    assert 'id="thread-rows"' in body


@pytest.mark.asyncio
async def test_threads_fragment_with_filter(client: AsyncClient) -> None:
    resp = await client.get(
        f"/ui/projects/{PROJECT}/threads/_rows",
        params={"status": "active", "limit": 10, "offset": 0},
    )
    assert resp.status_code == 200
    # Fragment is just the <tbody> rows, not a full page.
    assert "<!DOCTYPE html>" not in resp.text
    assert "no threads match these filters" in resp.text


@pytest.mark.asyncio
async def test_threads_page_with_seeded_thread(client: AsyncClient) -> None:
    # Seed via /v1 so the UI page renders an actual row.
    resp = await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": "T-UI-1",
            "title": "ui smoke",
            "owner": "tester",
            "propose_content": "hello",
        },
    )
    assert resp.status_code == 201

    resp = await client.get(f"/ui/projects/{PROJECT}/threads")
    assert resp.status_code == 200
    body = resp.text
    assert "T-UI-1" in body
    assert "ui smoke" in body
    assert ">tester<" in body or "tester" in body


# ---- thread detail page + messages fragment ------------------------------


@pytest.mark.asyncio
async def test_thread_detail_404_for_missing(client: AsyncClient) -> None:
    resp = await client.get(f"/ui/projects/{PROJECT}/threads/does-not-exist")
    assert resp.status_code == 404
    assert "ChatroomNotFoundError" in resp.text


@pytest.mark.asyncio
async def test_thread_detail_renders_existing(client: AsyncClient) -> None:
    resp = await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": "T-UI-2",
            "title": "detail render",
            "owner": "tester",
            "propose_content": "kickoff body",
        },
    )
    assert resp.status_code == 201

    resp = await client.get(f"/ui/projects/{PROJECT}/threads/T-UI-2")
    assert resp.status_code == 200
    body = resp.text
    assert "T-UI-2" in body
    assert "detail render" in body
    assert 'id="messages"' in body
    # Inline render of message_list partial includes the propose msg.
    assert "msg-001" in body
    assert "kickoff body" in body


@pytest.mark.asyncio
async def test_thread_messages_fragment(client: AsyncClient) -> None:
    resp = await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": "T-UI-3",
            "title": "msg fragment",
            "owner": "tester",
            "propose_content": "kickoff",
        },
    )
    assert resp.status_code == 201

    resp = await client.get(f"/ui/projects/{PROJECT}/threads/T-UI-3/_messages")
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" not in resp.text
    assert "msg-001" in resp.text
    assert "kickoff" in resp.text


# ---- events page + fragment ----------------------------------------------


@pytest.mark.asyncio
async def test_events_page_empty(client: AsyncClient) -> None:
    resp = await client.get(f"/ui/projects/{PROJECT}/events")
    assert resp.status_code == 200
    body = resp.text
    assert "events" in body.lower()
    assert "no events match these filters" in body


@pytest.mark.asyncio
async def test_events_fragment_with_seeded_thread(client: AsyncClient) -> None:
    resp = await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": "T-UI-4",
            "title": "events seed",
            "owner": "tester",
            "propose_content": "trigger event",
        },
    )
    assert resp.status_code == 201

    resp = await client.get(
        f"/ui/projects/{PROJECT}/events/_rows",
        params={"action": "open_thread", "limit": 10},
    )
    assert resp.status_code == 200
    assert "open_thread" in resp.text
    assert "T-UI-4" in resp.text


# ---- integrity page + fragment -------------------------------------------


@pytest.mark.asyncio
async def test_integrity_page_clean(client: AsyncClient) -> None:
    resp = await client.get(f"/ui/projects/{PROJECT}/integrity")
    assert resp.status_code == 200
    body = resp.text
    assert "integrity" in body.lower()
    assert "issue_count" in body
    assert "no integrity issues" in body


@pytest.mark.asyncio
async def test_integrity_fragment(client: AsyncClient) -> None:
    resp = await client.get(f"/ui/projects/{PROJECT}/integrity/_body")
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" not in resp.text
    assert "issue_count" in resp.text
