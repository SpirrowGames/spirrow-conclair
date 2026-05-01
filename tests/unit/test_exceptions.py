"""ChatroomError construction / details propagation."""

from __future__ import annotations

from spirrow_conclair.exceptions import (
    ChatroomDBError,
    ChatroomError,
    ChatroomIntegrityError,
    ChatroomNotFoundError,
    ChatroomPermissionError,
    ChatroomStateError,
)


def test_message_only() -> None:
    e = ChatroomError("oops")
    assert e.message == "oops"
    assert e.details == {}
    assert str(e) == "oops"


def test_details_dict_preserved() -> None:
    e = ChatroomNotFoundError("missing", details={"thread_id": "T-1"})
    assert e.details == {"thread_id": "T-1"}


def test_subclass_hierarchy() -> None:
    for cls in (
        ChatroomNotFoundError,
        ChatroomIntegrityError,
        ChatroomPermissionError,
        ChatroomStateError,
        ChatroomDBError,
    ):
        assert issubclass(cls, ChatroomError)


def test_details_default_empty_dict_is_independent() -> None:
    """Two error instances with default details must not share state."""
    a = ChatroomError("a")
    b = ChatroomError("b")
    a.details["x"] = 1
    assert b.details == {}
