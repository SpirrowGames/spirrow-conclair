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
   (the audit half is three-way, not binary: a non-owner close that carries a
   recorded sanction is counted rather than reported, and one that predates the
   recorder is neither -- see `services.close_sanction`)
4. reply_to (when set) must reference a msg in the same thread
5. references_threads (when set) must all exist in the same project
6. msg_id uniqueness — enforced by composite PK at the DB layer
7. a msg with closes_thread set must not also name a next_participant
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.exceptions import (
    ChatroomIntegrityError,
    ChatroomNotFoundError,
)
from spirrow_conclair.models import ChatroomEvent, Message, Thread
from spirrow_conclair.schemas.event import (
    IntegrityIssue,
    SanctionedCloseCounts,
    UnattributableClose,
)
from spirrow_conclair.services.close_sanction import (
    SanctionRecord,
    classify_non_owner_close,
    read_sanction_record,
)
from spirrow_conclair.services.msg_id_allocator import format_msg_id, parse_msg_id


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


def assert_next_participant_rule(
    *,
    next_participant: str | None,
    closes_thread: str | None,
) -> None:
    """Invariant 7: a msg that closes its thread names no successor.

    The pair this refuses is a **settled thread with a pending successor** — a
    row saying both that the work is over and that somebody still owes a turn.
    Nothing downstream can act on it, and ``messages`` is append-only, so it
    could not be repaired after the fact.

    "Nobody is next" and "this thread is finished" are the same fact, and the
    thread is its keeper (``closes_thread`` → ``status='resolved'`` /
    ``resolved_by_msg``). So there is exactly one way to record that a thread
    has no successor: close it. Note what that means — **there is no sentinel
    value here**. An earlier draft reserved ``'none'`` for "nobody is next" and
    required it to accompany a close, but once tied to the close it could say
    nothing the adjacent ``closes_thread`` did not already say; a second
    encoding of one fact is the very thing this invariant exists to prevent.
    (Tier B naysayer, PR #13.)

    **Call this after** :func:`assert_closes_thread_rule`. This function treats
    any non-``None`` ``closes_thread`` as "this msg closes its thread", which
    is only true once that assert has established the value names *this*
    thread and comes from its owner on a ``decide``.

    Participant names are deliberately **not** checked — not for existence, not
    against a roster, and no string is reserved. Answering "may Heisenberg act
    here?" requires the Prismind identity record, and Conclair must not pull
    identity state cross-service (the boundary ``role`` already draws).
    Magickit owns that half; this owns only what a single row can answer about
    itself.
    """
    if closes_thread is None:
        return

    if next_participant is not None:
        raise ChatroomIntegrityError(
            f"a msg that closes its thread cannot name a successor, but "
            f"next_participant='{next_participant}' was supplied alongside "
            f"closes_thread='{closes_thread}'. Closing the thread IS how "
            "'nobody is next' is recorded; to hand the thread on instead, name "
            "the participant and leave closes_thread unset.",
            details={
                "next_participant": next_participant,
                "closes_thread": closes_thread,
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


async def assert_msg_in_thread(
    session: AsyncSession,
    *,
    project: str,
    thread_id: str,
    msg_id: str,
    field: str,
) -> None:
    """A named field must reference an existing msg in this thread.

    The general form of ``assert_reply_to_in_thread`` above, for callers
    that point at a msg from outside ``messages`` -- currently the digest
    endpoints (``source_last_msg_id`` / ``target_msg_id``), which have no
    FK for the same reason ``actor_read_cursors`` has none.

    ``assert_reply_to_in_thread`` deliberately does *not* delegate here:
    its ``None`` short-circuit is part of its contract and is covered by
    its own tests. The overlap is two similar queries, not a missing
    abstraction.

    Scoping on ``thread_id`` is the whole point. ``msg_id`` is allocated
    project-wide (``msg_id_allocator``), so a check that only filtered on
    ``project`` would accept a sibling thread's msg -- and a digest whose
    coverage key belongs to another thread can never be measured.

    Args:
        session: Open session.
        project: Project scope.
        thread_id: The thread the msg must belong to.
        msg_id: The msg id to verify.
        field: Name of the field being checked, for the error message.

    Raises:
        ChatroomIntegrityError: If no such msg exists in this thread.
    """
    result = await session.execute(
        select(Message.msg_id).where(
            Message.project == project,
            Message.msg_id == msg_id,
            Message.thread_id == thread_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ChatroomIntegrityError(
            f"{field} '{msg_id}' does not exist in thread '{thread_id}'",
            details={field: msg_id, "thread_id": thread_id},
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


@dataclass
class AuditReport:
    """What ``audit_project`` found.

    ``issues`` is the part that should be zero on a healthy project. The other
    two fields exist because a non-owner close is not a two-valued question:
    counting sanctioned closes as breakage made the number grow with every
    merged PR, and calling an unrecorded old close "fine" would be a guess.
    See ``services.close_sanction``.
    """

    issues: list[IntegrityIssue] = field(default_factory=list)
    sanctioned_counts: SanctionedCloseCounts = field(
        default_factory=SanctionedCloseCounts
    )
    unattributable: list[UnattributableClose] = field(default_factory=list)


async def _fetch_close_evidence(
    session: AsyncSession, *, project: str, msg_ids: list[str]
) -> tuple[dict[str, SanctionRecord], set[str]]:
    """Sanction records and status-transition presence, keyed by *message*.

    ``chatroom_events.msg_id`` is the attribution key: it already exists on
    the row, so the sanction needs no column of its own and no migration, and
    the join is per-message rather than per-thread. That matters -- a thread
    can hold more than one closing msg (a direct INSERT, or two concurrent
    closes racing on a stale in-memory ``thread.status``), and a thread-level
    lookup would hand one msg's sanction to the other, hiding exactly the row
    the audit exists to surface.

    Cost: no index covers ``msg_id``, so this filters within the project --
    ``idx_events_project_ts`` leads on ``project``, and past that it is a
    scan. Deliberately not indexed: ``audit_project`` already reads every
    thread and every message of the project, so this adds a term to a
    full-project scan rather than a new order of cost, and an index on a
    write-heavy append-only table earns its keep only if that stops being
    true. An empty candidate list skips the query entirely.
    """
    if not msg_ids:
        return {}, set()

    rows = (
        await session.execute(
            select(ChatroomEvent.msg_id, ChatroomEvent.action, ChatroomEvent.details)
            .where(
                ChatroomEvent.project == project,
                ChatroomEvent.msg_id.in_(msg_ids),
            )
            .order_by(ChatroomEvent.id)
        )
    ).all()

    records: dict[str, SanctionRecord] = {}
    with_transition: set[str] = set()
    for msg_id, action, details in rows:
        if msg_id is None:
            continue
        if action == "status_transition":
            with_transition.add(msg_id)
        if msg_id not in records:
            record = read_sanction_record(details)
            if record is not None:
                records[msg_id] = record
    return records, with_transition


async def audit_project(
    session: AsyncSession,
    *,
    project: str,
    sanction_recording_since: datetime | None = None,
) -> AuditReport:
    """Walk all threads and messages of `project`, returning what it found.

    Never raises; the caller (integrity endpoint) returns 200 with the report.

    ``sanction_recording_since`` is the deployment's cutover instant (see
    ``services.close_sanction``). Left ``None``, no close is reported as
    corruption -- without a cutover, an unrecorded close is indistinguishable
    from one written before the recorder existed.
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

    # Invariant 3 (closes_thread_by_non_owner): closes_thread set + author
    # != owner. Not every such row is breakage: two sanctioned bypasses exist
    # (human Tier-C force-close, PR-gate ledger carve-out) and both are
    # legitimate. What separates them from a corrupt row is the sanction the
    # write path records against this msg_id -- and, where none exists, whether
    # the msg predates the recorder. Four outcomes, only one an issue; the
    # table and its reasoning live in `services.close_sanction`.
    non_owner_closes: list[Message] = []
    for m in msg_rows:
        if not m.closes_thread:
            continue
        if thread is None:
            # already reported as orphan
            continue
        if m.author != thread.owner:
            non_owner_closes.append(m)

    sanction_records, msgs_with_transition = await _fetch_close_evidence(
        session, project=project, msg_ids=[m.msg_id for m in non_owner_closes]
    )
    sanctioned_tally: Counter[str] = Counter()
    unattributable: list[UnattributableClose] = []
    for m in non_owner_closes:
        thread = threads_by_id[m.thread_id]
        classification = classify_non_owner_close(
            record=sanction_records.get(m.msg_id),
            # The message's own timestamp, never its event's. The rows this
            # has to catch are the ones that produced no event, so reading the
            # clock off the event would make the detector depend on the very
            # artifact whose absence it is detecting.
            msg_timestamp=m.timestamp,
            sanction_recording_since=sanction_recording_since,
        )
        if classification.verdict == "sanctioned":
            sanctioned_tally[str(classification.kind)] += 1
        elif classification.verdict == "unattributable":
            unattributable.append(
                UnattributableClose(
                    thread_id=m.thread_id,
                    msg_id=m.msg_id,
                    reason=classification.reason or "pre_recording",
                )
            )
        else:
            issues.append(
                IntegrityIssue(
                    type="closes_thread_by_non_owner",
                    thread_id=m.thread_id,
                    msg_id=m.msg_id,
                    details=(
                        f"Message author='{m.author}' set closes_thread but "
                        f"thread.owner='{thread.owner}', and no close sanction "
                        f"was recorded for it"
                    ),
                    has_status_transition_event=m.msg_id in msgs_with_transition,
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

    # Stale activity key: threads.last_msg_num is the one denormalised value
    # in the schema -- the sort key both triage surfaces rank on -- so it is
    # the one value that can disagree with the messages it summarises. The
    # rest of this audit checks references; this checks a cached derivation.
    # It is nearly free here because every msg is already loaded.
    #
    # A wrong key is quiet and dangerous in the same direction as the defect
    # the listing exists to fix: too low, and a live thread sinks out of
    # sight. So it is checked rather than trusted.
    for thread in threads_by_id.values():
        msgs = msgs_by_thread.get(thread.thread_id, [])
        expected = max(parse_msg_id(m.msg_id) for m in msgs) if msgs else None
        if thread.last_msg_num != expected:
            issues.append(
                IntegrityIssue(
                    type="stale_activity_key",
                    thread_id=thread.thread_id,
                    details=(
                        f"Thread.last_msg_num={thread.last_msg_num} but the "
                        f"newest msg in the thread is "
                        f"{format_msg_id(expected) if expected else '(none)'}"
                    ),
                )
            )

    return AuditReport(
        issues=issues,
        # Named explicitly rather than splatted, so the report's fields and the
        # sanction kinds stay a checked correspondence: a kind that gains no
        # field here is a type error, not a silently dropped count.
        sanctioned_counts=SanctionedCloseCounts(
            pr_gate_ledger=sanctioned_tally["pr_gate_ledger"],
            human_override=sanctioned_tally["human_override"],
        ),
        unattributable=unattributable,
    )


def now_utc() -> datetime:
    """Helper: timezone-aware UTC `datetime` for `IntegrityCheckResponse.checked_at`."""
    return datetime.now(timezone.utc)
