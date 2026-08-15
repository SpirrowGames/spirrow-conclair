"""Schemas for the per-identity read cursor endpoints.

Two endpoints land here:
- ``POST /v1/projects/{project}/threads/{thread_id}/read``
  (``MarkReadRequest`` / ``MarkReadResponse``)
- ``GET /v1/projects/{project}/unread``
  (``UnreadThreadItem`` / ``UnreadListResponse``)
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MarkReadRequest(BaseModel):
    """POST /threads/{tid}/read body.

    ``up_to_msg_id`` is optional. When omitted (or empty), the cursor
    advances to the thread's current latest msg -- the catch-up case.
    Pass an explicit msg_id to record an intermediate read position
    (e.g. for a tool that wants to bookmark "I read up to here").
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    identity_name: str = Field(min_length=1, max_length=200)
    # Pydantic strips whitespace by config; an empty string after strip
    # is treated as "advance to latest" by the route. We keep this as
    # Optional[str] (rather than rejecting empty) so the adapter on the
    # other side can hand-write ``up_to_msg_id=""`` for advance-to-latest
    # without serializing a JSON ``null``.
    up_to_msg_id: str | None = None


class MarkReadResponse(BaseModel):
    """POST /threads/{tid}/read response.

    ``advanced`` is True when the cursor moved forward (UPSERT + audit
    event happened). It's False on the monotonic-no-op: the request
    pointed at the current cursor or earlier, the row was not touched,
    no audit event was emitted. The response always reflects the
    *current* cursor state, not the requested value.
    """

    model_config = ConfigDict(from_attributes=True)

    project: str
    identity_name: str
    thread_id: str
    last_read_msg_id: str
    updated_at: datetime
    advanced: bool


class UnreadThreadItem(BaseModel):
    """One row of the inbox response.

    ``last_read_msg_id`` is ``None`` when this identity has never
    mark_read'd the thread -- the cursor row simply does not exist yet.
    ``unread_count`` in that case is the full size of the thread.
    """

    model_config = ConfigDict(from_attributes=True)

    thread_id: str
    title: str
    status: str
    owner: str
    latest_msg_id: str
    last_read_msg_id: str | None
    unread_count: int


class UnreadListResponse(BaseModel):
    """GET /unread paginated response.

    Sort order: ``unread_count DESC``, then newest msg first (the
    thread's max msg_id), then ``thread.created_at DESC``. The inbox
    surfaces the thread with the most new activity first, and breaks
    ties by when the thread was last *posted to* rather than when it
    was created.
    """

    items: list[UnreadThreadItem]
    total: int
    limit: int
    offset: int
