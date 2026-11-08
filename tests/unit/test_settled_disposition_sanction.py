"""``settled_disposition``: a non-owner close that carries out a proposer's ``done``.

chatroom ``spirrow-mindwire/T-done-disposition-auto-close`` msg-8218 §1
(boundary 1a), implementing msg-8185 §A / §B:

* the kind is in the vocabulary and maps to sanctioned;
* the wire shape: ``head_msg_id`` required; ``reason``, ``review_msg_id`` and
  the three ledger fields refused; every other kind refuses ``head_msg_id``;
* the write-time check against Conclair's own rows, under the row lock:
  (a) the msg is in the closed thread, (b) it is still the head -- the
  compare-and-set, (c) its disposition is ``done``, (d) it names no
  successor, (e) its persisted role is ``proposer``. Each is broken alone
  below, and all five together pass.

The DB half is pinned through the pure decision function and a recording fake
session (the gate has no database, see ``.mindwire-gate``);
``tests/integration/test_api_close_sanction.py`` runs the same cases end to end
against Postgres in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from spirrow_conclair.exceptions import ChatroomUnprocessableError
from spirrow_conclair.schemas.close_sanction_vocab import KIND_IS_SANCTIONED
from spirrow_conclair.schemas.event import SanctionedCloseCounts
from spirrow_conclair.schemas.message import CloseSanction
from spirrow_conclair.services.close_sanction import (
    NAYSAYER_ROLE,
    SETTLED_DISPOSITION_ROLE,
    SanctionRecord,
    classify_non_owner_close,
)
from spirrow_conclair.services.integrity import (
    assert_close_sanction_evidence,
    check_settled_disposition_row,
)

LEDGER = {
    "pr": "SpirrowGames/x#1",
    "merged_head": "deadbee",
    "approving_review_id": "PRR_1",
}

# ----- vocabulary -----


def test_settled_disposition_is_sanctioned() -> None:
    assert KIND_IS_SANCTIONED["settled_disposition"] is True


def test_a_recorded_settled_disposition_close_is_counted_not_reported() -> None:
    out = classify_non_owner_close(
        record=SanctionRecord(kind="settled_disposition"),
        msg_timestamp=datetime(2026, 11, 9, tzinfo=UTC),
        sanction_recording_since=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert out.verdict == "sanctioned"
    assert out.kind == "settled_disposition"


def test_the_audit_report_has_a_count_for_it() -> None:
    assert SanctionedCloseCounts().settled_disposition == 0


def test_the_eligible_role_is_proposer_only() -> None:
    """msg-8185 Obj-5-1: one role, so client and server agree on the set.
    Not the naysayer -- ADR-10 gives it no close authority."""
    assert SETTLED_DISPOSITION_ROLE == "proposer"
    assert SETTLED_DISPOSITION_ROLE != NAYSAYER_ROLE


# ----- wire shape -----


def test_settled_disposition_with_its_evidence_is_accepted() -> None:
    s = CloseSanction(kind="settled_disposition", head_msg_id="msg-8218")
    assert s.head_msg_id == "msg-8218"


@pytest.mark.parametrize("head_msg_id", [None, "", "   "])
def test_settled_disposition_requires_head_msg_id(head_msg_id: str | None) -> None:
    with pytest.raises(ValidationError, match="requires 'head_msg_id'"):
        CloseSanction(kind="settled_disposition", head_msg_id=head_msg_id)


def test_settled_disposition_refuses_a_reason() -> None:
    with pytest.raises(ValidationError, match=r"\['reason'\]"):
        CloseSanction(kind="settled_disposition", head_msg_id="msg-1", reason="done")


@pytest.mark.parametrize("field", sorted(LEDGER))
def test_settled_disposition_refuses_each_ledger_field(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        CloseSanction(
            kind="settled_disposition", head_msg_id="msg-1", **{field: LEDGER[field]}
        )


def test_settled_disposition_refuses_review_msg_id() -> None:
    with pytest.raises(ValidationError, match="does not carry 'review_msg_id'"):
        CloseSanction(
            kind="settled_disposition", head_msg_id="msg-1", review_msg_id="msg-2"
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "human_override", "reason": "Tier-C"},
        {"kind": "pr_gate_ledger", **LEDGER},
        {"kind": "naysayer_approved", "review_msg_id": "msg-2"},
        {"kind": "unspecified"},
    ],
    ids=["human_override", "pr_gate_ledger", "naysayer_approved", "unspecified"],
)
def test_other_kinds_refuse_head_msg_id(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="does not carry 'head_msg_id'"):
        CloseSanction(**payload, head_msg_id="msg-1")


# ----- the write-time check, pure half: (a)-(e) each broken alone -----

GOOD: dict[str, Any] = {
    "head_msg_id": "msg-8218",
    "thread_id": "T-1",
    "last_msg_num": 8218,
    "found": True,
    "role": "proposer",
    "next_participant": None,
    "disposition": {"kind": "done"},
}


def test_all_five_conditions_together_pass() -> None:
    check_settled_disposition_row(**GOOD)


def _refused(reason: str, **override: Any) -> ChatroomUnprocessableError:
    with pytest.raises(ChatroomUnprocessableError) as ei:
        check_settled_disposition_row(**{**GOOD, **override})
    assert ei.value.details["reason"] == reason
    assert ei.value.details["head_msg_id"] == GOOD["head_msg_id"]
    return ei.value


def test_a_not_in_thread() -> None:
    _refused("not_in_thread", found=False, role=None, disposition=None)


@pytest.mark.parametrize("last_msg_num", [8219, 8217, None])
def test_b_head_moved_is_the_compare_and_set(last_msg_num: int | None) -> None:
    """A msg after the settlement (8219) moves the head; so does any
    disagreement at all, including a thread with no activity key."""
    err = _refused("head_moved", last_msg_num=last_msg_num)
    assert err.details["last_msg_num"] == last_msg_num


@pytest.mark.parametrize(
    "disposition",
    [
        None,
        {"kind": "blocked_on", "trigger": {"arm": "pr", "ref": "a/b#1"}, "wake": "Bohr"},
        {},
        "done",
    ],
    ids=["null", "blocked_on", "empty", "not-an-object"],
)
def test_c_not_done(disposition: Any) -> None:
    _refused("not_done", disposition=disposition)


def test_d_names_a_successor() -> None:
    err = _refused("names_successor", next_participant="Heisenberg")
    assert err.details["next_participant"] == "Heisenberg"


@pytest.mark.parametrize(
    "role", [None, "naysayer", "implementer", "human", "integrator", "Proposer", ""]
)
def test_e_role_not_eligible(role: str | None) -> None:
    err = _refused("role_not_eligible", role=role)
    assert err.details["role"] == role


# ----- the write-time check, query half -----


class _Result:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def one_or_none(self) -> tuple[Any, ...] | None:
        return self._row


class _RecordingSession:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.row = row
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result(self.row)


async def _check(
    session: _RecordingSession, *, last_msg_num: int | None, head: str = "msg-2"
) -> None:
    await assert_close_sanction_evidence(
        session,  # type: ignore[arg-type]
        project="p",
        thread=SimpleNamespace(thread_id="T-1", last_msg_num=last_msg_num),  # type: ignore[arg-type]
        close_sanction=CloseSanction(kind="settled_disposition", head_msg_id=head),
    )


async def test_the_lookup_is_scoped_to_the_closed_thread() -> None:
    session = _RecordingSession(row=("proposer", None, {"kind": "done"}))
    await _check(session, last_msg_num=2)

    (stmt,) = session.statements
    where = {clause.left.name: clause.right.value for clause in stmt.whereclause.clauses}
    assert where == {"project": "p", "thread_id": "T-1", "msg_id": "msg-2"}


async def test_the_head_is_compared_with_the_threads_locked_key() -> None:
    """The comparison reads ``thread.last_msg_num`` off the object it is
    handed -- the row ``post_message_in_session`` locked -- not a fresh query."""
    session = _RecordingSession(row=("proposer", None, {"kind": "done"}))
    with pytest.raises(ChatroomUnprocessableError) as ei:
        await _check(session, last_msg_num=3)
    assert ei.value.details["reason"] == "head_moved"


async def test_no_row_is_a_422() -> None:
    with pytest.raises(ChatroomUnprocessableError) as ei:
        await _check(_RecordingSession(row=None), last_msg_num=2)
    assert ei.value.details["reason"] == "not_in_thread"


async def test_a_row_with_another_role_is_a_422() -> None:
    session = _RecordingSession(row=("naysayer", None, {"kind": "done"}))
    with pytest.raises(ChatroomUnprocessableError) as ei:
        await _check(session, last_msg_num=2)
    assert ei.value.details["reason"] == "role_not_eligible"
