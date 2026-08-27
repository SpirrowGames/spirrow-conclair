"""End-to-end tests for the thread digest endpoints.

Conclair stores digests it did not make. The tests that matter most here are
therefore not about the round-trip but about the two things a store can get
wrong: claiming coverage it does not have, and pretending to be chatroom
activity.
"""

from __future__ import annotations

from httpx import AsyncClient

PROJECT = "dig-proj"

BASE_BODY = {
    "digest": "Bohr が X 方式を提案、Heisenberg が実装。Einstein が Y を指摘。",
    "source_msg_count": 1,
    "producer": "magickit-digest-sweeper",
}


async def _open(
    client: AsyncClient, project: str, thread_id: str, owner: str = "alice"
) -> str:
    r = await client.post(
        f"/v1/projects/{project}/threads",
        json={
            "thread_id": thread_id,
            "title": "t",
            "owner": owner,
            "propose_content": "start",
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["msg"]["msg_id"])


async def _post(
    client: AsyncClient, project: str, thread_id: str, **body: object
) -> str:
    payload: dict[str, object] = {"type": "report", "author": "bob", "content": "c"}
    payload.update(body)
    r = await client.post(
        f"/v1/projects/{project}/threads/{thread_id}/messages", json=payload
    )
    assert r.status_code == 201, r.text
    return str(r.json()["msg"]["msg_id"])


async def _put_digest(
    client: AsyncClient, project: str, thread_id: str, **body: object
) -> tuple[int, dict]:
    r = await client.put(
        f"/v1/projects/{project}/threads/{thread_id}/digest",
        json={**BASE_BODY, **body},
    )
    return r.status_code, r.json()


async def _get_digest(
    client: AsyncClient, project: str, thread_id: str, **params: object
) -> tuple[int, dict]:
    r = await client.get(
        f"/v1/projects/{project}/threads/{thread_id}/digest", params=params
    )
    return r.status_code, r.json()


# --- round trip and provenance -------------------------------------------


async def test_put_then_get_round_trip_keeps_every_provenance_field(
    client: AsyncClient,
) -> None:
    first = await _open(client, PROJECT, "T-1")

    code, body = await _put_digest(
        client,
        PROJECT,
        "T-1",
        source_last_msg_id=first,
        style="concise",
        truncated=True,
        model="Qwen3-32B",
        tier="light",
        source_chars=21000,
        input_tokens=6000,
        output_tokens=380,
        duration_ms=18400,
    )
    assert code == 200, body

    code, body = await _get_digest(client, PROJECT, "T-1", style="concise")
    assert code == 200, body
    assert body["present"] is True
    digest = body["digest"]
    assert digest["digest"] == BASE_BODY["digest"]
    assert digest["source_last_msg_id"] == first
    assert digest["truncated"] is True
    assert digest["model"] == "Qwen3-32B"
    assert digest["tier"] == "light"
    assert digest["producer"] == "magickit-digest-sweeper"
    assert digest["source_chars"] == 21000
    assert digest["input_tokens"] == 6000
    assert digest["output_tokens"] == 380
    assert digest["duration_ms"] == 18400
    assert digest["generated_at"] is not None


# --- absence is a normal answer, a missing thread is not ------------------


async def test_no_digest_yet_is_200_with_present_false(client: AsyncClient) -> None:
    """"Not digested yet" must not be readable as an outage.

    A producer that treated a read failure as "no digest" would spend one
    light-tier call; one that treated "no digest" as a failure would never
    digest anything. So absence is stated, in a 200.
    """
    await _open(client, PROJECT, "T-quiet")

    code, body = await _get_digest(client, PROJECT, "T-quiet")

    assert code == 200, body
    assert body["present"] is False
    assert body["digest"] is None
    assert body["thread_last_msg_id"] is not None
    assert body["thread_msg_count"] == 1


async def test_missing_thread_is_404(client: AsyncClient) -> None:
    """Matches get_thread for the same URL prefix.

    Deliberately unlike `/control`, which never 404s: there, absence read as
    failure would stop every project. Here the asymmetry runs the other way,
    so a thread_id that does not exist is a real error.
    """
    code, body = await _get_digest(client, PROJECT, "T-nope")
    assert code == 404, body
    assert body["error_type"] == "ChatroomNotFoundError"


async def test_put_on_a_missing_thread_is_404(client: AsyncClient) -> None:
    code, body = await _put_digest(
        client, PROJECT, "T-nope", source_last_msg_id="msg-001"
    )
    assert code == 404, body


# --- the coverage key must belong to this thread -------------------------


async def test_unknown_source_last_msg_id_is_409(client: AsyncClient) -> None:
    await _open(client, PROJECT, "T-1")

    code, body = await _put_digest(
        client, PROJECT, "T-1", source_last_msg_id="msg-999"
    )

    assert code == 409, body
    assert body["error_type"] == "ChatroomIntegrityError"


async def test_a_sibling_threads_msg_id_is_409(client: AsyncClient) -> None:
    """`msg_id` is allocated project-wide, so this is the trap the assert exists for.

    Without the `thread_id` filter, T-2's msg would be accepted as T-1's
    coverage point -- and a digest whose key belongs to another thread can
    never have its shortfall measured.
    """
    await _open(client, PROJECT, "T-1")
    sibling_msg = await _open(client, PROJECT, "T-2")

    code, body = await _put_digest(
        client, PROJECT, "T-1", source_last_msg_id=sibling_msg
    )

    assert code == 409, body
    assert "T-1" in body["error"]


async def test_an_over_padded_msg_id_is_409(client: AsyncClient) -> None:
    """`format_msg_id` has one canonical form per integer.

    `msg-0042` and `msg-042` are not interchangeable, so an over-padded key
    would compare unequal forever and the digest would look permanently
    stale. Rejecting it at write time is the second reason this assert earns
    its round-trip.
    """
    real = await _open(client, PROJECT, "T-1")
    padded = "msg-0" + real.removeprefix("msg-")

    code, body = await _put_digest(client, PROJECT, "T-1", source_last_msg_id=padded)

    assert code == 409, body


# --- upsert semantics ----------------------------------------------------


async def test_a_second_put_replaces_the_first_including_provenance(
    client: AsyncClient,
) -> None:
    first = await _open(client, PROJECT, "T-1")
    second = await _post(client, PROJECT, "T-1")

    await _put_digest(
        client,
        PROJECT,
        "T-1",
        source_last_msg_id=first,
        digest="古い要約",
        model="Qwen3-32B",
        truncated=True,
        producer="magickit-digest-sweeper",
    )
    code, body = await _put_digest(
        client,
        PROJECT,
        "T-1",
        source_last_msg_id=second,
        source_msg_count=2,
        digest="新しい要約",
        producer="magickit-digest-ondemand",
    )
    assert code == 200, body

    code, body = await _get_digest(client, PROJECT, "T-1")
    digest = body["digest"]
    assert digest["digest"] == "新しい要約"
    assert digest["source_last_msg_id"] == second
    assert digest["producer"] == "magickit-digest-ondemand"
    # A re-PUT is a new generation: nothing from the old one may survive.
    assert digest["model"] is None
    assert digest["truncated"] is False


async def test_a_different_style_is_a_different_digest(client: AsyncClient) -> None:
    """`style` is part of the key so a new prompt cannot clobber the rendered one."""
    first = await _open(client, PROJECT, "T-1")

    await _put_digest(
        client, PROJECT, "T-1", source_last_msg_id=first, style="concise",
        digest="短い要約",
    )
    await _put_digest(
        client, PROJECT, "T-1", source_last_msg_id=first, style="bullet",
        digest="・箇条書き",
    )

    _, concise = await _get_digest(client, PROJECT, "T-1", style="concise")
    _, bullet = await _get_digest(client, PROJECT, "T-1", style="bullet")
    assert concise["digest"]["digest"] == "短い要約"
    assert bullet["digest"]["digest"] == "・箇条書き"


async def test_thread_and_message_scopes_coexist(client: AsyncClient) -> None:
    """Proves the two partial unique indexes do not collide."""
    first = await _open(client, PROJECT, "T-1")

    await _put_digest(
        client, PROJECT, "T-1", source_last_msg_id=first, digest="スレッド全体"
    )
    code, body = await _put_digest(
        client,
        PROJECT,
        "T-1",
        source_last_msg_id=first,
        scope="message",
        target_msg_id=first,
        digest="この1件だけ",
    )
    assert code == 200, body

    _, thread_scoped = await _get_digest(client, PROJECT, "T-1")
    _, msg_scoped = await _get_digest(
        client, PROJECT, "T-1", scope="message", target_msg_id=first
    )
    assert thread_scoped["digest"]["digest"] == "スレッド全体"
    assert thread_scoped["digest"]["target_msg_id"] is None
    assert msg_scoped["digest"]["digest"] == "この1件だけ"
    assert msg_scoped["digest"]["target_msg_id"] == first


async def test_message_scope_with_an_unknown_target_is_409(
    client: AsyncClient,
) -> None:
    first = await _open(client, PROJECT, "T-1")

    code, body = await _put_digest(
        client,
        PROJECT,
        "T-1",
        source_last_msg_id=first,
        scope="message",
        target_msg_id="msg-999",
    )

    assert code == 409, body


async def test_incoherent_scope_and_target_is_422(client: AsyncClient) -> None:
    """The pydantic validator answers before the CHECK constraint has to."""
    first = await _open(client, PROJECT, "T-1")

    code, _ = await _put_digest(
        client, PROJECT, "T-1", source_last_msg_id=first, target_msg_id=first
    )
    assert code == 422

    code, _ = await _put_digest(
        client, PROJECT, "T-1", source_last_msg_id=first, scope="message"
    )
    assert code == 422


# --- staleness is derived, not reported ----------------------------------


async def test_a_digest_at_the_head_is_not_stale(client: AsyncClient) -> None:
    first = await _open(client, PROJECT, "T-1")

    _, body = await _put_digest(client, PROJECT, "T-1", source_last_msg_id=first)

    assert body["digest"]["behind_by"] == 0
    assert body["digest"]["stale"] is False
    assert body["thread_last_msg_id"] == first


async def test_later_messages_make_it_stale(client: AsyncClient) -> None:
    first = await _open(client, PROJECT, "T-1")
    await _put_digest(client, PROJECT, "T-1", source_last_msg_id=first)

    await _post(client, PROJECT, "T-1")
    await _post(client, PROJECT, "T-1")

    _, body = await _get_digest(client, PROJECT, "T-1")
    assert body["digest"]["behind_by"] == 2
    assert body["digest"]["stale"] is True
    # The head it was measured against comes back too, so a caller does not
    # need a second read to see what `behind_by` is relative to.
    assert body["thread_last_msg_id"] != first


async def test_behind_by_ignores_sibling_threads(client: AsyncClient) -> None:
    """The count is filtered on thread_id, not derived from the sequence.

    `msg_id` is project-wide, so interleaved posts in another thread advance
    the numbers this digest's key is compared against. Arithmetic over the
    sequence -- or `thread.msg_count - source_msg_count` -- would report
    them as this thread's unread work.
    """
    t1_first = await _open(client, PROJECT, "T-1")
    await _open(client, PROJECT, "T-2")
    await _put_digest(client, PROJECT, "T-1", source_last_msg_id=t1_first)

    await _post(client, PROJECT, "T-2")
    await _post(client, PROJECT, "T-2")
    await _post(client, PROJECT, "T-2")
    await _post(client, PROJECT, "T-1")

    _, body = await _get_digest(client, PROJECT, "T-1")
    assert body["digest"]["behind_by"] == 1


async def test_source_msg_count_is_provenance_not_the_verdict(
    client: AsyncClient,
) -> None:
    """A truncated digest reports fewer msgs than the thread holds.

    That is the producer saying what it read, not a shortfall. Subtracting it
    would report windowing as staleness.
    """
    first = await _open(client, PROJECT, "T-1")
    await _post(client, PROJECT, "T-1")
    head = await _post(client, PROJECT, "T-1")
    assert first != head

    _, body = await _put_digest(
        client,
        PROJECT,
        "T-1",
        source_last_msg_id=head,
        source_msg_count=2,  # it only read 2 of the 3
        truncated=True,
    )

    assert body["thread_msg_count"] == 3
    assert body["digest"]["source_msg_count"] == 2
    assert body["digest"]["behind_by"] == 0
    assert body["digest"]["stale"] is False
    assert body["digest"]["truncated"] is True


# --- a digest write is not chatroom activity -----------------------------


async def test_a_digest_write_adds_no_chatroom_event(client: AsyncClient) -> None:
    """Two reasons, either sufficient — this is the regression test for both.

    (1) Magickit's ops dashboard reads `GET /events?limit=1` as its
        "稼働中の根拠". A digest write appearing there would report a dead
        loop as running: the dashboard would say the thing it exists to
        detect is fine.
    (2) `schemas/event.py::EventAction` is a closed Literal validated per
        row on the way out, while the column has no CHECK — so an unlisted
        action inserts happily and then 500s the whole event log.
    """
    first = await _open(client, PROJECT, "T-1")

    before = await client.get(f"/v1/projects/{PROJECT}/events")
    assert before.status_code == 200
    count_before = before.json()["total"]
    latest_before = (
        await client.get(f"/v1/projects/{PROJECT}/events", params={"limit": 1})
    ).json()["items"]

    await _put_digest(client, PROJECT, "T-1", source_last_msg_id=first)

    after = await client.get(f"/v1/projects/{PROJECT}/events")
    # Still 200: an unlisted action would have made this a 500.
    assert after.status_code == 200, after.text
    assert after.json()["total"] == count_before

    latest_after = (
        await client.get(f"/v1/projects/{PROJECT}/events", params={"limit": 1})
    ).json()["items"]
    assert latest_after == latest_before


# --- embedded in get_thread ---------------------------------------------


async def test_get_thread_embeds_the_digest_only_when_asked(
    client: AsyncClient,
) -> None:
    first = await _open(client, PROJECT, "T-1")
    await _put_digest(client, PROJECT, "T-1", source_last_msg_id=first)

    plain = await client.get(f"/v1/projects/{PROJECT}/threads/T-1")
    assert plain.status_code == 200
    assert plain.json()["digest"] is None

    asked = await client.get(
        f"/v1/projects/{PROJECT}/threads/T-1", params={"include_digest": "true"}
    )
    assert asked.status_code == 200
    assert asked.json()["digest"]["present"] is True


async def test_get_thread_reports_absence_distinctly_from_not_asking(
    client: AsyncClient,
) -> None:
    """Three states: null / present:false / present:true."""
    await _open(client, PROJECT, "T-quiet")

    asked = await client.get(
        f"/v1/projects/{PROJECT}/threads/T-quiet", params={"include_digest": "true"}
    )
    body = asked.json()
    assert body["digest"] is not None
    assert body["digest"]["present"] is False
    assert body["digest"]["digest"] is None


async def test_mode_and_include_digest_are_orthogonal(client: AsyncClient) -> None:
    """`mode=summary` filters messages; it says nothing about the digest.

    mindwire's read tools depend on `mode=summary` meaning "decide msg only
    on a resolved thread". Passing both must change neither behaviour.
    """
    first = await _open(client, PROJECT, "T-1")
    await _post(client, PROJECT, "T-1", type="handoff", author="alice")
    close = await client.post(
        f"/v1/projects/{PROJECT}/threads/T-1/close",
        json={"author": "alice", "summary_content": "決定"},
    )
    # 201: close_thread creates a decide msg (api/threads.py sets
    # HTTP_201_CREATED), even though it also mutates the thread.
    assert close.status_code == 201, close.text
    head = close.json()["decide_msg"]["msg_id"]

    await _put_digest(client, PROJECT, "T-1", source_last_msg_id=head)

    r = await client.get(
        f"/v1/projects/{PROJECT}/threads/T-1",
        params={"mode": "summary", "include_digest": "true"},
    )
    body = r.json()
    assert body["mode"] == "summary"
    # The message filter still applies, unchanged.
    assert [m["type"] for m in body["messages"]] == ["decide"]
    # And the digest is still there, as a separate object.
    assert body["digest"]["present"] is True
    assert body["digest"]["digest"]["source_last_msg_id"] == head
    # `msg_count` comes from the rollup, not from the filtered list.
    assert body["thread"]["msg_count"] == 3
    assert body["digest"]["thread_msg_count"] == 3
    assert first != head
