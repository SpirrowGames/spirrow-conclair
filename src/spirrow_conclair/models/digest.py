"""Thread digest ORM: an LLM summary produced elsewhere and stored here.

Conclair does no LLM work. The producer (Magickit, via Cognilens ->
Lexora) reads a thread, summarizes it, and PUTs the result; this table
keeps it and the API renders it with its age and its coverage attached.
The relationship is the one ``project_control.observed_*`` already has:
an outside actor reports, Conclair records, and the UI says how old the
record is rather than implying it is current.

``producer`` is therefore a **record, not a credential** -- the same
stance as ``actor`` in loop control. The tailnet is the trust boundary,
and Conclair could not verify a producer even if it wanted to, because
verifying would mean calling out to something.

Keys
----
``source_last_msg_id`` is the freshness key, and it works because
``messages`` is append-only with immutable rows: "covers everything up to
msg-N" is a permanent fact about this digest, not a timestamp that may
already have expired. Staleness is then *derived* -- count the msgs after
it -- so nothing has to be kept in sync.

``source_msg_count`` is what the producer said it read. It is provenance,
and it is never the freshness verdict: a producer that windowed or
truncated a long thread reports fewer msgs than the thread holds, and
subtracting it from the thread's count would report that windowing as
staleness. (``msg_id`` is allocated project-wide, so the arithmetic would
also count sibling threads' msgs -- see ``thread_rollup``.)

Uniqueness is two **partial** indexes rather than one composite key,
because ``target_msg_id`` is NULL for a whole-thread digest and Postgres
forbids NULL in a primary key. ``style`` is part of both, so a producer
experimenting with a new prompt cannot overwrite the digest the UI is
currently rendering.

Not an event
------------
A digest write deliberately produces **no** ``chatroom_events`` row.
Two reasons, either sufficient. Magickit's ops dashboard reads
``GET /events?limit=1`` as its "直近の動き / 稼働中の根拠" signal, so a
digest write there would report a dead loop as alive. And
``schemas/event.py::EventAction`` is a closed ``Literal`` validated per
row on the way out, so an unlisted action inserts fine and then 500s the
whole event log. ``generated_at`` / ``producer`` here are the record;
digest writes are a cache of chatroom activity, not activity.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from spirrow_conclair.models import Base

#: What a digest covers. ``thread`` is the whole thread (``target_msg_id``
#: NULL); ``message`` is one msg (``target_msg_id`` set). Both ship in
#: migration 0008 so per-message digests need no schema change.
DIGEST_SCOPES = ("thread", "message")

#: Style label for a digest whose producer did not name one. A label, not
#: a prompt: Conclair does not know what any style means, only that two
#: digests carrying different ones are different digests.
DEFAULT_DIGEST_STYLE = "default"


class ThreadDigest(Base):
    __tablename__ = "thread_digests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    target_msg_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    style: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=DEFAULT_DIGEST_STYLE
    )
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    source_last_msg_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_msg_count: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # `model`, not `model_name`: pydantic v2's default
    # `protected_namespaces=('model_',)` warns on the latter and not the
    # former, which is counter-intuitive enough to pin here. The wire
    # schema uses the same spelling.
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    producer: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    # What the producer actually fed the model, which is not derivable
    # here: it may have windowed or truncated the thread first. `digest`'s
    # own length is `length(digest)` and is deliberately not stored.
    source_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project", "thread_id"],
            ["threads.project", "threads.thread_id"],
            name="thread_digests_thread_fkey",
        ),
        CheckConstraint(
            f"scope IN {DIGEST_SCOPES}",
            name="thread_digests_scope_check",
        ),
        CheckConstraint(
            "(scope = 'thread' AND target_msg_id IS NULL) "
            "OR (scope = 'message' AND target_msg_id IS NOT NULL)",
            name="thread_digests_target_check",
        ),
        CheckConstraint(
            "length(btrim(digest)) > 0",
            name="thread_digests_digest_nonblank",
        ),
        CheckConstraint(
            "source_msg_count >= 1",
            name="thread_digests_source_count_check",
        ),
        Index(
            "uq_thread_digests_thread",
            "project",
            "thread_id",
            "style",
            unique=True,
            postgresql_where=text("scope = 'thread'"),
        ),
        Index(
            "uq_thread_digests_message",
            "project",
            "thread_id",
            "target_msg_id",
            "style",
            unique=True,
            postgresql_where=text("scope = 'message'"),
        ),
    )
