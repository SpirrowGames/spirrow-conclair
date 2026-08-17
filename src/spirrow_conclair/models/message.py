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

# The one ``next_participant`` value Conclair ascribes meaning to: "nobody is
# next." Every other value is an opaque participant name Conclair does not
# interpret -- naming who may act is the identity registry's job, and that
# lives in Magickit (see the ``next_participant`` column comment below).
NEXT_PARTICIPANT_NONE = "none"


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
    # Conclair interprets exactly one value, ``NEXT_PARTICIPANT_NONE`` -- see
    # ``messages_next_participant_close_check`` below. Participant *names* are
    # stored verbatim and never checked here: deciding whether "Heisenberg" may
    # act requires the Prismind identity record, and Conclair must not pull
    # identity state cross-service (same boundary as ``role`` above).
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
        # "Nobody is next" and "this thread is finished" are the same fact, and
        # the thread already carries the second one (``closes_thread`` ->
        # ``resolved`` / ``resolved_by_msg``). Recording it a second time as a
        # free-standing claim in a message is what let the two drift apart:
        # measured over the loop's history, of the three threads whose latest
        # message said "none", exactly zero had closed with it -- one was closed
        # 15 minutes later by a *second* message, and two are still open (one for
        # 37 days), silently, because a settled thread is the one stop the sweep
        # deliberately does not report.
        #
        # So the claim is only writable together with the act. A recorded
        # ``none`` therefore always means "this thread was closed" -- the same
        # shape as ``role``, where a recorded value always means "this was
        # verified".
        #
        # ``IS DISTINCT FROM`` (not ``<>``) because NULL must pass: an omitted
        # ``next_participant`` is unconstrained, which is what keeps every
        # pre-existing row legal and needs no backfill.
        #
        # Duplicated deliberately in ``services.integrity`` as a pre-write
        # assert. That one produces the 409 a caller can act on; this one makes
        # the state unrepresentable if a future write path forgets to ask.
        CheckConstraint(
            f"next_participant IS DISTINCT FROM '{NEXT_PARTICIPANT_NONE}' "
            "OR closes_thread IS NOT NULL",
            name="messages_next_participant_close_check",
        ),
        Index("idx_messages_thread", "project", "thread_id"),
        Index("idx_messages_type", "project", "type"),
    )
