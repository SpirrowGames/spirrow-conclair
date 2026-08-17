"""messages: add next_participant, and tie 'none' to closing the thread

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-17

Who acts next is currently prose at the end of ``content`` (``NEXT: <name>``).
This adds the field half of that -- and, in the same revision, the one rule
Conclair can enforce about it without reaching outside the row.

**Why the constraint ships with the column rather than after it.** ``0004``
(role) could not constrain its column, because pre-existing rows had no value
to satisfy a rule with and backfilling one would have fabricated an attestation.
Here every existing row gets ``NULL``, ``NULL`` passes (``IS DISTINCT FROM``),
and the check is therefore vacuously true on arrival. There is no window in
which the column exists unconstrained, so there is no cohort of legacy rows to
grandfather in later.

**The rule.** ``next_participant = 'none'`` asserts "nobody is next", which is
the same fact as "this thread is finished" -- and the thread already records
that, via ``closes_thread`` -> ``threads.status='resolved'`` /
``resolved_by_msg``. Two records of one fact drifted apart in practice: across
the loop's history the three threads whose latest message said ``none``
included zero that closed with it. One was closed 15 minutes later by a
*separate* message; the other two are still open (one for 37 days) and silent,
because "settled" is the single stop the sweep intentionally does not report.

Tying them makes the divergent state unrepresentable rather than merely
detectable -- which matters because ``messages`` is append-only, so a row
written wrong cannot be repaired afterwards.

Participant *names* are not validated here or anywhere in Conclair: deciding
whether a name may act needs the Prismind identity record, and Conclair must
not pull identity state cross-service. That check belongs to Magickit. This
revision constrains only what one row can answer about itself.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

CHECK_NAME = "messages_next_participant_close_check"


def upgrade() -> None:
    op.add_column("messages", sa.Column("next_participant", sa.Text(), nullable=True))
    # Mirrors the CheckConstraint on models.Message. Kept as literal SQL rather
    # than importing the model so a migration never depends on the current shape
    # of the ORM.
    op.create_check_constraint(
        CHECK_NAME,
        "messages",
        "next_participant IS DISTINCT FROM 'none' OR closes_thread IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(CHECK_NAME, "messages", type_="check")
    op.drop_column("messages", "next_participant")
