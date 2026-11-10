"""Agent session endpoints (T45 PR1).

PUT /v1/agent_sessions/{cc_session_uuid}   -- declare (start / end / declare)
GET /v1/agent_sessions?project=&identity=  -- list, each row with its board_state

The only intended writer is Magickit's ``session_declare`` (PR 2).
Authorisation is the same as every other endpoint here (the tailnet), and
PR1 adds nothing. The write-once rules live in ``services.agent_session``;
this module only opens the transaction and renders the result.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query
from sqlalchemy import select

from spirrow_conclair.db import SessionDep
from spirrow_conclair.models import AgentSession as AgentSessionRow
from spirrow_conclair.schemas.agent_session import (
    AgentSession,
    AgentSessionListResponse,
    BoardState,
    DeclareAgentSessionRequest,
)
from spirrow_conclair.services.agent_session import declare, derive_board_state

router = APIRouter(prefix="/v1/agent_sessions", tags=["agent_sessions"])

SessionUuidPath = Annotated[str, Path(min_length=1, max_length=200)]


def to_response(row: AgentSessionRow) -> AgentSession:
    """Render a row. ``mindwire_session_id`` is never copied, because the
    response model has no field for it."""
    return AgentSession(
        cc_session_uuid=row.cc_session_uuid,
        project=row.project,
        identity=row.identity,
        instance_id=row.instance_id,
        embodiment=row.embodiment,
        host=row.host,
        transcript_path=row.transcript_path,
        thread_id=row.thread_id,
        source=row.source,
        started_at=row.started_at,
        last_declared_at=row.last_declared_at,
        ended_at=row.ended_at,
        end_reason=row.end_reason,
        board_state=BoardState(**derive_board_state(row)),
    )


@router.put(
    "/{cc_session_uuid}",
    response_model=AgentSession,
    summary="Declare an agent session (write-once; 409 ChatroomSessionConflictError)",
)
async def put_agent_session(
    cc_session_uuid: SessionUuidPath,
    body: DeclareAgentSessionRequest,
    session: SessionDep,
) -> AgentSession:
    async with session.begin():
        row = await declare(session, cc_session_uuid, body)
        response = to_response(row)
    return response


@router.get(
    "",
    response_model=AgentSessionListResponse,
    summary="List agent sessions, newest declaration first",
)
async def list_agent_sessions(
    session: SessionDep,
    project: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    identity: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> AgentSessionListResponse:
    stmt = select(AgentSessionRow)
    if project is not None:
        stmt = stmt.where(AgentSessionRow.project == project)
    if identity is not None:
        stmt = stmt.where(AgentSessionRow.identity == identity)
    stmt = stmt.order_by(AgentSessionRow.last_declared_at.desc(), AgentSessionRow.cc_session_uuid)
    rows = (await session.execute(stmt)).scalars().all()
    return AgentSessionListResponse(items=[to_response(r) for r in rows])
