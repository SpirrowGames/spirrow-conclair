"""Exception hierarchy for spirrow-conclair.

These map onto HTTP status codes in the FastAPI exception handlers
(see api-design.md §5):

| Exception                  | HTTP code | Notes                                |
|----------------------------|-----------|--------------------------------------|
| ChatroomNotFoundError      | 404       | thread / msg / project not found     |
| ChatroomIntegrityError     | 409       | invariant violation                  |
| ChatroomPermissionError    | 403       | non-owner action attempted           |
| ChatroomStateError         | 409       | invalid status transition            |
| ChatroomDBError            | 500       | unexpected DB-level failure          |
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


class ChatroomPermissionError(ChatroomError):
    """Caller does not have permission for this action (e.g. non-owner close)."""


class ChatroomStateError(ChatroomError):
    """Operation requested in an incompatible thread/message state."""


class ChatroomDBError(ChatroomError):
    """Unexpected DB-level error (wraps underlying SQLAlchemy / asyncpg failures)."""
