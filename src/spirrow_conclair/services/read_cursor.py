"""Pure logic for the per-identity read cursor.

The DB-aware upsert + ChatroomEvent emission lives in
``api/read_cursor.py`` next to the route so the txn boundary is
explicit (matches the pattern in ``api/threads.py``). This module is
just the two arithmetic decisions: how many msgs are unread, and is
this mark_read actually advancing forward.

The monotonic-forward rule (the user picked "rewind = silent no-op")
is encoded in ``should_advance_cursor``: it returns False whenever the
requested cursor is at or behind the current one. The route then short-
circuits without writing.
"""

from __future__ import annotations

from spirrow_conclair.services.msg_id_allocator import parse_msg_id


def compute_unread_count(
    latest_msg_id: str | None,
    last_read_msg_id: str | None,
) -> int:
    """Return how many msgs after the cursor exist in the thread.

    Semantics:
    - ``latest_msg_id is None`` (empty thread) -> 0 (nothing to read).
    - ``last_read_msg_id is None`` (cursor row absent for this identity)
      -> the whole thread is unread; returns ``parse(latest)`` which is
      the count of msgs from msg-001 onward.
    - Otherwise: ``parse(latest) - parse(cursor)``, clamped at 0 to
      cover the no-op-rewind case where a stale cursor value somehow
      survived after a thread shrink (not actually supported, but cheap
      to guard).
    """
    if not latest_msg_id:
        return 0
    latest = parse_msg_id(latest_msg_id)
    if not last_read_msg_id:
        return latest
    diff = latest - parse_msg_id(last_read_msg_id)
    return diff if diff > 0 else 0


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
    """
    if current is None:
        return True
    return parse_msg_id(requested) > parse_msg_id(current)
