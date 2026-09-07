"""Pure integrity rules.

`assert_closes_thread_rule` and `assert_next_participant_rule` are fully
synchronous and exercised here. The other invariants
(assert_propose_invariant / assert_reply_to_in_thread /
assert_references_threads_exist / fetch_thread_or_raise / audit_project)
all touch the database and are covered by integration tests in T10.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from spirrow_conclair.exceptions import ChatroomIntegrityError, ChatroomStateError
from spirrow_conclair.models import Thread
from spirrow_conclair.services.integrity import (
    assert_closes_thread_rule,
    assert_next_participant_rule,
    assert_thread_writable,
)


def _thread(
    owner: str = "alice",
    thread_id: str = "T-1",
    status: str = "active",
    resolved_by_msg: str | None = None,
) -> Thread:
    return Thread(
        project="p",
        thread_id=thread_id,
        title="t",
        owner=owner,
        status=status,
        created_at=datetime.now(timezone.utc),
        created_by_msg="msg-001",
        resolved_by_msg=resolved_by_msg,
    )


def test_no_closes_thread_is_noop() -> None:
    # all combinations of msg_type / author should pass when closes_thread is None
    assert_closes_thread_rule(
        thread=_thread(),
        msg_type="question",
        closes_thread=None,
        author="bob",
    )


def test_owner_decide_matching_thread_is_ok() -> None:
    assert_closes_thread_rule(
        thread=_thread(owner="alice", thread_id="T-1"),
        msg_type="decide",
        closes_thread="T-1",
        author="alice",
    )


@pytest.mark.parametrize(
    "msg_type", ["propose", "question", "answer", "report", "handoff", "ack"]
)
def test_closes_thread_with_non_decide_type_raises(msg_type: str) -> None:
    with pytest.raises(ChatroomIntegrityError) as ei:
        assert_closes_thread_rule(
            thread=_thread(),
            msg_type=msg_type,
            closes_thread="T-1",
            author="alice",
        )
    assert ei.value.details["msg_type"] == msg_type


def test_closes_thread_value_must_match_url_thread_id() -> None:
    with pytest.raises(ChatroomIntegrityError) as ei:
        assert_closes_thread_rule(
            thread=_thread(thread_id="T-1"),
            msg_type="decide",
            closes_thread="T-OTHER",
            author="alice",
        )
    assert ei.value.details["closes_thread"] == "T-OTHER"
    assert ei.value.details["thread_id"] == "T-1"


def test_closes_thread_by_non_owner_raises() -> None:
    with pytest.raises(ChatroomIntegrityError) as ei:
        assert_closes_thread_rule(
            thread=_thread(owner="alice"),
            msg_type="decide",
            closes_thread="T-1",
            author="bob",
        )
    assert ei.value.details["thread_owner"] == "alice"
    assert ei.value.details["author"] == "bob"


# ADR-2026-06-04-19 D-5: owner_override relaxes ONLY the owner clause.


def test_owner_override_allows_non_owner_decide() -> None:
    # No exception — human force-close.
    assert_closes_thread_rule(
        thread=_thread(owner="alice", thread_id="T-1"),
        msg_type="decide",
        closes_thread="T-1",
        author="human",
        owner_override=True,
    )


def test_owner_override_still_enforces_decide_type() -> None:
    # type='decide' invariant is NOT relaxed by owner_override.
    with pytest.raises(ChatroomIntegrityError) as ei:
        assert_closes_thread_rule(
            thread=_thread(owner="alice", thread_id="T-1"),
            msg_type="report",
            closes_thread="T-1",
            author="human",
            owner_override=True,
        )
    assert ei.value.details["msg_type"] == "report"


def test_owner_override_still_enforces_thread_id_match() -> None:
    # closes_thread == thread_id invariant is NOT relaxed by owner_override.
    with pytest.raises(ChatroomIntegrityError):
        assert_closes_thread_rule(
            thread=_thread(owner="alice", thread_id="T-1"),
            msg_type="decide",
            closes_thread="T-OTHER",
            author="human",
            owner_override=True,
        )


# Invariant 7: a msg that closes its thread names no successor.


def test_close_without_successor_is_ok() -> None:
    # How "nobody is next" is recorded: by closing, and only by closing.
    assert_next_participant_rule(next_participant=None, closes_thread="T-1")


def test_close_with_successor_raises() -> None:
    # The refused pair: the work is over AND somebody still owes a turn.
    with pytest.raises(ChatroomIntegrityError) as ei:
        assert_next_participant_rule(
            next_participant="Heisenberg", closes_thread="T-1"
        )
    assert ei.value.details["next_participant"] == "Heisenberg"
    assert ei.value.details["closes_thread"] == "T-1"


def test_successor_without_close_is_ok() -> None:
    # An ordinary handoff.
    assert_next_participant_rule(next_participant="Heisenberg", closes_thread=None)


def test_neither_is_ok() -> None:
    # Omission is the pre-existing behaviour and stays unvalidated -- this is
    # what keeps every message written before the column existed legal, and
    # why the check could ship in the same revision as the column.
    assert_next_participant_rule(next_participant=None, closes_thread=None)


# No string is reserved. These two pin that, because the obvious reading of
# "nobody is next" -- a sentinel like 'none' -- is exactly what this design
# rejected: tied to a close it could say nothing `closes_thread` did not
# already say, and untied it would be the divergence the invariant forbids.
# (Tier B naysayer, PR #13.)


@pytest.mark.parametrize("name", ["Heisenberg", "human", "none", "orchestrator", ""])
def test_no_value_is_special_on_an_open_thread(name: str) -> None:
    # Including the literal 'none': to Conclair it is a participant name like
    # any other, and whether it is a legal one is Magickit's question -- that
    # needs the Prismind identity record, which Conclair must not read.
    assert_next_participant_rule(next_participant=name, closes_thread=None)


@pytest.mark.parametrize("name", ["Heisenberg", "human", "none", ""])
def test_no_value_is_special_on_a_closing_msg_either(name: str) -> None:
    # 'none' is refused here for the same reason 'Heisenberg' is: the rule is
    # about the field being *set*, not about which string it holds.
    with pytest.raises(ChatroomIntegrityError):
        assert_next_participant_rule(next_participant=name, closes_thread="T-1")


# assert_thread_writable — msg-406 §5.1.
#
# The pair this refuses is a settled thread taking a new message. It runs
# **after** the row-lock refresh in `post_message_in_session`, so a passing
# check reflects the newest committed status; the tests below cover the
# pure predicate.


@pytest.mark.parametrize("status", ["active", "awaiting_reply"])
def test_writable_when_thread_is_open(status: str) -> None:
    # No exception on either open status — both are writable.
    assert_thread_writable(_thread(status=status))


@pytest.mark.parametrize("status", ["superseded", "parked"])
def test_writable_when_thread_is_non_resolved_terminal(status: str) -> None:
    # msg-406 §5.3 non-goal: superseded / parked are deliberately not
    # refused by this assert. `parked` carries an active re-parenting
    # workflow that writes to it; `superseded` has no `resolved_by_msg`
    # and reads differently -- both are open questions the msg-406 disposition
    # explicitly declined to answer. A regression that added them to the set
    # would fire here.
    assert_thread_writable(_thread(status=status))


def test_resolved_thread_is_refused_with_state_error() -> None:
    # The msg-405/msg-406 disposition: resolved is terminal for writes,
    # and the refusal is `ChatroomStateError` -> 409 (the same class the
    # pre-existing re-close path already surfaces).
    with pytest.raises(ChatroomStateError) as ei:
        assert_thread_writable(
            _thread(status="resolved", resolved_by_msg="msg-042")
        )
    # `resolved` in the message so callers can grep -- the `test_re_close`
    # assertion in test_api_close.py reads exactly this substring.
    assert "resolved" in ei.value.message


def test_refusal_details_include_resolved_by_msg_pointer() -> None:
    # The refused caller (typically an agent) needs a machine-readable
    # pointer to the decision. msg-406 §5.1: "断られた client (多くは agent)
    # に『決着はここにある / 続けたいなら新しいスレッド』を機械可読で返す".
    with pytest.raises(ChatroomStateError) as ei:
        assert_thread_writable(
            _thread(status="resolved", resolved_by_msg="msg-042")
        )
    assert ei.value.details == {
        "thread_id": "T-1",
        "status": "resolved",
        "resolved_by_msg": "msg-042",
    }


def test_refusal_details_pass_through_null_resolved_by_msg() -> None:
    # A resolved thread with a NULL `resolved_by_msg` is an
    # `inconsistent_resolved` audit finding (see `audit_project`), but
    # this assert still fires -- refusing a write to a corrupted-terminal
    # thread is at least as correct as accepting it. The details field
    # carries the NULL through untouched so the caller can see what the
    # server actually holds, not a substituted default.
    with pytest.raises(ChatroomStateError) as ei:
        assert_thread_writable(
            _thread(status="resolved", resolved_by_msg=None)
        )
    assert ei.value.details["resolved_by_msg"] is None
