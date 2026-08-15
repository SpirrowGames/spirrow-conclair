"""End-to-end tests for the read cursor endpoints."""

from __future__ import annotations

from httpx import AsyncClient


async def _open(client: AsyncClient, project: str, thread_id: str, owner: str = "alice") -> None:
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


async def _post(client: AsyncClient, project: str, thread_id: str, **body) -> tuple[int, dict]:
    r = await client.post(
        f"/v1/projects/{project}/threads/{thread_id}/messages", json=body
    )
    return r.status_code, r.json()


async def _mark_read(client: AsyncClient, project: str, thread_id: str, **body) -> tuple[int, dict]:
    r = await client.post(
        f"/v1/projects/{project}/threads/{thread_id}/read", json=body
    )
    return r.status_code, r.json()


async def _unread(client: AsyncClient, project: str, identity_name: str, **params) -> tuple[int, dict]:
    r = await client.get(
        f"/v1/projects/{project}/unread",
        params={"identity_name": identity_name, **params},
    )
    return r.status_code, r.json()


# --- mark_read happy paths ----------------------------------------------


async def test_first_mark_read_advances_to_specified_msg(client: AsyncClient) -> None:
    """First mark_read on a thread inserts a cursor row and returns
    advanced=True with the requested position."""
    await _open(client, "p", "T-1")
    # The open inserts msg-001 (propose). Post a couple more so we have
    # a target other than the latest.
    await _post(client, "p", "T-1", type="report", author="bob", content="r1")
    await _post(client, "p", "T-1", type="report", author="bob", content="r2")

    code, body = await _mark_read(
        client, "p", "T-1",
        identity_name="Bohr", up_to_msg_id="msg-002",
    )

    assert code == 200, body
    assert body["advanced"] is True
    assert body["last_read_msg_id"] == "msg-002"
    assert body["identity_name"] == "Bohr"
    assert body["thread_id"] == "T-1"


async def test_mark_read_empty_msg_id_advances_to_latest(client: AsyncClient) -> None:
    """``up_to_msg_id=""`` is the catch-up shortcut — advance to the
    current latest msg in the thread."""
    await _open(client, "p", "T-1")
    await _post(client, "p", "T-1", type="report", author="bob", content="r1")
    await _post(client, "p", "T-1", type="report", author="bob", content="r2")
    # latest is now msg-003.

    code, body = await _mark_read(
        client, "p", "T-1",
        identity_name="Bohr", up_to_msg_id="",
    )

    assert code == 200
    assert body["advanced"] is True
    assert body["last_read_msg_id"] == "msg-003"


async def test_mark_read_null_msg_id_advances_to_latest(client: AsyncClient) -> None:
    """``up_to_msg_id`` omitted entirely is equivalent to empty string."""
    await _open(client, "p", "T-1")
    code, body = await _mark_read(
        client, "p", "T-1",
        identity_name="Bohr",
    )
    assert code == 200
    assert body["advanced"] is True
    assert body["last_read_msg_id"] == "msg-001"


# --- mark_read edge cases -----------------------------------------------


async def test_mark_read_same_position_is_noop(client: AsyncClient) -> None:
    """Re-marking at the same position is idempotent: returns
    advanced=False and does not emit a new audit event."""
    await _open(client, "p", "T-1")
    await _post(client, "p", "T-1", type="report", author="bob", content="r")
    await _mark_read(client, "p", "T-1", identity_name="Bohr", up_to_msg_id="msg-002")

    code, body = await _mark_read(
        client, "p", "T-1",
        identity_name="Bohr", up_to_msg_id="msg-002",
    )

    assert code == 200
    assert body["advanced"] is False
    assert body["last_read_msg_id"] == "msg-002"

    # The audit log should have exactly one mark_read event (the first
    # advance), not two.
    r = await client.get(
        "/v1/projects/p/events", params={"action": "mark_read"}
    )
    assert r.status_code == 200
    assert r.json()["total"] == 1


async def test_mark_read_rewind_is_silent_noop(client: AsyncClient) -> None:
    """Requesting a msg_id older than the current cursor returns the
    current cursor unchanged with advanced=False (the user picked
    monotonic-forward only)."""
    await _open(client, "p", "T-1")
    await _post(client, "p", "T-1", type="report", author="bob", content="r1")
    await _post(client, "p", "T-1", type="report", author="bob", content="r2")
    await _mark_read(client, "p", "T-1", identity_name="Bohr", up_to_msg_id="msg-003")

    code, body = await _mark_read(
        client, "p", "T-1",
        identity_name="Bohr", up_to_msg_id="msg-001",
    )

    assert code == 200
    assert body["advanced"] is False
    # The cursor stays at msg-003 -- the rewind request did not move it.
    assert body["last_read_msg_id"] == "msg-003"


async def test_mark_read_thread_not_found(client: AsyncClient) -> None:
    code, body = await _mark_read(
        client, "p", "T-missing",
        identity_name="Bohr", up_to_msg_id="msg-001",
    )
    assert code == 404
    assert body["error_type"] == "ChatroomNotFoundError"


async def test_mark_read_msg_not_in_thread(client: AsyncClient) -> None:
    """An explicit msg_id that isn't in the thread is an integrity
    error -- this prevents pointing the cursor at someone else's msg
    from a different thread or at a never-allocated id."""
    await _open(client, "p", "T-1")

    code, body = await _mark_read(
        client, "p", "T-1",
        identity_name="Bohr", up_to_msg_id="msg-999",
    )
    assert code == 409
    assert body["error_type"] == "ChatroomIntegrityError"
    assert "msg-999" in body["error"]


# --- unread list --------------------------------------------------------


async def test_unread_empty_when_caught_up(client: AsyncClient) -> None:
    await _open(client, "p", "T-1")
    await _mark_read(client, "p", "T-1", identity_name="Bohr", up_to_msg_id="")

    code, body = await _unread(client, "p", "Bohr")

    assert code == 200
    assert body["total"] == 0
    assert body["items"] == []


async def test_unread_never_read_thread_counts_all_msgs(client: AsyncClient) -> None:
    """A thread the identity has never mark_read'd appears in the inbox
    with unread_count = full thread size and last_read_msg_id = null
    (the cursor row doesn't exist yet -- handoff-safety default)."""
    await _open(client, "p", "T-1")
    await _post(client, "p", "T-1", type="report", author="alice", content="r1")
    await _post(client, "p", "T-1", type="report", author="alice", content="r2")
    # 3 msgs total: propose + 2 reports.

    code, body = await _unread(client, "p", "Bohr")

    assert code == 200
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["thread_id"] == "T-1"
    assert item["latest_msg_id"] == "msg-003"
    assert item["last_read_msg_id"] is None
    assert item["unread_count"] == 3


async def test_unread_partial_read_returns_diff(client: AsyncClient) -> None:
    await _open(client, "p", "T-1")
    await _post(client, "p", "T-1", type="report", author="alice", content="r1")
    await _post(client, "p", "T-1", type="report", author="alice", content="r2")
    await _mark_read(
        client, "p", "T-1",
        identity_name="Bohr", up_to_msg_id="msg-001",
    )

    code, body = await _unread(client, "p", "Bohr")

    assert code == 200
    assert body["total"] == 1
    item = body["items"][0]
    assert item["last_read_msg_id"] == "msg-001"
    assert item["latest_msg_id"] == "msg-003"
    assert item["unread_count"] == 2


async def test_unread_count_is_per_thread_not_project_wide(
    client: AsyncClient,
) -> None:
    """Regression for the project-wide-msg_id arithmetic bug.

    ``msg_id`` is allocated project-wide, so two threads with
    interleaved post histories share the same numeric sequence. The
    inbox ``unread_count`` for each thread must reflect the count of
    msgs in *that* thread, never the gap in the project-wide numeric
    sequence (which would include msgs from sibling threads).

    Scenario:
      - T-A opens at msg-001 (propose by alice)
      - T-B opens at msg-002 (propose by alice)
      - T-A posts msg-003, msg-004 (alice)
      - T-B posts msg-005, msg-006, msg-007 (alice)

      T-A has 3 msgs total (msg-001, msg-003, msg-004).
      T-B has 4 msgs total (msg-002, msg-005, msg-006, msg-007).

      For Bohr (never read either), the project-wide-numeric formula
      would have reported T-A unread_count=4 and T-B unread_count=7 (a
      total of 11, more than the 7 msgs in the project). The corrected
      per-thread count must give 3 and 4.
    """
    await _open(client, "p", "T-A", owner="alice")
    await _open(client, "p", "T-B", owner="alice")
    await _post(client, "p", "T-A", type="report", author="alice", content="a-1")
    await _post(client, "p", "T-A", type="report", author="alice", content="a-2")
    await _post(client, "p", "T-B", type="report", author="alice", content="b-1")
    await _post(client, "p", "T-B", type="report", author="alice", content="b-2")
    await _post(client, "p", "T-B", type="report", author="alice", content="b-3")

    code, body = await _unread(client, "p", "Bohr")
    assert code == 200, body
    assert body["total"] == 2

    by_id = {it["thread_id"]: it for it in body["items"]}
    assert by_id["T-A"]["unread_count"] == 3
    assert by_id["T-A"]["latest_msg_id"] == "msg-004"
    assert by_id["T-B"]["unread_count"] == 4
    assert by_id["T-B"]["latest_msg_id"] == "msg-007"


async def test_unread_count_with_cursor_in_interleaved_project(
    client: AsyncClient,
) -> None:
    """Same interleaved setup, but Bohr has read partway into T-A.

    Confirms the cursor comparison is also per-thread: advancing T-A's
    cursor to msg-003 leaves only msg-004 unread there (1 msg, not the
    numeric difference 7-3=4 the buggy formula would have given), and
    T-B's cursor is untouched so T-B still shows 4 unread.
    """
    await _open(client, "p", "T-A", owner="alice")
    await _open(client, "p", "T-B", owner="alice")
    await _post(client, "p", "T-A", type="report", author="alice", content="a-1")  # msg-003
    await _post(client, "p", "T-A", type="report", author="alice", content="a-2")  # msg-004
    await _post(client, "p", "T-B", type="report", author="alice", content="b-1")  # msg-005
    await _post(client, "p", "T-B", type="report", author="alice", content="b-2")  # msg-006
    await _post(client, "p", "T-B", type="report", author="alice", content="b-3")  # msg-007

    await _mark_read(
        client, "p", "T-A",
        identity_name="Bohr", up_to_msg_id="msg-003",
    )

    code, body = await _unread(client, "p", "Bohr")
    assert code == 200
    assert body["total"] == 2

    by_id = {it["thread_id"]: it for it in body["items"]}
    # T-A: cursor at msg-003, only msg-004 (also in T-A) is unread.
    assert by_id["T-A"]["unread_count"] == 1
    assert by_id["T-A"]["last_read_msg_id"] == "msg-003"
    # T-B: no cursor; all 4 of its msgs are unread (msg-002, 005, 006, 007).
    assert by_id["T-B"]["unread_count"] == 4
    assert by_id["T-B"]["last_read_msg_id"] is None


async def test_unread_ties_break_on_activity_not_creation(client: AsyncClient) -> None:
    """The docstring has always said "most unread first, then by thread
    recency"; the tiebreaker sorted on `created_at`, so a thread opened
    later but silent outranked an older one posted to since. Equal unread
    counts here make the tiebreaker the only thing under test."""
    await _open(client, "p", "T-old")  # msg-001
    await _open(client, "p", "T-young")  # msg-002
    await _post(client, "p", "T-old", type="report", author="bob", content="r")  # msg-003
    await _mark_read(
        client, "p", "T-old", identity_name="Heisenberg", up_to_msg_id="msg-001",
    )

    code, body = await _unread(client, "p", "Heisenberg")

    assert code == 200, body
    # The tie is real: one unread msg each.
    assert {i["thread_id"]: i["unread_count"] for i in body["items"]} == {
        "T-old": 1,
        "T-young": 1,
    }
    assert [i["thread_id"] for i in body["items"]] == ["T-old", "T-young"]


async def test_unread_excludes_resolved_by_default(client: AsyncClient) -> None:
    await _open(client, "p", "T-1")
    # Close the thread -- alice is the owner.
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "done", "author": "alice"},
    )
    assert r.status_code == 201, r.text

    # Bohr has never read this thread, but it's resolved so it should
    # not appear in the default inbox.
    code, body = await _unread(client, "p", "Bohr")
    assert code == 200
    assert body["total"] == 0


async def test_unread_includes_resolved_with_flag(client: AsyncClient) -> None:
    await _open(client, "p", "T-1")
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={"summary_content": "done", "author": "alice"},
    )
    assert r.status_code == 201

    code, body = await _unread(
        client, "p", "Bohr", include_resolved="true",
    )
    assert code == 200
    assert body["total"] == 1
    assert body["items"][0]["status"] == "resolved"


async def test_unread_per_identity_isolation(client: AsyncClient) -> None:
    """Bohr's cursor doesn't affect Heisenberg's inbox."""
    await _open(client, "p", "T-1")
    await _post(client, "p", "T-1", type="report", author="alice", content="r")
    await _mark_read(
        client, "p", "T-1",
        identity_name="Bohr", up_to_msg_id="",
    )

    bohr_code, bohr_body = await _unread(client, "p", "Bohr")
    heis_code, heis_body = await _unread(client, "p", "Heisenberg")

    assert bohr_body["total"] == 0
    assert heis_body["total"] == 1
    assert heis_body["items"][0]["unread_count"] == 2


# --- audit log ----------------------------------------------------------


async def test_mark_read_emits_audit_event(client: AsyncClient) -> None:
    await _open(client, "p", "T-1")
    await _mark_read(
        client, "p", "T-1",
        identity_name="Bohr", up_to_msg_id="",
    )

    r = await client.get(
        "/v1/projects/p/events", params={"action": "mark_read"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    ev = body["items"][0]
    assert ev["actor"] == "Bohr"
    assert ev["action"] == "mark_read"
    assert ev["thread_id"] == "T-1"
    assert ev["msg_id"] == "msg-001"
    assert ev["details"] == {"from": None, "to": "msg-001"}


async def test_mark_read_event_records_previous_cursor(client: AsyncClient) -> None:
    """The audit event's ``from`` field records the prior cursor value
    so a reader can reconstruct the sequence of advances."""
    await _open(client, "p", "T-1")
    await _post(client, "p", "T-1", type="report", author="alice", content="r")
    await _mark_read(client, "p", "T-1", identity_name="Bohr", up_to_msg_id="msg-001")
    await _mark_read(client, "p", "T-1", identity_name="Bohr", up_to_msg_id="msg-002")

    r = await client.get(
        "/v1/projects/p/events", params={"action": "mark_read"}
    )
    body = r.json()
    assert body["total"] == 2
    # events come back in timestamp asc order
    first, second = sorted(body["items"], key=lambda e: e["timestamp"])
    assert first["details"] == {"from": None, "to": "msg-001"}
    assert second["details"] == {"from": "msg-001", "to": "msg-002"}
