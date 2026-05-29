"""messages: add nullable embodiment column

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-29

ADR-2026-05-29-12 step (i): add ``embodiment`` to the messages table as
a nullable Text column so the per-msg self-declared runtime form of the
authoring agent can be persisted. Pre-existing messages are left with
NULL (no backfill required -- they predate the ADR and have no declared
embodiment to migrate). Magickit enforces the mandatory-on-
{handoff,ack,decide} validation at the orchestration layer; Conclair
only persists.

Step (ii) (5-API callsite migration) and step (iii) (column removal on
the Identity record on the Prismind side) are handled by their own PRs.
This migration is the additive half of the ADR-12 schema change; it
does not modify or remove any existing column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("embodiment", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "embodiment")
