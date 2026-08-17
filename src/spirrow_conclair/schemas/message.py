"""Message-related schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from spirrow_conclair.schemas.thread import Thread

MessageType = Literal[
    "propose", "question", "answer", "decide", "report", "handoff", "ack"
]


class Message(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

    project: str
    msg_id: str
    thread_id: str
    author: str
    timestamp: datetime
    commit_ref: str | None = None
    type: MessageType
    content: str
    reply_to: str | None = None
    references_threads: list[str] = Field(default_factory=list)
    related_tasks: list[str] = Field(default_factory=list)
    closes_thread: str | None = None
    tags: list[str] = Field(default_factory=list)
    # ADR-2026-05-29-12: self-declared runtime form of the authoring agent.
    embodiment: str | None = None
    # ADR-2026-05-27-09 / msg-002 §2: per-msg role the author was acting under.
    # Conclair persists only; Magickit enforces role × allowed_roles.
    role: str | None = None
    # Who acts next. Conclair ascribes meaning to 'none' only (invariant 7);
    # participant names are persisted verbatim and validated by Magickit.
    next_participant: str | None = None


class PostMessageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    type: MessageType
    author: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    reply_to: str | None = None
    references_threads: list[str] = Field(default_factory=list)
    related_tasks: list[str] = Field(default_factory=list)
    closes_thread: str | None = None
    tags: list[str] = Field(default_factory=list)
    commit_ref: str | None = None
    timestamp: datetime | None = None
    embodiment: str | None = None  # ADR-2026-05-29-12 self-declared
    # ADR-2026-05-27-09 / msg-002 §2: role the author was acting under for
    # this msg. Conclair persists verbatim; Magickit validates against the
    # Prismind identity record's allowed_roles before forwarding.
    role: str | None = None
    # Who acts next, as a field instead of prose at the end of `content`.
    # Omitted -> nothing recorded, nothing checked (pre-existing behaviour).
    # Supplied -> persisted verbatim, with exactly one value interpreted:
    # 'none' is refused unless this msg also closes its thread (invariant 7,
    # services.integrity.assert_next_participant_rule). Participant names are
    # not validated here — that needs the identity record, which is Magickit's.
    next_participant: str | None = None
    # ADR-2026-06-04-19 D-5: when true, skip the owner==author check for a
    # closes_thread decide so a Tier-C human can force-close a non-owned
    # thread. Conclair only honors the flag (no identity logic) — Magickit is
    # the sole decision point and sets it iff the author is a human identity.
    # The decision to relax the gate's review requirement is separate (that
    # stays in Magickit); this flag relaxes ownership only.
    owner_override: bool = False
    owner_override_reason: str | None = None


class PostMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    msg: Message
    thread_status_changed_to: str | None = None


class CloseThreadRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    summary_content: str = Field(min_length=1)
    author: str = Field(min_length=1, max_length=200)
    affects_threads: list[str] = Field(default_factory=list)
    related_tasks: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    commit_ref: str | None = None
    timestamp: datetime | None = None
    # ADR-2026-05-29-12 self-declared. close emits an internal decide msg
    # which is in the mandatory set; Magickit enforces, Conclair persists.
    embodiment: str | None = None
    # ADR-2026-05-27-09 / msg-002 §2: role the closer was acting under,
    # stamped onto the internal decide msg. Conclair persists verbatim;
    # Magickit validates role × allowed_roles before forwarding.
    role: str | None = None
    # Stamped onto the internal decide msg. This route always sets
    # closes_thread, so 'none' is legal here — and this is where it is usually
    # meant. Not defaulted: see the note at the call site in api/threads.py.
    next_participant: str | None = None
    # ADR-2026-06-04-19 D-5: human (Tier-C) force-close of a non-owned thread.
    # See CloseThreadRequest note above — Conclair only honors the flag;
    # Magickit decides (human-only) and supplies the reason for the audit.
    owner_override: bool = False
    owner_override_reason: str | None = None


class CloseThreadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    thread: Thread
    decide_msg: Message
