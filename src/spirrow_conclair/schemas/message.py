"""Message-related schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spirrow_conclair.schemas.thread import Thread

MessageType = Literal[
    "propose", "question", "answer", "decide", "report", "handoff", "ack"
]


class CloseSanction(BaseModel):
    """Why a non-owner close was allowed through, as structure rather than a bit.

    ``owner_override`` says only that *some* bypass applied. Two distinct ones
    ride it -- a human Tier-C force-close and the PR-gate ledger carve-out --
    and the audit could not tell them apart from a genuinely corrupt row, so a
    project's issue count grew by one per merged PR. See
    ``services.close_sanction``.

    Conclair does not check the claim (D-3: no cross-service lookups, same
    boundary as ``role``). It checks only that the claim is *shaped* so a
    reader can check it later: a ledger carve-out without ``merged_head`` is
    not re-derivable, and ``messages`` / ``chatroom_events`` are append-only,
    so a hollow record cannot be repaired afterwards. A caller that omits the
    evidence gets 422 now instead of an unfalsifiable row forever.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    kind: Literal["human_override", "pr_gate_ledger", "unspecified"]
    #: human_override: the Tier-C reason. Required for that kind.
    reason: str | None = None
    #: pr_gate_ledger evidence, all three required for that kind.
    pr: str | None = None
    merged_head: str | None = None
    approving_review_id: str | None = None

    @model_validator(mode="after")
    def _evidence_matches_kind(self) -> CloseSanction:
        ledger_fields = {
            "pr": self.pr,
            "merged_head": self.merged_head,
            "approving_review_id": self.approving_review_id,
        }
        if self.kind == "human_override":
            if not self.reason:
                raise ValueError("close_sanction kind='human_override' requires 'reason'")
            supplied = [name for name, v in ledger_fields.items() if v]
            if supplied:
                raise ValueError(
                    f"close_sanction kind='human_override' does not carry "
                    f"ledger evidence, got {sorted(supplied)}"
                )
        elif self.kind == "pr_gate_ledger":
            missing = [name for name, v in ledger_fields.items() if not v]
            if missing:
                raise ValueError(
                    f"close_sanction kind='pr_gate_ledger' requires {sorted(missing)}"
                )
            if self.reason:
                raise ValueError(
                    "close_sanction kind='pr_gate_ledger' does not carry 'reason'"
                )
        else:  # unspecified: a claim of no claim, so it carries no evidence
            supplied = [name for name, v in ledger_fields.items() if v]
            if self.reason:
                supplied.append("reason")
            if supplied:
                raise ValueError(
                    f"close_sanction kind='unspecified' carries no evidence, "
                    f"got {sorted(supplied)}"
                )
        return self


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
    # Who acts next. Persisted verbatim; Magickit validates the name. Null on
    # a msg that closes its thread — closing IS "nobody is next" (invariant 7).
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
    # Supplied -> persisted verbatim; no value is reserved and no name is
    # verified here (that needs the identity record, which is Magickit's). The
    # only rule is structural: a msg setting closes_thread must not also name a
    # successor (invariant 7, services.integrity.assert_next_participant_rule).
    next_participant: str | None = None
    # ADR-2026-06-04-19 D-5: when true, skip the owner==author check for a
    # closes_thread decide so a Tier-C human can force-close a non-owned
    # thread. Conclair only honors the flag (no identity logic) — Magickit is
    # the sole decision point and sets it iff the author is a human identity.
    # The decision to relax the gate's review requirement is separate (that
    # stays in Magickit); this flag relaxes ownership only.
    owner_override: bool = False
    owner_override_reason: str | None = None
    # Which bypass `owner_override` stands for, so the audit can tell a
    # sanctioned close from a corrupt one. Optional and additive: a caller
    # that sends only the boolean is recorded as kind='unspecified', which
    # keeps its close out of the issue list without claiming to know why it
    # was allowed. See `services.close_sanction`.
    close_sanction: CloseSanction | None = None


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
    # Accepted only to be refused: this route always sets closes_thread, so
    # invariant 7 leaves NULL as the single legal value. The field exists so a
    # caller who supplies one gets a 409 rather than a silent discard.
    next_participant: str | None = None
    # ADR-2026-06-04-19 D-5: human (Tier-C) force-close of a non-owned thread.
    # See CloseThreadRequest note above — Conclair only honors the flag;
    # Magickit decides (human-only) and supplies the reason for the audit.
    owner_override: bool = False
    owner_override_reason: str | None = None
    # See PostMessageRequest.close_sanction. This is the route the PR-gate
    # ledger carve-out uses, so it is the one that carries `pr_gate_ledger`
    # evidence in practice.
    close_sanction: CloseSanction | None = None


class CloseThreadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    thread: Thread
    decide_msg: Message
