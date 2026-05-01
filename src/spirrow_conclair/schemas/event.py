"""ChatroomEvent + integrity-check response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventAction = Literal["open_thread", "post_message", "status_transition"]
IntegrityIssueType = Literal[
    "missing_propose",
    "closes_thread_by_non_owner",
    "invalid_reply_to",
    "dangling_thread_reference",
    "orphan_message",
    "inconsistent_resolved",
]


class ChatroomEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project: str
    timestamp: datetime
    actor: str
    action: EventAction
    thread_id: str | None = None
    msg_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class EventListResponse(BaseModel):
    items: list[ChatroomEvent]
    total: int
    limit: int
    offset: int


class IntegrityIssue(BaseModel):
    type: IntegrityIssueType
    thread_id: str | None = None
    msg_id: str | None = None
    details: str


class IntegrityCheckResponse(BaseModel):
    issues: list[IntegrityIssue]
    issue_count: int
    checked_at: datetime
