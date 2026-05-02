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


# ---- form posts ----------------------------------------------------------


@pytest.mark.asyncio
async def test_open_thread_form_redirects(client: AsyncClient) -> None:
    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads",
        data={
            "thread_id": "T-FORM-1",
            "title": "form open",
            "owner": "form-tester",
            "propose_content": "via form",
            "tags": "ui,form",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("hx-redirect") == f"/ui/projects/{PROJECT}/threads/T-FORM-1"
    assert "opened thread" in resp.text


@pytest.mark.asyncio
async def test_open_thread_form_duplicate_returns_flash(client: AsyncClient) -> None:
    payload = {
        "thread_id": "T-DUP",
        "title": "first",
        "owner": "form-tester",
        "propose_content": "first body",
    }
    resp1 = await client.post(f"/ui/projects/{PROJECT}/threads", data=payload)
    assert resp1.status_code == 200
    assert "hx-redirect" in {k.lower() for k in resp1.headers.keys()}

    resp2 = await client.post(f"/ui/projects/{PROJECT}/threads", data=payload)
    assert resp2.status_code == 200
    # No redirect on the second attempt — should be an inline flash.
    assert "hx-redirect" not in {k.lower() for k in resp2.headers.keys()}
    assert "ChatroomIntegrityError" in resp2.text


@pytest.mark.asyncio
async def test_open_thread_form_validation_error(client: AsyncClient) -> None:
    # Whitespace-only owner survives FastAPI Form() (which collapses empty
    # strings to None) but fails OpenThreadRequest's str_strip_whitespace +
    # min_length=1 — exactly what we want our flash partial to render.
    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads",
        data={
            "thread_id": "T-BAD",
            "title": "x",
            "owner": "   ",
            "propose_content": "y",
        },
    )
    assert resp.status_code == 200
    assert "ValidationError" in resp.text
    assert "owner" in resp.text.lower()


@pytest.mark.asyncio
async def test_post_message_form_triggers_messagePosted(
    client: AsyncClient,
) -> None:
    # Seed thread first.
    await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": "T-MSG-FORM",
            "title": "msg form",
            "owner": "tester",
            "propose_content": "kickoff",
        },
    )

    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads/T-MSG-FORM/messages",
        data={
            "type": "question",
            "author": "tester",
            "content": "any thoughts",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("hx-trigger") == "messagePosted"
    assert "posted msg-002" in resp.text


@pytest.mark.asyncio
async def test_post_message_form_handoff_status_change(
    client: AsyncClient,
) -> None:
    await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": "T-HANDOFF",
            "title": "handoff",
            "owner": "tester",
            "propose_content": "kickoff",
        },
    )

    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads/T-HANDOFF/messages",
        data={
            "type": "handoff",
            "author": "tester",
            "content": "over to you",
        },
    )
    assert resp.status_code == 200
    assert "status" in resp.text
    assert "awaiting_reply" in resp.text


@pytest.mark.asyncio
async def test_post_message_form_thread_not_found(client: AsyncClient) -> None:
    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads/T-NOPE/messages",
        data={
            "type": "question",
            "author": "tester",
            "content": "x",
        },
    )
    assert resp.status_code == 200
    assert "ChatroomNotFoundError" in resp.text


@pytest.mark.asyncio
async def test_close_thread_form_owner_succeeds(client: AsyncClient) -> None:
    await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": "T-CLOSE-OK",
            "title": "close ok",
            "owner": "alice",
            "propose_content": "kickoff",
        },
    )

    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads/T-CLOSE-OK/close",
        data={
            "author": "alice",
            "summary_content": "## Resolution\nshipping",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("hx-refresh") == "true"
    assert "closed thread" in resp.text


@pytest.mark.asyncio
async def test_close_thread_form_non_owner_403_flash(client: AsyncClient) -> None:
    await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": "T-CLOSE-DENY",
            "title": "deny",
            "owner": "alice",
            "propose_content": "kickoff",
        },
    )

    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads/T-CLOSE-DENY/close",
        data={
            "author": "bob",
            "summary_content": "## Resolution\nattempt",
        },
    )
    assert resp.status_code == 200
    assert "hx-refresh" not in {k.lower() for k in resp.headers.keys()}
    assert "ChatroomPermissionError" in resp.text


@pytest.mark.asyncio
async def test_close_thread_already_resolved_state_error(
    client: AsyncClient,
) -> None:
    await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": "T-DOUBLE-CLOSE",
            "title": "double",
            "owner": "alice",
            "propose_content": "kickoff",
        },
    )
    # First close succeeds.
    first = await client.post(
        f"/ui/projects/{PROJECT}/threads/T-DOUBLE-CLOSE/close",
        data={"author": "alice", "summary_content": "first close"},
    )
    assert first.status_code == 200
    assert first.headers.get("hx-refresh") == "true"

    # Second close should be a state error flash.
    second = await client.post(
        f"/ui/projects/{PROJECT}/threads/T-DOUBLE-CLOSE/close",
        data={"author": "alice", "summary_content": "second close"},
    )
    assert second.status_code == 200
    assert "hx-refresh" not in {k.lower() for k in second.headers.keys()}
    assert "ChatroomStateError" in second.text or "ChatroomIntegrityError" in second.text


@pytest.mark.asyncio
async def test_thread_detail_post_form_present_when_active(
    client: AsyncClient,
) -> None:
    await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": "T-FORM-VISIBLE",
            "title": "visible",
            "owner": "tester",
            "propose_content": "kickoff",
        },
    )

    resp = await client.get(f"/ui/projects/{PROJECT}/threads/T-FORM-VISIBLE")
    assert resp.status_code == 200
    body = resp.text
    assert "post message" in body.lower()
    assert 'hx-post="/ui/projects/' in body
    assert "close thread (owner only)" in body.lower()
