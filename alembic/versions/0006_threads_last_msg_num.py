"""threads.last_msg_num: the activity sort key, stored

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-15

Both triage surfaces (``GET /threads``, ``GET /unread``) rank threads by
their newest msg. Deriving that rank on read meant a ``GROUP BY
thread_id`` over ``messages`` had to complete before the page's LIMIT
could apply -- and because the aggregate's only filter is ``project``,
the plan was a parallel sequential scan of the **whole** table, so one
project's listing slowed down as any *other* project grew. Measured at
300k msgs: 85 ms, rising to 133 ms with a second project of equal size
in the table, against 2.6 ms for the pre-rollup listing.

So the sort key -- and only the sort key -- moves onto ``threads``.
``msg_count`` and ``last_activity_at`` stay derived: nothing orders by
them, and they can be aggregated for the <=100 rows a page returns,
where ``idx_messages_thread`` applies.

The backfill computes the same expression the read path used, so the
column starts out equal to what the aggregate would have returned. A
thread with no msgs stays NULL (the listing sorts NULLS LAST).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("threads", sa.Column("last_msg_num", sa.BigInteger(), nullable=True))

    # Same expression as services/thread_rollup.msg_num_expr: msg_id is
    # `msg-NNN` with variable padding, so the numeric part is what orders
    # (lexicographically `msg-9` > `msg-100`).
    op.execute(
        """
        UPDATE threads t
           SET last_msg_num = m.latest_num
          FROM (
                SELECT project,
                       thread_id,
                       max(CAST(SUBSTRING(msg_id FROM 5) AS BIGINT)) AS latest_num
                  FROM messages
                 GROUP BY project, thread_id
               ) m
         WHERE m.project = t.project
           AND m.thread_id = t.thread_id
        """
    )

    # Ordered to match the listing's ORDER BY exactly: a plain ASC index read
    # backwards yields NULLS FIRST, which would not satisfy NULLS LAST.
    op.execute(
        "CREATE INDEX idx_threads_activity "
        "ON threads (project, last_msg_num DESC NULLS LAST)"
    )


def downgrade() -> None:
    op.drop_index("idx_threads_activity", table_name="threads")
    op.drop_column("threads", "last_msg_num")
