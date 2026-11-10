"""Exception hierarchy for spirrow-conclair.

These map onto HTTP status codes in the FastAPI exception handlers
(see api-design.md §5):

| Exception                    | HTTP code | Notes                              |
|------------------------------|-----------|------------------------------------|
| ChatroomNotFoundError        | 404       | thread / msg / project not found   |
| ChatroomIntegrityError       | 409       | invariant violation                |
| ChatroomSessionConflictError | 409       | agent_sessions write-once refusal  |
| ChatroomPermissionError      | 403       | non-owner action attempted         |
| ChatroomStateError           | 409       | invalid status transition          |
| ChatroomThreadResolvedError  | 409       | write into a resolved thread       |
| ChatroomUnprocessableError   | 422       | request evidence fails a DB check  |
| ChatroomDBError              | 500       | unexpected DB-level failure        |

Only the leaf classes above get an explicit handler. A subclass with no
handler of its own is resolved by Starlette's MRO walk to its nearest
registered ancestor, which is why ``ChatroomThreadResolvedError`` needs no
entry in ``register_error_handlers`` to be a 409.
"""

from __future__ import annotations

from typing import Any


class ChatroomError(Exception):
    """Base for all spirrow-conclair domain errors."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ChatroomNotFoundError(ChatroomError):
    """Requested resource does not exist."""


class ChatroomIntegrityError(ChatroomError):
    """Invariant violation (FK / unique / format / propose / closes_thread rule)."""


class ChatroomSessionConflictError(ChatroomIntegrityError):
    """A write to ``agent_sessions`` broke the write-once rules (D2' / D7).

    This is a named subclass for the same reason as
    ``ChatroomThreadResolvedError``: ``_payload`` emits
    ``type(exc).__name__``, so the class name is the ``error_type`` on the
    wire, and the MRO walk gives it the parent's 409 without a handler of its
    own. Renaming the class is an API change, and
    ``tests/unit/test_error_handlers.py`` pins the string.

    ``details`` carries exactly ``{"field", "reason"}``, where ``reason`` is
    one of ``immutable`` / ``already_set`` / ``session_id_mismatch``. It never
    carries the stored value, and in particular never ``mindwire_session_id``,
    which the board must not show. The final envelope is fixed in PR 2.
    """

    def __init__(self, message: str, *, field: str, reason: str) -> None:
        super().__init__(message, details={"field": field, "reason": reason})


class ChatroomUnprocessableError(ChatroomError):
    """The request is well-formed but its evidence does not check out.

    A 422, the same status FastAPI gives a body that fails schema
    validation, because to the caller it is the same kind of mistake: the
    payload asserts something that is not so. The difference is only that
    answering needs a DB read, so pydantic cannot do it. Currently raised
    for a ``naysayer_approved`` close sanction whose ``review_msg_id`` is
    not a naysayer msg in the closed thread, and for a
    ``settled_disposition`` close sanction whose ``head_msg_id`` is not the
    closed thread's current head, or not a proposer's ``done``
    (``services.integrity.assert_close_sanction_evidence``).

    Not a ``ChatroomIntegrityError`` subclass on purpose: that would inherit
    its 409 through the MRO walk, and a 409 says "the state of the thread
    forbids this", which is not what happened.
    """


class ChatroomPermissionError(ChatroomError):
    """Caller does not have permission for this action (e.g. non-owner close)."""


class ChatroomStateError(ChatroomError):
    """Operation requested in an incompatible thread/message state."""


class ChatroomThreadResolvedError(ChatroomStateError):
    """A write was refused because the target thread is ``resolved``.

    A named subclass rather than a distinct error, because the thing that has
    to travel is the **name on the wire**. ``_payload`` in
    ``api/error_handlers.py`` emits ``type(exc).__name__``, so raising this
    class -- and nothing else about it -- changes the envelope's
    ``error_type`` from ``"ChatroomStateError"`` to
    ``"ChatroomThreadResolvedError"``.

    Why that string matters, and to whom: spirrow-mindwire classifies
    "the target thread is resolved" at exactly one place
    (``magickit/client.py:_is_thread_resolved_envelope``) so its producers can
    ``except ThreadResolvedError`` instead of matching prose. That classifier
    accepts two forms -- an ``error_type`` that names the state directly, or
    ``ChatroomStateError`` **plus** the literal marker ``status='resolved'``
    in the message text. Before ``assert_thread_writable`` existed, the
    refusal came from ``compute_transition``, whose message renders that
    marker verbatim, so the second form matched. ``assert_thread_writable``
    now answers first on that same call path and its message deliberately
    does not contain the marker (it spells the remedy out in prose instead),
    which silently drops the refusal out of both forms. Naming the state here
    puts it back into the first form -- the one that does not depend on
    anybody's sentence staying worded the way it is today.

    Subclassing is what keeps that free of blast radius. Every existing
    ``except ChatroomStateError`` still catches this, and Starlette resolves
    exception handlers by walking ``type(exc).__mro__``, so the 409 mapping
    registered for the parent still applies with no new handler.

    The wire string is therefore a contract with an out-of-repo consumer, not
    an implementation detail: renaming this class changes an API response.
    ``tests/unit/test_error_handlers.py`` pins the emitted string rather than
    the class, because asserting the class alone would keep passing while the
    envelope drifted -- which is the exact failure this class exists to undo.
    """


class ChatroomDBError(ChatroomError):
    """Unexpected DB-level error (wraps underlying SQLAlchemy / asyncpg failures)."""
