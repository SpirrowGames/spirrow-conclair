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
        raise ChatroomStateError(
            f"Cannot close thread '{thread.thread_id}' in status='{thread.status}'",
            details={
                "thread_id": thread.thread_id,
                "current_status": thread.status,
            },
        )

    return (None, {})
