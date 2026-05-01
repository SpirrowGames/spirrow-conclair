"""Thread endpoints (open / list / get).

POST /v1/projects/{project}/threads      — open_thread
GET  /v1/projects/{project}/threads      — list_threads
GET  /v1/projects/{project}/threads/{id} — get_thread (mode=full|summary)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, status
from sqlalchemy import BigInteger, cast, func, select

from spirrow_conclair.api.messages import post_message_in_session
from spirrow_conclair.db import SessionDep
from spirrow_conclair.exceptions import ChatroomIntegrityError, ChatroomNotFoundError
from spirrow_conclair.models import ChatroomEvent, Message, Thread
from spirrow_conclair.schemas import (
    CloseThreadRequest,
    CloseThreadResponse,
    Message as MessageSchema,
)
from spirrow_conclair.schemas import (
    OpenThreadRequest,
    OpenThreadResponse,
    Thread as ThreadSchema,
    ThreadListResponse,
    ThreadStatus,
    ThreadView,
)
from spirrow_conclair.services import integrity as integrity_svc
from spirrow_conclair.services.msg_id_allocator import allocate_next_msg_id
from spirrow_conclair.services.permissions import assert_owner_can_close

router = APIRouter(prefix="/v1/projects/{project}/threads", tags=["threads"])

ProjectPath = Annotated[str, Path(min_length=1, max_length=200)]
ThreadIdPath = Annotated[str, Path(min_length=1, max_length=200)]


# --- POST /threads (open_thread) -----------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=OpenThreadResponse,
    summary="Open a new thread (creates the thread + its propose message in one txn)",
)
async def open_thread(
    project: ProjectPath,
    body: OpenThreadRequest,
    session: SessionDep,
) -> OpenThreadResponse:
    timestamp = body.timestamp or datetime.now(timezone.utc)

    async with session.begin():
        # Reject if a thread with the same id already exists in this project.
        existing = await session.scalar(
            select(Thread.thread_id).where(
                Thread.project == project, Thread.thread_id == body.thread_id
            )
        )
        if existing:
            raise ChatroomIntegrityError(
                f"Thread '{body.thread_id}' already exists in project '{project}'",
                details={"project": project, "thread_id": body.thread_id},
            )

        msg_id = await allocate_next_msg_id(session, project)

        thread_orm = Thread(
            project=project,
            thread_id=body.thread_id,
            title=body.title,
            owner=body.owner,
            status="active",
            created_at=timestamp,
            created_by_msg=msg_id,
            resolved_by_msg=None,
            affects_threads=[],
            tags=list(body.tags),
        )
        msg_orm = Message(
            project=project,
            msg_id=msg_id,
            thread_id=body.thread_id,
            author=body.owner,
            timestamp=timestamp,
            commit_ref=body.commit_ref,
            type="propose",
            content=body.propose_content,
            reply_to=None,
            references_threads=[],
            related_tasks=[],
            closes_thread=None,
            tags=list(body.tags),
        )
        event_orm = ChatroomEvent(
            project=project,
            timestamp=timestamp,
            actor=body.owner,
            action="open_thread",
            thread_id=body.thread_id,
            msg_id=msg_id,
            details={},
        )
        # Composite FK (messages.project,thread_id -> threads) is not picked
        # up by SQLAlchemy's topological flush order, so flush the thread
        # first to satisfy the constraint.
        session.add(thread_orm)
        await session.flush()
        session.add(msg_orm)
        session.add(event_orm)

    return OpenThreadResponse(
        thread=ThreadSchema.model_validate(thread_orm),
        msg=MessageSchema.model_validate(msg_orm),
    )


# --- GET /threads (list_threads) -----------------------------------------


@router.get(
    "",
    response_model=ThreadListResponse,
    summary="List threads with optional status / owner filter",
)
async def list_threads(
    project: ProjectPath,
    session: SessionDep,
    status_filter: Annotated[
        list[ThreadStatus] | None,
        Query(alias="status", description="Filter by thread status (repeatable)"),
    ] = None,
    owner: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ThreadListResponse:
    base = select(Thread).where(Thread.project == project)
    if status_filter:
        base = base.where(Thread.status.in_(status_filter))
    if owner:
        base = base.where(Thread.owner == owner)

    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0

    rows = (
        await session.execute(
            base.order_by(Thread.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    return ThreadListResponse(
        items=[ThreadSchema.model_validate(t) for t in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# --- GET /threads/{thread_id} (get_thread) -------------------------------


@router.get(
    "/{thread_id}",
    response_model=ThreadView,
    summary="Get a thread with its messages (full or summary mode)",
)
async def get_thread(
    project: ProjectPath,
    thread_id: ThreadIdPath,
    session: SessionDep,
    mode: Annotated[Literal["full", "summary"], Query()] = "full",
) -> ThreadView:
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

    msg_query = (
        select(Message)
        .where(Message.project == project, Message.thread_id == thread_id)
        .order_by(cast(func.substring(Message.msg_id, 5), BigInteger))
    )
    # `summary` view on a resolved thread returns only the decide msg.
    # Active / awaiting_reply / superseded / parked threads always show
    # the full message list; the spec's "archive concept" is realised
    # purely through this filter.
    if mode == "summary" and thread.status == "resolved":
        msg_query = msg_query.where(Message.type == "decide")

    msg_rows = (await session.execute(msg_query)).scalars().all()

    return ThreadView(
        thread=ThreadSchema.model_validate(thread),
        messages=[MessageSchema.model_validate(m) for m in msg_rows],
        mode=mode,
    )


# --- POST /threads/{thread_id}/close (close_thread shortcut) -------------


@router.post(
    "/{thread_id}/close",
    status_code=status.HTTP_201_CREATED,
    response_model=CloseThreadResponse,
    summary="Close a thread by posting a decide msg (owner-only)",
)
async def close_thread(
    project: ProjectPath,
    thread_id: ThreadIdPath,
    body: CloseThreadRequest,
    session: SessionDep,
) -> CloseThreadResponse:
    async with session.begin():
        thread = await integrity_svc.fetch_thread_or_raise(
            session, project=project, thread_id=thread_id
        )
        # Owner check first so non-owner attempts surface as 403 rather
        # than as the integrity 409 from assert_closes_thread_rule.
        assert_owner_can_close(thread, body.author)

        # affects_threads is a thread-level field; patch it before
        # post_message_in_session so it's persisted in the same txn.
        if body.affects_threads:
            thread.affects_threads = list(body.affects_threads)

        msg_orm, _transition = await post_message_in_session(
            session,
            project=project,
            thread=thread,
            msg_type="decide",
            author=body.author,
            content=body.summary_content,
            references_threads=None,
            related_tasks=body.related_tasks,
            closes_thread=thread_id,
            tags=body.tags,
            commit_ref=body.commit_ref,
            timestamp=body.timestamp,
        )

    return CloseThreadResponse(
        thread=ThreadSchema.model_validate(thread),
        decide_msg=MessageSchema.model_validate(msg_orm),
    )
