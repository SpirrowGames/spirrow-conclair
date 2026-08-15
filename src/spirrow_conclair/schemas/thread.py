"""Thread-related schemas."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from spirrow_conclair.schemas.message import Message

ThreadStatus = Literal["active", "awaiting_reply", "resolved", "superseded", "parked"]


class Thread(BaseModel):
    """A thread's stored state plus its activity rollup.

    **Two msg_id fields, and they mean different things.**

    - ``created_by_msg`` — the thread's **first** msg (the propose). It
      never changes, and it says nothing about how much is in the
      thread. Before the rollup fields existed it was the only msg_id
      on this object, which invited reading a busy thread as an empty
      one (2026-08-15 near-miss).
    - ``last_msg_id`` — the thread's **latest** msg. Same value as the
      inbox's ``UnreadThreadItem.latest_msg_id``.

    ``last_msg_id`` / ``msg_count`` / ``last_activity_at`` are derived
    per request from ``messages`` (see ``services/thread_rollup``): no
    write path assigns them, so they cannot go stale. (``threads`` does
    carry one denormalised value, ``last_msg_num`` — the listing's sort
    key, which is not on this object and is audited by
    ``stale_activity_key``.) These three are ``None`` / ``0`` only for a
    thread with no msgs at all; ``open_thread`` makes that unreachable,
    but the listing reports such a row rather than dropping it.

    ``last_activity_at`` is the timestamp **of** ``last_msg_id`` — one
    msg's two fields, not two independent maxima. The distinction is
    load-bearing because ``timestamp`` is a value callers supply while
    ``last_msg_id`` follows the server-allocated sequence: a backfill
    makes the newest msg in the sequence the oldest by date, and a
    per-column max would then pair one msg's id with another msg's
    date. For the same reason the listing *ranks* on the sequence and
    only *shows* ``last_activity_at`` — a caller must not be able to
    choose where its thread lands in someone else's triage list.
    """

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
    # Deliberately required (not defaulted): every construction site must
    # supply a rollup it actually computed. A default would let a route
    # emit `msg_count: 0` for a thread with 40 msgs -- a wrong number is
    # worse here than a missing field, since the whole point of these
    # fields is to be trusted at a glance during triage.
    last_msg_id: str | None
    msg_count: int
    last_activity_at: datetime | None


class OpenThreadRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    thread_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    owner: str = Field(min_length=1, max_length=200)
    propose_content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    commit_ref: str | None = None
    timestamp: datetime | None = None
    # ADR-2026-05-29-12 self-declared. propose is not in the mandatory set
    # (Einstein N-3 / msg-325 §4) but the receiver is wired here so the
    # value is recorded on the propose msg when supplied.
    embodiment: str | None = None
    # ADR-2026-05-27-09 / msg-002 §2: role the opener was acting under,
    # stamped onto the propose msg. Conclair persists verbatim; Magickit
    # validates against the Prismind identity record before forwarding.
    role: str | None = None


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
