"""Cross-project chatroom summary.

Every other read here is scoped to one project, which is right for the
chatroom UI: you are looking at a discussion. A dashboard asks the opposite
question -- "which of my projects need attention?" -- and answering it by
listing threads per project would mean one HTTP round trip per project just
to read a count.

So this is one aggregate query instead. It reports, per project, the thread
counts by status, how many are waiting on a gate, and when the project was
last touched -- enough to rank projects by whether they need a human,
without returning a single thread body.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import Text, cast, func, select

from spirrow_conclair.db import SessionDep
from spirrow_conclair.models import Message, Thread
from spirrow_conclair.schemas.project import (
    ProjectSummary,
    ProjectSummaryListResponse,
)

router = APIRouter(prefix="/v1/projects", tags=["projects"])

# Threads carry gate markers as tags (e.g. "gate:naysayer"). A gated thread
# is blocked on someone else's review, which is the state a dashboard most
# needs to surface, so it is counted separately rather than folded into
# "active".
GATE_TAG_PREFIX = "gate:"


@router.get(
    "",
    response_model=ProjectSummaryListResponse,
    summary="Per-project thread counts (cross-project dashboard view)",
)
async def list_project_summaries(session: SessionDep) -> ProjectSummaryListResponse:
    """Summarize every project that has at least one thread."""
    # Thread counts by (project, status), plus the gate count. The tag test
    # is done in SQL so a project with thousands of threads still costs one
    # row per (project, status) on the wire.
    gate_match = f'%"{GATE_TAG_PREFIX}%'
    thread_rows = (
        await session.execute(
            select(
                Thread.project,
                Thread.status,
                func.count().label("n"),
                func.count()
                .filter(cast(Thread.tags, Text).like(gate_match))
                .label("gated"),
                func.max(Thread.created_at).label("latest_thread_at"),
            ).group_by(Thread.project, Thread.status)
        )
    ).all()

    # Last message per project: "last touched" is what tells a reader whether
    # a project is warm, and thread creation time does not capture it.
    message_rows = (
        await session.execute(
            select(
                Message.project,
                func.count().label("n"),
                func.max(Message.timestamp).label("latest_msg_at"),
            ).group_by(Message.project)
        )
    ).all()

    by_project: dict[str, dict] = {}
    for row in thread_rows:
        entry = by_project.setdefault(
            row.project,
            {
                "project": row.project,
                "threads_by_status": {},
                "thread_count": 0,
                "gated_thread_count": 0,
                "message_count": 0,
                "last_activity_at": None,
            },
        )
        entry["threads_by_status"][row.status] = row.n
        entry["thread_count"] += row.n
        entry["gated_thread_count"] += row.gated or 0

    for row in message_rows:
        entry = by_project.get(row.project)
        if entry is None:
            continue
        entry["message_count"] = row.n
        entry["last_activity_at"] = row.latest_msg_at

    items = [ProjectSummary(**entry) for entry in by_project.values()]
    # Most recently touched first: the dashboard's top row should be the
    # project someone is actually working in.
    items.sort(
        key=lambda item: (item.last_activity_at is not None, item.last_activity_at),
        reverse=True,
    )
    return ProjectSummaryListResponse(items=items, total=len(items))
