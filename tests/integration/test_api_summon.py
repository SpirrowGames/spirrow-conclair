"""End-to-end tests for the Summon mark (``active_summon``).

Spec: spirrow-magickit ``T-human-call-summon-server-side`` msg-1435 (v1.2 §1),
msg-1437 (v1.2.1); as applied here in ``T-conclair-active-summon`` (Bohr r1
msg-004, r2 msg-006). Every branch checks the response AND the stored state:
the three columns and ``project_summon_history``. A "nothing changes" branch
compares the state before and after, so an implementation that answers
correctly while writing anyway still fails.

Lock-order races live in ``test_summon_lock_order.py``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from spirrow_conclair.models.project_control import SUMMON_OPEN_STATUSES
from tests.summon_contract import SERVER_SINCE_RE, classify, load_cases

P = "sm-proj"
OTHER = "sm-other"

NO_ROW = "no-row"

SessionFactory = async_sessionmaker[AsyncSession]


# --- helpers ----------------------------------------------------------------


async def _open(client: AsyncClient, thread_id: str, project: str = P) -> None:
    r = await client.post(
        f"/v1/projects/{project}/threads",
        json={
            "thread_id": thread_id,
            "title": "t",
            "owner": "alice",
            "propose_content": "start",
        },
    )
    assert r.status_code == 201, r.text


async def _close(client: AsyncClient, thread_id: str, project: str = P) -> None:
    r = await client.post(
        f"/v1/projects/{project}/threads/{thread_id}/close",
        json={"summary_content": "done", "author": "alice"},
    )
    assert r.status_code == 201, r.text


async def _put(
    client: AsyncClient, thread_id: str, by: str = "takahito", project: str = P, **extra: Any
) -> tuple[int, dict]:
    r = await client.put(
        f"/v1/projects/{project}/control/summon",
        json={"thread_id": thread_id, "by": by, **extra},
    )
    return r.status_code, r.json()


async def _clear(
    client: AsyncClient, thread_id: str, by: str = "takahito", project: str = P, **extra: Any
) -> tuple[int, dict]:
    r = await client.post(
        f"/v1/projects/{project}/control/summon/clear",
        json={"thread_id": thread_id, "by": by, **extra},
    )
    return r.status_code, r.json()


async def _get(client: AsyncClient, project: str = P) -> dict:
    r = await client.get(f"/v1/projects/{project}/control")
    assert r.status_code == 200, r.text
    return r.json()


async def _mark(sf: SessionFactory, project: str = P) -> Any:
    """``NO_ROW``, or the stored ``(thread_id, since, by)`` triple."""
    async with sf() as s:
        row = (
            await s.execute(
                text(
                    "SELECT active_summon_thread_id, active_summon_since, active_summon_by "
                    "FROM project_control WHERE project = :p"
                ),
                {"p": project},
            )
        ).first()
    return NO_ROW if row is None else tuple(row)


async def _history(sf: SessionFactory, project: str = P) -> list[dict]:
    async with sf() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT action, thread_id, actor, note FROM project_summon_history "
                    "WHERE project = :p ORDER BY id"
                ),
                {"p": project},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def _state(sf: SessionFactory, project: str = P) -> tuple[Any, list[dict]]:
    return await _mark(sf, project), await _history(sf, project)


async def _exec(sf: SessionFactory, sql: str, **params: Any) -> None:
    async with sf() as s:
        await s.execute(text(sql), params)
        await s.commit()


async def _delete_thread(sf: SessionFactory, thread_id: str, project: str = P) -> None:
    """DELETE a thread row (no API exists for it; the trigger must cover it)."""
    async with sf() as s:
        for table in ("actor_read_cursors", "thread_digests", "messages"):
            await s.execute(
                text(f"DELETE FROM {table} WHERE project = :p AND thread_id = :t"),
                {"p": project, "t": thread_id},
            )
        await s.execute(
            text("DELETE FROM threads WHERE project = :p AND thread_id = :t"),
            {"p": project, "t": thread_id},
        )
        await s.commit()


def _assert_error(body: dict, code: str) -> None:
    assert body["error_type"] == code, body
    assert body["details"]["code"] == code, body


# --- GET ----------------------------------------------------------------------


async def test_get_carries_active_summon_null_for_an_unconfigured_project(
    client: AsyncClient,
) -> None:
    body = await _get(client, "never-touched")

    assert "active_summon" in body and body["active_summon"] is None
    assert classify(body) == {"kind": "none"}


async def test_get_after_put_is_contract_valid(client: AsyncClient) -> None:
    await _open(client, "T-a")
    assert (await _put(client, "T-a"))[0] == 200

    body = await _get(client)

    assert SERVER_SINCE_RE.match(body["active_summon"]["since"]), body
    got = classify(body)
    assert got["kind"] == "valid"
    assert (got["thread_id"], got["by"]) == ("T-a", "takahito")


async def test_every_response_shape_classifies_like_the_fixture(client: AsyncClient) -> None:
    """The server only ever emits fixture kinds ``none`` and ``valid``."""
    kinds = {c["expect"]["kind"] for c in load_cases()}
    assert {"none", "valid"} <= kinds

    await _open(client, "T-a")
    assert classify(await _get(client))["kind"] == "none"
    await _put(client, "T-a")
    assert classify(await _get(client))["kind"] == "valid"
    await _clear(client, "T-a")
    assert classify(await _get(client))["kind"] == "none"


async def test_control_put_and_observed_responses_also_carry_the_mark(
    client: AsyncClient,
) -> None:
    await _open(client, "T-a")
    await _put(client, "T-a")

    r1 = await client.put(f"/v1/projects/{P}/control", json={"state": "hold", "actor": "h"})
    r2 = await client.post(
        f"/v1/projects/{P}/control/observed", json={"state": "hold", "actor": "loop"}
    )

    assert r1.json()["active_summon"]["thread_id"] == "T-a"
    assert r2.json()["active_summon"]["thread_id"] == "T-a"


# --- PUT ----------------------------------------------------------------------


async def test_put_on_no_mark_sets_it_and_writes_one_set_row(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")

    code, body = await _put(client, "T-a", note="今これ")

    assert code == 200, body
    assert body["active_summon"]["thread_id"] == "T-a"
    assert body["active_summon"]["by"] == "takahito"
    assert SERVER_SINCE_RE.match(body["active_summon"]["since"]), body
    # The project stays unconfigured: a Summon is not a desired state.
    assert body["configured"] is False and body["desired_state"] == "run"

    mark, history = await _state(session_factory)
    assert mark[0] == "T-a" and mark[2] == "takahito" and isinstance(mark[1], datetime)
    assert datetime.fromisoformat(body["active_summon"]["since"]) == mark[1]
    assert history == [
        {"action": "set", "thread_id": "T-a", "actor": "takahito", "note": "今これ"}
    ]


async def test_put_same_thread_again_is_idempotent(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    _, first = await _put(client, "T-a", by="takahito")
    before = await _state(session_factory)

    code, again = await _put(client, "T-a", by="someone-else")

    assert code == 200, again
    assert again["active_summon"] == first["active_summon"]
    assert await _state(session_factory) == before
    assert len(before[1]) == 1


async def test_put_with_another_mark_is_409_summon_conflict_and_changes_nothing(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _open(client, "T-b")
    await _put(client, "T-a")
    before = await _state(session_factory)

    code, body = await _put(client, "T-b")

    assert code == 409, body
    _assert_error(body, "summon_conflict")
    assert body["details"]["active_summon"]["thread_id"] == "T-a"
    assert await _state(session_factory) == before


async def test_put_unknown_thread_is_404_and_creates_no_row(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    code, body = await _put(client, "T-nope")

    assert code == 404, body
    assert body["error_type"] == "ChatroomNotFoundError"
    assert await _state(session_factory) == (NO_ROW, [])


async def test_put_unknown_thread_keeps_an_existing_mark(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _put(client, "T-a")
    before = await _state(session_factory)

    code, _ = await _put(client, "T-nope")

    assert code == 404
    assert await _state(session_factory) == before


async def test_put_thread_of_another_project_is_404_and_touches_neither(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-x", project=OTHER)
    await _open(client, "T-a")
    await _put(client, "T-a")
    before_p = await _state(session_factory, P)

    code, body = await _put(client, "T-x", project=P)

    assert code == 404, body
    assert await _state(session_factory, P) == before_p
    assert await _state(session_factory, OTHER) == (NO_ROW, [])


async def test_put_on_a_resolved_thread_is_409_summon_thread_not_open(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _close(client, "T-a")

    code, body = await _put(client, "T-a")

    assert code == 409, body
    _assert_error(body, "summon_thread_not_open")
    assert body["details"]["thread_status"] == "resolved"
    mark, history = await _state(session_factory)
    assert mark in (NO_ROW, (None, None, None))
    assert history == []


@pytest.mark.parametrize("status", ["superseded", "parked"])
async def test_put_on_other_non_open_statuses_is_refused(
    client: AsyncClient, session_factory: SessionFactory, status: str
) -> None:
    await _open(client, "T-a")
    await _exec(
        session_factory,
        "UPDATE threads SET status = :s WHERE project = :p AND thread_id = 'T-a'",
        s=status,
        p=P,
    )

    code, body = await _put(client, "T-a")

    assert code == 409, body
    _assert_error(body, "summon_thread_not_open")
    assert (await _history(session_factory)) == []


@pytest.mark.parametrize("field", ["thread_id", "by"])
@pytest.mark.parametrize("blank", ["", "   "])
async def test_put_blank_is_422_invalid_argument_and_writes_nothing(
    client: AsyncClient, session_factory: SessionFactory, field: str, blank: str
) -> None:
    await _open(client, "T-a")
    body = {"thread_id": "T-a", "by": "takahito", field: blank}

    r = await client.put(f"/v1/projects/{P}/control/summon", json=body)

    assert r.status_code == 422, r.text
    _assert_error(r.json(), "invalid_argument")
    assert r.json()["details"]["fields"] == [field]
    assert await _state(session_factory) == (NO_ROW, [])


# --- clear --------------------------------------------------------------------


async def test_clear_matching_mark_clears_and_writes_one_clear_row(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _put(client, "T-a")

    code, body = await _clear(client, "T-a", by="fermi", note="済")

    assert code == 200, body
    assert body["active_summon"] is None
    mark, history = await _state(session_factory)
    assert mark == (None, None, None)
    assert history[-1] == {"action": "clear", "thread_id": "T-a", "actor": "fermi", "note": "済"}
    assert len(history) == 2


async def test_clear_other_thread_is_409_summon_mismatch_and_keeps_the_mark(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _put(client, "T-a")
    before = await _state(session_factory)

    code, body = await _clear(client, "T-b")

    assert code == 409, body
    _assert_error(body, "summon_mismatch")
    assert body["details"]["active_summon"]["thread_id"] == "T-a"
    assert await _state(session_factory) == before
    assert before[0][0] == "T-a"


async def test_clear_with_no_mark_is_a_200_noop(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _put(client, "T-a")
    await _clear(client, "T-a")
    before = await _state(session_factory)

    code, body = await _clear(client, "T-a")

    assert code == 200, body
    assert body["active_summon"] is None
    assert await _state(session_factory) == before


async def test_clear_on_a_project_without_a_row_is_200_and_creates_none(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    code, body = await _clear(client, "T-a", project="never-touched")

    assert code == 200, body
    assert body["active_summon"] is None and body["configured"] is False
    assert await _state(session_factory, "never-touched") == (NO_ROW, [])


@pytest.mark.parametrize("field", ["thread_id", "by"])
async def test_clear_blank_is_422_and_keeps_the_mark(
    client: AsyncClient, session_factory: SessionFactory, field: str
) -> None:
    await _open(client, "T-a")
    await _put(client, "T-a")
    before = await _state(session_factory)

    code, body = await _clear(client, **{"thread_id": "T-a", "by": "takahito", field: " "})

    assert code == 422, body
    _assert_error(body, "invalid_argument")
    assert await _state(session_factory) == before


# --- CHECK --------------------------------------------------------------------


@pytest.mark.parametrize(
    "assignment",
    [
        "active_summon_thread_id = 'T-a'",
        "active_summon_thread_id = 'T-a', active_summon_since = now()",
        "active_summon_since = now(), active_summon_by = 'x'",
        "active_summon_thread_id = '  ', active_summon_since = now(), active_summon_by = 'x'",
        "active_summon_thread_id = 'T-a', active_summon_since = now(), active_summon_by = ''",
        "active_summon_thread_id = 'T-a', active_summon_since = now(), active_summon_by = ' '",
    ],
)
async def test_check_rejects_partial_or_blank_marks(
    session_factory: SessionFactory, assignment: str
) -> None:
    await _exec(session_factory, "INSERT INTO project_control (project) VALUES (:p)", p=P)

    with pytest.raises(IntegrityError, match="project_control_active_summon_check"):
        await _exec(
            session_factory,
            f"UPDATE project_control SET {assignment} WHERE project = :p",
            p=P,
        )

    assert await _mark(session_factory) == (None, None, None)


# --- triggers -----------------------------------------------------------------


async def test_closing_the_thread_clears_the_mark_by_system_status(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _put(client, "T-a")

    await _close(client, "T-a")

    mark, history = await _state(session_factory)
    assert mark == (None, None, None)
    assert history[-1] == {
        "action": "clear",
        "thread_id": "T-a",
        "actor": "system:status:resolved",
        "note": None,
    }
    assert (await _get(client))["active_summon"] is None


@pytest.mark.parametrize("status", ["superseded", "parked"])
async def test_other_known_non_open_statuses_clear_the_mark(
    client: AsyncClient, session_factory: SessionFactory, status: str
) -> None:
    await _open(client, "T-a")
    await _put(client, "T-a")

    await _exec(
        session_factory,
        "UPDATE threads SET status = :s WHERE project = :p AND thread_id = 'T-a'",
        s=status,
        p=P,
    )

    mark, history = await _state(session_factory)
    assert mark == (None, None, None)
    assert history[-1]["actor"] == f"system:status:{status}"


async def test_closing_another_thread_keeps_the_mark(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _open(client, "T-b")
    await _put(client, "T-a")
    before = await _state(session_factory)

    await _close(client, "T-b")

    assert await _state(session_factory) == before


@pytest.mark.parametrize(
    ("start", "to"),
    [("active", "awaiting_reply"), ("awaiting_reply", "active")],
)
async def test_transitions_inside_the_allow_list_keep_the_mark(
    client: AsyncClient, session_factory: SessionFactory, start: str, to: str
) -> None:
    await _open(client, "T-a")
    await _exec(
        session_factory,
        "UPDATE threads SET status = :s WHERE project = :p AND thread_id = 'T-a'",
        s=start,
        p=P,
    )
    await _put(client, "T-a")
    before = await _state(session_factory)

    await _exec(
        session_factory,
        "UPDATE threads SET status = :s WHERE project = :p AND thread_id = 'T-a'",
        s=to,
        p=P,
    )

    assert await _state(session_factory) == before


async def test_same_value_status_update_does_not_clear(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _put(client, "T-a")
    before = await _state(session_factory)

    await _exec(
        session_factory,
        "UPDATE threads SET status = status, title = 'renamed' "
        "WHERE project = :p AND thread_id = 'T-a'",
        p=P,
    )

    assert await _state(session_factory) == before


async def test_delete_of_the_marked_thread_clears_by_thread_deleted(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _put(client, "T-a")

    await _delete_thread(session_factory, "T-a")

    mark, history = await _state(session_factory)
    assert mark == (None, None, None)
    assert history[-1] == {
        "action": "clear",
        "thread_id": "T-a",
        "actor": "system:thread_deleted",
        "note": None,
    }


async def test_delete_of_an_unmarked_thread_keeps_the_mark(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")
    await _open(client, "T-b")
    await _put(client, "T-a")
    before = await _state(session_factory)

    await _delete_thread(session_factory, "T-b")

    assert await _state(session_factory) == before


async def test_delete_in_a_project_without_a_control_row_is_harmless(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _open(client, "T-a")

    await _delete_thread(session_factory, "T-a")

    assert await _state(session_factory) == (NO_ROW, [])


# Status values Conclair's own constraints forbid (an unknown status, NULL).
# The trigger is the backstop for them, so it is exercised with the
# constraint dropped -- inside ONE connection and ONE transaction that is
# rolled back, so no other test ever sees the weakened table. Afterwards a
# separate connection confirms the constraints are back.


async def _trigger_with_constraint_dropped(
    client: AsyncClient,
    session_factory: SessionFactory,
    ddl: str,
    new_status_sql: str,
) -> tuple[Any, list[dict]]:
    await _open(client, "T-a")
    await _put(client, "T-a")

    async with session_factory() as s:
        trans = await s.begin()
        try:
            await s.execute(text(ddl))
            await s.execute(
                text(
                    f"UPDATE threads SET status = {new_status_sql} "
                    "WHERE project = :p AND thread_id = 'T-a'"
                ),
                {"p": P},
            )
            mark = tuple(
                (
                    await s.execute(
                        text(
                            "SELECT active_summon_thread_id, active_summon_since, "
                            "active_summon_by FROM project_control WHERE project = :p"
                        ),
                        {"p": P},
                    )
                ).one()
            )
            rows = (
                await s.execute(
                    text(
                        "SELECT action, thread_id, actor, note FROM project_summon_history "
                        "WHERE project = :p ORDER BY id"
                    ),
                    {"p": P},
                )
            ).mappings().all()
        finally:
            await trans.rollback()
    return mark, [dict(r) for r in rows]


async def _assert_threads_constraints_intact(sf: SessionFactory) -> None:
    async with sf() as s:
        check = (
            await s.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'threads_status_check' "
                    "AND conrelid = 'threads'::regclass"
                )
            )
        ).scalar_one()
        not_null = (
            await s.execute(
                text(
                    "SELECT attnotnull FROM pg_attribute "
                    "WHERE attrelid = 'threads'::regclass AND attname = 'status'"
                )
            )
        ).scalar_one()
    assert check == 1
    assert not_null is True


async def test_unknown_status_clears_by_system_status_value(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    mark, history = await _trigger_with_constraint_dropped(
        client,
        session_factory,
        "ALTER TABLE threads DROP CONSTRAINT threads_status_check",
        "'zzz'",
    )

    assert mark == (None, None, None)
    assert history[-1]["actor"] == "system:status:zzz"
    await _assert_threads_constraints_intact(session_factory)
    # The rollback took the clear with it.
    assert (await _mark(session_factory))[0] == "T-a"


async def test_null_status_clears_by_system_status_null(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    mark, history = await _trigger_with_constraint_dropped(
        client,
        session_factory,
        "ALTER TABLE threads ALTER COLUMN status DROP NOT NULL",
        "NULL",
    )

    assert mark == (None, None, None)
    assert history[-1]["actor"] == "system:status:<null>"
    await _assert_threads_constraints_intact(session_factory)
    assert (await _mark(session_factory))[0] == "T-a"


# --- the allow-list as deployed ------------------------------------------------


async def test_deployed_trigger_function_uses_the_models_allow_list(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as s:
        body = (
            await s.execute(
                text("SELECT prosrc FROM pg_proc WHERE proname = 'threads_summon_clear_on_status'")
            )
        ).scalar_one()
    match = re.search(r"NOT IN \(([^)]*)\)", body)
    assert match, body
    deployed = tuple(v.strip().strip("'") for v in match.group(1).split(","))
    assert deployed == SUMMON_OPEN_STATUSES
