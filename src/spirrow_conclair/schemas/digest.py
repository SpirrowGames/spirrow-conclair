"""Schemas for the thread digest endpoints.

- ``PUT /v1/projects/{project}/threads/{thread_id}/digest``  (``PutThreadDigestRequest``)
- ``GET /v1/projects/{project}/threads/{thread_id}/digest``  (``ThreadDigestResponse``)
- ``GET /v1/projects/{project}/threads/{thread_id}?include_digest=true``
  (embeds ``ThreadDigestResponse`` on ``ThreadView.digest``)

The word **digest** is used throughout and never "summary". ``mode=summary``
on ``get_thread`` already means something else -- on a resolved thread,
return only the decide msg -- and mindwire's read tools depend on that
meaning. The two are orthogonal and must stay so.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spirrow_conclair.models.digest import DEFAULT_DIGEST_STYLE

#: Mirrors ``models.digest.DIGEST_SCOPES``. A Literal so FastAPI rejects
#: an unknown value with 422 before the route body runs -- the CHECK
#: constraint is the backstop, not the gate.
DigestScope = Literal["thread", "message"]


class ThreadDigest(BaseModel):
    """One stored digest, plus the coverage verdict derived on read.

    ``behind_by`` / ``stale`` are computed server-side because Conclair is
    the only party holding both the digest and the messages in one
    snapshot. A consumer re-deriving them would need a second round-trip,
    and the pair would then describe two different moments.
    """

    model_config = ConfigDict(from_attributes=True)

    scope: DigestScope
    target_msg_id: str | None
    style: str
    digest: str
    source_last_msg_id: str
    #: What the producer said it read. Provenance only -- see
    #: ``models/digest.py``. The freshness verdict is ``behind_by``.
    source_msg_count: int
    #: The producer windowed or shortened the thread before summarizing,
    #: so this digest describes part of what it covers.
    truncated: bool
    model: str | None
    tier: str | None
    producer: str
    generated_at: datetime
    source_chars: int | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None

    #: Messages in this thread newer than ``source_last_msg_id``. A COUNT
    #: over ``messages`` filtered by ``thread_id`` -- never
    #: ``thread.msg_count - source_msg_count``: ``msg_id`` is allocated
    #: project-wide, and ``source_msg_count`` is the producer's claim.
    behind_by: int
    #: Exactly ``behind_by > 0``. Single-sourced on purpose: two
    #: definitions of one word is how they drift.
    stale: bool


class PutThreadDigestRequest(BaseModel):
    """A finished digest, produced elsewhere.

    ``producer`` is a record of who wrote it, not an authenticated
    identity -- the same stance as ``actor`` in loop control. Conclair
    could not verify it without calling out, and calling out is the one
    thing it must not do.

    ``source_last_msg_id`` must come from the *same* read the digest was
    computed from. A producer that re-uses an id from an earlier listing
    marks the digest as covering messages it never saw.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    digest: str = Field(min_length=1, max_length=100_000)
    source_last_msg_id: str = Field(min_length=1, max_length=200)
    source_msg_count: int = Field(ge=1)
    producer: str = Field(min_length=1, max_length=200)

    scope: DigestScope = "thread"
    target_msg_id: str | None = Field(default=None, max_length=200)
    style: str = Field(default=DEFAULT_DIGEST_STYLE, min_length=1, max_length=64)
    truncated: bool = False
    model: str | None = Field(default=None, max_length=200)
    tier: str | None = Field(default=None, max_length=64)
    source_chars: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _scope_and_target_agree(self) -> Self:
        """A whole-thread digest names no message; a per-message one must.

        Enforced here so the caller gets a 422 naming the field, rather
        than the 409 the CHECK constraint would produce. The constraint
        stays as the backstop for anything that reaches the DB another
        way.
        """
        if self.scope == "thread" and self.target_msg_id is not None:
            raise ValueError("target_msg_id must be omitted when scope is 'thread'")
        if self.scope == "message" and not self.target_msg_id:
            raise ValueError("target_msg_id is required when scope is 'message'")
        return self


class ThreadDigestResponse(BaseModel):
    """A thread's digest, or the fact that it has none.

    ``present`` beside a nullable ``digest`` is deliberate redundancy,
    exactly like ``ControlStateResponse.configured`` beside a null
    ``desired_at``: absence is a *normal* answer here, so it is stated
    rather than inferred. Consumers branch on ``present``, not on an
    error, because "not digested yet" must not be readable as an outage.

    ``thread_last_msg_id`` is the head this verdict was made against, so a
    caller can see what ``behind_by`` was measured from without a second
    read.
    """

    project: str
    thread_id: str
    scope: DigestScope
    #: Which digest this response is about: the style actually found when
    #: one was, otherwise the style the caller pinned. ``None`` means the
    #: caller pinned nothing and nothing was stored -- there is no style to
    #: name, and naming the write-side default here would assert a digest
    #: exists under a label that may never have been written.
    style: str | None
    thread_last_msg_id: str | None
    thread_msg_count: int
    present: bool
    digest: ThreadDigest | None
