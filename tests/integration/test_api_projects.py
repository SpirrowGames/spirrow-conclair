"""Cross-project summary endpoint.

The dashboard ranks projects by whether they need a human, so the two
things worth pinning are that the counts are per-project (no bleed) and
that gate-tagged threads are counted separately from merely-active ones.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _open(client: AsyncClient, project: str, thread_id: str, **kw) -> None:
    await client.post(
        f"/v1/projects/{project}/threads",
        json={
            "thread_id": thread_id,
            "title": thread_id,
            "owner": "tester",
            "propose_content": "kickoff",
            **kw,
        },
    )


def _find(payload: dict, project: str) -> dict | None:
    return next((i for i in payload["items"] if i["project"] == project), None)


@pytest.mark.asyncio
async def test_empty_when_no_threads(client: AsyncClient) -> None:
    resp = await client.get("/v1/projects")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


@pytest.mark.asyncio
async def test_counts_are_scoped_per_project(client: AsyncClient) -> None:
    await _open(client, "proj-a", "T-1")
    await _open(client, "proj-a", "T-2")
    await _open(client, "proj-b", "T-1")

    payload = (await client.get("/v1/projects")).json()

    assert payload["total"] == 2
    assert _find(payload, "proj-a")["thread_count"] == 2
    assert _find(payload, "proj-b")["thread_count"] == 1


@pytest.mark.asyncio
async def test_gate_tagged_threads_are_counted_separately(client: AsyncClient) -> None:
    await _open(client, "proj-gate", "T-plain", tags=["design"])
    await _open(client, "proj-gate", "T-gated", tags=["gate:naysayer", "design"])

    entry = _find((await client.get("/v1/projects")).json(), "proj-gate")

    assert entry["thread_count"] == 2
    assert entry["gated_thread_count"] == 1


@pytest.mark.asyncio
async def test_a_tag_merely_containing_gate_is_not_counted(client: AsyncClient) -> None:
    """The marker is a prefix; "mitigate:x" or "gateway" must not match."""
    await _open(client, "proj-prefix", "T-1", tags=["gateway", "mitigate:risk"])

    entry = _find((await client.get("/v1/projects")).json(), "proj-prefix")

    assert entry["gated_thread_count"] == 0


@pytest.mark.asyncio
async def test_status_breakdown_follows_transitions(client: AsyncClient) -> None:
    await _open(client, "proj-status", "T-open")
    await _open(client, "proj-status", "T-closing")
    await client.post(
        "/v1/projects/proj-status/threads/T-closing/close",
        json={"author": "tester", "summary_content": "## Resolution\n\ndone"},
    )

    entry = _find((await client.get("/v1/projects")).json(), "proj-status")

    assert entry["threads_by_status"]["active"] == 1
    assert entry["threads_by_status"]["resolved"] == 1


@pytest.mark.asyncio
async def test_message_count_and_last_activity(client: AsyncClient) -> None:
    await _open(client, "proj-msgs", "T-1")
    await client.post(
        "/v1/projects/proj-msgs/threads/T-1/messages",
        json={"type": "question", "author": "tester", "content": "q"},
    )

    entry = _find((await client.get("/v1/projects")).json(), "proj-msgs")

    # propose + question
    assert entry["message_count"] == 2
    assert entry["last_activity_at"] is not None


@pytest.mark.asyncio
async def test_most_recently_active_project_sorts_first(client: AsyncClient) -> None:
    await _open(client, "proj-old", "T-1")
    await _open(client, "proj-new", "T-1")
    await client.post(
        "/v1/projects/proj-new/threads/T-1/messages",
        json={"type": "report", "author": "tester", "content": "latest"},
    )

    payload = (await client.get("/v1/projects")).json()

    assert payload["items"][0]["project"] == "proj-new"
