"""Thread ORM."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, PrimaryKeyConstraint, Text, text
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
    # The numeric part of this thread's newest msg_id: the *sort key* for both
    # triage surfaces, and the only derived value kept on the row.
    #
    # It is here because deriving it cost too much. Ordering the listing on a
    # `GROUP BY thread_id` rollup made the page scan and aggregate the whole
    # `messages` table -- every project's, not just this one's -- before the
    # LIMIT could apply: measured 85 ms at 300k msgs and 133 ms once a sibling
    # project of equal size shared the table, against 2.6 ms for the
    # pre-rollup listing (tests/integration/test_thread_listing_scale.py).
    #
    # `msg_count` and `last_activity_at` are deliberately NOT stored: they are
    # display values, nothing sorts on them, and they can be aggregated for
    # the <=100 rows a page actually returns, where `idx_messages_thread`
    # applies. Storing only what the ORDER BY needs keeps the write-path
    # coupling to one assignment.
    #
    # NULL means "no msgs" -- unreachable through open_thread, which writes
    # the propose in the same txn, but the listing sorts NULLS LAST rather
    # than dropping such a row. Kept honest by the `stale_activity_key`
    # integrity check, which recomputes it from `messages`.
    last_msg_num: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("project", "thread_id", name="threads_pkey"),
        CheckConstraint(
            f"status IN {THREAD_STATUSES}",
            name="threads_status_check",
        ),
        Index("idx_threads_status", "project", "status"),
        Index("idx_threads_owner", "project", "owner"),
        # Matches the listing's ORDER BY exactly (a plain ASC index scanned
        # backwards would give NULLS FIRST), so the page is an index scan
        # rather than a sort of every thread in the project.
        Index(
            "idx_threads_activity",
            text("project"),
            text("last_msg_num DESC NULLS LAST"),
        ),
    )
