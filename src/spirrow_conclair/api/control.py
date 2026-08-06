"""Per-project loop control endpoints (HOLD / RESUME).

GET  /v1/projects/{project}/control           — current desired + observed
PUT  /v1/projects/{project}/control           — set desired  (operators)
POST /v1/projects/{project}/control/observed  — report observed (the loop)
GET  /v1/projects/{project}/control/history   — recent desired changes

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
from typing import Annotated

from fastapi import APIRouter, Path, Query, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from spirrow_conclair.db import SessionDep
from spirrow_conclair.models import ProjectControl, ProjectControlHistory
from spirrow_conclair.models.project_control import DEFAULT_CONTROL_STATE
from spirrow_conclair.schemas import (
    ControlHistoryItem,
    ControlHistoryListResponse,
    ControlStateResponse,
    ReportObservedRequest,
    SetControlRequest,
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
