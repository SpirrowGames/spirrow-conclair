"""Attribution of non-owner closes, end to end.

`closes_thread_by_non_owner` used to fire on every sanctioned close, so a
project's issue count grew by one per merged PR and `issue_count == 0` stopped
meaning anything. These pins hold the replacement in both directions: the two
sanctioned bypasses must contribute nothing, and a close that never went
through the write path must still be reported exactly once.

The rows that matter most cannot be produced through the API -- that is the
point of the invariant -- so they are injected at the DB layer, which is also
the only way to write the shapes an audit exists to catch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CUTOVER = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
BEFORE_CUTOVER = CUTOVER - timedelta(days=30)
AFTER_CUTOVER = CUTOVER + timedelta(days=1)

LEDGER_SANCTION = {
    "kind": "pr_gate_ledger",
    "pr": "SpirrowGames/spirrow-playproof#57",
    "merged_head": "5be03e6",
    "approving_review_id": "PRR_kwDO_example",
}
HUMAN_SANCTION = {"kind": "human_override", "reason": "owner identity retired"}


@pytest.fixture(autouse=True)
def _cutover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here runs with the recorder declared live at CUTOVER.

    Settings are re-read per request (`Depends(get_settings)`), so setting the
    environment is enough; the test that needs it *unset* clears it again.
    """
    monkeypatch.setenv("SANCTION_RECORDING_SINCE", CUTOVER.isoformat())


async def _open(client: AsyncClient, thread_id: str, owner: str = "orchestrator") -> None:
    r = await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": thread_id,
            "title": "t",
            "owner": owner,
            "propose_content": "start",
            "timestamp": AFTER_CUTOVER.isoformat(),
        },
    )
    assert r.status_code == 201, r.text


async def _inject_unrecorded_close(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    thread_id: str,
    msg_id: str,
    author: str,
    timestamp: datetime,
    with_status_transition_event: bool,
    resolve_thread: bool = True,
) -> None:
    """Write a closing msg straight into the table, bypassing every assert.

    This is the population bucket ③ names: a `closes_thread` row that the
    write path never saw. `with_status_transition_event` decides whether it
    also gets the audit event a real close would have emitted -- the two cases
    need opposite investigations, which is what the reported
    `has_status_transition_event` flag is for.

    `threads.last_msg_num` is kept in step so the injection does not also
    trip `stale_activity_key` and blur the counts being asserted.
    """
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO messages "
                "(project, msg_id, thread_id, author, timestamp, type, content, "
                " references_threads, related_tasks, closes_thread, tags) "
                "VALUES ('p', :msg_id, :thread_id, :author, :ts, 'decide', 'forced', "
                " '[]', '[]', :thread_id, '[]')"
            ),
            {
                "msg_id": msg_id,
                "thread_id": thread_id,
                "author": author,
                "ts": timestamp,
            },
        )
        if with_status_transition_event:
            await session.execute(
                text(
                    "INSERT INTO chatroom_events "
                    "(project, timestamp, actor, action, thread_id, msg_id, details) "
                    "VALUES ('p', :ts, :author, 'status_transition', :thread_id, "
                    " :msg_id, '{\"from\":\"active\",\"to\":\"resolved\"}')"
                ),
                {
                    "msg_id": msg_id,
                    "thread_id": thread_id,
                    "author": author,
                    "ts": timestamp,
                },
            )
        params: dict[str, Any] = {
            "msg_num": int(msg_id.removeprefix("msg-")),
            "thread_id": thread_id,
        }
        resolved = ""
        if resolve_thread:
            resolved = "status = 'resolved', resolved_by_msg = :msg_id, "
            params["msg_id"] = msg_id
        await session.execute(
            text(
                f"UPDATE threads SET {resolved}"
                "last_msg_num = GREATEST(last_msg_num, CAST(:msg_num AS BIGINT)) "
                "WHERE project = 'p' AND thread_id = :thread_id"
            ),
            params,
        )
        await session.commit()


async def _audit(client: AsyncClient) -> dict[str, Any]:
    r = await client.get("/v1/projects/p/integrity")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


# ----- pin 1 / 2: the two sanctioned bypasses contribute nothing -----


async def test_ledger_carve_out_is_counted_not_reported(client: AsyncClient) -> None:
    await _open(client, "T-pr-review-p-57")
    r = await client.post(
        "/v1/projects/p/threads/T-pr-review-p-57/close",
        json={
            "summary_content": "ledger closed",
            "author": "Bohr",
            "owner_override": True,
            "close_sanction": LEDGER_SANCTION,
        },
    )
    assert r.status_code == 201, r.text

    body = await _audit(client)
    assert body["issue_count"] == 0, body["issues"]
    assert body["sanctioned_counts"] == {"pr_gate_ledger": 1, "human_override": 0}
    assert body["unattributable"] == []


async def test_human_force_close_is_counted_not_reported(client: AsyncClient) -> None:
    await _open(client, "T-legacy", owner="claude.ai")
    r = await client.post(
        "/v1/projects/p/threads/T-legacy/close",
        json={
            "summary_content": "superseded",
            "author": "human",
            "owner_override": True,
            "owner_override_reason": "owner identity retired",
            "close_sanction": HUMAN_SANCTION,
        },
    )
    assert r.status_code == 201, r.text

    body = await _audit(client)
    assert body["issue_count"] == 0, body["issues"]
    assert body["sanctioned_counts"] == {"pr_gate_ledger": 0, "human_override": 1}


async def test_the_sanction_is_recorded_against_its_own_message(
    client: AsyncClient,
) -> None:
    """The record is per-message, which is what makes the audit's join sound.

    The event's own `msg_id` column carries the attribution, so a second
    closing row in the same thread cannot inherit this one's sanction.
    """
    await _open(client, "T-pr-review-p-58")
    r = await client.post(
        "/v1/projects/p/threads/T-pr-review-p-58/close",
        json={
            "summary_content": "ledger closed",
            "author": "Bohr",
            "owner_override": True,
            "close_sanction": LEDGER_SANCTION,
        },
    )
    decide_msg_id = r.json()["decide_msg"]["msg_id"]

    events = (await client.get("/v1/projects/p/events")).json()["items"]
    carrying = [e for e in events if "close_sanction" in e["details"]]
    assert len(carrying) == 1, events
    assert carrying[0]["msg_id"] == decide_msg_id
    assert carrying[0]["details"]["close_sanction"] == LEDGER_SANCTION


async def test_an_owner_closing_their_own_thread_records_nothing(
    client: AsyncClient,
) -> None:
    """The recorder's predicate is the audit's predicate, and nothing wider."""
    await _open(client, "T-own", owner="alice")
    await client.post(
        "/v1/projects/p/threads/T-own/close",
        json={"summary_content": "done", "author": "alice"},
    )

    events = (await client.get("/v1/projects/p/events")).json()["items"]
    assert not [e for e in events if "close_sanction" in e["details"]]
    body = await _audit(client)
    assert body["issue_count"] == 0
    assert body["sanctioned_counts"] == {"pr_gate_ledger": 0, "human_override": 0}
    assert body["unattributable"] == []


# ----- pin 3 / 8: an unrecorded close is still reported, exactly once -----


async def test_unrecorded_close_after_the_cutover_is_reported(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reached the write path's table without its recorder: still an issue.

    Given an event but no sanction -- the shape a rollback window produces --
    so `has_status_transition_event` reports true and the investigation starts
    from "which version was deployed", not "who wrote to the DB".
    """
    await _open(client, "T-1")
    await _inject_unrecorded_close(
        session_factory,
        thread_id="T-1",
        msg_id="msg-900",
        author="Bohr",
        timestamp=AFTER_CUTOVER,
        with_status_transition_event=True,
    )

    body = await _audit(client)
    assert body["issue_count"] == 1, body["issues"]
    issue = body["issues"][0]
    assert issue["type"] == "closes_thread_by_non_owner"
    assert issue["msg_id"] == "msg-900"
    assert issue["has_status_transition_event"] is True
    assert body["unattributable"] == []


async def test_close_with_no_status_transition_event_at_all(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pin #8: the checker must not depend on the artifact it is missing.

    An earlier draft compared the *event's* timestamp against the cutover.
    For exactly this row there is no event, so that comparison was against
    NULL -- the row would raise or vanish, and the one case bucket ③ exists
    for was the one it could not classify.
    """
    await _open(client, "T-1")
    await _inject_unrecorded_close(
        session_factory,
        thread_id="T-1",
        msg_id="msg-900",
        author="Bohr",
        timestamp=AFTER_CUTOVER,
        with_status_transition_event=False,
    )

    body = await _audit(client)
    issues = [i for i in body["issues"] if i["type"] == "closes_thread_by_non_owner"]
    assert len(issues) == 1, body["issues"]
    assert issues[0]["msg_id"] == "msg-900"
    assert issues[0]["has_status_transition_event"] is False


# ----- pin 7: one thread, two closing messages -----


async def test_a_sanctioned_close_does_not_cover_a_sibling_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The cardinality pin.

    A thread has exactly one `status_transition` to `resolved`, so an audit
    that looked the sanction up by *thread* would find the legitimate close's
    record and clear the injected row with it -- a false negative produced by
    the very mechanism meant to remove false positives. Keyed by msg_id, the
    sanction covers only the row it names.

    The same asymmetry applies to the diagnostic flag: the thread does have a
    status-transition event, but it belongs to the other message.
    """
    await _open(client, "T-1")
    r = await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={
            "summary_content": "ledger closed",
            "author": "Bohr",
            "owner_override": True,
            "close_sanction": LEDGER_SANCTION,
        },
    )
    assert r.status_code == 201, r.text
    await _inject_unrecorded_close(
        session_factory,
        thread_id="T-1",
        msg_id="msg-900",
        author="mallory",
        timestamp=AFTER_CUTOVER,
        with_status_transition_event=False,
        # The legitimate close already set resolved_by_msg; leaving it alone
        # keeps `inconsistent_resolved` quiet so the count below is about the
        # invariant under test.
        resolve_thread=False,
    )

    body = await _audit(client)
    assert body["issue_count"] == 1, body["issues"]
    assert body["issues"][0]["msg_id"] == "msg-900"
    assert body["issues"][0]["has_status_transition_event"] is False
    assert body["sanctioned_counts"]["pr_gate_ledger"] == 1


# ----- pin 4: legacy rows leave `issues` without being erased -----


async def test_legacy_closes_move_out_of_issues_into_unattributable(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The 45 existing rows: not repaired, not backfilled, not called healthy.

    They are frozen -- the recorder makes the set unable to grow -- and
    `issue_count` becomes usable the moment they leave it.
    """
    await _open(client, "T-1")
    await _inject_unrecorded_close(
        session_factory,
        thread_id="T-1",
        msg_id="msg-900",
        author="Bohr",
        timestamp=BEFORE_CUTOVER,
        with_status_transition_event=True,
    )

    body = await _audit(client)
    assert body["issue_count"] == 0, body["issues"]
    assert body["unattributable"] == [
        {"thread_id": "T-1", "msg_id": "msg-900", "reason": "pre_recording"}
    ]
    assert body["sanction_recording_since"].startswith("2026-09-06T12:00:00")


async def test_without_a_configured_cutover_nothing_is_called_corruption(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploying the code without setting the instant disarms bucket ③.

    Reported rather than assumed: `sanction_recording_since: null` in the
    response is how a reader learns the strictest check is not running.
    """
    monkeypatch.delenv("SANCTION_RECORDING_SINCE", raising=False)
    await _open(client, "T-1")
    await _inject_unrecorded_close(
        session_factory,
        thread_id="T-1",
        msg_id="msg-900",
        author="Bohr",
        timestamp=AFTER_CUTOVER,
        with_status_transition_event=False,
    )

    body = await _audit(client)
    assert body["issue_count"] == 0, body["issues"]
    assert body["sanction_recording_since"] is None
    assert body["unattributable"][0]["reason"] == "pre_recording"


# ----- deploy skew: the bare boolean is survivable in both orders -----


async def test_a_legacy_owner_override_is_unclassified_not_corrupt(
    client: AsyncClient,
) -> None:
    """Conclair upgraded, Magickit not yet.

    The close still succeeds, is recorded as `unspecified`, and stays out of
    `issues` even though it is well after the cutover -- the classification
    branches on the record first and consults the clock only when there is
    none. Its own bucket, so a count that should fall to zero after Magickit
    ships is visible if it does not.
    """
    await _open(client, "T-pr-review-p-57")
    r = await client.post(
        "/v1/projects/p/threads/T-pr-review-p-57/close",
        json={
            "summary_content": "ledger closed",
            "author": "Bohr",
            "owner_override": True,
        },
    )
    assert r.status_code == 201, r.text

    body = await _audit(client)
    assert body["issue_count"] == 0, body["issues"]
    assert body["sanctioned_counts"] == {"pr_gate_ledger": 0, "human_override": 0}
    assert [u["reason"] for u in body["unattributable"]] == ["unclassified_override"]


async def test_an_unknown_sanction_field_is_ignored_not_refused(
    client: AsyncClient,
) -> None:
    """Magickit upgraded first: the request must not 422.

    Request schemas do not forbid extras, so a field Conclair has not learned
    yet is dropped rather than rejected -- the close goes through and lands in
    `unclassified_override`, which is the visible form of the skew.
    """
    await _open(client, "T-pr-review-p-57")
    r = await client.post(
        "/v1/projects/p/threads/T-pr-review-p-57/close",
        json={
            "summary_content": "ledger closed",
            "author": "Bohr",
            "owner_override": True,
            "close_sanction_v2": LEDGER_SANCTION,
        },
    )
    assert r.status_code == 201, r.text
    body = await _audit(client)
    assert body["issue_count"] == 0, body["issues"]
    assert [u["reason"] for u in body["unattributable"]] == ["unclassified_override"]


async def test_a_hollow_ledger_claim_is_refused_at_the_boundary(
    client: AsyncClient,
) -> None:
    """`chatroom_events` is append-only, so an unfalsifiable record is forever."""
    await _open(client, "T-pr-review-p-57")
    r = await client.post(
        "/v1/projects/p/threads/T-pr-review-p-57/close",
        json={
            "summary_content": "ledger closed",
            "author": "Bohr",
            "owner_override": True,
            "close_sanction": {"kind": "pr_gate_ledger", "pr": "x#1"},
        },
    )
    assert r.status_code == 422, r.text


# ----- pin 5: the other invariants are untouched -----


async def test_the_other_invariants_still_fire_alongside(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A sanctioned close must not quiet the checks that share the report."""
    await _open(client, "T-1")
    await client.post(
        "/v1/projects/p/threads/T-1/close",
        json={
            "summary_content": "ledger closed",
            "author": "Bohr",
            "owner_override": True,
            "close_sanction": LEDGER_SANCTION,
        },
    )
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO threads "
                "(project, thread_id, title, owner, status, created_at, "
                " created_by_msg, resolved_by_msg, affects_threads, tags) "
                "VALUES ('p','T-bad','t','alice','resolved', :now, 'msg-x', "
                " NULL, '[]', '[]')"
            ),
            {"now": AFTER_CUTOVER},
        )
        await session.commit()

    body = await _audit(client)
    types = {i["type"] for i in body["issues"]}
    assert "inconsistent_resolved" in types
    assert "closes_thread_by_non_owner" not in types
    assert body["sanctioned_counts"]["pr_gate_ledger"] == 1


async def test_post_message_route_records_the_sanction_too(
    client: AsyncClient,
) -> None:
    """Both close routes share one recorder, so both must carry attribution.

    A recorder placed per-endpoint would leave the forgotten one's closes
    indistinguishable from rows written straight to the table.
    """
    await _open(client, "T-1")
    r = await client.post(
        "/v1/projects/p/threads/T-1/messages",
        json={
            "type": "decide",
            "author": "human",
            "content": "force closed",
            "closes_thread": "T-1",
            "owner_override": True,
            "owner_override_reason": "owner identity retired",
            "close_sanction": HUMAN_SANCTION,
        },
    )
    assert r.status_code == 201, r.text

    body = await _audit(client)
    assert body["issue_count"] == 0, body["issues"]
    assert body["sanctioned_counts"]["human_override"] == 1
