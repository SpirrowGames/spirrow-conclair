"""§6 one-shot measurement -- msg-406.

Counts messages posted *after* their thread's own ``resolved_by_msg`` across
every project, grouped by ``(type, author)``. The number decides whether
``assert_thread_writable`` (msg-406 §5.1) can be turned on directly, or
whether an active writer needs to migrate first:

* 0 rows, or human-authored rows only  -> Bohr §6 case A. Turning the assert
  on stops nothing that is running today.
* any rows authored by a loop mechanism (conductor / gate / sweep / ...) ->
  case B. Migrate that producer first; a hard refuse would stop the loop.

Numeric comparison, not lexicographic. ``msg_id`` is ``msg-NNN`` with
dynamic padding (see ``services/msg_id_allocator``), so a plain ``>``
between two strings falls apart the moment a project's counter crosses a
zero-pad boundary -- ``'msg-99999' > 'msg-100000'`` is ``True`` in
lexicographic order. Comparing the ``BIGINT`` casts of the numeric tails
is the same shape ``allocate_next_msg_id`` and ``msg_num_expr`` already
use; if this script disagreed with those, the disagreement is the reason
Einstein flagged it (msg-405 advisory).

``thread_id`` is included in the join even though msg_ids are project-wide,
because comparing "after this thread's decide" without also constraining
``m.thread_id = t.thread_id`` would compare a message against a sibling
thread's ``resolved_by_msg`` -- the same class of trap ``fetch_rollups``
avoids by scoping to the caller-chosen thread ids.

Run once, paste the result into the msg-406 branch's PR body, and delete
neither the script nor its output. Bohr §6 was explicit that this is a
one-shot: institutionalising it as a recurring check would report a
structural zero forever and train reviewers to ignore an integrity number.

Usage
-----
::

    DATABASE_URL='postgresql+asyncpg://.../conclair' \\
        uv run python scripts/measure_post_after_resolved.py

The script only reads: no schema change, no INSERT, no UPDATE. Safe on a
live production DB.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# The one query. Left as a raw SQL string on purpose: the shape a reviewer
# has to check -- BIGINT compare, thread-scoped join, only rows past
# ``resolved_by_msg`` -- reads more directly here than through an ORM
# translation, and this file is not imported by the app.
_QUERY = text(
    """
    WITH resolved AS (
        SELECT
            t.project,
            t.thread_id,
            t.resolved_by_msg,
            CAST(SUBSTRING(t.resolved_by_msg FROM 5) AS BIGINT) AS resolved_num
        FROM threads AS t
        WHERE t.status = 'resolved'
          AND t.resolved_by_msg IS NOT NULL
    )
    SELECT
        m.type AS msg_type,
        m.author AS author,
        COUNT(*) AS row_count
    FROM messages AS m
    JOIN resolved AS r
      ON r.project = m.project
     AND r.thread_id = m.thread_id
    WHERE CAST(SUBSTRING(m.msg_id FROM 5) AS BIGINT) > r.resolved_num
    GROUP BY m.type, m.author
    ORDER BY row_count DESC, m.type, m.author
    """
)


async def _run(url: str) -> Sequence[tuple[str, str, int]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(_QUERY)
            return [(row[0], row[1], int(row[2])) for row in result.all()]
    finally:
        await engine.dispose()


def _print(rows: Sequence[tuple[str, str, int]]) -> None:
    total = sum(count for _, _, count in rows)
    print(f"Total msgs posted after resolved_by_msg: {total}")
    if not rows:
        print("(no rows -- Bohr §6 case A: safe to enable assert_thread_writable)")
        return
    print()
    print(f"{'type':<12} {'author':<40} {'count':>8}")
    print(f"{'-' * 12} {'-' * 40} {'-' * 8}")
    for msg_type, author, count in rows:
        print(f"{msg_type:<12} {author:<40} {count:>8}")


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    rows = asyncio.run(_run(url))
    _print(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
