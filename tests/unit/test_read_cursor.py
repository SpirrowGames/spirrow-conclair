"""Unit tests for the read cursor service (pure functions)."""

from __future__ import annotations

import pytest

from spirrow_conclair.exceptions import ChatroomIntegrityError
from spirrow_conclair.services.read_cursor import (
    compute_unread_count,
    should_advance_cursor,
)


class TestComputeUnreadCount:
    def test_empty_thread_returns_zero(self):
        assert compute_unread_count(None, None) == 0
        assert compute_unread_count("", None) == 0
        assert compute_unread_count("", "msg-005") == 0

    def test_no_cursor_returns_full_thread_size(self):
        # The cursor row not existing yet is the "never read" case --
        # treat the whole thread as unread (handoff-safety default).
        assert compute_unread_count("msg-007", None) == 7
        assert compute_unread_count("msg-001", None) == 1

    def test_cursor_equals_latest_returns_zero(self):
        assert compute_unread_count("msg-042", "msg-042") == 0

    def test_cursor_behind_latest_returns_diff(self):
        assert compute_unread_count("msg-100", "msg-042") == 58
        assert compute_unread_count("msg-003", "msg-001") == 2

    def test_cursor_ahead_clamps_at_zero(self):
        # A stale cursor pointing past the current latest shouldn't
        # surface as a negative count. Not actually reachable in normal
        # operation (we never shrink threads), but the guard is cheap.
        assert compute_unread_count("msg-005", "msg-010") == 0

    def test_zero_padded_width_independence(self):
        # The padding width can differ between cursor and latest once the
        # allocator crosses 1000 (msg-009 vs msg-1000). The numeric
        # comparison must still work.
        assert compute_unread_count("msg-1005", "msg-009") == 996

    def test_malformed_cursor_raises(self):
        with pytest.raises(ChatroomIntegrityError):
            compute_unread_count("msg-005", "garbage")


class TestShouldAdvanceCursor:
    def test_none_current_always_advances(self):
        # First mark_read on a never-read thread always advances.
        assert should_advance_cursor(None, "msg-001") is True
        assert should_advance_cursor(None, "msg-100") is True

    def test_requested_strictly_newer_advances(self):
        assert should_advance_cursor("msg-005", "msg-006") is True
        assert should_advance_cursor("msg-001", "msg-1000") is True

    def test_same_position_does_not_advance(self):
        assert should_advance_cursor("msg-042", "msg-042") is False

    def test_rewind_does_not_advance(self):
        # The user picked monotonic-forward only -- a request pointing
        # behind the current cursor is a silent no-op, not an error.
        assert should_advance_cursor("msg-042", "msg-010") is False
        assert should_advance_cursor("msg-1000", "msg-009") is False

    def test_malformed_requested_raises(self):
        with pytest.raises(ChatroomIntegrityError):
            should_advance_cursor("msg-005", "garbage")
