"""actor_read_cursors: per-identity read cursor

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-29

Adds the ``actor_read_cursors`` table that records "how far has this
identity read in this thread" as ``last_read_msg_id`` per
``(project, identity_name, thread_id)``. The row is created on the
first ``chatroom_mark_read`` for the identity in the thread; before
that, the inbox query treats every msg in the thread as unread (this
is the safer default that the user picked to avoid handoff drops).

FK to ``threads(project, thread_id)`` matches the pattern in
``messages``. No FK on ``last_read_msg_id`` -- the inbox query just
does a numeric comparison via ``msg_id_allocator`` and any stale
value would surface via the existing chatroom integrity audit.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "actor_read_cursors",
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("identity_name", sa.Text, nullable=False),
        sa.Column("thread_id", sa.Text, nullable=False),
        sa.Column("last_read_msg_id", sa.Text, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "project", "identity_name", "thread_id",
            name="actor_read_cursors_pkey",
        ),
        sa.ForeignKeyConstraint(
            ["project", "thread_id"],
            ["threads.project", "threads.thread_id"],
            name="actor_read_cursors_thread_fkey",
        ),
    )
    op.create_index(
        "idx_read_cursors_identity",
        "actor_read_cursors",
        ["project", "identity_name"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_read_cursors_identity",
        table_name="actor_read_cursors",
    )
    op.drop_table("actor_read_cursors")
