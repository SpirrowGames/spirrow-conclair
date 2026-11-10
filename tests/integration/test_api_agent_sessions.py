"""End-to-end tests for the agent session endpoints (T45 PR1, DoD).

The rule-by-rule matrix (every fixed column, every kind) is in
``tests/unit/test_agent_session.py``, against the pure ``plan_declare``. This
file pins what only a real Postgres can show: the row really is one row,
a refusal really writes nothing, the CHECK really refuses a loop row without
a session id, and two simultaneous ``start`` declarations neither duplicate
the row nor raise.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.models import AgentSession

SECRET = "mw-session-7f3a"
HOOK: dict[str, Any] = {
    "project": "p",
    "source": "hook",
    "identity": "Bohr",
    "instance_id": "proposer-1",
    "embodiment": "terminal_coding_agent",
    "host": "sg-tomtebo-01",
    "thread_id": "T-1",
}
LOOP: dict[str, Any] = {**HOOK, "source": "loop", "mindwire_session_id": SECRET}


async def _put(client: AsyncClient, uuid: str, base: dict[str, Any], kind: str, **over: Any):
    return await client.put(f"/v1/agent_sessions/{uuid}", json={**base, "kind": kind, **over})


async def _count(db: AsyncSession, uuid: str) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(AgentSession)
            .where(AgentSession.cc_session_uuid == uuid)
        )
        or 0
    )


async def _stored(db: AsyncSession, uuid: str) -> AgentSession:
    db.expire_all()
    row = await db.get(AgentSession, uuid)
    assert row is not None
    return row


async def test_start_twice_keeps_one_row_and_advances_last_declared_at(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """D3: a /compact re-declaration does not register the session twice."""
    first = await _put(client, "u-1", HOOK, "start")
    assert first.status_code == 200, first.text
    second = await _put(client, "u-1", HOOK, "start")
    assert second.status_code == 200, second.text

    assert await _count(db_session, "u-1") == 1
    a, b = first.json(), second.json()
    assert b["started_at"] == a["started_at"]
    assert b["last_declared_at"] > a["last_declared_at"]


async def test_identity_omitted_is_stored_null(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """D4: identity is not guessed."""
    body = {k: v for k, v in HOOK.items() if k != "identity"}
    r = await _put(client, "u-1", body, "start")
    assert r.status_code == 200, r.text
    assert r.json()["identity"] is None
    assert r.json()["board_state"] == {"state": "unobserved", "reason": "identity_undeclared"}
    assert (await _stored(db_session, "u-1")).identity is None


async def test_fixed_column_overwrite_is_a_named_409(client: AsyncClient) -> None:
    await _put(client, "u-1", HOOK, "start")
    r = await _put(client, "u-1", HOOK, "declare", host="elsewhere")
    assert r.status_code == 409
    assert r.json()["error_type"] == "ChatroomSessionConflictError"
    assert r.json()["details"] == {"field": "host", "reason": "immutable"}


async def test_transcript_path_once_then_conflict(client: AsyncClient) -> None:
    await _put(client, "u-1", HOOK, "start")
    ok = await _put(client, "u-1", HOOK, "declare", transcript_path="/a.jsonl")
    assert ok.status_code == 200 and ok.json()["transcript_path"] == "/a.jsonl"
    again = await _put(client, "u-1", HOOK, "declare", transcript_path="/b.jsonl")
    assert again.status_code == 409
    assert again.json()["details"] == {"field": "transcript_path", "reason": "already_set"}


async def test_ended_at_written_once(client: AsyncClient) -> None:
    await _put(client, "u-1", HOOK, "start")
    assert (await _put(client, "u-1", HOOK, "end", end_reason="exit")).status_code == 200
    r = await _put(client, "u-1", HOOK, "end")
    assert r.status_code == 409
    assert r.json()["details"] == {"field": "ended_at", "reason": "already_set"}


async def test_hook_end_is_withdrawn_by_start_loop_end_is_not(client: AsyncClient) -> None:
    await _put(client, "u-h", HOOK, "start", transcript_path="/h")
    ended = await _put(client, "u-h", HOOK, "end")
    assert ended.json()["board_state"] == {
        "state": "unobserved",
        "reason": "end_declared_unverified",
    }
    restarted = await _put(client, "u-h", HOOK, "start")
    assert restarted.json()["ended_at"] is None
    assert restarted.json()["board_state"] == {"state": "declared", "reason": None}

    await _put(client, "u-l", LOOP, "start", transcript_path="/l")
    await _put(client, "u-l", LOOP, "end")
    restarted = await _put(client, "u-l", LOOP, "start")
    assert restarted.status_code == 200
    assert restarted.json()["ended_at"] is not None
    assert restarted.json()["board_state"] == {"state": "terminated", "reason": None}


@pytest.mark.parametrize("kind", ["start", "declare", "end"])
@pytest.mark.parametrize("presented", [None, "mw-session-other"], ids=["omitted", "wrong"])
async def test_loop_row_refuses_any_kind_without_matching_session_id(
    client: AsyncClient, db_session: AsyncSession, kind: str, presented: str | None
) -> None:
    """msg-1322: every write to a loop row needs the session id; a refusal writes nothing."""
    created = await _put(client, "u-1", LOOP, "start")
    assert created.status_code == 200, created.text
    before = await _stored(db_session, "u-1")
    before_last, before_ended = before.last_declared_at, before.ended_at

    body = {**LOOP, "mindwire_session_id": presented}
    if presented is None:
        body.pop("mindwire_session_id")
    r = await _put(client, "u-1", body, kind, transcript_path="/t.jsonl")

    assert r.status_code == 409
    assert r.json()["error_type"] == "ChatroomSessionConflictError"
    assert r.json()["details"] == {
        "field": "mindwire_session_id",
        "reason": "session_id_mismatch",
    }
    assert SECRET not in r.text

    after = await _stored(db_session, "u-1")
    assert after.transcript_path is None
    assert after.last_declared_at == before_last
    assert after.ended_at == before_ended


async def test_loop_row_with_matching_session_id_takes_transcript(client: AsyncClient) -> None:
    await _put(client, "u-1", LOOP, "start")
    r = await _put(client, "u-1", LOOP, "declare", transcript_path="/t.jsonl")
    assert r.status_code == 200, r.text
    assert r.json()["transcript_path"] == "/t.jsonl"


async def test_loop_insert_without_session_id_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    body = {k: v for k, v in LOOP.items() if k != "mindwire_session_id"}
    r = await _put(client, "u-1", body, "start")
    assert r.status_code == 409
    assert await _count(db_session, "u-1") == 0


async def test_responses_never_carry_mindwire_session_id(client: AsyncClient) -> None:
    put = await _put(client, "u-1", LOOP, "start")
    listed = await client.get("/v1/agent_sessions", params={"project": "p"})
    for r in (put, listed):
        assert r.status_code == 200
        assert "mindwire_session_id" not in r.text
        assert SECRET not in r.text


async def test_list_filters_by_project_and_identity(client: AsyncClient) -> None:
    await _put(client, "u-1", HOOK, "start")
    await _put(client, "u-2", {**HOOK, "identity": "Einstein"}, "start")
    await _put(client, "u-3", {**HOOK, "project": "q"}, "start")

    by_identity = await client.get(
        "/v1/agent_sessions", params={"project": "p", "identity": "Bohr"}
    )
    assert [i["cc_session_uuid"] for i in by_identity.json()["items"]] == ["u-1"]
    by_project = await client.get("/v1/agent_sessions", params={"project": "p"})
    assert {i["cc_session_uuid"] for i in by_project.json()["items"]} == {"u-1", "u-2"}
    for item in by_project.json()["items"]:
        assert item["board_state"] == {"state": "unobserved", "reason": "transcript_undeclared"}


async def test_concurrent_start_for_one_uuid_makes_one_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Two simultaneous starts: one row, no exception (the row-lock race)."""
    results = await asyncio.gather(*(_put(client, "u-race", HOOK, "start") for _ in range(2)))
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert await _count(db_session, "u-race") == 1
