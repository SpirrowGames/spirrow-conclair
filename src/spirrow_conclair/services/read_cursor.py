"""Pure logic for the per-identity read cursor.

The DB-aware upsert + ChatroomEvent emission lives in
``api/read_cursor.py`` next to the route so the txn boundary is
explicit (matches the pattern in ``api/threads.py``). This module
hosts the monotonic-forward rule the cursor advance path needs.

Note: an earlier ``compute_unread_count`` helper was removed here in
favor of doing the count in SQL: ``msg_id`` is allocated project-wide
and shared across threads, so any pure-Python arithmetic on
``parse(latest) - parse(cursor)`` over-counts msgs from sibling
threads. The corrected per-thread count is computed via a correlated
subquery inside ``api/read_cursor.list_unread`` -- the SQL is the
single source of truth for "how many msgs in this thread are after
the cursor".
"""

from __future__ import annotations

from spirrow_conclair.services.msg_id_allocator import parse_msg_id


def should_advance_cursor(
    current: str | None,
    requested: str,
) -> bool:
    """Return True iff ``requested`` is strictly newer than ``current``.

    The route uses this to decide whether to UPSERT + emit a
    ``mark_read`` ChatroomEvent, or to short-circuit and return
    ``advanced=False`` (no DB write, no audit log entry).

    The user picked monotonic-forward only -- a request for the same
    position or older is a deliberate no-op, not an error, so the route
    surfaces this as ``advanced=False`` with the current cursor
    unchanged in the response.

    Note: this is the one case where project-wide ``msg_id`` numeric
    comparison is the correct semantics. The cursor is always advanced
    to a msg known to be in this thread (the route validates the input
    msg_id, or picks the thread's own latest), so comparing two
    project-wide values that we know are both in the same thread
    correctly answers "is the new position strictly newer".
    """
    if current is None:
        return True
    return parse_msg_id(requested) > parse_msg_id(current)
