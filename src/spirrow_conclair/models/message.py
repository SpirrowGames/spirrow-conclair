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
        Index("idx_messages_thread", "project", "thread_id"),
        Index("idx_messages_type", "project", "type"),
    )
