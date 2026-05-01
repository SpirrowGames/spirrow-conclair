"""Event log endpoint (audit trail viewer)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query
from sqlalchemy import func, select

from spirrow_conclair.db import SessionDep
from spirrow_conclair.models import ChatroomEvent
from spirrow_conclair.schemas import (
    ChatroomEvent as ChatroomEventSchema,
)
from spirrow_conclair.schemas import EventListResponse

router = APIRouter(prefix="/v1/projects/{project}/events", tags=["events"])

ProjectPath = Annotated[str, Path(min_length=1, max_length=200)]
EventAction = Literal["open_thread", "post_message", "status_transition"]


@router.get(
    "",
    response_model=EventListResponse,
    summary="List chatroom_events with optional filters (audit trail)",
)
async def list_events(
    project: ProjectPath,
    session: SessionDep,
    thread_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    action: Annotated[EventAction | None, Query()] = None,
    since: Annotated[datetime | None, Query(description="Inclusive lower bound")] = None,
    until: Annotated[datetime | None, Query(description="Exclusive upper bound")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventListResponse:
    base = select(ChatroomEvent).where(ChatroomEvent.project == project)
    if thread_id:
        base = base.where(ChatroomEvent.thread_id == thread_id)
    if action:
        base = base.where(ChatroomEvent.action == action)
    if since:
        base = base.where(ChatroomEvent.timestamp >= since)
    if until:
        base = base.where(ChatroomEvent.timestamp < until)

    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0

    rows = (
        await session.execute(
            base.order_by(
                ChatroomEvent.timestamp.desc(),
                ChatroomEvent.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    return EventListResponse(
        items=[ChatroomEventSchema.model_validate(e) for e in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
