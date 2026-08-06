"""project_control: per-project loop autonomy state (HOLD / RESUME)

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-04

Adds ``project_control`` (one row per configured-or-observed project)
and the append-only ``project_control_history``.

No backfill. The absence of a row means "nobody has set this project's
state", which the API reports as ``configured: false`` with the default
``run`` -- inserting rows for existing projects would erase that
distinction without adding information.

``desired_state`` is nullable so the loop can report ``observed`` for a
project that has never been configured; see the module docstring in
``models/project_control.py``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


CONTROL_STATES = ("run", "supervised", "hold")


def upgrade() -> None:
    op.create_table(
        "project_control",
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("desired_state", sa.Text, nullable=True),
        sa.Column("desired_actor", sa.Text, nullable=True),
        sa.Column("desired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("observed_state", sa.Text, nullable=True),
        sa.Column("observed_actor", sa.Text, nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("project", name="project_control_pkey"),
        sa.CheckConstraint(
            f"desired_state IS NULL OR desired_state IN {CONTROL_STATES}",
            name="project_control_desired_check",
        ),
        sa.CheckConstraint(
            f"observed_state IS NULL OR observed_state IN {CONTROL_STATES}",
            name="project_control_observed_check",
        ),
    )

    op.create_table(
        "project_control_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("changed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.CheckConstraint(
            f"state IN {CONTROL_STATES}",
            name="project_control_history_state_check",
        ),
    )
    op.create_index(
        "idx_control_history_project",
        "project_control_history",
        ["project", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_control_history_project",
        table_name="project_control_history",
    )
    op.drop_table("project_control_history")
    op.drop_table("project_control")
