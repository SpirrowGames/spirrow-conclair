"""messages: add nullable role column

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-29

ADR-2026-05-27-09 / msg-002 §2: add ``role`` to the messages table as a
nullable Text column so the per-msg role the author was acting under can
be persisted. Pre-existing messages are left with NULL (no backfill --
they predate the ADR and have no declared role).

Conclair persists only; the role × allowed_roles validation runs at the
Magickit orchestration layer against the Prismind identity record. This
is the additive half of the (A) feature; the Magickit-side validation
ships in a separate PR.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("role", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "role")
