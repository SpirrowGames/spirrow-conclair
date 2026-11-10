"""agent_sessions: which Claude Code session an agent is running (T45 PR1)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-10

One row per Claude Code session UUID (``cc_session_uuid``, the PK). There
are two writers, one per session (D1): the loop adapter (``source='loop'``)
and the interactive-session hooks (``source='hook'``). Both reach Conclair
through Magickit's ``session_declare`` (PR 2). This revision adds the table
only; the write-once rules (D2' / D7) live in ``services/agent_session.py``.

* ``agent_sessions_source_check`` -- ``source`` is ``loop`` or ``hook``.
* ``agent_sessions_loop_session_id_check`` --
  ``source <> 'loop' OR mindwire_session_id IS NOT NULL``. A loop row has to
  carry the id that the service compares every later write against. A loop
  row stored with NULL would let a write that omits the id "match" (NULL
  against NULL). (msg-1322)

``embodiment`` has no CHECK. Validating its vocabulary is Magickit's job and
Conclair only stores it, the same split as ``messages.embodiment`` (0002).

``project`` has no FK, matching ``project_control`` (0005): there is no
projects table to point at.

``instance_id`` / ``embodiment`` / ``host`` are nullable (msg-1320 §1 ※;
kept after the ADR-09/12 read-back, msg-1550).

**Rolling back.** ``downgrade()`` drops the table, and every declared
session with it. Nothing else references it.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

SOURCES = ("loop", "hook")
SOURCE_CHECK = "agent_sessions_source_check"
LOOP_SESSION_ID_CHECK = "agent_sessions_loop_session_id_check"


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("cc_session_uuid", sa.Text, nullable=False),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("identity", sa.Text, nullable=True),
        sa.Column("instance_id", sa.Text, nullable=True),
        sa.Column("embodiment", sa.Text, nullable=True),
        sa.Column("host", sa.Text, nullable=True),
        sa.Column("transcript_path", sa.Text, nullable=True),
        sa.Column("mindwire_session_id", sa.Text, nullable=True),
        sa.Column("thread_id", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_declared_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("cc_session_uuid", name="agent_sessions_pkey"),
        # Literal SQL that mirrors models.AgentSession, so the migration never
        # depends on the current shape of the ORM.
        sa.CheckConstraint(f"source IN {SOURCES}", name=SOURCE_CHECK),
        sa.CheckConstraint(
            "source <> 'loop' OR mindwire_session_id IS NOT NULL",
            name=LOOP_SESSION_ID_CHECK,
        ),
    )
    op.create_index("idx_agent_sessions_identity", "agent_sessions", ["identity"])
    op.create_index("idx_agent_sessions_project", "agent_sessions", ["project"])


def downgrade() -> None:
    op.drop_index("idx_agent_sessions_project", table_name="agent_sessions")
    op.drop_index("idx_agent_sessions_identity", table_name="agent_sessions")
    op.drop_table("agent_sessions")
