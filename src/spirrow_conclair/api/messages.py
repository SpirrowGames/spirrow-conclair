"""Message endpoints (post)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.db import SessionDep
from spirrow_conclair.models import ChatroomEvent, Message, Thread
from spirrow_conclair.schemas import (
    CloseSanction,
    PostMessageRequest,
    PostMessageResponse,
)
from spirrow_conclair.schemas import (
    Message as MessageSchema,
)
from spirrow_conclair.services import integrity as integrity_svc
from spirrow_conclair.services.close_sanction import (
    CLOSE_SANCTION_KEY,
    UNSPECIFIED_SANCTION,
)
from spirrow_conclair.services.msg_id_allocator import allocate_next_msg_id, parse_msg_id
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
    embodiment: str | None = None,
    role: str | None = None,
    next_participant: str | None = None,
    owner_override: bool = False,
    owner_override_reason: str | None = None,
    close_sanction: CloseSanction | None = None,
) -> tuple[Message, str | None]:
    """Insert a single message + status-transition event (if any).

    MUST be called inside an active transaction. Returns the new Message
    and the new thread.status if a transition occurred (else None). The
    `thread` arg is updated in-place to reflect the post-transition state.

    ADR-2026-06-04-19 D-5: ``owner_override`` relaxes only the ownership
    clause of the closes_thread rule. When it actually takes effect (the
    author is not the owner), the post_message audit event records the
    bypass (``owner_override`` / ``thread_owner`` / ``owner_override_reason``)
    so "who force-closed whose thread, and why" is traceable.

    **This function is the sanction recorder**, and that placement is the
    property the audit's strictest bucket rests on. Both routes that can write
    a ``closes_thread`` msg funnel through here (``POST .../messages`` and
    ``POST .../close``; ``open_thread`` hardcodes ``closes_thread=None``), so
    "no record" can mean "did not come through the write path". Putting the
    recorder in the callers instead would make one forgotten call site
    indistinguishable from a direct INSERT -- see ``services.close_sanction``.
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
    # owner_override only meaningfully bypasses when the author isn't the
    # owner; capture that so the audit reflects an *actual* force-close.
    owner_override_applied = bool(owner_override) and author != thread.owner
    integrity_svc.assert_closes_thread_rule(
        thread=thread,
        msg_type=msg_type,
        closes_thread=closes_thread,
        author=author,
        owner_override=owner_override,
    )
    # Order matters: this one accepts any non-None closes_thread as proof the
    # msg closes its thread, which the assert above is what makes true (right
    # thread, right author, type='decide'). See assert_next_participant_rule.
    integrity_svc.assert_next_participant_rule(
        next_participant=next_participant,
        closes_thread=closes_thread,
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
        embodiment=embodiment,
        role=role,
        next_participant=next_participant,
    )
    session.add(msg_orm)
    # The thread's activity sort key. This is the *only* place a msg is added
    # to an existing thread, and open_thread is the only other place a msg is
    # created at all, so those two assignments are the whole write path for
    # `threads.last_msg_num`. It is monotonic by construction (the allocator
    # hands out increasing numbers within a project), and the
    # `stale_activity_key` integrity check recomputes it from `messages`.
    thread.last_msg_num = parse_msg_id(msg_id)
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

    post_details: dict[str, Any] = {"type": msg_type}
    if owner_override_applied:
        post_details["owner_override"] = True
        post_details["thread_owner"] = thread.owner
        post_details["owner_override_reason"] = owner_override_reason
    # The predicate here is deliberately the *audit's* predicate, spelled the
    # same way: `audit_project` walks msgs with `closes_thread` set whose
    # author is not the owner, and every such msg written from here carries a
    # record. Deriving it from `owner_override` instead would drift the two
    # apart the moment another clause learns to relax ownership.
    if closes_thread is not None and author != thread.owner:
        post_details[CLOSE_SANCTION_KEY] = (
            close_sanction.model_dump(exclude_none=True)
            if close_sanction is not None
            # A bare `owner_override` says a bypass applied and nothing about
            # which one. Recorded as exactly that rather than inferred from
            # the presence of `owner_override_reason`: a carve-out caller is
            # free to pass a reason too, so that proxy would launder a guess
            # into the audit trail.
            else dict(UNSPECIFIED_SANCTION)
        )
    session.add(
        ChatroomEvent(
            project=project,
            timestamp=timestamp,
            actor=author,
            action="post_message",
            thread_id=thread.thread_id,
            msg_id=msg_id,
            details=post_details,
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
            embodiment=body.embodiment,
            role=body.role,
            next_participant=body.next_participant,
            owner_override=body.owner_override,
            owner_override_reason=body.owner_override_reason,
            close_sanction=body.close_sanction,
        )

    return PostMessageResponse(
        msg=MessageSchema.model_validate(msg_orm),
        thread_status_changed_to=transition_to,
    )
