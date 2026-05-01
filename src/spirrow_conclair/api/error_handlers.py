"""Map ChatroomError subclasses onto HTTP status codes.

The shape `{error_type, error, details?}` is locked by api-design.md §1.4
and must be honored by every error path (including FastAPI's own 422).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from spirrow_conclair.exceptions import (
    ChatroomDBError,
    ChatroomError,
    ChatroomIntegrityError,
    ChatroomNotFoundError,
    ChatroomPermissionError,
    ChatroomStateError,
)


def _payload(exc: ChatroomError, status: int) -> JSONResponse:
    body: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "error": exc.message,
    }
    if exc.details:
        body["details"] = exc.details
    return JSONResponse(status_code=status, content=body)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ChatroomNotFoundError)
    async def _not_found(request: Request, exc: ChatroomNotFoundError) -> JSONResponse:
        return _payload(exc, 404)

    @app.exception_handler(ChatroomPermissionError)
    async def _permission(
        request: Request, exc: ChatroomPermissionError
    ) -> JSONResponse:
        return _payload(exc, 403)

    @app.exception_handler(ChatroomIntegrityError)
    async def _integrity(
        request: Request, exc: ChatroomIntegrityError
    ) -> JSONResponse:
        return _payload(exc, 409)

    @app.exception_handler(ChatroomStateError)
    async def _state(request: Request, exc: ChatroomStateError) -> JSONResponse:
        return _payload(exc, 409)

    @app.exception_handler(ChatroomDBError)
    async def _db(request: Request, exc: ChatroomDBError) -> JSONResponse:
        return _payload(exc, 500)

    # Reshape FastAPI's 422 into the common envelope.
    @app.exception_handler(RequestValidationError)
    async def _validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error_type": "ValidationError",
                "error": "Request body failed validation",
                "details": {"errors": exc.errors()},
            },
        )

    # Wrap unexpected SQLAlchemy IntegrityError (e.g. CHECK / FK constraint
    # slipping past pre-write asserts) so the client still sees the common
    # 409 envelope rather than an opaque 500.
    @app.exception_handler(SAIntegrityError)
    async def _sa_integrity(
        request: Request, exc: SAIntegrityError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error_type": "ChatroomIntegrityError",
                "error": "Database integrity constraint violated",
                "details": {"orig": str(exc.orig) if exc.orig else str(exc)},
            },
        )
