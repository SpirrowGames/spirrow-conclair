"""Permission checks (honor-system, no auth in v1)."""

from __future__ import annotations

from spirrow_conclair.exceptions import ChatroomPermissionError
from spirrow_conclair.models import Thread


def assert_owner_can_close(
    thread: Thread, author: str, *, owner_override: bool = False
) -> None:
    """Only the thread owner can post a `decide+closes_thread` (or call close_thread).

    ADR-2026-06-04-19 D-5: ``owner_override=True`` skips the ownership check so
    a Tier-C human can force-close a non-owned thread. Conclair does not decide
    *who* may set it — Magickit sets it only for human identities. Ownership is
    the only invariant relaxed; the naysayer gate (Magickit) is independent.
    """
    if owner_override:
        return
    if author != thread.owner:
        raise ChatroomPermissionError(
            f"Only the thread owner can close. "
            f"thread.owner='{thread.owner}', author='{author}'",
            details={
                "thread_id": thread.thread_id,
                "thread_owner": thread.owner,
                "author": author,
            },
        )
