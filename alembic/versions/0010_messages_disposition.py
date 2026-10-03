"""messages: add disposition (how the author left the thread)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-03

Magickit has forwarded an optional ``disposition`` on chatroom posts since
spirrow-magickit#98 (``{kind: done} | {kind: blocked_on, trigger: {arm,
ref}, wake}``). Conclair had no field for it, and ``PostMessageRequest``
leaves pydantic's ``extra`` at its default, so the value was dropped without
an error. This adds the column and two CHECKs.

* ``messages_disposition_shape_check`` -- ``kind`` is ``done`` or
  ``blocked_on``. The rest of the shape is the pydantic model's job; this is
  the floor for a writer that bypasses the API.
* ``messages_disposition_close_check`` -- a row that closes its thread may
  carry ``{kind: done}`` or nothing. Same reasoning as invariant 7 (0007): a
  row saying both "closed" and "someone must still be woken" cannot be acted
  on, and ``messages`` is append-only.

**Both predicates are NULL-safe.** A CHECK passes when it evaluates to NULL,
and ``disposition->>'kind'`` is NULL for ``{}`` and for any non-object value,
so a bare ``->>'kind' IN (...)`` would accept exactly the malformed values it
exists to refuse. ``jsonb_typeof(...) = 'object'`` and ``COALESCE(..., '')``
make those evaluate to FALSE. (PR-gate on #32.)

**Both CHECKs ship with the column.** Every existing row is NULL, and NULL
satisfies both predicates, so they are true on arrival -- no legacy cohort to
grandfather, same as 0007.

**Rolling back.** ``downgrade()`` drops both CHECKs and the column, and with
it every disposition written after this revision was applied. The default
rollback is therefore *not* a downgrade: revert the API code and leave this
revision applied (the column stays, unused). Note that a plain code revert to
a commit that predates this file fails to start, because ExecStartPre runs
``alembic upgrade head`` and the old code does not know revision 0010; if
the schema must go, run ``alembic downgrade 0009`` with the new code first,
then revert. Take a ``scripts/backup.sh`` dump before deploying.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

SHAPE_CHECK = "messages_disposition_shape_check"
CLOSE_CHECK = "messages_disposition_close_check"


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("disposition", postgresql.JSONB(), nullable=True),
    )
    # Literal SQL, mirroring the CheckConstraints on models.Message, so a
    # migration never depends on the current shape of the ORM.
    op.create_check_constraint(
        SHAPE_CHECK,
        "messages",
        "disposition IS NULL OR (jsonb_typeof(disposition) = 'object' "
        "AND COALESCE(disposition->>'kind', '') IN ('done', 'blocked_on'))",
    )
    op.create_check_constraint(
        CLOSE_CHECK,
        "messages",
        "closes_thread IS NULL OR disposition IS NULL "
        "OR COALESCE(disposition->>'kind', '') = 'done'",
    )


def downgrade() -> None:
    op.drop_constraint(CLOSE_CHECK, "messages", type_="check")
    op.drop_constraint(SHAPE_CHECK, "messages", type_="check")
    op.drop_column("messages", "disposition")
