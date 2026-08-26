"""thread_digests: LLM-generated summaries produced elsewhere, stored here

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-27

Conclair stores digests; it does not make them. The producer (Magickit,
via Cognilens -> Lexora) PUTs a finished digest and this table keeps it,
the same shape as ``project_control.observed_*``: an outside actor
reports, Conclair records, and the UI is honest about how old the record
is. Conclair must stay a leaf -- it calls no other Spirrow service -- so
"who made this and how" is recorded (``producer`` / ``model`` / ``tier``)
rather than known.

**The freshness key is ``source_last_msg_id``.** ``messages`` is
append-only and its rows are immutable, so "this digest covers everything
up to msg-N" is a permanent fact rather than a cache timestamp that might
already be wrong. A digest is stale iff that differs from the thread's
current last msg, and the API derives that verdict by counting the
messages after it -- never by subtracting ``source_msg_count``, which is
what the *producer said it read* and is provenance only.

**Surrogate PK + two partial unique indexes**, not a natural composite
PK. ``target_msg_id`` must be NULL for a whole-thread digest -- that is
the honest encoding of "this covers the thread, not one message" -- and
Postgres forbids NULL in a primary key. The alternative, ``NOT NULL
DEFAULT ''``, puts a magic value in a key; this schema avoids that
elsewhere for the same reason (``read_cursor``: "missing cursor" is no
row, not a NULL column). The cost is that ``ON CONFLICT`` must name the
index predicate.

**Both scopes ship here.** Per-message digests need no second migration,
only a different ``scope``.

**``style`` is part of both uniqueness keys.** One thread can hold
several digests written by different prompts, so a producer trying a new
style cannot silently destroy the one the UI renders.

**No FK on ``source_last_msg_id`` / ``target_msg_id``.** Same regime as
``actor_read_cursors.last_read_msg_id``: validated by a pre-write assert,
compared numerically on read.

**No backfill, and no ``requested_at`` / pending column.** Absence of a
row means "not generated yet", which the API reports as
``present: false``. An honest "generating now" would need a lease, not a
flag -- a producer that dies leaves a flag saying 生成中 forever, and
Conclair runs no timers and (by the leaf constraint) cannot ask the
producer whether the job is alive. Adding one later is a nullable
``add_column``, so nothing here forecloses it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


DIGEST_SCOPES = ("thread", "message")


def upgrade() -> None:
    op.create_table(
        "thread_digests",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("thread_id", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("target_msg_id", sa.Text, nullable=True),
        sa.Column("style", sa.Text, nullable=False, server_default="default"),
        sa.Column("digest", sa.Text, nullable=False),
        sa.Column("source_last_msg_id", sa.Text, nullable=False),
        sa.Column("source_msg_count", sa.Integer, nullable=False),
        sa.Column(
            "truncated", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("tier", sa.Text, nullable=True),
        sa.Column("producer", sa.Text, nullable=False),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source_chars", sa.Integer, nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.ForeignKeyConstraint(
            ["project", "thread_id"],
            ["threads.project", "threads.thread_id"],
            name="thread_digests_thread_fkey",
        ),
        sa.CheckConstraint(
            f"scope IN {DIGEST_SCOPES}",
            name="thread_digests_scope_check",
        ),
        sa.CheckConstraint(
            "(scope = 'thread' AND target_msg_id IS NULL) "
            "OR (scope = 'message' AND target_msg_id IS NOT NULL)",
            name="thread_digests_target_check",
        ),
        sa.CheckConstraint(
            "length(btrim(digest)) > 0",
            name="thread_digests_digest_nonblank",
        ),
        sa.CheckConstraint(
            "source_msg_count >= 1",
            name="thread_digests_source_count_check",
        ),
    )

    # The predicates are load-bearing, not an optimisation: without them
    # the two indexes collide (a whole-thread digest and a per-message one
    # would fight over the same key), and `ON CONFLICT ... index_where`
    # would find no matching index and fail at runtime with an error that
    # points nowhere near here. `test_migration_digests` asserts the
    # predicates, not just the index names.
    op.create_index(
        "uq_thread_digests_thread",
        "thread_digests",
        ["project", "thread_id", "style"],
        unique=True,
        postgresql_where=sa.text("scope = 'thread'"),
    )
    op.create_index(
        "uq_thread_digests_message",
        "thread_digests",
        ["project", "thread_id", "target_msg_id", "style"],
        unique=True,
        postgresql_where=sa.text("scope = 'message'"),
    )


def downgrade() -> None:
    op.drop_index("uq_thread_digests_message", table_name="thread_digests")
    op.drop_index("uq_thread_digests_thread", table_name="thread_digests")
    op.drop_table("thread_digests")
