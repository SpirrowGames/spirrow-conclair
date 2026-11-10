"""Unit tests for the agent_sessions write-once rules and board mapping (T45 PR1).

``plan_declare`` and ``derive_board_state`` are pure, so the bulk of the DoD
(msg-1320 §5 as amended by msg-1322) is pinned here, where the gate can run
it without Docker. The DB half, which covers the lock, the CHECK, the
concurrent insert and the HTTP round trip, is
``tests/integration/test_api_agent_sessions.py``. The 409 envelope's
``error_type`` string is pinned in ``test_error_handlers.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from spirrow_conclair.api.agent_sessions import to_response
from spirrow_conclair.exceptions import ChatroomSessionConflictError
from spirrow_conclair.models import AgentSession
from spirrow_conclair.schemas.agent_session import (
    AgentSession as AgentSessionOut,
)
from spirrow_conclair.schemas.agent_session import DeclareAgentSessionRequest
from spirrow_conclair.services.agent_session import (
    FIXED_COLUMNS,
    derive_board_state,
    plan_declare,
)

T0 = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
SECRET = "mw-session-7f3a"

HOOK_BASE: dict[str, Any] = {
    "project": "spirrow-mindwire",
    "source": "hook",
    "identity": "Bohr",
    "instance_id": "proposer-1",
    "embodiment": "terminal_coding_agent",
    "host": "sg-tomtebo-01",
    "thread_id": "T-1",
}
LOOP_BASE: dict[str, Any] = {**HOOK_BASE, "source": "loop", "mindwire_session_id": SECRET}


def _req(base: dict[str, Any], kind: str = "declare", **over: Any) -> DeclareAgentSessionRequest:
    return DeclareAgentSessionRequest(**{**base, "kind": kind, **over})


def _row(base: dict[str, Any], **over: Any) -> AgentSession:
    values = {
        "cc_session_uuid": "u-1",
        "transcript_path": None,
        "mindwire_session_id": None,
        "started_at": T0,
        "last_declared_at": T0,
        "ended_at": None,
        "end_reason": None,
        **base,
        **over,
    }
    return AgentSession(**values)


def _apply(row: AgentSession, updates: dict[str, Any]) -> AgentSession:
    for k, v in updates.items():
        setattr(row, k, v)
    return row


# --- insert ----------------------------------------------------------------


def test_insert_sets_started_and_last_declared_to_now() -> None:
    values = plan_declare(None, _req(HOOK_BASE, "start"), T0, uuid="u-1")
    assert values["started_at"] == T0
    assert values["last_declared_at"] == T0
    assert values["ended_at"] is None


def test_insert_honours_given_started_at() -> None:
    values = plan_declare(None, _req(HOOK_BASE, "start", started_at=T0), T1, uuid="u-1")
    assert values["started_at"] == T0
    assert values["last_declared_at"] == T1


def test_identity_omitted_inserts_null() -> None:
    """D4: identity is not guessed."""
    base = {k: v for k, v in HOOK_BASE.items() if k != "identity"}
    values = plan_declare(None, _req(base, "start"), T0, uuid="u-1")
    assert values["identity"] is None


def test_insert_with_end_writes_end() -> None:
    values = plan_declare(None, _req(HOOK_BASE, "end", end_reason="logout"), T0, uuid="u-1")
    assert values["ended_at"] == T0
    assert values["end_reason"] == "logout"


# --- D3 --------------------------------------------------------------------


def test_second_start_only_advances_last_declared_at() -> None:
    row = _row(HOOK_BASE)
    updates = plan_declare(row, _req(HOOK_BASE, "start"), T1)
    # The ended_* reset is a no-op on a row that has no end.
    assert {k: v for k, v in updates.items() if k not in ("ended_at", "end_reason")} == {
        "last_declared_at": T1
    }
    assert updates.get("ended_at") is None


# --- fixed columns ---------------------------------------------------------


@pytest.mark.parametrize("column", [c for c in FIXED_COLUMNS if c != "source"])
def test_every_fixed_column_is_immutable_on_hook_rows(column: str) -> None:
    row = _row(HOOK_BASE)
    with pytest.raises(ChatroomSessionConflictError) as info:
        plan_declare(row, _req(HOOK_BASE, **{column: "changed"}), T1)
    assert info.value.details == {"field": column, "reason": "immutable"}


@pytest.mark.parametrize(
    "column", [c for c in FIXED_COLUMNS if c not in ("source", "mindwire_session_id")]
)
def test_every_fixed_column_is_immutable_on_loop_rows(column: str) -> None:
    row = _row(LOOP_BASE)
    with pytest.raises(ChatroomSessionConflictError) as info:
        plan_declare(row, _req(LOOP_BASE, **{column: "changed"}), T1)
    assert info.value.details == {"field": column, "reason": "immutable"}


def test_source_is_immutable() -> None:
    row = _row(HOOK_BASE)
    with pytest.raises(ChatroomSessionConflictError) as info:
        plan_declare(row, _req(LOOP_BASE), T1)
    assert info.value.details == {"field": "source", "reason": "immutable"}


def test_started_at_is_immutable_when_given() -> None:
    row = _row(HOOK_BASE)
    with pytest.raises(ChatroomSessionConflictError) as info:
        plan_declare(row, _req(HOOK_BASE, started_at=T1), T1)
    assert info.value.details == {"field": "started_at", "reason": "immutable"}
    # The same value, or none at all, passes.
    plan_declare(row, _req(HOOK_BASE, started_at=T0), T1)
    plan_declare(row, _req(HOOK_BASE), T1)


def test_omitting_a_stored_fixed_value_is_a_change() -> None:
    """None is compared like any other value: write-once means no NULL-ing."""
    row = _row(HOOK_BASE)
    with pytest.raises(ChatroomSessionConflictError) as info:
        plan_declare(row, _req(HOOK_BASE, identity=None), T1)
    assert info.value.details == {"field": "identity", "reason": "immutable"}


# --- transcript_path -------------------------------------------------------


def test_transcript_path_null_to_value_once() -> None:
    row = _row(HOOK_BASE)
    updates = plan_declare(row, _req(HOOK_BASE, transcript_path="/a.jsonl"), T1)
    assert updates["transcript_path"] == "/a.jsonl"
    _apply(row, updates)

    # The same value again is a no-op, and omitting it does not clear it.
    assert "transcript_path" not in plan_declare(
        row, _req(HOOK_BASE, transcript_path="/a.jsonl"), T1
    )
    assert "transcript_path" not in plan_declare(row, _req(HOOK_BASE), T1)

    with pytest.raises(ChatroomSessionConflictError) as info:
        plan_declare(row, _req(HOOK_BASE, transcript_path="/b.jsonl"), T1)
    assert info.value.details == {"field": "transcript_path", "reason": "already_set"}


# --- end -------------------------------------------------------------------


def test_ended_at_is_written_once() -> None:
    row = _row(HOOK_BASE)
    _apply(row, plan_declare(row, _req(HOOK_BASE, "end", end_reason="exit"), T1))
    assert row.ended_at == T1 and row.end_reason == "exit"

    with pytest.raises(ChatroomSessionConflictError) as info:
        plan_declare(row, _req(HOOK_BASE, "end"), T1)
    assert info.value.details == {"field": "ended_at", "reason": "already_set"}


def test_end_fields_are_refused_outside_kind_end() -> None:
    for kind in ("start", "declare"):
        with pytest.raises(ValidationError):
            _req(HOOK_BASE, kind, ended_at=T1)
        with pytest.raises(ValidationError):
            _req(HOOK_BASE, kind, end_reason="x")


# --- D7 withdrawal ---------------------------------------------------------


def test_hook_row_end_is_withdrawn_by_start() -> None:
    row = _row(HOOK_BASE, ended_at=T0, end_reason="exit")
    _apply(row, plan_declare(row, _req(HOOK_BASE, "start"), T1))
    assert row.ended_at is None and row.end_reason is None


def test_hook_row_end_is_not_withdrawn_by_declare() -> None:
    row = _row(HOOK_BASE, ended_at=T0, end_reason="exit")
    _apply(row, plan_declare(row, _req(HOOK_BASE, "declare"), T1))
    assert row.ended_at == T0


def test_loop_row_end_is_not_withdrawn_by_start() -> None:
    row = _row(LOOP_BASE, ended_at=T0, end_reason="exit")
    _apply(row, plan_declare(row, _req(LOOP_BASE, "start"), T1))
    assert row.ended_at == T0 and row.end_reason == "exit"


# --- loop rows: mindwire_session_id gates every kind (msg-1322) ------------


@pytest.mark.parametrize("kind", ["start", "declare", "end"])
@pytest.mark.parametrize("presented", [None, "mw-session-other"], ids=["omitted", "wrong"])
def test_loop_row_requires_matching_session_id(kind: str, presented: str | None) -> None:
    row = _row(LOOP_BASE)
    req = _req(LOOP_BASE, kind, mindwire_session_id=presented, transcript_path="/t.jsonl")
    with pytest.raises(ChatroomSessionConflictError) as info:
        plan_declare(row, req, T1)
    assert info.value.details == {"field": "mindwire_session_id", "reason": "session_id_mismatch"}
    # Nothing in the error names the stored value.
    assert SECRET not in str(info.value) and SECRET not in repr(info.value.details)
    # Nothing was written.
    assert row.transcript_path is None
    assert row.last_declared_at == T0
    assert row.ended_at is None


def test_new_loop_row_without_session_id_is_a_named_conflict() -> None:
    """msg-1556: refused in the service, before any INSERT, not by the DB CHECK."""
    base = {k: v for k, v in LOOP_BASE.items() if k != "mindwire_session_id"}
    for kind in ("start", "declare", "end"):
        with pytest.raises(ChatroomSessionConflictError) as info:
            plan_declare(None, _req(base, kind), T0, uuid="u-1")
        assert info.value.details == {
            "field": "mindwire_session_id",
            "reason": "session_id_mismatch",
        }


def test_new_hook_row_without_session_id_is_accepted() -> None:
    values = plan_declare(None, _req(HOOK_BASE, "start"), T0, uuid="u-1")
    assert values["mindwire_session_id"] is None


def test_loop_session_id_is_checked_before_fixed_columns() -> None:
    """Even a request that also changes a fixed column reports the mismatch."""
    row = _row(LOOP_BASE)
    with pytest.raises(ChatroomSessionConflictError) as info:
        plan_declare(row, _req(LOOP_BASE, mindwire_session_id=None, identity="Mallory"), T1)
    assert info.value.details["reason"] == "session_id_mismatch"


def test_loop_row_with_matching_session_id_accepts_transcript() -> None:
    row = _row(LOOP_BASE)
    updates = plan_declare(row, _req(LOOP_BASE, transcript_path="/t.jsonl"), T1)
    assert updates["transcript_path"] == "/t.jsonl"


def test_loop_row_end_with_matching_session_id() -> None:
    row = _row(LOOP_BASE)
    _apply(row, plan_declare(row, _req(LOOP_BASE, "end"), T1))
    assert row.ended_at == T1


# --- derive_board_state ----------------------------------------------------


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"identity": None}, ("unobserved", "identity_undeclared")),
        # Order: identity_undeclared wins over a declared end.
        ({"identity": None, "ended_at": T0}, ("unobserved", "identity_undeclared")),
        ({"transcript_path": "/t", "ended_at": T0}, ("unobserved", "end_declared_unverified")),
        # Order: an unverified end wins over a missing transcript.
        ({"ended_at": T0}, ("unobserved", "end_declared_unverified")),
        ({}, ("unobserved", "transcript_undeclared")),
        ({"transcript_path": "/t"}, ("declared", None)),
    ],
)
def test_board_state_hook(over: dict[str, Any], expected: tuple[str, str | None]) -> None:
    got = derive_board_state(_row(HOOK_BASE, **over))
    assert (got["state"], got["reason"]) == expected


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        (
            {"identity": None, "transcript_path": "/t", "ended_at": T0},
            ("unobserved", "identity_undeclared"),
        ),
        # Order: a missing transcript wins over a loop end.
        ({"ended_at": T0}, ("unobserved", "transcript_undeclared")),
        ({"transcript_path": "/t", "ended_at": T0}, ("terminated", None)),
        ({"transcript_path": "/t"}, ("declared", None)),
    ],
)
def test_board_state_loop(over: dict[str, Any], expected: tuple[str, str | None]) -> None:
    got = derive_board_state(_row(LOOP_BASE, **over))
    assert (got["state"], got["reason"]) == expected


# --- response shape --------------------------------------------------------


def test_response_never_carries_mindwire_session_id() -> None:
    assert "mindwire_session_id" not in AgentSessionOut.model_fields
    out = to_response(_row(LOOP_BASE, transcript_path="/t"))
    dumped = out.model_dump_json()
    assert "mindwire_session_id" not in dumped
    assert SECRET not in dumped
    assert out.board_state.state == "declared"
