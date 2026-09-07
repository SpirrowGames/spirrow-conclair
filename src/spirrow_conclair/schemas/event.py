"""ChatroomEvent + integrity-check response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventAction = Literal[
    "open_thread", "post_message", "status_transition", "mark_read",
]
IntegrityIssueType = Literal[
    "missing_propose",
    "closes_thread_by_non_owner",
    "invalid_reply_to",
    "dangling_thread_reference",
    "orphan_message",
    "inconsistent_resolved",
    "stale_activity_key",
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


UnattributableReason = Literal["pre_recording", "unclassified_override"]


class IntegrityIssue(BaseModel):
    type: IntegrityIssueType
    thread_id: str | None = None
    msg_id: str | None = None
    details: str
    # Diagnostic only, and only on `closes_thread_by_non_owner`: whether *this
    # message* has a `status_transition` audit event. Never an input to the
    # classification -- it is keyed on the event's own `msg_id` column, so it
    # answers "did this row reach the write path", which splits a rollback
    # window from a direct INSERT. Those need opposite investigations, and the
    # thread-level form of the same question would answer for a sibling
    # message instead.
    has_status_transition_event: bool | None = None


class UnattributableClose(BaseModel):
    """A non-owner close that is neither accounted for nor demonstrably wrong.

    ``pre_recording`` closes predate the sanction recorder and freeze once it
    ships; they are not backfilled, because the only surviving evidence is a
    line of prose in the decide body and reading prose as proof is the failure
    mode this whole change exists to avoid. ``unclassified_override`` means a
    caller sent the bare ``owner_override`` boolean -- expected while Magickit
    catches up, a defect signal once it has.
    """

    thread_id: str
    msg_id: str
    reason: UnattributableReason


class SanctionedCloseCounts(BaseModel):
    """Counts only, never rows.

    Listing sanctioned closes would make the report grow with normal
    development -- the exact disease that made ``issue_count`` unusable.
    """

    pr_gate_ledger: int = 0
    human_override: int = 0


class IntegrityCheckResponse(BaseModel):
    issues: list[IntegrityIssue]
    issue_count: int
    checked_at: datetime
    sanctioned_counts: SanctionedCloseCounts = Field(
        default_factory=SanctionedCloseCounts
    )
    unattributable: list[UnattributableClose] = Field(default_factory=list)
    # The instant this deployment began recording close sanctions. `null` means
    # none is configured, and then no close is reported as corruption -- shown
    # in the report rather than kept in a config file so a reader can see that
    # the strictest bucket is disarmed.
    sanction_recording_since: datetime | None = None
