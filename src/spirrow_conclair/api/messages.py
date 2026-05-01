"""Message endpoints (post)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.db import SessionDep
from spirrow_conclair.models import ChatroomEvent, Message, Thread
from spirrow_conclair.schemas import (
    Message as MessageSchema,
)
from spirrow_conclair.schemas import (
    PostMessageRequest,
    PostMessageResponse,
)
from spirrow_conclair.services import integrity as integrity_svc
from spirrow_conclair.services.msg_id_allocator import allocate_next_msg_id
from spirrow_conclair.services.status_transition import compute_transition

router = APIRouter(
    prefix="/v1/projects/{project}/threads/{thread_id}/messages",
    tags=["messages"],
)

ProjectPath = Annotated[str, Path(min_length=1, max_length=200)]
ThreadIdPath = Annotated[str, Path(min_length=1, max_length=200)]


async def post_message_in_session(
    session: AsyncSession,
    *,
    project: str,
    thread: Thread,
    msg_type: str,
    author: str,
    content: str,
    reply_to: str | None = None,
    references_threads: list[str] | None = None,
    related_tasks: list[str] | None = None,
    closes_thread: str | None = None,
    tags: list[str] | None = None,
    commit_ref: str | None = None,
    timestamp: datetime | None = None,
) -> tuple[Message, str | None]:
    """Insert a single message + status-transition event (if any).

    MUST be called inside an active transaction. Returns the new Message
    and the new thread.status if a transition occurred (else None). The
    `thread` arg is updated in-place to reflect the post-transition state.
    """
    references_threads = list(references_threads or [])
    related_tasks = list(related_tasks or [])
    tags = list(tags or [])
    timestamp = timestamp or datetime.now(timezone.utc)

    # Pre-write asserts (each raises ChatroomIntegrityError on violation).
    await integrity_svc.assert_propose_invariant(
        session,
        project=project,
        thread=thread,
        msg_type=msg_type,
        author=author,
    )
    integrity_svc.assert_closes_thread_rule(
        thread=thread,
        msg_type=msg_type,
        closes_thread=closes_thread,
        author=author,
    )
    await integrity_svc.assert_reply_to_in_thread(
        session,
        project=project,
        thread_id=thread.thread_id,
        reply_to=reply_to,
    )
    await integrity_svc.assert_references_threads_exist(
        session,
        project=project,
        references_threads=references_threads,
    )

    msg_id = await allocate_next_msg_id(session, project)
    msg_orm = Message(
        project=project,
        msg_id=msg_id,
        thread_id=thread.thread_id,
        author=author,
        timestamp=timestamp,
        commit_ref=commit_ref,
        type=msg_type,
        content=content,
        reply_to=reply_to,
        references_threads=references_threads,
        related_tasks=related_tasks,
        closes_thread=closes_thread,
        tags=tags,
    )
    session.add(msg_orm)
    # Make the new msg visible to the status-transition event row insert
    # in the same transaction.
    await session.flush()

    # status transition (raises ChatroomStateError on closed-thread decide)
    new_status, extra_fields = compute_transition(thread, msg_orm)

    transition_to: str | None = None
    if new_status is not None:
        prev_status = thread.status
        thread.status = new_status
        for field, value in extra_fields.items():
            setattr(thread, field, value)
        transition_to = new_status
        session.add(
            ChatroomEvent(
                project=project,
                timestamp=timestamp,
                actor=author,
                action="status_transition",
                thread_id=thread.thread_id,
                msg_id=msg_id,
                details={"from": prev_status, "to": new_status},
            )
        )

    session.add(
        ChatroomEvent(
            project=project,
            timestamp=timestamp,
            actor=author,
            action="post_message",
            thread_id=thread.thread_id,
            msg_id=msg_id,
            details={"type": msg_type},
        )
    )

    return msg_orm, transition_to


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PostMessageResponse,
    summary="Post a message in an existing thread (status transitions applied automatically)",
)
async def post_message(
    project: ProjectPath,
    thread_id: ThreadIdPath,
    body: PostMessageRequest,
    session: SessionDep,
) -> PostMessageResponse:
    async with session.begin():
        thread = await integrity_svc.fetch_thread_or_raise(
            session, project=project, thread_id=thread_id
        )
        msg_orm, transition_to = await post_message_in_session(
            session,
            project=project,
            thread=thread,
            msg_type=body.type,
            author=body.author,
            content=body.content,
            reply_to=body.reply_to,
            references_threads=body.references_threads,
            related_tasks=body.related_tasks,
            closes_thread=body.closes_thread,
            tags=body.tags,
            commit_ref=body.commit_ref,
            timestamp=body.timestamp,
        )

    return PostMessageResponse(
        msg=MessageSchema.model_validate(msg_orm),
        thread_status_changed_to=transition_to,
    )
