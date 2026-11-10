"""active_summon: a project's Summon mark on project_control + its history

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-10

A Summon is an operator's mark that one thread of a project must be worked
first (spirrow-magickit ``T-human-call-summon-server-side`` msg-1435 v1.2 §1,
msg-1437 v1.2.1, msg-1439 v1.2.2; this repository's thread
``T-conclair-active-summon``).

What this adds:

1. Three columns on ``project_control``: ``active_summon_thread_id`` /
   ``_since`` / ``_by``. CHECK ``project_control_active_summon_check``
   allows exactly two shapes: all three NULL, or all three non-NULL with a
   non-blank thread_id and by. A half-written mark cannot be stored.

2. ``project_summon_history`` -- an append-only log of set / clear. It is a
   table of its own rather than more rows in ``project_control_history``:
   that table's ``state`` column is CHECKed to the desired-state vocabulary
   and it records a different thing (who asked for HOLD / RESUME).

3. ``clear_active_summon(project, thread_id, by)`` and two triggers on
   ``threads`` that call it, so a Summon cannot outlive its thread:

   - ``trg_threads_summon_clear_on_status``: AFTER UPDATE OF status, when the
     status actually changed and the new one is not an open status (NULL
     included). ``by = 'system:status:' || coalesce(NEW.status, '<null>')``.
   - ``trg_threads_summon_clear_on_delete``: AFTER DELETE, a separate
     function that reads only OLD. ``by = 'system:thread_deleted'``.

   The function locks the control row by project alone (``FOR UPDATE``) and
   compares thread_id inside PL/pgSQL, so the lock is taken whether or not
   the thread holds the mark (msg-1439). Consequence, recorded as a known
   property (msg-1440): every thread close / delete takes the project's
   control-row lock, so in one project those operations and HOLD / RESUME
   are serialized with each other.

``SUMMON_OPEN_STATUSES`` below is written into the SQL function and must
equal ``models.project_control.SUMMON_OPEN_STATUSES``; an integration test
reads the deployed function body and compares.

No backfill: no project has a Summon before this revision.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


# Frozen copy for this revision. Do not import the model's constant: a
# migration must keep producing the schema it produced when it was written.
SUMMON_OPEN_STATUSES = ("active", "awaiting_reply")

_OPEN_SQL = ", ".join(f"'{s}'" for s in SUMMON_OPEN_STATUSES)


def upgrade() -> None:
    op.add_column(
        "project_control",
        sa.Column("active_summon_thread_id", sa.Text, nullable=True),
    )
    op.add_column(
        "project_control",
        sa.Column("active_summon_since", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "project_control",
        sa.Column("active_summon_by", sa.Text, nullable=True),
    )
    op.create_check_constraint(
        "project_control_active_summon_check",
        "project_control",
        "(active_summon_thread_id IS NULL AND active_summon_since IS NULL"
        " AND active_summon_by IS NULL)"
        " OR (active_summon_thread_id IS NOT NULL AND active_summon_since IS NOT NULL"
        " AND active_summon_by IS NOT NULL"
        " AND btrim(active_summon_thread_id) <> '' AND btrim(active_summon_by) <> '')",
    )

    op.create_table(
        "project_summon_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("thread_id", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("changed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.CheckConstraint(
            "action IN ('set', 'clear')",
            name="project_summon_history_action_check",
        ),
    )
    op.create_index(
        "idx_summon_history_project",
        "project_summon_history",
        ["project", "changed_at"],
    )

    op.execute(
        """
        CREATE FUNCTION clear_active_summon(p_project text, p_thread_id text, p_by text)
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_current text;
        BEGIN
            -- Lock by project alone, then compare in PL/pgSQL (msg-1439):
            -- the lock is taken whether or not this thread holds the mark.
            SELECT active_summon_thread_id INTO v_current
              FROM project_control
             WHERE project = p_project
               FOR UPDATE;
            IF NOT FOUND OR v_current IS NULL OR v_current <> p_thread_id THEN
                RETURN;
            END IF;
            UPDATE project_control
               SET active_summon_thread_id = NULL,
                   active_summon_since = NULL,
                   active_summon_by = NULL
             WHERE project = p_project;
            INSERT INTO project_summon_history
                   (project, action, thread_id, actor, changed_at, note)
            VALUES (p_project, 'clear', p_thread_id, p_by, now(), NULL);
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION threads_summon_clear_on_status()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- NULL is "not open" too: `NULL IN (...)` is NULL, so test it
            -- explicitly rather than relying on NOT IN.
            IF NEW.status IS NULL OR NEW.status NOT IN ({_OPEN_SQL}) THEN
                PERFORM clear_active_summon(
                    NEW.project,
                    NEW.thread_id,
                    'system:status:' || coalesce(NEW.status, '<null>')
                );
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_threads_summon_clear_on_status
        AFTER UPDATE OF status ON threads
        FOR EACH ROW
        WHEN (OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION threads_summon_clear_on_status()
        """
    )
    op.execute(
        """
        CREATE FUNCTION threads_summon_clear_on_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM clear_active_summon(
                OLD.project, OLD.thread_id, 'system:thread_deleted'
            );
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_threads_summon_clear_on_delete
        AFTER DELETE ON threads
        FOR EACH ROW
        EXECUTE FUNCTION threads_summon_clear_on_delete()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_threads_summon_clear_on_delete ON threads")
    op.execute("DROP FUNCTION IF EXISTS threads_summon_clear_on_delete()")
    op.execute("DROP TRIGGER IF EXISTS trg_threads_summon_clear_on_status ON threads")
    op.execute("DROP FUNCTION IF EXISTS threads_summon_clear_on_status()")
    op.execute("DROP FUNCTION IF EXISTS clear_active_summon(text, text, text)")
    op.drop_index("idx_summon_history_project", table_name="project_summon_history")
    op.drop_table("project_summon_history")
    op.drop_constraint(
        "project_control_active_summon_check", "project_control", type_="check"
    )
    op.drop_column("project_control", "active_summon_by")
    op.drop_column("project_control", "active_summon_since")
    op.drop_column("project_control", "active_summon_thread_id")
