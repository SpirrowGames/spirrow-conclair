"""Unit tests for the read cursor service (pure functions).

``compute_unread_count`` was removed in the bug-fix that moved
per-thread counting into SQL -- see services/read_cursor.py docstring
for the rationale. The integration suite
(``tests/integration/test_api_read_cursor.py``) now pins the count
semantics directly against the live SQL.
"""

from __future__ import annotations

import pytest

from spirrow_conclair.exceptions import ChatroomIntegrityError
from spirrow_conclair.services.read_cursor import should_advance_cursor


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
