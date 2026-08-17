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

from spirrow_conclair.exceptions import ChatroomIntegrityError
from spirrow_conclair.models import Thread
from spirrow_conclair.services.integrity import (
    assert_closes_thread_rule,
    assert_next_participant_rule,
)


def _thread(owner: str = "alice", thread_id: str = "T-1") -> Thread:
    return Thread(
        project="p",
        thread_id=thread_id,
        title="t",
        owner=owner,
        status="active",
        created_at=datetime.now(timezone.utc),
        created_by_msg="msg-001",
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
