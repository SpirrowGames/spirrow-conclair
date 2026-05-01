"""initial: threads, messages, chatroom_events

Revision ID: 0001
Revises:
Create Date: 2026-05-01

Faithfully implements the schema in chatroom-archive-tool: System Design v2 §5.
Hand-written rather than autogenerate so JSONB defaults and CHECK constraints
match the design exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# Alembic identifiers.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


THREAD_STATUSES = ("active", "awaiting_reply", "resolved", "superseded", "parked")
MESSAGE_TYPES = (
    "propose", "question", "answer", "decide", "report", "handoff", "ack",
)


def upgrade() -> None:
    op.create_table(
        "threads",
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("thread_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_by_msg", sa.Text, nullable=False),
        sa.Column("resolved_by_msg", sa.Text, nullable=True),
        sa.Column("affects_threads", JSONB, nullable=False, server_default="[]"),
        sa.Column("tags", JSONB, nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("project", "thread_id", name="threads_pkey"),
        sa.CheckConstraint(
            f"status IN {THREAD_STATUSES}",
            name="threads_status_check",
        ),
    )
    op.create_index("idx_threads_status", "threads", ["project", "status"])
    op.create_index("idx_threads_owner", "threads", ["project", "owner"])

    op.create_table(
        "messages",
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("msg_id", sa.Text, nullable=False),
        sa.Column("thread_id", sa.Text, nullable=False),
        sa.Column("author", sa.Text, nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("commit_ref", sa.Text, nullable=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("reply_to", sa.Text, nullable=True),
        sa.Column("references_threads", JSONB, nullable=False, server_default="[]"),
        sa.Column("related_tasks", JSONB, nullable=False, server_default="[]"),
        sa.Column("closes_thread", sa.Text, nullable=True),
        sa.Column("tags", JSONB, nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("project", "msg_id", name="messages_pkey"),
        sa.ForeignKeyConstraint(
            ["project", "thread_id"],
            ["threads.project", "threads.thread_id"],
            name="messages_thread_fkey",
        ),
        sa.CheckConstraint(
            f"type IN {MESSAGE_TYPES}",
            name="messages_type_check",
        ),
    )
    op.create_index("idx_messages_thread", "messages", ["project", "thread_id"])
    op.create_index("idx_messages_type", "messages", ["project", "type"])

    op.create_table(
        "chatroom_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("thread_id", sa.Text, nullable=True),
        sa.Column("msg_id", sa.Text, nullable=True),
        sa.Column("details", JSONB, nullable=False, server_default="{}"),
    )
    op.create_index("idx_events_project_ts", "chatroom_events", ["project", "timestamp"])
    op.create_index("idx_events_thread", "chatroom_events", ["project", "thread_id"])


def downgrade() -> None:
    op.drop_index("idx_events_thread", table_name="chatroom_events")
    op.drop_index("idx_events_project_ts", table_name="chatroom_events")
    op.drop_table("chatroom_events")

    op.drop_index("idx_messages_type", table_name="messages")
    op.drop_index("idx_messages_thread", table_name="messages")
    op.drop_table("messages")

    op.drop_index("idx_threads_owner", table_name="threads")
    op.drop_index("idx_threads_status", table_name="threads")
    op.drop_table("threads")
