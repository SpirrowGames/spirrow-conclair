"""Unit tests for the error envelope this API puts on the wire.

Two concerns, both about the envelope rather than about the code that
raises:

1. **The validation envelope must render at all.** The worst shape this
   handler can take is that the endpoint whose job is to say what you got
   wrong is the one that breaks. pydantic v2 puts the original exception
   object into ``ctx["error"]`` when a ``@model_validator`` raises, so
   ``exc.errors()`` is not JSON-safe -- and a 422 that fails to render
   becomes a 500 that says nothing.

2. **The ``error_type`` string is a contract, so it is pinned as a string.**
   ``_payload`` emits ``type(exc).__name__``, which makes a class name part
   of the API response. Asserting the exception class instead would keep
   passing while the emitted string changed, and an out-of-repo consumer
   reads that string (see ``ChatroomThreadResolvedError``).

These run without a database on purpose. The envelope is decided by the
handler and the exception class, not by storage, and the integration suite
that would otherwise own this needs a postgres container -- which means it
is skipped exactly where someone is most likely to be editing quickly.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spirrow_conclair.api.error_handlers import _jsonable, register_error_handlers
from spirrow_conclair.exceptions import ChatroomStateError, ChatroomThreadResolvedError
from spirrow_conclair.models import Thread
from spirrow_conclair.services.integrity import assert_thread_writable


def test_scalars_pass_through_unchanged() -> None:
    for value in ("s", 1, 1.5, True, None):
        assert _jsonable(value) is value or _jsonable(value) == value


def test_an_exception_becomes_its_message() -> None:
    """The case that broke: a live ValueError inside `ctx`."""
    err = ValueError("target_msg_id must be omitted when scope is 'thread'")

    assert _jsonable(err) == "target_msg_id must be omitted when scope is 'thread'"


def test_a_realistic_pydantic_error_list_survives() -> None:
    """Shaped like the real `exc.errors()` from a failing model_validator."""
    errors = [
        {
            "type": "value_error",
            "loc": ("body",),
            "msg": "Value error, target_msg_id must be omitted",
            "input": {"digest": "要約", "target_msg_id": "msg-001"},
            "ctx": {"error": ValueError("target_msg_id must be omitted")},
        }
    ]

    out = _jsonable(errors)

    import json

    # The whole point: this must not raise.
    rendered = json.dumps(out, ensure_ascii=False)
    assert "target_msg_id must be omitted" in rendered
    # Tuples become lists so `loc` survives as JSON.
    assert out[0]["loc"] == ["body"]
    assert out[0]["ctx"]["error"] == "target_msg_id must be omitted"
    # The caller's own input is preserved, not flattened to a string.
    assert out[0]["input"]["digest"] == "要約"


def test_a_field_constraint_error_is_untouched() -> None:
    """The shape that always worked must keep working.

    Every schema here used only `Field(...)` constraints until the digest
    cross-field rule, which is why the bug went unnoticed.
    """
    errors = [
        {
            "type": "string_too_short",
            "loc": ("body", "digest"),
            "msg": "String should have at least 1 character",
            "input": "",
            "ctx": {"min_length": 1},
        }
    ]

    out = _jsonable(errors)

    assert out[0]["ctx"] == {"min_length": 1}
    assert out[0]["input"] == ""


def test_nesting_is_handled_all_the_way_down() -> None:
    out = _jsonable({"a": [{"b": (ValueError("deep"),)}]})

    assert out == {"a": [{"b": ["deep"]}]}


def test_a_non_string_key_is_stringified() -> None:
    assert _jsonable({1: "x"}) == {"1": "x"}


# ---------------------------------------------------------------------------
# The resolved-thread refusal's wire form.
#
# `assert_thread_writable` took over a refusal that used to be raised by
# `compute_transition`, and the two spell their messages differently. A
# consumer that identified the refusal by its prose stopped recognising it at
# that handover, with both repositories' own suites still green -- each side
# was self-consistent. What follows pins the half that crosses the boundary:
# the status code and the literal `error_type` string.
# ---------------------------------------------------------------------------


@pytest.fixture
def envelope_app() -> FastAPI:
    """The real handlers, in front of the real assert. No database.

    The route raises by calling `assert_thread_writable` rather than by
    constructing the exception inline, so the message and `details` under
    test are the ones production emits.
    """
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/probe")
    def _probe() -> None:
        assert_thread_writable(
            Thread(
                project="p",
                thread_id="T-1",
                title="t",
                owner="alice",
                status="resolved",
                created_by_msg="msg-001",
                resolved_by_msg="msg-042",
            )
        )

    return app


@pytest.fixture
def probe(envelope_app: FastAPI) -> TestClient:
    return TestClient(envelope_app, raise_server_exceptions=False)


def test_resolved_refusal_is_409(probe: TestClient) -> None:
    assert probe.get("/probe").status_code == 409


def test_resolved_refusal_error_type_string_is_exact(probe: TestClient) -> None:
    """The contract with spirrow-mindwire, asserted as the string it is.

    Its client classifies "target thread is resolved" on this exact value.
    A rename of the exception class is an API change and must fail here.
    """
    body = probe.get("/probe").json()

    assert body["error_type"] == "ChatroomThreadResolvedError"


def test_the_prose_is_not_what_carries_the_meaning(probe: TestClient) -> None:
    """Why the string above has to be the identifiable part.

    This message does not contain the `status='resolved'` substring that the
    older `compute_transition` refusal rendered, and it is free not to -- it
    spells out a remedy instead. That freedom is exactly why the state is
    named in `error_type`. If someone later edits this sentence to reinstate
    the marker, the assertion below fails and points at this reasoning rather
    than letting a second, redundant coupling grow back.
    """
    body = probe.get("/probe").json()

    assert "status='resolved'" not in body["error"]
    # The status still travels, in a field that is not prose.
    assert body["details"]["status"] == "resolved"
    assert body["details"]["resolved_by_msg"] == "msg-042"


def test_409_comes_from_the_parent_handler_not_a_new_one(
    envelope_app: FastAPI, probe: TestClient
) -> None:
    """No handler is registered for the subclass; the MRO walk supplies it.

    This is what makes the change small: `register_error_handlers` is
    untouched, and the mapping cannot drift apart from the parent's.
    """
    assert ChatroomThreadResolvedError not in envelope_app.exception_handlers
    assert ChatroomStateError in envelope_app.exception_handlers
    assert probe.get("/probe").status_code == 409
