"""Status transition matrix.

Pure-function tests; no DB. Covers every (msg.type, thread.status) pair
plus the closes_thread variants of `decide`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from spirrow_conclair.exceptions import ChatroomStateError
from spirrow_conclair.models import Message, Thread
from spirrow_conclair.models.message import MESSAGE_TYPES
from spirrow_conclair.models.thread import THREAD_STATUSES
from spirrow_conclair.services.status_transition import compute_transition

# Mapping from (msg_type, thread_status, closes_thread_matches) to expected
# (new_status, raises_state_error). closes_thread_matches=False means the
# msg either has no closes_thread or it points at a different thread_id.
_OPEN = ("active", "awaiting_reply")
_CLOSED = ("resolved", "superseded", "parked")


def _mk_thread(status: str, thread_id: str = "T-1", owner: str = "alice") -> Thread:
    return Thread(
        project="p",
        thread_id=thread_id,
        title="t",
        owner=owner,
        status=status,
        created_at=datetime.now(timezone.utc),
        created_by_msg="msg-001",
    )


def _mk_msg(
    msg_type: str,
    *,
    thread_id: str = "T-1",
    msg_id: str = "msg-002",
    closes_thread: str | None = None,
) -> Message:
    return Message(
        project="p",
        msg_id=msg_id,
        thread_id=thread_id,
        author="alice",
        timestamp=datetime.now(timezone.utc),
        type=msg_type,
        content="x",
        closes_thread=closes_thread,
    )


# --- handoff -----------------------------------------------------------


def test_handoff_active_to_awaiting_reply() -> None:
    new_status, extra = compute_transition(_mk_thread("active"), _mk_msg("handoff"))
    assert new_status == "awaiting_reply"
    assert extra == {}


@pytest.mark.parametrize("status", ["awaiting_reply", "resolved", "superseded", "parked"])
def test_handoff_other_statuses_no_transition(status: str) -> None:
    new_status, extra = compute_transition(_mk_thread(status), _mk_msg("handoff"))
    assert new_status is None
    assert extra == {}


# --- ack ---------------------------------------------------------------


def test_ack_awaiting_reply_to_active() -> None:
    new_status, extra = compute_transition(
        _mk_thread("awaiting_reply"), _mk_msg("ack")
    )
    assert new_status == "active"
    assert extra == {}


@pytest.mark.parametrize("status", ["active", "resolved", "superseded", "parked"])
def test_ack_other_statuses_no_transition(status: str) -> None:
    new_status, _ = compute_transition(_mk_thread(status), _mk_msg("ack"))
    assert new_status is None


# --- decide + closes_thread on the SAME thread -------------------------


@pytest.mark.parametrize("status", _OPEN)
def test_decide_closes_open_thread_to_resolved(status: str) -> None:
    thread = _mk_thread(status, thread_id="T-1")
    msg = _mk_msg("decide", closes_thread="T-1", msg_id="msg-005")
    new_status, extra = compute_transition(thread, msg)
    assert new_status == "resolved"
    assert extra == {"resolved_by_msg": "msg-005"}


@pytest.mark.parametrize("status", _CLOSED)
def test_decide_closes_closed_thread_raises_state_error(status: str) -> None:
    thread = _mk_thread(status, thread_id="T-1")
    msg = _mk_msg("decide", closes_thread="T-1")
    with pytest.raises(ChatroomStateError) as ei:
        compute_transition(thread, msg)
    assert ei.value.details["current_status"] == status


# --- decide WITHOUT closes_thread (or pointing elsewhere) --------------


@pytest.mark.parametrize("status", THREAD_STATUSES)
def test_decide_without_closes_thread_no_transition(status: str) -> None:
    thread = _mk_thread(status)
    msg = _mk_msg("decide", closes_thread=None)
    new_status, extra = compute_transition(thread, msg)
    assert new_status is None
    assert extra == {}


@pytest.mark.parametrize("status", THREAD_STATUSES)
def test_decide_closing_other_thread_no_transition(status: str) -> None:
    thread = _mk_thread(status, thread_id="T-1")
    msg = _mk_msg("decide", closes_thread="T-OTHER")
    new_status, extra = compute_transition(thread, msg)
    assert new_status is None
    assert extra == {}


# --- inert types: propose / question / answer / report -----------------


@pytest.mark.parametrize("msg_type", ["propose", "question", "answer", "report"])
@pytest.mark.parametrize("status", THREAD_STATUSES)
def test_inert_types_no_transition(msg_type: str, status: str) -> None:
    new_status, extra = compute_transition(_mk_thread(status), _mk_msg(msg_type))
    assert new_status is None
    assert extra == {}


# --- exhaustive coverage check ------------------------------------------


def test_message_types_constant_matches_design() -> None:
    """Pin the type vocabulary so future schema additions force test review."""
    assert set(MESSAGE_TYPES) == {
        "propose", "question", "answer", "decide", "report", "handoff", "ack",
    }


def test_thread_statuses_constant_matches_design() -> None:
    assert set(THREAD_STATUSES) == {
        "active", "awaiting_reply", "resolved", "superseded", "parked",
    }
