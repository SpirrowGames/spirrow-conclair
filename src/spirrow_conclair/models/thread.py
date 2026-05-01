"""Thread ORM."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, PrimaryKeyConstraint, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from spirrow_conclair.models import Base

THREAD_STATUSES = ("active", "awaiting_reply", "resolved", "superseded", "parked")


class Thread(Base):
    __tablename__ = "threads"

    project: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_by_msg: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_by_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    affects_threads: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")

    __table_args__ = (
        PrimaryKeyConstraint("project", "thread_id", name="threads_pkey"),
        CheckConstraint(
            f"status IN {THREAD_STATUSES}",
            name="threads_status_check",
        ),
        Index("idx_threads_status", "project", "status"),
        Index("idx_threads_owner", "project", "owner"),
    )
