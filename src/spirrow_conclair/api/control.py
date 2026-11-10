"""Per-project loop control endpoints (HOLD / RESUME).

GET  /v1/projects/{project}/control           — current desired + observed
PUT  /v1/projects/{project}/control           — set desired  (operators)
POST /v1/projects/{project}/control/observed  — report observed (the loop)
GET  /v1/projects/{project}/control/history   — recent desired changes
PUT  /v1/projects/{project}/control/summon    — set the Summon (operators)
POST /v1/projects/{project}/control/summon/clear — compare-and-clear it

Two rules shape this module:

1. ``desired`` and ``observed`` are written by different endpoints and
   neither touches the other's columns. A loop that could write
   ``desired`` could resume a project a human had stopped, and the
   stop would look like it had simply not taken.

2. GET never 404s. "Nobody configured this project" is a normal answer
   (``configured: false`` + the ``run`` default), so any error status
   from this endpoint means the read genuinely failed -- which callers
   must treat as ``hold``. Collapsing the two would make a missing row
   indistinguishable from a dead database, and the loop would run on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from spirrow_conclair.db import SessionDep
from spirrow_conclair.exceptions import (
    ChatroomInvalidArgumentError,
    ChatroomNotFoundError,
    ChatroomSummonStateError,
)
from spirrow_conclair.models import (
    ProjectControl,
    ProjectControlHistory,
    ProjectSummonHistory,
    Thread,
)
from spirrow_conclair.models.project_control import (
    DEFAULT_CONTROL_STATE,
    SUMMON_OPEN_STATUSES,
)
from spirrow_conclair.schemas import (
    ActiveSummon,
    ControlHistoryItem,
    ControlHistoryListResponse,
    ControlStateResponse,
    ReportObservedRequest,
    SetControlRequest,
    SummonRequest,
)

router = APIRouter(prefix="/v1/projects/{project}/control", tags=["control"])

ProjectPath = Annotated[str, Path(min_length=1, max_length=200)]


def _to_response(project: str, row: ProjectControl | None) -> ControlStateResponse:
    """Render a row (or its absence) as the wire shape.

    A missing row and a row whose ``desired_state`` is NULL mean the same
    thing -- unconfigured -- and both report the default. The second case
    exists because the loop can report ``observed`` for a project that was
    never explicitly set.
    """
    configured = row is not None and row.desired_state is not None
    return ControlStateResponse(
        project=project,
        desired_state=(
            row.desired_state if configured and row else DEFAULT_CONTROL_STATE
        ),  # type: ignore[arg-type]
        desired_actor=row.desired_actor if configured and row else None,
        desired_at=row.desired_at if configured and row else None,
        observed_state=row.observed_state if row else None,  # type: ignore[arg-type]
        observed_actor=row.observed_actor if row else None,
        observed_at=row.observed_at if row else None,
        configured=configured,
        active_summon=_summon_of(row),
    )


def _summon_of(row: ProjectControl | None) -> ActiveSummon | None:
    """The row's Summon, or None. The CHECK makes the three columns all-or-none."""
    if row is None or row.active_summon_thread_id is None:
        return None
    return ActiveSummon(
        thread_id=row.active_summon_thread_id,
        since=row.active_summon_since,  # type: ignore[arg-type]
        by=row.active_summon_by,  # type: ignore[arg-type]
    )


async def _fetch(session: SessionDep, project: str) -> ProjectControl | None:
    return await session.scalar(
        select(ProjectControl).where(ProjectControl.project == project)
    )


# --- GET /control ---------------------------------------------------------


@router.get(
    "",
    response_model=ControlStateResponse,
    summary="Current loop control state (never 404s; unset projects report the default)",
)
async def get_control(
    project: ProjectPath,
    session: SessionDep,
) -> ControlStateResponse:
    return _to_response(project, await _fetch(session, project))


# --- PUT /control (set desired) -------------------------------------------


@router.put(
    "",
    response_model=ControlStateResponse,
    summary="Set the desired loop control state (operator action)",
)
async def set_control(
    project: ProjectPath,
    body: SetControlRequest,
    session: SessionDep,
) -> ControlStateResponse:
    now = datetime.now(timezone.utc)

    async with session.begin():
        # UPSERT touching only the desired_* columns. observed_* is left
        # exactly as the loop last wrote it: setting a new desired value
        # does not mean the loop has seen it, and the UI's "反映待ち"
        # indicator depends on the two staying independent.
        stmt = (
            pg_insert(ProjectControl)
            .values(
                project=project,
                desired_state=body.state,
                desired_actor=body.actor,
                desired_at=now,
            )
            .on_conflict_do_update(
                index_elements=[ProjectControl.project],
                set_={
                    "desired_state": body.state,
                    "desired_actor": body.actor,
                    "desired_at": now,
                },
            )
        )
        await session.execute(stmt)

        # Every PUT is recorded, including a no-op re-press of the state
        # already in effect: "someone pressed HOLD again at 02:11" is a
        # fact about the operator, and suppressing it would make the log
        # lie about what happened during an incident.
        session.add(
            ProjectControlHistory(
                project=project,
                state=body.state,
                actor=body.actor,
                changed_at=now,
                note=body.note or None,
            )
        )

        row = await _fetch(session, project)

    return _to_response(project, row)


# --- POST /control/observed (loop reports what it read) -------------------


@router.post(
    "/observed",
    status_code=status.HTTP_200_OK,
    response_model=ControlStateResponse,
    summary="Report the state the loop observed (loop only; never writes desired)",
)
async def report_observed(
    project: ProjectPath,
    body: ReportObservedRequest,
    session: SessionDep,
) -> ControlStateResponse:
    now = datetime.now(timezone.utc)

    async with session.begin():
        # Mirror image of set_control: only observed_* is in `values` and
        # in `set_`, so an INSERT here leaves desired_* NULL (the project
        # stays "unconfigured") and an UPDATE cannot disturb a setting an
        # operator made.
        stmt = (
            pg_insert(ProjectControl)
            .values(
                project=project,
                observed_state=body.state,
                observed_actor=body.actor,
                observed_at=now,
            )
            .on_conflict_do_update(
                index_elements=[ProjectControl.project],
                set_={
                    "observed_state": body.state,
                    "observed_actor": body.actor,
                    "observed_at": now,
                },
            )
        )
        await session.execute(stmt)

        # No history row. The loop reports every round; these would bury
        # the operator actions the log exists to explain.
        row = await _fetch(session, project)

    return _to_response(project, row)


# --- GET /control/history -------------------------------------------------


@router.get(
    "/history",
    response_model=ControlHistoryListResponse,
    summary="Recent desired-state changes (newest first)",
)
async def get_control_history(
    project: ProjectPath,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 20,
) -> ControlHistoryListResponse:
    total = await session.scalar(
        select(func.count())
        .select_from(ProjectControlHistory)
        .where(ProjectControlHistory.project == project)
    ) or 0

    rows = (
        await session.execute(
            select(ProjectControlHistory)
            .where(ProjectControlHistory.project == project)
            # `id` breaks ties: two PUTs inside the same clock tick are
            # still ordered by insertion.
            .order_by(
                ProjectControlHistory.changed_at.desc(),
                ProjectControlHistory.id.desc(),
            )
            .limit(limit)
        )
    ).scalars().all()

    return ControlHistoryListResponse(
        items=[ControlHistoryItem.model_validate(r) for r in rows],
        total=total,
        limit=limit,
    )


# --- Summon ---------------------------------------------------------------
#
# Spec: spirrow-magickit T-human-call-summon-server-side msg-1435 (v1.2 §1),
# msg-1437 (v1.2.1), msg-1439 (v1.2.2), as applied to this repo in
# T-conclair-active-summon (Bohr r1 msg-004 + r2 msg-006).
#
# Lock order. PUT locks the thread row (FOR SHARE), then the project's
# control row (FOR UPDATE). The triggers on ``threads`` run while the status
# UPDATE / DELETE holds that thread row, and ``clear_active_summon`` then
# takes the control row FOR UPDATE: the same thread -> control order. So a
# PUT and a status change / DELETE of the SAME thread cannot invert lock
# order. That is the whole claim. A transaction that ends several threads
# can still deadlock with another doing the same in another order (tx1 ends
# A and takes control; tx2, holding B, waits on control; tx1 then updates
# B). PostgreSQL's deadlock detector rolls one back, so no half-applied mark
# survives; no retry machinery is added for it.
#
# Known property (msg-1440): ending or deleting ANY thread takes its
# project's control-row lock, mark or no mark, so in one project thread
# closes and HOLD / RESUME are serialized with each other.
#
# Isolation. These endpoints run at the session default (READ COMMITTED). A
# caller at REPEATABLE READ or above whose trigger-driven clear meets a
# concurrently changed control row gets a serialization failure and rolls
# back; the mark is never silently left behind.


def _refuse_blank(body: SummonRequest) -> None:
    blank = [
        name
        for name, value in (("thread_id", body.thread_id), ("by", body.by))
        if not value.strip()
    ]
    if blank:
        raise ChatroomInvalidArgumentError(
            f"{', '.join(blank)} must be a non-blank string",
            details={"code": "invalid_argument", "fields": blank},
        )


async def _lock_control(session: SessionDep, project: str) -> ProjectControl | None:
    """SELECT the control row FOR UPDATE, refreshing any cached instance."""
    return await session.scalar(
        select(ProjectControl)
        .where(ProjectControl.project == project)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _summon_details(row: ProjectControl | None) -> dict[str, Any]:
    current = _summon_of(row)
    return {"active_summon": current.model_dump(mode="json") if current else None}


@router.put(
    "/summon",
    response_model=ControlStateResponse,
    summary="Mark a thread as the project's active Summon (operator action)",
)
async def set_summon(
    project: ProjectPath,
    body: SummonRequest,
    session: SessionDep,
) -> ControlStateResponse:
    _refuse_blank(body)
    now = datetime.now(timezone.utc)

    async with session.begin():
        # 1. The thread, FOR SHARE. It must exist in THIS project (another
        #    project's thread is simply not found) and must not change status
        #    until we commit: a concurrent close blocks on this lock, then its
        #    trigger sees our mark and clears it. Checked before any INSERT,
        #    so a 404 never creates a control row.
        thread = await session.scalar(
            select(Thread)
            .where(Thread.project == project, Thread.thread_id == body.thread_id)
            .with_for_update(read=True)
        )
        if thread is None:
            raise ChatroomNotFoundError(
                f"thread {body.thread_id!r} not found in project {project!r}",
                details={"project": project, "thread_id": body.thread_id},
            )
        if thread.status not in SUMMON_OPEN_STATUSES:
            raise ChatroomSummonStateError(
                "summon_thread_not_open",
                f"thread {body.thread_id!r} is {thread.status!r}; a Summon needs "
                f"one of {list(SUMMON_OPEN_STATUSES)}",
                details={
                    "thread_status": thread.status,
                    **_summon_details(await _fetch(session, project)),
                },
            )

        # 2. The control row: created empty if absent (desired / observed stay
        #    NULL, so the project stays "unconfigured"), then FOR UPDATE.
        await session.execute(
            pg_insert(ProjectControl)
            .values(project=project)
            .on_conflict_do_nothing(index_elements=[ProjectControl.project])
        )
        row = await _lock_control(session, project)
        assert row is not None  # inserted or already there, and now locked

        current = row.active_summon_thread_id
        if current == body.thread_id:
            pass  # idempotent: since, by and history stay as first written
        elif current is not None:
            raise ChatroomSummonStateError(
                "summon_conflict",
                f"project {project!r} already has a Summon on {current!r}; "
                "clear it first",
                details=_summon_details(row),
            )
        else:
            row.active_summon_thread_id = body.thread_id
            row.active_summon_since = now
            row.active_summon_by = body.by
            session.add(
                ProjectSummonHistory(
                    project=project,
                    action="set",
                    thread_id=body.thread_id,
                    actor=body.by,
                    changed_at=now,
                    note=body.note or None,
                )
            )
            await session.flush()

    return _to_response(project, row)


@router.post(
    "/summon/clear",
    status_code=status.HTTP_200_OK,
    response_model=ControlStateResponse,
    summary="Compare-and-clear the project's Summon (operator action)",
)
async def clear_summon(
    project: ProjectPath,
    body: SummonRequest,
    session: SessionDep,
) -> ControlStateResponse:
    _refuse_blank(body)
    now = datetime.now(timezone.utc)

    async with session.begin():
        # No INSERT: clearing nothing must not create a row.
        row = await _lock_control(session, project)
        current = row.active_summon_thread_id if row is not None else None
        if current is None:
            pass  # no mark: a 200 no-op, nothing written
        elif current != body.thread_id:
            # A stale screen must not erase a newer mark.
            raise ChatroomSummonStateError(
                "summon_mismatch",
                f"the Summon is on {current!r}, not {body.thread_id!r}; "
                "nothing was cleared",
                details=_summon_details(row),
            )
        else:
            assert row is not None
            row.active_summon_thread_id = None
            row.active_summon_since = None
            row.active_summon_by = None
            session.add(
                ProjectSummonHistory(
                    project=project,
                    action="clear",
                    thread_id=body.thread_id,
                    actor=body.by,
                    changed_at=now,
                    note=body.note or None,
                )
            )
            await session.flush()

    return _to_response(project, row)
