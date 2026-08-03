"""Schemas for the cross-project chatroom summary."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectSummary(BaseModel):
    """One project's chatroom state, without any thread bodies."""

    model_config = ConfigDict(from_attributes=True)

    project: str
    thread_count: int
    # Keyed by thread status ("active", "awaiting_reply", "resolved", ...).
    # A mapping rather than fixed fields so a new status does not require a
    # schema change in lockstep with the DB CHECK constraint.
    threads_by_status: dict[str, int] = Field(default_factory=dict)
    # Threads tagged "gate:*" -- blocked on someone else's review.
    gated_thread_count: int = 0
    message_count: int = 0
    # Timestamp of the most recent message, or null for a project whose
    # threads exist but hold no messages.
    last_activity_at: datetime | None = None


class ProjectSummaryListResponse(BaseModel):
    items: list[ProjectSummary]
    total: int
