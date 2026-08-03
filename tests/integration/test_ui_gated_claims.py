"""The direct-to-Conclair UI path refuses claims it cannot validate.

Conclair validates nothing by design -- identity, ``allowed_roles``, and the
human-only override flags all live in Magickit. The forms nonetheless carry
``role`` / override fields, because the Magickit-served UI enforces them.

Reaching these handlers directly (loopback :8115) therefore means the claim
has met no gate. Dropping it silently would let the post succeed while
``messages.role`` stayed null: the user believes they declared a role, and
the invariant "role non-null <-> it passed validation" still *looks*
satisfied. These tests pin the refusal, and pin that ordinary posts are
untouched by the guard.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

PROJECT = "ui-gated-claims"


async def _seed(client: AsyncClient, thread_id: str) -> None:
    await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": thread_id,
            "title": "gated claims",
            "owner": "human",
            "propose_content": "kickoff",
        },
    )


@pytest.mark.asyncio
async def test_post_message_without_claims_still_works(client: AsyncClient) -> None:
    """The guard must not disturb the ordinary path."""
    await _seed(client, "T-PLAIN")

    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads/T-PLAIN/messages",
        data={"type": "question", "author": "human", "content": "plain question"},
    )

    assert resp.status_code == 200
    assert "UngatedClaimRejected" not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "implementer"),
        ("owner_override_reason", "above-loop call"),
        ("naysayer_override_reason", "human judgement"),
    ],
)
async def test_post_message_rejects_a_gated_claim(
    client: AsyncClient, field: str, value: str
) -> None:
    thread_id = f"T-CLAIM-{field}"
    await _seed(client, thread_id)

    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads/{thread_id}/messages",
        data={
            "type": "question",
            "author": "human",
            "content": "with a claim",
            field: value,
        },
    )

    assert "UngatedClaimRejected" in resp.text
    assert field in resp.text


@pytest.mark.asyncio
async def test_open_thread_rejects_a_role_claim(client: AsyncClient) -> None:
    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads",
        data={
            "thread_id": "T-GATED-OPEN",
            "title": "t",
            "owner": "human",
            "propose_content": "c",
            "role": "proposer",
        },
    )

    assert "UngatedClaimRejected" in resp.text

    # And the thread was not created.
    listing = await client.get(f"/v1/projects/{PROJECT}/threads")
    assert all(t["thread_id"] != "T-GATED-OPEN" for t in listing.json()["items"])


@pytest.mark.asyncio
async def test_close_rejects_a_gated_claim(client: AsyncClient) -> None:
    await _seed(client, "T-CLOSE-CLAIM")

    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads/T-CLOSE-CLAIM/close",
        data={
            "author": "human",
            "summary_content": "done",
            "owner_override_reason": "above-loop call",
        },
    )

    assert "UngatedClaimRejected" in resp.text

    thread = await client.get(f"/v1/projects/{PROJECT}/threads/T-CLOSE-CLAIM")
    assert thread.json()["thread"]["status"] != "resolved"


@pytest.mark.asyncio
async def test_rejected_claim_writes_nothing(client: AsyncClient) -> None:
    """A refusal must not be a partial write."""
    await _seed(client, "T-NOWRITE")

    before = await client.get(f"/v1/projects/{PROJECT}/threads/T-NOWRITE")
    count_before = len(before.json()["messages"])

    await client.post(
        f"/ui/projects/{PROJECT}/threads/T-NOWRITE/messages",
        data={
            "type": "question",
            "author": "human",
            "content": "should not land",
            "role": "implementer",
        },
    )

    after = await client.get(f"/v1/projects/{PROJECT}/threads/T-NOWRITE")
    assert len(after.json()["messages"]) == count_before
