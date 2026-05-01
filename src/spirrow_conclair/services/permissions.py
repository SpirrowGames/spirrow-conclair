"""Permission checks (honor-system, no auth in v1)."""

from __future__ import annotations

from spirrow_conclair.exceptions import ChatroomPermissionError
from spirrow_conclair.models import Thread


def assert_owner_can_close(thread: Thread, author: str) -> None:
    """Only the thread owner can post a `decide+closes_thread` (or call close_thread)."""
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
