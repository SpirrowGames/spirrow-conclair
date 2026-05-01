"""Pure integrity rules.

`assert_closes_thread_rule` is fully synchronous and exercised here.
The other invariants (assert_propose_invariant / assert_reply_to_in_thread /
assert_references_threads_exist / fetch_thread_or_raise / audit_project)
all touch the database and are covered by integration tests in T10.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from spirrow_conclair.exceptions import ChatroomIntegrityError
from spirrow_conclair.models import Thread
from spirrow_conclair.services.integrity import assert_closes_thread_rule


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
