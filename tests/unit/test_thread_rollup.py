"""Pure parts of the per-thread activity rollup.

The aggregate itself needs a database and is covered end-to-end in
`tests/integration/test_api_threads.py`. What is worth pinning without
one is the projection into the wire shape: an all-NULL aggregate row and
the allocator's padding are both easy to get subtly wrong, and both are
read by a human deciding whether a thread is alive.
"""

from __future__ import annotations

from datetime import UTC, datetime

from spirrow_conclair.services.thread_rollup import EMPTY_ROLLUP, ThreadRollup


def test_from_parts_formats_the_msg_id() -> None:
    ts = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    rollup = ThreadRollup.from_parts(7, 3, ts)

    assert rollup.last_msg_id == "msg-007"
    assert rollup.msg_count == 3
    assert rollup.last_activity_at == ts


def test_from_parts_keeps_the_allocator_padding_past_three_digits() -> None:
    """`msg-1027` must not come back as `msg-1027` zero-padded to 3."""
    assert ThreadRollup.from_parts(1027, 1027, None).last_msg_id == "msg-1027"


def test_from_parts_on_a_thread_with_no_msgs_is_empty_not_zero_id() -> None:
    """A thread with no msgs aggregates to all-NULL; that must read as
    "nothing here", not as msg-000."""
    assert ThreadRollup.from_parts(None, None, None) == EMPTY_ROLLUP
