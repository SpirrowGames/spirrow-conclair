"""Read cursor endpoints (mark_read / inbox).

POST /v1/projects/{project}/threads/{thread_id}/read  — advance the
        per-identity cursor for this thread.
GET  /v1/projects/{project}/unread                    — inbox for an
        identity: list threads with at least one unread msg.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Path, Query, status
from sqlalchemy import BigInteger, and_, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from spirrow_conclair.db import SessionDep
from spirrow_conclair.exceptions import ChatroomIntegrityError, ChatroomNotFoundError
from spirrow_conclair.models import ActorReadCursor, ChatroomEvent, Message, Thread
from spirrow_conclair.schemas import (
    MarkReadRequest,
    MarkReadResponse,
    UnreadListResponse,
    UnreadThreadItem,
)
from spirrow_conclair.services.msg_id_allocator import format_msg_id
from spirrow_conclair.services.read_cursor import (
    compute_unread_count,
    should_advance_cursor,
)

router = APIRouter(prefix="/v1/projects/{project}", tags=["read_cursor"])

ProjectPath = Annotated[str, Path(min_length=1, max_length=200)]
ThreadIdPath = Annotated[str, Path(min_length=1, max_length=200)]


# --- POST /threads/{thread_id}/read --------------------------------------


@router.post(
    "/threads/{thread_id}/read",
    status_code=status.HTTP_200_OK,
    response_model=MarkReadResponse,
    summary=(
        "Advance the per-identity read cursor for this thread "
        "(monotonic forward-only)"
    ),
)
async def mark_read(
    project: ProjectPath,
    thread_id: ThreadIdPath,
    body: MarkReadRequest,
    session: SessionDep,
) -> MarkReadResponse:
    now = datetime.now(timezone.utc)

    async with session.begin():
        # 1. Thread exists?
        thread = await session.scalar(
            select(Thread).where(
                Thread.project == project, Thread.thread_id == thread_id
            )
        )
        if thread is None:
            raise ChatroomNotFoundError(
                f"Thread '{thread_id}' not found in project '{project}'",
                details={"project": project, "thread_id": thread_id},
            )

        # 2. Resolve the target msg_id. Empty / None -> latest. The
        # latest is the numeric max msg_id within this thread (same
        # numeric ordering convention as msg_id_allocator).
        requested = (body.up_to_msg_id or "").strip()
        if not requested:
            target_msg_id = await session.scalar(
                select(Message.msg_id)
                .where(
                    Message.project == project,
                    Message.thread_id == thread_id,
                )
                .order_by(
                    cast(func.substring(Message.msg_id, 5), BigInteger).desc()
                )
                .limit(1)
            )
            if target_msg_id is None:
                # A thread without any msg shouldn't exist (open_thread
                # always inserts the propose), but guard the route to be
                # explicit instead of surfacing a None later.
                raise ChatroomIntegrityError(
                    f"Thread '{thread_id}' has no messages",
                    details={"project": project, "thread_id": thread_id},
                )
        else:
            # Explicit value: validate the msg_id lives in this thread.
            target_msg_id = await session.scalar(
                select(Message.msg_id).where(
                    Message.project == project,
                    Message.thread_id == thread_id,
                    Message.msg_id == requested,
                )
            )
            if target_msg_id is None:
                raise ChatroomIntegrityError(
                    f"msg_id '{requested}' is not in thread '{thread_id}'",
                    details={
                        "project": project,
                        "thread_id": thread_id,
                        "msg_id": requested,
                    },
                )

        # 3. Current cursor (may be absent for never-read threads).
        existing = await session.scalar(
            select(ActorReadCursor).where(
                ActorReadCursor.project == project,
                ActorReadCursor.identity_name == body.identity_name,
                ActorReadCursor.thread_id == thread_id,
            )
        )

        # 4. Monotonic-forward gate. The user picked "rewind = silent
        # no-op": when the requested position is not strictly newer than
        # the current cursor, we return advanced=False without touching
        # the row or emitting an audit event.
        current = existing.last_read_msg_id if existing else None
        if not should_advance_cursor(current, target_msg_id):
            return MarkReadResponse(
                project=project,
                identity_name=body.identity_name,
                thread_id=thread_id,
                last_read_msg_id=current,
                updated_at=existing.updated_at,
                advanced=False,
            )

        # 5. UPSERT the cursor. Single round-trip via Postgres ON
        # CONFLICT. Keeps the previous value in scope for the audit
        # event's `from`.
        prev_cursor = current
        stmt = (
            pg_insert(ActorReadCursor)
            .values(
                project=project,
                identity_name=body.identity_name,
                thread_id=thread_id,
                last_read_msg_id=target_msg_id,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[
                    ActorReadCursor.project,
                    ActorReadCursor.identity_name,
                    ActorReadCursor.thread_id,
                ],
                set_={
                    "last_read_msg_id": target_msg_id,
                    "updated_at": now,
                },
            )
        )
        await session.execute(stmt)

        # 6. Audit event (user picked "emit on advance"). Mirrors the
        # `status_transition` event shape so consumers can branch on
        # `action`.
        session.add(
            ChatroomEvent(
                project=project,
                timestamp=now,
                actor=body.identity_name,
                action="mark_read",
                thread_id=thread_id,
                msg_id=target_msg_id,
                details={"from": prev_cursor, "to": target_msg_id},
            )
        )

    return MarkReadResponse(
        project=project,
        identity_name=body.identity_name,
        thread_id=thread_id,
        last_read_msg_id=target_msg_id,
        updated_at=now,
        advanced=True,
    )


# --- GET /unread ---------------------------------------------------------


@router.get(
    "/unread",
    response_model=UnreadListResponse,
    summary=(
        "Inbox: threads with at least one msg the identity has not read"
    ),
)
async def list_unread(
    project: ProjectPath,
    session: SessionDep,
    identity_name: Annotated[str, Query(min_length=1, max_length=200)],
    include_resolved: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UnreadListResponse:
    # Subquery: latest msg_id per thread in this project (numeric max,
    # matching msg_id_allocator's ordering).
    latest_per_thread = (
        select(
            Message.thread_id.label("thread_id"),
            func.max(
                cast(func.substring(Message.msg_id, 5), BigInteger)
            ).label("latest_num"),
        )
        .where(Message.project == project)
        .group_by(Message.thread_id)
        .subquery("latest_per_thread")
    )

    # The cursor subquery is restricted to the requesting identity so
    # the LEFT JOIN below produces NULL when this identity has never
    # marked the thread read.
    cursor_q = (
        select(
            ActorReadCursor.thread_id.label("thread_id"),
            ActorReadCursor.last_read_msg_id.label("last_read_msg_id"),
        )
        .where(
            ActorReadCursor.project == project,
            ActorReadCursor.identity_name == identity_name,
        )
        .subquery("cursors")
    )

    base = (
        select(
            Thread.thread_id.label("thread_id"),
            Thread.title.label("title"),
            Thread.status.label("status"),
            Thread.owner.label("owner"),
            Thread.created_at.label("created_at"),
            latest_per_thread.c.latest_num.label("latest_num"),
            cursor_q.c.last_read_msg_id.label("last_read_msg_id"),
        )
        .join(
            latest_per_thread,
            and_(
                latest_per_thread.c.thread_id == Thread.thread_id,
            ),
            isouter=False,
        )
        .join(
            cursor_q,
            cursor_q.c.thread_id == Thread.thread_id,
            isouter=True,
        )
        .where(Thread.project == project)
    )
    if not include_resolved:
        base = base.where(Thread.status != "resolved")

    # Unread filter: cursor NULL (never-read) OR latest_num >
    # parse(last_read_msg_id). Doing the parse in SQL keeps the filter
    # index-friendly (substring + cast) and matches msg_id_allocator's
    # numeric ordering exactly.
    cursor_num = cast(
        func.substring(cursor_q.c.last_read_msg_id, 5), BigInteger
    )
    unread_filter = (cursor_q.c.last_read_msg_id.is_(None)) | (
        latest_per_thread.c.latest_num > cursor_num
    )
    base = base.where(unread_filter)

    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0

    rows = (
        await session.execute(
            base.order_by(
                # unread_count desc is order-equivalent to (latest_num -
                # coalesce(cursor_num, 0)) desc. Pre-fall back to 0 with
                # COALESCE to avoid NULL polluting the sort.
                (
                    latest_per_thread.c.latest_num - func.coalesce(cursor_num, 0)
                ).desc(),
                Thread.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items: list[UnreadThreadItem] = []
    for row in rows:
        # Reuse the allocator's format helper so the response uses the
        # same zero-padding semantics as the rest of the system.
        latest_msg_id = format_msg_id(row.latest_num)
        items.append(
            UnreadThreadItem(
                thread_id=row.thread_id,
                title=row.title,
                status=row.status,
                owner=row.owner,
                latest_msg_id=latest_msg_id,
                last_read_msg_id=row.last_read_msg_id,
                unread_count=compute_unread_count(
                    latest_msg_id, row.last_read_msg_id
                ),
            )
        )

    return UnreadListResponse(
        items=items, total=total, limit=limit, offset=offset,
    )
