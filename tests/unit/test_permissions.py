"""Permission checks (pure)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from spirrow_conclair.exceptions import ChatroomPermissionError
from spirrow_conclair.models import Thread
from spirrow_conclair.services.permissions import assert_owner_can_close


def _thread(owner: str) -> Thread:
    return Thread(
        project="p",
        thread_id="T-1",
        title="t",
        owner=owner,
        status="active",
        created_at=datetime.now(timezone.utc),
        created_by_msg="msg-001",
    )


def test_owner_can_close() -> None:
    assert_owner_can_close(_thread("alice"), "alice")  # no exception


def test_non_owner_raises() -> None:
    with pytest.raises(ChatroomPermissionError) as ei:
        assert_owner_can_close(_thread("alice"), "bob")
    assert ei.value.details["thread_owner"] == "alice"
    assert ei.value.details["author"] == "bob"


def test_empty_author_treated_as_non_owner() -> None:
    with pytest.raises(ChatroomPermissionError):
        assert_owner_can_close(_thread("alice"), "")


def test_owner_match_is_case_sensitive() -> None:
    with pytest.raises(ChatroomPermissionError):
        assert_owner_can_close(_thread("alice"), "Alice")


# ADR-2026-06-04-19 D-5: owner_override skips the ownership clause.


def test_owner_override_allows_non_owner() -> None:
    # No exception: a force-close (Magickit set this only for a human).
    assert_owner_can_close(_thread("alice"), "human", owner_override=True)


def test_owner_override_false_still_enforces() -> None:
    with pytest.raises(ChatroomPermissionError):
        assert_owner_can_close(_thread("alice"), "human", owner_override=False)


def test_owner_override_noop_for_actual_owner() -> None:
    # Harmless when the author *is* the owner (Magickit sets it for all humans).
    assert_owner_can_close(_thread("alice"), "alice", owner_override=True)
