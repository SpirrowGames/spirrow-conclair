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
from sqlalchemy import BigInteger, cast, func, nulls_last, or_, select, true
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
from spirrow_conclair.services.read_cursor import should_advance_cursor
from spirrow_conclair.services.thread_rollup import msg_num_expr

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

    # ``msg_id`` is allocated project-wide (msg_id_allocator) and shared
    # across threads in the same project, so per-thread aggregates MUST
    # be derived from rows that match ``messages.thread_id`` -- not from
    # numeric subtraction on the project-wide sequence, which would
    # count msgs from sibling threads. The per-thread counts below are
    # computed in SQL, inside the page query, not assembled in Python.
    msg_num = msg_num_expr()
    cursor_num = cast(
        func.substring(cursor_q.c.last_read_msg_id, 5), BigInteger
    )

    # The latest msg_id in *this* thread (for the response `latest_msg_id`
    # and the cursor-advance gate) is read straight off `threads`: it is the
    # stored activity key, the same column `GET /threads` ranks on, so the two
    # triage surfaces cannot drift apart. It used to be a GROUP BY over every
    # msg in the project, joined in -- see `services/thread_rollup` for what
    # that cost and why it moved.
    # Per-thread unread count: the msgs in this thread whose numeric msg_id
    # is strictly greater than the identity's cursor (all of them when the
    # cursor is null).
    #
    # Its cost is (threads that reach it) x (one evaluation each) x (msgs
    # scanned per evaluation), and the first two factors are what matter.
    # Measured before this shape (#30, CI `perf`, 300k msgs / 5k threads,
    # ~60 msgs per thread -- well under the "few hundred" this comment used
    # to assume): 636 ms per request, because the count was a correlated
    # scalar subquery placed in the SELECT list, the WHERE and the ORDER BY,
    # which Postgres planned as SubPlans and ran up to three times per
    # thread, for every thread, before LIMIT could apply. Per-thread size
    # was never the problem; thread count times evaluations was. Hence:
    #
    # - one LATERAL join, so the SELECT list, the WHERE and the ORDER BY all
    #   read the same per-row result (one evaluation per surviving thread);
    # - the `last_msg_num` pre-filter below, so caught-up threads never get
    #   counted at all.
    #
    # The join is INNER: `count(*)` always yields exactly one row, so outer
    # and inner are the same rows, and with `unread_count > 0` in the WHERE
    # INNER is the honest statement of what the row needs.
    # `tests/integration/test_thread_listing_scale.py::
    # test_unread_inbox_cost_breakdown` re-measures this and pins the plan
    # shape; if it reds, the premise above has moved.
    unread_lateral = (
        select(func.count().label("unread_count"))
        .select_from(Message)
        .where(
            Message.project == project,
            Message.thread_id == Thread.thread_id,
            msg_num > func.coalesce(cursor_num, 0),
        )
        .lateral("unread")
    )
    unread_count = unread_lateral.c.unread_count

    base = (
        select(
            Thread.thread_id.label("thread_id"),
            Thread.title.label("title"),
            Thread.status.label("status"),
            Thread.owner.label("owner"),
            Thread.created_at.label("created_at"),
            Thread.last_msg_num.label("latest_num"),
            cursor_q.c.last_read_msg_id.label("last_read_msg_id"),
            unread_count.label("unread_count"),
        )
        .select_from(Thread)
        .join(
            cursor_q,
            cursor_q.c.thread_id == Thread.thread_id,
            isouter=True,
        )
        .join(unread_lateral, true())
        .where(Thread.project == project)
    )
    if not include_resolved:
        base = base.where(Thread.status != "resolved")

    # Membership: a row is in the inbox iff `unread_count > 0`, counted from
    # `messages` (this subsumes the older "cursor NULL OR latest_num >
    # cursor_num" check). It reads the lateral's output column, so it adds
    # no evaluation.
    #
    # In front of it sits a cheap pre-filter on the stored activity key:
    # a thread whose newest msg is not past the cursor cannot have an
    # unread msg, so it never reaches the per-thread count. This is a
    # narrowing only, never the membership rule -- `unread_count > 0`
    # stays the contract, so a key that is too *high* (a deleted msg, a
    # hand repair) costs one wasted count and still cannot put a
    # zero-unread row in the inbox. A key that is too *low* would hide a
    # thread; the two write sites assign it in the msg's own transaction
    # and `stale_activity_key` audits it. A NULL key is let through to the
    # count rather than read as "no msgs": the integrity audit treats a
    # NULL on a thread that has msgs as stale, and the ordering below
    # already decides where such a row goes, so it must stay reachable.
    base = base.where(
        or_(
            Thread.last_msg_num.is_(None),
            Thread.last_msg_num > func.coalesce(cursor_num, 0),
        )
    )
    base = base.where(unread_count > 0)

    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0

    rows = (
        await session.execute(
            base.order_by(
                # "Most unread first, then by thread recency" -- the
                # first page is the actionable surface. Recency here is
                # recency of *activity* (latest msg), not of creation:
                # ordering by created_at sank threads that are alive but
                # old below ones that are new and silent, which is the
                # opposite of what a triage surface owes the reader.
                # `last_msg_num` is a stored column on the row already
                # being read, so this key costs nothing to add -- and it
                # is the same key `GET /threads` ranks on.
                #
                # NULLS LAST is explicit because Postgres defaults a DESC
                # sort to NULLS FIRST, and the two triage surfaces must
                # not disagree about where an unranked row goes (the
                # listing already spells it out). A thread with no msgs
                # cannot reach here -- `unread_count > 0` excludes it --
                # so the reachable NULL is a *stale* key on a thread that
                # does have unread msgs, i.e. the one row whose rank is
                # known to be untrustworthy. NULLS FIRST would put exactly
                # that row at the top of the inbox.
                unread_count.desc(),
                nulls_last(Thread.last_msg_num.desc()),
                Thread.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items: list[UnreadThreadItem] = []
    for row in rows:
        # Reuse the allocator's format helper so the response uses the
        # same zero-padding semantics as the rest of the system. A row is
        # here only because `unread_count > 0`, so the thread has msgs and
        # its activity key is set; the guard is for a key that has somehow
        # gone stale, where reporting msg-000 would be worse than nothing.
        latest_msg_id = (
            format_msg_id(row.latest_num) if row.latest_num is not None else ""
        )
        items.append(
            UnreadThreadItem(
                thread_id=row.thread_id,
                title=row.title,
                status=row.status,
                owner=row.owner,
                latest_msg_id=latest_msg_id,
                last_read_msg_id=row.last_read_msg_id,
                unread_count=row.unread_count,
            )
        )

    return UnreadListResponse(
        items=items, total=total, limit=limit, offset=offset,
    )
