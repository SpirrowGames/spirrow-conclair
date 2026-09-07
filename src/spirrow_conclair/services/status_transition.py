"""Pure thread-status transition rules.

Per System Design v2 §8 / api-design.md §3.2:

| msg.type                      | thread.status (before)   | thread.status (after) |
|-------------------------------|--------------------------|-----------------------|
| handoff                       | active                   | awaiting_reply        |
| ack                           | awaiting_reply           | active                |
| decide + closes_thread match  | active or awaiting_reply | resolved              |
| anything else                 | (no change)              | (no change)           |

Decide on a closed (`resolved` / `superseded` / `parked`) thread is a state
error and surfaces as `ChatroomStateError`.
"""

from __future__ import annotations

from typing import Any

from spirrow_conclair.exceptions import ChatroomStateError
from spirrow_conclair.models import Message, Thread

_OPEN_STATUSES = ("active", "awaiting_reply")


def compute_transition(
    thread: Thread, new_msg: Message
) -> tuple[str | None, dict[str, Any]]:
    """Decide whether `new_msg` should change `thread.status`.

    Returns:
        (new_status, extra_fields)
        - new_status: target status string, or None when no transition
        - extra_fields: additional thread columns to update alongside status
          (currently only resolved_by_msg)
    Raises:
        ChatroomStateError: decide+closes_thread on a non-open thread.
    """
    if new_msg.type == "handoff" and thread.status == "active":
        return ("awaiting_reply", {})

    if new_msg.type == "ack" and thread.status == "awaiting_reply":
        return ("active", {})

    if new_msg.type == "decide" and new_msg.closes_thread == thread.thread_id:
        if thread.status in _OPEN_STATUSES:
            return ("resolved", {"resolved_by_msg": new_msg.msg_id})
        # This branch is live, not a leftover guard. R2's
        # ``assert_thread_writable`` (services/integrity.py) does not
        # cover it: ``_WRITE_TERMINAL_STATUS`` names exactly one status,
        # ``resolved``, so writes into ``parked`` / ``superseded`` threads
        # pass that assert. Pinned by
        # ``tests/unit/test_integrity.py::test_writable_when_thread_is_non_resolved_terminal``
        # and, at the HTTP surface, by
        # ``tests/integration/test_api_messages.py::test_post_to_parked_thread_is_still_accepted``
        # / ``::test_post_to_superseded_thread_is_still_accepted``.
        #
        # So the pair that arrives here is: the thread owner (or a human
        # Tier-C force-close) posts ``decide`` + ``closes_thread`` against
        # a row already in ``parked`` / ``superseded``. Every earlier
        # assert passes -- ``assert_closes_thread_rule`` checks type,
        # target thread and owner, never status.
        #
        # Whether such rows exist is a *data* question, not a code one:
        # no Conclair endpoint puts a thread into ``parked`` or
        # ``superseded`` today, which is why the integration tests above
        # seed the status with raw SQL. What is measured here is only
        # that the code path is open -- not that production traffic walks
        # it, and not that it cannot.
        #
        # Deleting the raise would not restore a status error elsewhere:
        # ``compute_transition`` would fall through to ``(None, {})``, the
        # closing msg would be recorded, the thread would stay ``parked``,
        # ``resolved_by_msg`` would never be set, and nothing would report
        # it. ``/integrity``'s ``inconsistent_resolved`` only compares
        # ``status == 'resolved'`` against ``resolved_by_msg`` (neither
        # half trips on that row) and its Invariant-3 walk
        # (``closes_thread_by_non_owner``) only compares msg author against
        # thread owner (here they match). A silent half-close is the exact
        # failure class this arc exists to close.
        #
        # ``tests/unit/test_status_transition.py``'s
        # ``test_decide_closes_closed_thread_raises_state_error`` pins the
        # branch for all three closed statuses. Bohr msg-409 §3
        # reached the same keep-it conclusion; the reasoning above is
        # msg-466 §2, which corrected the reachability claim this comment
        # used to make.
        raise ChatroomStateError(
            f"Cannot close thread '{thread.thread_id}' in status='{thread.status}'",
            details={
                "thread_id": thread.thread_id,
                "current_status": thread.status,
            },
        )

    return (None, {})
