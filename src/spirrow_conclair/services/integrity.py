"""Integrity invariants (System Design v2 §9).

Two flavors:

1. **Pre-write asserts** (`assert_*`) — called from API handlers immediately
   before INSERT to fail fast with `ChatroomIntegrityError` /
   `ChatroomPermissionError`. These need DB round-trips to look up the
   target thread and existing messages, so they are async.

2. **Audit report** (`audit_project`) — scans the entire dataset for a
   project and returns an `IntegrityIssue` list for the
   `GET /v1/projects/{project}/integrity` endpoint. Never raises; even
   broken state is just reported.

Invariants enforced (per design v2 §9):
1. msg.thread_id exists in threads
2. propose msg is the first msg of its thread, and author == thread.owner
3. msg with closes_thread set must have type='decide' AND author == thread.owner
4. reply_to (when set) must reference a msg in the same thread
5. references_threads (when set) must all exist in the same project
6. msg_id uniqueness — enforced by composite PK at the DB layer
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.exceptions import (
    ChatroomIntegrityError,
    ChatroomNotFoundError,
)
from spirrow_conclair.models import Message, Thread
from spirrow_conclair.schemas.event import IntegrityIssue


# ----- pre-write asserts -----


async def fetch_thread_or_raise(
    session: AsyncSession, *, project: str, thread_id: str
) -> Thread:
    """Return the Thread or raise ChatroomNotFoundError."""
    result = await session.execute(
        select(Thread).where(
            Thread.project == project, Thread.thread_id == thread_id
        )
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise ChatroomNotFoundError(
            f"Thread '{thread_id}' not found in project '{project}'",
            details={"project": project, "thread_id": thread_id},
        )
    return thread


async def assert_propose_invariant(
    session: AsyncSession,
    *,
    project: str,
    thread: Thread,
    msg_type: str,
    author: str,
) -> None:
    """Invariant 2: a thread's first message must be `propose` and authored
    by the owner. Conversely, no later message may be `propose`.
    """
    existing_count = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.project == project, Message.thread_id == thread.thread_id)
    )

    if msg_type == "propose":
        if existing_count and existing_count > 0:
            raise ChatroomIntegrityError(
                f"Thread '{thread.thread_id}' already has messages; "
                f"a second 'propose' is not allowed",
                details={
                    "thread_id": thread.thread_id,
                    "existing_message_count": existing_count,
                },
            )
        if author != thread.owner:
            raise ChatroomIntegrityError(
                f"propose msg must be authored by the thread owner. "
                f"thread.owner='{thread.owner}', author='{author}'",
                details={
                    "thread_id": thread.thread_id,
                    "thread_owner": thread.owner,
                    "author": author,
                },
            )
    else:
        # any non-propose msg requires an existing propose to be present
        if not existing_count:
            raise ChatroomIntegrityError(
                f"First message of thread '{thread.thread_id}' must be 'propose', "
                f"got '{msg_type}'",
                details={
                    "thread_id": thread.thread_id,
                    "first_msg_type": msg_type,
                },
            )


def assert_closes_thread_rule(
    *,
    thread: Thread,
    msg_type: str,
    closes_thread: str | None,
    author: str,
    owner_override: bool = False,
) -> None:
    """Invariant 3: a `closes_thread` value is only valid on a `decide` msg
    whose author matches the thread owner, and must reference its own thread.

    ADR-2026-06-04-19 D-5: ``owner_override=True`` relaxes *only* the
    ``author == thread.owner`` clause (human Tier-C force-close). The
    ``type='decide'`` and ``closes_thread == thread_id`` invariants always
    hold.
    """
    if closes_thread is None:
        return

    if msg_type != "decide":
        raise ChatroomIntegrityError(
            f"closes_thread is only valid with type='decide', got type='{msg_type}'",
            details={"msg_type": msg_type, "closes_thread": closes_thread},
        )

    if closes_thread != thread.thread_id:
        raise ChatroomIntegrityError(
            f"closes_thread '{closes_thread}' does not match the URL thread_id "
            f"'{thread.thread_id}'",
            details={
                "closes_thread": closes_thread,
                "thread_id": thread.thread_id,
            },
        )

    if author != thread.owner and not owner_override:
        # Permission concept overlaps with integrity here, but this branch
        # is also catchable by services.permissions.assert_owner_can_close.
        # Keep it as IntegrityError so the dispatch is uniform when called
        # via post_message; close_thread endpoint will use the dedicated
        # PermissionError path before reaching here.
        raise ChatroomIntegrityError(
            f"Only the thread owner can post a closes_thread decide msg. "
            f"thread.owner='{thread.owner}', author='{author}'",
            details={
                "thread_id": thread.thread_id,
                "thread_owner": thread.owner,
                "author": author,
            },
        )


async def assert_reply_to_in_thread(
    session: AsyncSession,
    *,
    project: str,
    thread_id: str,
    reply_to: str | None,
) -> None:
    """Invariant 4: reply_to must reference a msg in the same thread."""
    if reply_to is None:
        return

    result = await session.execute(
        select(Message.msg_id).where(
            Message.project == project,
            Message.msg_id == reply_to,
            Message.thread_id == thread_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ChatroomIntegrityError(
            f"reply_to '{reply_to}' does not exist in thread '{thread_id}'",
            details={"reply_to": reply_to, "thread_id": thread_id},
        )


async def assert_references_threads_exist(
    session: AsyncSession,
    *,
    project: str,
    references_threads: list[str],
) -> None:
    """Invariant 5: every references_threads entry must exist in the same project."""
    if not references_threads:
        return

    result = await session.execute(
        select(Thread.thread_id).where(
            Thread.project == project,
            Thread.thread_id.in_(references_threads),
        )
    )
    found = {row[0] for row in result.all()}
    missing = [t for t in references_threads if t not in found]
    if missing:
        raise ChatroomIntegrityError(
            f"references_threads contains unknown thread_id(s): {missing}",
            details={"missing": missing, "project": project},
        )


# ----- full audit report -----


async def audit_project(
    session: AsyncSession, *, project: str
) -> list[IntegrityIssue]:
    """Walk all threads and messages of `project`, returning issues found.

    Never raises; the caller (integrity endpoint) returns 200 with the list.
    """
    issues: list[IntegrityIssue] = []

    # Thread map for cross-references
    thread_rows = (
        await session.execute(select(Thread).where(Thread.project == project))
    ).scalars().all()
    threads_by_id: dict[str, Thread] = {t.thread_id: t for t in thread_rows}

    # All messages of the project, ordered by msg_id (numeric, not lex —
    # to keep `msg-9` < `msg-10` once we cross zero-pad boundaries).
    msg_rows = (
        await session.execute(
            select(Message)
            .where(Message.project == project)
            .order_by(cast(func.substring(Message.msg_id, 5), BigInteger))
        )
    ).scalars().all()
    msgs_by_thread: dict[str, list[Message]] = {}
    for m in msg_rows:
        msgs_by_thread.setdefault(m.thread_id, []).append(m)

    # Invariant 1 (orphan_message): msg.thread_id missing from threads
    for m in msg_rows:
        if m.thread_id not in threads_by_id:
            issues.append(
                IntegrityIssue(
                    type="orphan_message",
                    thread_id=m.thread_id,
                    msg_id=m.msg_id,
                    details=(
                        f"Message '{m.msg_id}' references unknown thread "
                        f"'{m.thread_id}' (FK violation slipped through)"
                    ),
                )
            )

    # Invariant 2 (missing_propose): each thread's first msg must be propose by owner
    for thread_id, thread in threads_by_id.items():
        msgs = msgs_by_thread.get(thread_id, [])
        if not msgs:
            issues.append(
                IntegrityIssue(
                    type="missing_propose",
                    thread_id=thread_id,
                    details="Thread has no messages (expected at least a propose)",
                )
            )
            continue
        first = msgs[0]
        if first.type != "propose":
            issues.append(
                IntegrityIssue(
                    type="missing_propose",
                    thread_id=thread_id,
                    msg_id=first.msg_id,
                    details=(
                        f"First message of thread '{thread_id}' has "
                        f"type='{first.type}', expected 'propose'"
                    ),
                )
            )
        elif first.author != thread.owner:
            issues.append(
                IntegrityIssue(
                    type="missing_propose",
                    thread_id=thread_id,
                    msg_id=first.msg_id,
                    details=(
                        f"propose msg author='{first.author}' does not match "
                        f"thread.owner='{thread.owner}'"
                    ),
                )
            )

    # Invariant 3 (closes_thread_by_non_owner): closes_thread set + author != owner
    for m in msg_rows:
        if not m.closes_thread:
            continue
        thread = threads_by_id.get(m.thread_id)
        if thread is None:
            # already reported as orphan
            continue
        if m.author != thread.owner:
            issues.append(
                IntegrityIssue(
                    type="closes_thread_by_non_owner",
                    thread_id=m.thread_id,
                    msg_id=m.msg_id,
                    details=(
                        f"Message author='{m.author}' set closes_thread but "
                        f"thread.owner='{thread.owner}'"
                    ),
                )
            )

    # Invariant 4 (invalid_reply_to): reply_to not in same thread
    msgs_in_thread_ids: dict[str, set[str]] = {
        tid: {m.msg_id for m in ms} for tid, ms in msgs_by_thread.items()
    }
    for m in msg_rows:
        if m.reply_to is None:
            continue
        same_thread = msgs_in_thread_ids.get(m.thread_id, set())
        if m.reply_to not in same_thread:
            issues.append(
                IntegrityIssue(
                    type="invalid_reply_to",
                    thread_id=m.thread_id,
                    msg_id=m.msg_id,
                    details=(
                        f"reply_to '{m.reply_to}' not found in thread '{m.thread_id}'"
                    ),
                )
            )

    # Invariant 5 (dangling_thread_reference): references_threads entry missing
    known_thread_ids = set(threads_by_id.keys())
    for m in msg_rows:
        for ref in m.references_threads or []:
            if ref not in known_thread_ids:
                issues.append(
                    IntegrityIssue(
                        type="dangling_thread_reference",
                        thread_id=m.thread_id,
                        msg_id=m.msg_id,
                        details=(
                            f"references_threads entry '{ref}' does not exist "
                            f"in project '{project}'"
                        ),
                    )
                )

    # Inconsistent resolved: thread.status='resolved' but resolved_by_msg empty,
    # or non-resolved thread has resolved_by_msg populated.
    for thread in threads_by_id.values():
        if thread.status == "resolved" and not thread.resolved_by_msg:
            issues.append(
                IntegrityIssue(
                    type="inconsistent_resolved",
                    thread_id=thread.thread_id,
                    details="Thread.status='resolved' but resolved_by_msg is empty",
                )
            )
        elif thread.status != "resolved" and thread.resolved_by_msg:
            issues.append(
                IntegrityIssue(
                    type="inconsistent_resolved",
                    thread_id=thread.thread_id,
                    details=(
                        f"Thread.resolved_by_msg='{thread.resolved_by_msg}' "
                        f"but status='{thread.status}'"
                    ),
                )
            )

    return issues


def now_utc() -> datetime:
    """Helper: timezone-aware UTC `datetime` for `IntegrityCheckResponse.checked_at`."""
    return datetime.now(timezone.utc)
