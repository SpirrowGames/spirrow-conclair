"""Disposition: wire shape (pydantic) and invariant 8 (pre-write assert).

The shape tests iterate the Literals in ``schemas.disposition_vocab`` instead
of re-declaring the vocabulary, so the wire is checked to *derive* from that
one declaration site (same approach as ``test_close_sanction_vocabulary``).
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from spirrow_conclair.exceptions import ChatroomIntegrityError
from spirrow_conclair.schemas import Message, PostMessageRequest
from spirrow_conclair.schemas.disposition_vocab import (
    DispositionArm,
    DispositionFallbackReason,
    DispositionKind,
)
from spirrow_conclair.services.integrity import assert_disposition_close_rule


def _req(disposition: Any) -> PostMessageRequest:
    return PostMessageRequest.model_validate(
        {"type": "handoff", "author": "a", "content": "c", "disposition": disposition}
    )


def _blocked(**over: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "kind": "blocked_on",
        "trigger": {"arm": "pr", "ref": "SpirrowGames/spirrow-conclair#32"},
        "wake": "Heisenberg",
    }
    value.update(over)
    return value


# ----- the field exists and is optional -----------------------------------


def test_omitted_is_none() -> None:
    body = PostMessageRequest.model_validate({"type": "report", "author": "a", "content": "c"})
    assert body.disposition is None


def test_done_round_trips() -> None:
    body = _req({"kind": "done"})
    assert body.disposition is not None
    assert body.disposition.model_dump(mode="json", exclude_none=True) == {"kind": "done"}


@pytest.mark.parametrize("arm", get_args(DispositionArm))
def test_blocked_on_accepts_every_arm(arm: str) -> None:
    value = _blocked(trigger={"arm": arm, "ref": "r"})
    body = _req(value)
    assert body.disposition is not None
    assert body.disposition.model_dump(mode="json", exclude_none=True) == value


def test_kind_vocabulary_is_exactly_the_union() -> None:
    # Every DispositionKind member has a model, and nothing else is accepted.
    for kind in get_args(DispositionKind):
        payload = {"kind": "done"} if kind == "done" else _blocked()
        assert _req(payload).disposition is not None
    with pytest.raises(ValidationError):
        _req({"kind": "malformed"})


# ----- strictness: unknown keys refused, not dropped ----------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "done", "wake": "x"},
        _blocked(extra="x"),
        _blocked(trigger={"arm": "pr", "ref": "r", "extra": "x"}),
        _blocked(
            fallback={
                "reason": "owner_unresolved",
                "owner": "o",
                "declared": "done",
                "extra": "x",
            }
        ),
    ],
)
def test_unknown_keys_are_refused(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _req(payload)


@pytest.mark.parametrize(
    "payload",
    [
        _blocked(trigger={"arm": "nope", "ref": "r"}),
        _blocked(trigger={"arm": "pr", "ref": ""}),
        _blocked(trigger={"arm": "pr", "ref": "   "}),
        _blocked(wake=""),
        _blocked(wake="  "),
        {"kind": "blocked_on", "trigger": {"arm": "pr", "ref": "r"}},
        {"kind": "blocked_on", "wake": "w"},
        {"trigger": {"arm": "pr", "ref": "r"}, "wake": "w"},
        "done",
    ],
)
def test_malformed_shapes_are_refused(payload: Any) -> None:
    with pytest.raises(ValidationError):
        _req(payload)


# ----- fallback (D-8 (3) system rewrite record) ---------------------------


@pytest.mark.parametrize("reason", get_args(DispositionFallbackReason))
def test_fallback_is_accepted_on_blocked_on(reason: str) -> None:
    value = _blocked(
        trigger={"arm": "human", "ref": "T-x"},
        fallback={"reason": reason, "owner": "ghost-bot", "declared": "done"},
    )
    body = _req(value)
    assert body.disposition is not None
    assert body.disposition.model_dump(mode="json", exclude_none=True) == value


@pytest.mark.parametrize(
    "fallback",
    [
        {"reason": "other", "owner": "o", "declared": "done"},
        {"reason": "owner_unresolved", "owner": "", "declared": "done"},
        {"reason": "owner_unresolved", "owner": "o", "declared": "blocked_on"},
        {"reason": "owner_unresolved", "declared": "done"},
    ],
)
def test_incomplete_or_wrong_fallback_is_refused(fallback: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _req(_blocked(fallback=fallback))


def test_fallback_is_not_accepted_on_done() -> None:
    with pytest.raises(ValidationError):
        _req(
            {
                "kind": "done",
                "fallback": {"reason": "owner_unresolved", "owner": "o", "declared": "done"},
            }
        )


# ----- the response model reads the stored JSON back ----------------------


def test_message_schema_reads_stored_json() -> None:
    stored = _blocked()
    msg = Message.model_validate(
        {
            "project": "p",
            "msg_id": "msg-001",
            "thread_id": "T-1",
            "author": "a",
            "timestamp": "2026-10-03T00:00:00Z",
            "type": "handoff",
            "content": "c",
            "disposition": stored,
        }
    )
    assert msg.model_dump(mode="json")["disposition"] == {**stored, "fallback": None}


def test_message_schema_null_disposition() -> None:
    msg = Message.model_validate(
        {
            "project": "p",
            "msg_id": "msg-001",
            "thread_id": "T-1",
            "author": "a",
            "timestamp": "2026-10-03T00:00:00Z",
            "type": "report",
            "content": "c",
        }
    )
    assert msg.disposition is None


@pytest.mark.parametrize(
    "stored",
    [
        {"kind": "blocked_on"},
        {"kind": "done", "typo": True},
        {"kind": "blocked_on", "trigger": {"arm": "nope", "ref": "r"}, "wake": "w"},
    ],
    ids=["no-trigger", "extra-key", "unknown-arm"],
)
def test_message_schema_returns_floor_only_rows_raw(stored: dict[str, Any]) -> None:
    # A row that passes the DB floor but not the strict model (written around
    # the API) must not fail the read: it comes back as stored, not a 500.
    msg = Message.model_validate(
        {
            "project": "p",
            "msg_id": "msg-001",
            "thread_id": "T-1",
            "author": "a",
            "timestamp": "2026-10-03T00:00:00Z",
            "type": "report",
            "content": "c",
            "disposition": stored,
        }
    )
    assert msg.model_dump(mode="json")["disposition"] == stored


def test_message_schema_prefers_the_strict_model() -> None:
    # left_to_right: a valid row is the typed model, not the raw fallback.
    msg = Message.model_validate(
        {
            "project": "p",
            "msg_id": "msg-001",
            "thread_id": "T-1",
            "author": "a",
            "timestamp": "2026-10-03T00:00:00Z",
            "type": "handoff",
            "content": "c",
            "disposition": _blocked(),
        }
    )
    assert not isinstance(msg.disposition, dict)


def test_request_side_stays_strict() -> None:
    # The leniency is read-only: a caller still gets a 422 for the same value.
    with pytest.raises(ValidationError):
        _req({"kind": "blocked_on"})


# ----- invariant 8 ---------------------------------------------------------


@pytest.mark.parametrize("disposition", [None, {"kind": "done"}, _blocked()])
def test_non_closing_msg_accepts_any_disposition(disposition: Any) -> None:
    assert_disposition_close_rule(disposition=disposition, closes_thread=None)


@pytest.mark.parametrize("disposition", [None, {"kind": "done"}])
def test_closing_msg_accepts_done_or_nothing(disposition: Any) -> None:
    assert_disposition_close_rule(disposition=disposition, closes_thread="T-1")


def test_closing_msg_refuses_blocked_on() -> None:
    with pytest.raises(ChatroomIntegrityError) as ei:
        assert_disposition_close_rule(disposition=_blocked(), closes_thread="T-1")
    assert ei.value.details == {"disposition_kind": "blocked_on", "closes_thread": "T-1"}
