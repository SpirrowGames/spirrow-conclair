"""Thread digest endpoints — store and read an LLM summary made elsewhere.

PUT /v1/projects/{project}/threads/{thread_id}/digest  — upsert (producers)
GET /v1/projects/{project}/threads/{thread_id}/digest  — read + coverage

Its own module for the same reason ``control.py`` is: a different writer,
a different rule about absence, and a docstring that has to say both.

**Conclair does not make digests.** The producer is Magickit (Cognilens ->
Lexora); this endpoint is the store. Conclair must stay a leaf that calls
no other Spirrow service, so ``producer`` / ``model`` / ``tier`` are
*recorded*, not verified -- the same stance as ``actor`` in loop control,
with the tailnet as the trust boundary.

**Why GET 404s here and ``/control`` does not.** The asymmetry is in what
a wrong reading costs. ``/control`` never 404s because a consumer that
read "not configured" as "read failed" would stop every project, so
absence had to be a 200. Here it runs the other way: a producer that
reads "read failed" as "no digest" spends one light-tier LLM call. So the
two cases are separated the way the rest of the API separates them --

* thread does not exist -> **404**, matching ``get_thread`` for the same
  URL prefix. Answering 200 for a thread_id that 404s one path up would
  be its own trap.
* thread exists, no digest -> **200**, ``present: false``, ``digest: null``.
  Absence is a normal answer and is stated, not inferred.

**Why an upsert returns 200 either way.** There is no honest 200/201 split
for "replace whatever is there", and ``PUT /control`` already answers 200
for both create and update.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Path, Query
from sqlalchemy.dialects.postgresql import insert as pg_insert

from spirrow_conclair.db import SessionDep
from spirrow_conclair.models import ThreadDigest
from spirrow_conclair.models.digest import DEFAULT_DIGEST_STYLE
from spirrow_conclair.schemas.digest import (
    DigestScope,
    PutThreadDigestRequest,
    ThreadDigestResponse,
)
from spirrow_conclair.services import integrity as integrity_svc
from spirrow_conclair.services.digest import fetch_digest_response

router = APIRouter(
    prefix="/v1/projects/{project}/threads/{thread_id}/digest", tags=["digest"]
)

ProjectPath = Annotated[str, Path(min_length=1, max_length=200)]
ThreadIdPath = Annotated[str, Path(min_length=1, max_length=200)]


# --- PUT /digest (a producer stores a finished digest) --------------------


@router.put(
    "",
    response_model=ThreadDigestResponse,
    summary="Store (upsert) a thread digest produced elsewhere",
)
async def put_digest(
    project: ProjectPath,
    thread_id: ThreadIdPath,
    body: PutThreadDigestRequest,
    session: SessionDep,
) -> ThreadDigestResponse:
    async with session.begin():
        await integrity_svc.fetch_thread_or_raise(
            session, project=project, thread_id=thread_id
        )

        # The coverage key must name a msg that is actually in this thread.
        # There is no FK (same regime as `actor_read_cursors`), so this is
        # the check -- and it earns its round-trip twice over: `msg_id` is
        # allocated project-wide, so without the thread_id filter a
        # sibling thread's msg would be accepted and the digest's coverage
        # could never be measured; and `format_msg_id` has one canonical
        # form per integer, so an over-padded `msg-0042` is rejected here
        # rather than looking permanently stale later.
        await integrity_svc.assert_msg_in_thread(
            session,
            project=project,
            thread_id=thread_id,
            msg_id=body.source_last_msg_id,
            field="source_last_msg_id",
        )
        if body.scope == "message" and body.target_msg_id is not None:
            await integrity_svc.assert_msg_in_thread(
                session,
                project=project,
                thread_id=thread_id,
                msg_id=body.target_msg_id,
                field="target_msg_id",
            )

        values = {
            "project": project,
            "thread_id": thread_id,
            "scope": body.scope,
            "target_msg_id": body.target_msg_id,
            "style": body.style,
            "digest": body.digest,
            "source_last_msg_id": body.source_last_msg_id,
            "source_msg_count": body.source_msg_count,
            "truncated": body.truncated,
            "model": body.model,
            "tier": body.tier,
            "producer": body.producer,
            "generated_at": datetime.now(timezone.utc),
            "source_chars": body.source_chars,
            "input_tokens": body.input_tokens,
            "output_tokens": body.output_tokens,
            "duration_ms": body.duration_ms,
        }

        # Every non-key column is replaced, provenance included: a re-PUT
        # is a new generation, and leaving half of the old one behind
        # would produce a row describing a digest that never existed.
        #
        # `index_where` is required because uniqueness is two *partial*
        # indexes (target_msg_id is NULL for a whole-thread digest, and
        # Postgres forbids NULL in a PK). Without the predicate there is
        # no index for ON CONFLICT to match.
        replaceable = {
            key: value
            for key, value in values.items()
            if key not in {"project", "thread_id", "scope", "style"}
        }
        if body.scope == "thread":
            conflict_columns = ["project", "thread_id", "style"]
            conflict_where = ThreadDigest.scope == "thread"
        else:
            conflict_columns = ["project", "thread_id", "target_msg_id", "style"]
            conflict_where = ThreadDigest.scope == "message"

        await session.execute(
            pg_insert(ThreadDigest)
            .values(**values)
            .on_conflict_do_update(
                index_elements=conflict_columns,
                index_where=conflict_where,
                set_=replaceable,
            )
        )

        # No `chatroom_events` row. Two reasons, either sufficient.
        #
        # (1) Magickit's ops dashboard reads
        #     `GET /v1/projects/{p}/events?limit=1` as its "直近の動き /
        #     稼働中の根拠" signal. A digest upsert appearing there would
        #     report a project whose loop is dead as running -- the
        #     dashboard would say the thing it exists to detect is fine.
        # (2) `schemas/event.py::EventAction` is a closed Literal, and
        #     `api/events.py` validates it per row on the way out. The DB
        #     column has no CHECK, so an unlisted action inserts happily
        #     and then 500s the entire event log, including that ops read.
        #
        # `generated_at` / `producer` on the digest row are the record.
        # Digest writes are a cache of chatroom activity, not activity.

        # Re-read inside the transaction so the `behind_by` we return is
        # the value as of this write, not as of a later snapshot.
        return await fetch_digest_response(
            session,
            project=project,
            thread_id=thread_id,
            scope=body.scope,
            target_msg_id=body.target_msg_id,
            style=body.style,
        )


# --- GET /digest ----------------------------------------------------------


@router.get(
    "",
    response_model=ThreadDigestResponse,
    summary="A thread's digest and how far behind it is (200 + present=false when none)",
)
async def get_digest(
    project: ProjectPath,
    thread_id: ThreadIdPath,
    session: SessionDep,
    scope: Annotated[DigestScope, Query()] = "thread",
    target_msg_id: Annotated[str | None, Query(max_length=200)] = None,
    style: Annotated[str, Query(min_length=1, max_length=64)] = DEFAULT_DIGEST_STYLE,
) -> ThreadDigestResponse:
    await integrity_svc.fetch_thread_or_raise(
        session, project=project, thread_id=thread_id
    )
    return await fetch_digest_response(
        session,
        project=project,
        thread_id=thread_id,
        scope=scope,
        target_msg_id=target_msg_id,
        style=style,
    )
