"""Schemas for the agent session endpoints (T45 PR1).

- ``PUT /v1/agent_sessions/{cc_session_uuid}`` (``DeclareAgentSessionRequest``
  -> ``AgentSession``)
- ``GET /v1/agent_sessions?project=&identity=`` (``AgentSessionListResponse``)

``mindwire_session_id`` is accepted in the request and appears in no response
model. That is a type-level guarantee rather than a filter someone has to
remember: the response models have no field for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Mirrors ``models.agent_session.AGENT_SESSION_SOURCES``.
AgentSessionSource = Literal["loop", "hook"]

#: ``start`` is the only kind that clears a hook row's end (D7). ``end`` is the
#: only kind that writes one. ``declare`` does neither.
DeclareKind = Literal["start", "end", "declare"]

#: The board states ``derive_board_state`` can return. ``declared`` does not
#: mean "alive"; liveness evidence is T48's job.
BoardStateName = Literal["unobserved", "declared", "terminated"]


class DeclareAgentSessionRequest(BaseModel):
    """The body of a session declaration.

    The fixed columns are a full declaration. A value of ``None`` is
    compared against the stored value like any other value, because
    "omitted" and "NULL" are the same thing to a write-once column. There are
    two exceptions. ``started_at`` defaults to ``now()`` on insert, so on an
    existing row it is compared only when it is given. ``transcript_path``
    only ever moves NULL -> value, so ``None`` never asks to change it.
    """

    model_config = ConfigDict(extra="forbid")

    kind: DeclareKind
    project: str = Field(min_length=1, max_length=200)
    source: AgentSessionSource
    identity: str | None = Field(default=None, max_length=200)
    instance_id: str | None = Field(default=None, max_length=200)
    embodiment: str | None = Field(default=None, max_length=200)
    host: str | None = Field(default=None, max_length=500)
    transcript_path: str | None = Field(default=None, max_length=4000)
    mindwire_session_id: str | None = Field(default=None, max_length=200)
    thread_id: str | None = Field(default=None, max_length=200)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _end_fields_only_on_end(self) -> DeclareAgentSessionRequest:
        if self.kind != "end" and (self.ended_at is not None or self.end_reason is not None):
            raise ValueError("ended_at / end_reason are accepted only when kind='end'")
        return self


class BoardState(BaseModel):
    state: BoardStateName
    reason: str | None


class AgentSession(BaseModel):
    """One agent session as the API returns it. It has no ``mindwire_session_id``."""

    model_config = ConfigDict(from_attributes=True)

    cc_session_uuid: str
    project: str
    identity: str | None
    instance_id: str | None
    embodiment: str | None
    host: str | None
    transcript_path: str | None
    thread_id: str | None
    source: AgentSessionSource
    started_at: datetime
    last_declared_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    board_state: BoardState


class AgentSessionListResponse(BaseModel):
    items: list[AgentSession]
