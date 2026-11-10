"""The write-once rules for ``agent_sessions`` (D2' / D7), in one place.

There are two layers:

* ``plan_declare(row, req, now)`` is pure. Given the stored row (or ``None``)
  and a declaration, it returns the column values to write, or raises
  ``ChatroomSessionConflictError``. It writes nothing, so a refusal
  leaves every column untouched, ``last_declared_at`` included.
* ``declare(session, uuid, req)`` is the DB half. It locks the row with
  ``SELECT ... FOR UPDATE`` and decides against that locked state.
  A missing row is inserted with ``INSERT ... ON CONFLICT DO NOTHING``.
  If a concurrent writer won the insert, the row is re-read under the lock
  and the declaration is decided as an update. Two simultaneous ``start``
  declarations for one UUID therefore produce one row and no exception. This
  is the race T-conclair-double-close-race-no-row-lock was about.

``derive_board_state`` is the part of the D7 state mapping that Conclair
owns. It is computed on read and never stored.

For a missing row, one rule applies before the INSERT is built: a
``source='loop'`` declaration without ``mindwire_session_id`` is refused as
``session_id_mismatch`` (the same envelope as rule 1 below). The CHECK in
``0011`` still backs it at the DB.

Rules, in evaluation order, for an existing row (msg-1320 §2 with msg-1322):

1. ``source='loop'`` row: the request's ``mindwire_session_id`` must equal the
   stored one, whatever the ``kind``. Omitting it counts as a mismatch. The
   reason is always ``session_id_mismatch``, so the caller cannot tell which
   case it hit.
2. Fixed columns: any difference is refused as ``immutable``.
3. ``transcript_path``: NULL -> value once. A different value after that is
   refused as ``already_set``.
4. ``kind='end'``: allowed only while ``ended_at`` is NULL. Otherwise it is
   refused as ``already_set``.
5. ``kind='start'`` on a ``source='hook'`` row: ``ended_at`` / ``end_reason``
   go back to NULL (D7: a hook's end is unverified, and a SessionStart
   re-declaration withdraws it). Loop rows are not reset.
6. ``last_declared_at = now``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.exceptions import ChatroomSessionConflictError
from spirrow_conclair.models import AgentSession
from spirrow_conclair.schemas.agent_session import DeclareAgentSessionRequest

#: Columns a declaration may never change once the row exists. ``started_at``
#: is also fixed, but it is compared only when given (see
#: ``DeclareAgentSessionRequest``), so it is handled separately.
FIXED_COLUMNS = (
    # ``source`` first: a writer of the other kind is reported as that, not
    # as whichever column its declaration happens to differ in first.
    "source",
    "project",
    "identity",
    "instance_id",
    "embodiment",
    "host",
    "mindwire_session_id",
    "thread_id",
)


def _conflict(field: str, reason: str) -> ChatroomSessionConflictError:
    # The message names the field and the rule, never a stored value.
    return ChatroomSessionConflictError(
        f"agent_sessions.{field}: write refused ({reason})",
        field=field,
        reason=reason,
    )


def _insert_values(uuid: str, req: DeclareAgentSessionRequest, now: datetime) -> dict[str, Any]:
    values: dict[str, Any] = {
        "cc_session_uuid": uuid,
        "project": req.project,
        "identity": req.identity,
        "instance_id": req.instance_id,
        "embodiment": req.embodiment,
        "host": req.host,
        "transcript_path": req.transcript_path,
        "mindwire_session_id": req.mindwire_session_id,
        "thread_id": req.thread_id,
        "source": req.source,
        "started_at": req.started_at or now,
        "last_declared_at": now,
        "ended_at": None,
        "end_reason": None,
    }
    if req.kind == "end":
        values["ended_at"] = req.ended_at or now
        values["end_reason"] = req.end_reason
    return values


def plan_declare(
    row: AgentSession | None,
    req: DeclareAgentSessionRequest,
    now: datetime,
    *,
    uuid: str | None = None,
) -> dict[str, Any]:
    """Return the values to write for ``req`` against ``row``, or raise.

    With ``row=None`` the result is a full INSERT row (``uuid`` required).
    Otherwise it is the partial UPDATE, and it always contains
    ``last_declared_at``.
    """
    if row is None:
        if uuid is None:
            raise ValueError("uuid is required to plan an insert")
        # 0. A new loop row must carry its session id. Refuse it here, before
        #    any INSERT is issued, so the caller gets the same named 409 and
        #    ``{field, reason}`` details as a mismatch on an existing loop row.
        #    The DB CHECK (0011) stays as the last line of defence; without
        #    this branch it would answer with ChatroomIntegrityError and the
        #    raw constraint text instead (msg-1553 / msg-1556).
        if req.source == "loop" and req.mindwire_session_id is None:
            raise _conflict("mindwire_session_id", "session_id_mismatch")
        return _insert_values(uuid, req, now)

    # 1. Loop rows: the session id gates every kind, and it is checked first.
    if row.source == "loop" and req.mindwire_session_id != row.mindwire_session_id:
        raise _conflict("mindwire_session_id", "session_id_mismatch")

    # 2. Fixed columns.
    for column in FIXED_COLUMNS:
        if getattr(req, column) != getattr(row, column):
            raise _conflict(column, "immutable")
    if req.started_at is not None and req.started_at != row.started_at:
        raise _conflict("started_at", "immutable")

    updates: dict[str, Any] = {}

    # 3. transcript_path: NULL -> value, once.
    if req.transcript_path is not None:
        if row.transcript_path is None:
            updates["transcript_path"] = req.transcript_path
        elif req.transcript_path != row.transcript_path:
            raise _conflict("transcript_path", "already_set")

    # 4. End: written once.
    if req.kind == "end":
        if row.ended_at is not None:
            raise _conflict("ended_at", "already_set")
        updates["ended_at"] = req.ended_at or now
        updates["end_reason"] = req.end_reason

    # 5. D7 withdrawal: hook rows only.
    if req.kind == "start" and row.source == "hook":
        updates["ended_at"] = None
        updates["end_reason"] = None

    # 6.
    updates["last_declared_at"] = now
    return updates


async def _locked(session: AsyncSession, uuid: str) -> AgentSession | None:
    """``SELECT ... FOR UPDATE`` on the row: the one agent_sessions lock site.

    ``populate_existing`` makes the decision run on the locked, committed
    state, never on an instance this session already holds. The literal
    ``with_for_update=True`` is pinned by
    ``tests/unit/test_row_lock_is_at_the_choke_point.py``.
    """
    return await session.get(AgentSession, uuid, with_for_update=True, populate_existing=True)


async def declare(
    session: AsyncSession,
    uuid: str,
    req: DeclareAgentSessionRequest,
    *,
    now: datetime | None = None,
) -> AgentSession:
    """Apply one declaration inside the caller's transaction and return the row."""
    now = now or datetime.now(UTC)

    row = await _locked(session, uuid)
    if row is None:
        inserted = await session.scalar(
            pg_insert(AgentSession)
            .values(**plan_declare(None, req, now, uuid=uuid))
            .on_conflict_do_nothing(index_elements=[AgentSession.cc_session_uuid])
            .returning(AgentSession.cc_session_uuid)
        )
        row = await _locked(session, uuid)
        if inserted is not None:
            assert row is not None
            return row
        # A concurrent writer created the row first. Decide as an update.
        assert row is not None

    for column, value in plan_declare(row, req, now).items():
        setattr(row, column, value)
    await session.flush()
    return row


def derive_board_state(row: AgentSession) -> dict[str, str | None]:
    """The D7 mapping Conclair owns. It is pure and evaluated top-down.

    The order is pinned by tests. In particular, a row with no identity
    *and* a declared end reports ``identity_undeclared``. ``declared`` does
    not mean the session is alive.
    """
    if row.identity is None:
        return {"state": "unobserved", "reason": "identity_undeclared"}
    if row.source == "hook" and row.ended_at is not None:
        return {"state": "unobserved", "reason": "end_declared_unverified"}
    if row.transcript_path is None:
        return {"state": "unobserved", "reason": "transcript_undeclared"}
    if row.source == "loop" and row.ended_at is not None:
        return {"state": "terminated", "reason": None}
    return {"state": "declared", "reason": None}
