"""messages: expression index for the inbox's per-thread unread count

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02

``GET /unread`` counts, per thread, the msgs whose number is past the
reader's cursor. ``idx_messages_thread`` (project, thread_id) reaches a
thread's rows, but the number is an expression over ``msg_id``
(``services.thread_rollup.msg_num_expr``), so every row had to be fetched
from the heap to be filtered. Measured in #30 (CI ``perf``, 300k msgs /
5k threads): ~315k buffers per evaluation of the count.

With the expression as the index's third key, the count's whole
predicate is answered by the index -- an index-only scan that touches only
the rows past the cursor.

**``INCLUDE (msg_id)`` is load-bearing.** Postgres treats a query as
index-only-capable only when every *column* it reads is stored in the
index; an expression over ``msg_id`` does not count as storing
``msg_id``. Without the INCLUDE, #31's first stage-3 run (CI ``perf``,
run 36959317317) planned a plain ``Index Scan`` on this index -- the
heap fetch per msg stayed.

**The expression must stay identical to ``msg_num_expr``.** Postgres
matches an expression index only against the same expression, which is
also why ``msg_num_expr`` renders its ``5`` inline rather than as a bound
parameter.

**Kept on evidence, not on principle.** The decision that added this
(T-unread-correlated-count-scale msg-5619 §3) also set its removal rule:
if it does not improve the 100x / no-cursors median by at least 30% over
the LATERAL rewrite alone, it comes out. #31's body records the numbers.

Not ``CONCURRENTLY``: alembic runs in a transaction, and the largest live
project is ~2.5k msgs, where the build is instantaneous.
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX idx_messages_thread_num "
        "ON messages (project, thread_id, (CAST(SUBSTRING(msg_id FROM 5) AS BIGINT))) "
        "INCLUDE (msg_id)"
    )


def downgrade() -> None:
    op.drop_index("idx_messages_thread_num", table_name="messages")
