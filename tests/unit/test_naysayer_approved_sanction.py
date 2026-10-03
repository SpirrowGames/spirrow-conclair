"""``naysayer_approved``: a close justified by a fresh naysayer APPROVE.

T-close-sanction-unspecified-kind-cannot-be-decomposed, conclair PR-1:

* D-1 -- the kind is in the vocabulary and maps to sanctioned.
* D-2a -- the wire shape (``review_msg_id`` required; ``reason`` and the three
  ledger fields refused) and the write-time check against Conclair's own
  rows: the msg must be in the closed thread and carry persisted
  ``role == "naysayer"``, else 422. The verdict is not checked.
* D-3 -- every kind has an explicit evidence branch; the final ``else`` raises.
* D-7(3) -- a non-naysayer msg of the same thread, or a msg of another
  thread, is a 422.

The DB half of D-2a / D-7(3) is pinned here through its pure decision
function and a recording fake session (the gate has no database, see
``.mindwire-gate``); ``tests/integration/test_api_close_sanction.py`` runs the
same cases end to end against Postgres in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from spirrow_conclair.api.error_handlers import register_error_handlers
from spirrow_conclair.exceptions import (
    ChatroomIntegrityError,
    ChatroomUnprocessableError,
)
from spirrow_conclair.schemas.close_sanction_vocab import (
    KIND_IS_SANCTIONED,
    CloseSanctionKind,
)
from spirrow_conclair.schemas.message import CloseSanction
from spirrow_conclair.services.close_sanction import (
    NAYSAYER_ROLE,
    SanctionRecord,
    classify_non_owner_close,
)
from spirrow_conclair.services.integrity import (
    assert_close_sanction_evidence,
    check_naysayer_review_row,
)

LEDGER = {
    "pr": "SpirrowGames/x#1",
    "merged_head": "deadbee",
    "approving_review_id": "PRR_1",
}

# ----- D-1 -----


def test_naysayer_approved_is_sanctioned() -> None:
    assert KIND_IS_SANCTIONED["naysayer_approved"] is True


def test_a_recorded_naysayer_approved_close_is_counted_not_reported() -> None:
    out = classify_non_owner_close(
        record=SanctionRecord(kind="naysayer_approved"),
        msg_timestamp=datetime(2026, 10, 3, tzinfo=UTC),
        sanction_recording_since=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert out.verdict == "sanctioned"
    assert out.kind == "naysayer_approved"


# ----- D-2a: wire shape -----


def test_naysayer_approved_with_its_evidence_is_accepted() -> None:
    s = CloseSanction(kind="naysayer_approved", review_msg_id="msg-1084")
    assert s.review_msg_id == "msg-1084"


@pytest.mark.parametrize("review_msg_id", [None, "", "   "])
def test_naysayer_approved_requires_review_msg_id(review_msg_id: str | None) -> None:
    with pytest.raises(ValidationError, match="requires 'review_msg_id'"):
        CloseSanction(kind="naysayer_approved", review_msg_id=review_msg_id)


def test_naysayer_approved_refuses_a_reason() -> None:
    """The prose travels in the sibling ``owner_override_reason``."""
    with pytest.raises(ValidationError, match=r"\['reason'\]"):
        CloseSanction(
            kind="naysayer_approved", review_msg_id="msg-1", reason="looks good"
        )


@pytest.mark.parametrize("field", sorted(LEDGER))
def test_naysayer_approved_refuses_each_ledger_field(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        CloseSanction(
            kind="naysayer_approved", review_msg_id="msg-1", **{field: LEDGER[field]}
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "human_override", "reason": "Tier-C"},
        {"kind": "pr_gate_ledger", **LEDGER},
        {"kind": "unspecified"},
    ],
    ids=["human_override", "pr_gate_ledger", "unspecified"],
)
def test_other_kinds_refuse_review_msg_id(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="does not carry 'review_msg_id'"):
        CloseSanction(**payload, review_msg_id="msg-1")


# ----- D-3: the validator has no implicit branch -----


def _constructed(kind: str) -> CloseSanction:
    """A model that skipped validation, so the validator can be run by hand
    on any ``kind`` -- including one the ``Literal`` would refuse first."""
    return CloseSanction.model_construct(
        kind=kind,  # type: ignore[arg-type]
        reason=None,
        pr=None,
        merged_head=None,
        approving_review_id=None,
        review_msg_id=None,
    )


@pytest.mark.parametrize("kind", list(get_args(CloseSanctionKind)))
def test_every_kind_has_an_explicit_evidence_branch(kind: str) -> None:
    """Iterates the vocabulary, so a kind added without a branch turns red.

    With no evidence supplied, a kind may legitimately be refused with a
    ``ValueError`` (``human_override`` needs a reason). What it must never do
    is reach the final ``else``: that is the ``AssertionError``, and it is
    what the old implicit ``else: # unspecified`` silently absorbed.
    """
    try:
        _constructed(kind)._evidence_matches_kind()
    except ValueError:
        pass


def test_the_final_else_raises_rather_than_absorbing_a_kind() -> None:
    with pytest.raises(AssertionError, match="no evidence branch"):
        _constructed("not_a_real_kind")._evidence_matches_kind()


# ----- D-2a / D-7(3): the write-time check, pure half -----


def test_a_naysayer_msg_in_the_thread_passes() -> None:
    check_naysayer_review_row(
        review_msg_id="msg-1084", thread_id="T-1", found=True, role=NAYSAYER_ROLE
    )


def test_a_msg_not_in_the_thread_is_refused() -> None:
    """D-7(3), other thread: the thread-scoped lookup finds nothing."""
    with pytest.raises(ChatroomUnprocessableError, match="does not exist in thread"):
        check_naysayer_review_row(
            review_msg_id="msg-9", thread_id="T-1", found=False, role=None
        )


@pytest.mark.parametrize("role", [None, "implementer", "proposer", "Naysayer", ""])
def test_a_non_naysayer_msg_in_the_thread_is_refused(role: str | None) -> None:
    """D-7(3), same thread: e.g. the propose msg (msg-515), or a role-less post."""
    with pytest.raises(ChatroomUnprocessableError, match="persisted role"):
        check_naysayer_review_row(
            review_msg_id="msg-515", thread_id="T-1", found=True, role=role
        )


def test_the_refusal_is_not_a_409() -> None:
    """A 409 says the thread's state forbids the write; this is bad evidence."""
    assert not issubclass(ChatroomUnprocessableError, ChatroomIntegrityError)


# ----- D-2a / D-7(3): the write-time check, query half -----


class _Result:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def one_or_none(self) -> tuple[Any, ...] | None:
        return self._row


class _RecordingSession:
    """Records the statement and answers with a fixed row."""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.row = row
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result(self.row)


async def _check(session: _RecordingSession, sanction: CloseSanction | None) -> None:
    await assert_close_sanction_evidence(
        session,  # type: ignore[arg-type]
        project="p",
        thread_id="T-1",
        close_sanction=sanction,
    )


async def test_the_lookup_is_scoped_to_the_closed_thread() -> None:
    """msg ids are project-wide, so without ``thread_id`` a sibling thread's
    naysayer msg would pass (D-7(3), other thread)."""
    session = _RecordingSession(row=(NAYSAYER_ROLE,))
    await _check(session, CloseSanction(kind="naysayer_approved", review_msg_id="msg-2"))

    (stmt,) = session.statements
    where = {clause.left.name: clause.right.value for clause in stmt.whereclause.clauses}
    assert where == {"project": "p", "thread_id": "T-1", "msg_id": "msg-2"}


async def test_no_row_is_a_422() -> None:
    session = _RecordingSession(row=None)
    with pytest.raises(ChatroomUnprocessableError):
        await _check(
            session, CloseSanction(kind="naysayer_approved", review_msg_id="msg-2")
        )


async def test_a_row_with_another_role_is_a_422() -> None:
    session = _RecordingSession(row=("proposer",))
    with pytest.raises(ChatroomUnprocessableError):
        await _check(
            session, CloseSanction(kind="naysayer_approved", review_msg_id="msg-2")
        )


@pytest.mark.parametrize(
    "sanction",
    [
        None,
        CloseSanction(kind="unspecified"),
        CloseSanction(kind="human_override", reason="Tier-C"),
        CloseSanction(kind="pr_gate_ledger", **LEDGER),
    ],
    ids=["none", "unspecified", "human_override", "pr_gate_ledger"],
)
async def test_other_kinds_cost_no_query(sanction: CloseSanction | None) -> None:
    """Only the kind whose evidence lives in this DB is looked up."""
    session = _RecordingSession(row=None)
    await _check(session, sanction)
    assert session.statements == []


# ----- the 422 on the wire -----


def test_the_refusal_is_a_422_in_the_common_envelope() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/probe")
    def _probe() -> None:
        check_naysayer_review_row(
            review_msg_id="msg-515", thread_id="T-1", found=True, role="implementer"
        )

    r = TestClient(app, raise_server_exceptions=False).get("/probe")
    assert r.status_code == 422
    body = r.json()
    assert body["error_type"] == "ChatroomUnprocessableError"
    assert body["details"]["review_msg_id"] == "msg-515"
