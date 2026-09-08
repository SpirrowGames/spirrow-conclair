"""ChatroomError construction / details propagation."""

from __future__ import annotations

from spirrow_conclair.exceptions import (
    ChatroomDBError,
    ChatroomError,
    ChatroomIntegrityError,
    ChatroomNotFoundError,
    ChatroomPermissionError,
    ChatroomStateError,
    ChatroomThreadResolvedError,
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
        ChatroomThreadResolvedError,
        ChatroomDBError,
    ):
        assert issubclass(cls, ChatroomError)


def test_thread_resolved_is_a_state_error() -> None:
    """The subclass relationship is what keeps the change blast-free.

    Two things ride on it and neither is visible at the raise site: the 409
    mapping (Starlette finds the parent's handler by walking the MRO) and
    every pre-existing ``except ChatroomStateError``, which must keep
    catching this refusal.
    """
    assert issubclass(ChatroomThreadResolvedError, ChatroomStateError)

    try:
        raise ChatroomThreadResolvedError("resolved")
    except ChatroomStateError as exc:
        assert type(exc) is ChatroomThreadResolvedError
    else:  # pragma: no cover - the except above always fires
        raise AssertionError("an existing `except ChatroomStateError` stopped catching it")


def test_details_default_empty_dict_is_independent() -> None:
    """Two error instances with default details must not share state."""
    a = ChatroomError("a")
    b = ChatroomError("b")
    a.details["x"] = 1
    assert b.details == {}
