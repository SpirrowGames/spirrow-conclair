"""Thread-related schemas."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from spirrow_conclair.schemas.message import Message

ThreadStatus = Literal["active", "awaiting_reply", "resolved", "superseded", "parked"]


class Thread(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

    project: str
    thread_id: str
    title: str
    owner: str
    status: ThreadStatus
    created_at: datetime
    created_by_msg: str
    resolved_by_msg: str | None = None
    affects_threads: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class OpenThreadRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    thread_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    owner: str = Field(min_length=1, max_length=200)
    propose_content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    commit_ref: str | None = None
    timestamp: datetime | None = None


class OpenThreadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    thread: Thread
    msg: "Message"


class ThreadListResponse(BaseModel):
    items: list[Thread]
    total: int
    limit: int
    offset: int


class ThreadView(BaseModel):
    """Response for GET /threads/{thread_id} — thread + (filtered) messages."""

    model_config = ConfigDict(from_attributes=True)

    thread: Thread
    messages: list["Message"]
    mode: Literal["full", "summary"]
