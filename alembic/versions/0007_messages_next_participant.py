"""messages: add next_participant, and forbid a successor on a closing msg

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-17

Who acts next is currently prose at the end of ``content`` (``NEXT: <name>``).
This adds the field half -- and, in the same revision, the one rule Conclair
can enforce about it without reaching outside the row: a msg that closes its
thread names no successor.

**What it forbids.** A row that says both "the work is over" and "somebody
still owes a turn". Nothing downstream can act on that pair, and ``messages``
is append-only, so it could not be repaired after the fact. "Nobody is next"
and "this thread is finished" are the same fact, and the thread already keeps
it (``closes_thread`` -> ``threads.status='resolved'`` / ``resolved_by_msg``);
the message layer states it once, by closing, and never again.

**What is deliberately absent.** No sentinel value meaning "no successor". An
earlier draft of this revision reserved the string ``'none'`` for that and
required it to accompany a close -- but once tied to the close it could say
nothing the adjacent ``closes_thread`` did not already say, making it a second
encoding of a single fact. That is the failure this constraint exists to
prevent, so it was dropped: there is exactly one way to record that a thread
has no successor, and it is to close the thread. (Tier B naysayer, PR #13.)

**Why the constraint ships with the column rather than after it.** ``0004``
(role) could not constrain its column, because pre-existing rows had no value
to satisfy a rule with and backfilling one would have fabricated an
attestation. Here every existing row gets ``NULL``, and a NULL
``next_participant`` satisfies the check whatever ``closes_thread`` holds, so
it is vacuously true on arrival -- including for the rows that already close
their threads. There is no window in which the column exists unconstrained,
and no legacy cohort to grandfather in later.

Participant names are not validated here or anywhere in Conclair: deciding
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
        "closes_thread IS NULL OR next_participant IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint(CHECK_NAME, "messages", type_="check")
    op.drop_column("messages", "next_participant")
