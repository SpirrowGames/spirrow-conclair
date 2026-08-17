"""Message ORM."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, PrimaryKeyConstraint, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from spirrow_conclair.models import Base

MESSAGE_TYPES = (
    "propose",
    "question",
    "answer",
    "decide",
    "report",
    "handoff",
    "ack",
)


class Message(Base):
    __tablename__ = "messages"

    project: Mapped[str] = mapped_column(Text, nullable=False)
    msg_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    commit_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    references_threads: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    related_tasks: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    closes_thread: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    # ADR-2026-05-29-12: self-declared runtime form of the authoring agent.
    # Nullable for backward compatibility with pre-ADR-12 messages; on
    # state-transitioning types ({handoff, ack, decide}) Magickit enforces
    # mandatory declaration with a human-identity exemption.
    embodiment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ADR-2026-05-27-09 / msg-002 §2: per-msg role the author was acting under.
    # Conclair persists only; role × allowed_roles validation is enforced at
    # the Magickit orchestration layer against the Prismind identity record
    # (Conclair must not pull identity state cross-service).
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who acts next, as a field rather than as prose in ``content``. Nullable:
    # omitting it is the pre-existing behaviour and stays unvalidated, so every
    # message written before this column existed remains legal.
    #
    # Conclair ascribes meaning to **no value here** -- it stores the string it
    # was given. Deciding whether "Heisenberg" may act requires the Prismind
    # identity record, and Conclair must not pull identity state cross-service
    # (same boundary as ``role`` above). The one thing it does enforce is
    # structural and needs nothing outside the row: see
    # ``messages_next_participant_close_check``.
    next_participant: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("project", "msg_id", name="messages_pkey"),
        ForeignKeyConstraint(
            ["project", "thread_id"],
            ["threads.project", "threads.thread_id"],
            name="messages_thread_fkey",
        ),
        CheckConstraint(
            f"type IN {MESSAGE_TYPES}",
            name="messages_type_check",
        ),
        # A msg that closes its thread names no successor. "Nobody is next" and
        # "this thread is finished" are the same fact, and the thread is its
        # keeper (``closes_thread`` -> ``resolved`` / ``resolved_by_msg``), so
        # the message layer states it once -- by closing -- and never again.
        #
        # What this forbids is a *settled thread with a pending successor*: a
        # row that simultaneously says the work is over and that somebody still
        # owes a turn. Nothing downstream can act on that, and ``messages`` is
        # append-only, so it could not be repaired afterwards.
        #
        # Note what is deliberately NOT here: a sentinel meaning "no successor".
        # An earlier draft reserved the string 'none' for that and required it
        # to accompany a close -- but once tied, it could say nothing the
        # adjacent ``closes_thread`` did not already say, so it was a second
        # encoding of one fact, which is the very thing this constraint exists
        # to prevent. There is now exactly one way to record that a thread has
        # no successor: close it. (Tier B naysayer on PR #13.)
        #
        # Duplicated deliberately in ``services.integrity`` as a pre-write
        # assert. That one produces the 409 a caller can act on; this one makes
        # the state unrepresentable if a future write path forgets to ask.
        CheckConstraint(
            "closes_thread IS NULL OR next_participant IS NULL",
            name="messages_next_participant_close_check",
        ),
        Index("idx_messages_thread", "project", "thread_id"),
        Index("idx_messages_type", "project", "type"),
    )
