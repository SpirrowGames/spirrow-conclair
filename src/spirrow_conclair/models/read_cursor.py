"""Per-identity read cursor ORM.

Records "how far has this identity read in this thread" as a single
``last_read_msg_id`` per ``(project, identity_name, thread_id)`` triple.
The row is created on first ``chatroom_mark_read`` for the identity in
the thread; before that, the inbox query treats the entire thread as
unread (handoff-safety default). Monotonic-forward only: a mark_read
request older than the existing cursor is a silent no-op at the route
layer (see ``services.read_cursor.should_advance_cursor``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKeyConstraint, Index, PrimaryKeyConstraint, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from spirrow_conclair.models import Base


class ActorReadCursor(Base):
    __tablename__ = "actor_read_cursors"

    project: Mapped[str] = mapped_column(Text, nullable=False)
    identity_name: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Row only exists after the first mark_read in this (project, identity,
    # thread), so NOT NULL is honest -- "missing cursor" is encoded as
    # "no row" rather than as a NULL column.
    last_read_msg_id: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "project", "identity_name", "thread_id",
            name="actor_read_cursors_pkey",
        ),
        # Match messages_thread_fkey: the cursor cannot outlive the thread.
        # last_read_msg_id intentionally has no FK to messages -- the inbox
        # query reads it as a numeric comparison via msg_id_allocator's
        # parse_msg_id and any stale value would surface via the chatroom
        # integrity audit, the same regime as the rest of the schema.
        ForeignKeyConstraint(
            ["project", "thread_id"],
            ["threads.project", "threads.thread_id"],
            name="actor_read_cursors_thread_fkey",
        ),
        # Supports the inbox query (GET /unread?identity_name=...) so the
        # planner can seek on (project, identity_name) before joining
        # against threads.
        Index("idx_read_cursors_identity", "project", "identity_name"),
    )
