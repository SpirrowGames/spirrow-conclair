"""Reading a thread's digest, with its coverage measured against the log.

One function, used by both ``api/digest.py`` and ``api/threads.py``, so
the standalone endpoint and the embedded ``ThreadView.digest`` cannot
drift into two different answers to the same question.

The question is not "is there a digest" but "how much of this thread does
it cover". ``messages`` is append-only, so a digest that says it covers
up to ``msg-042`` covers up to ``msg-042`` forever, and the shortfall is
just the msgs after it. That count is the verdict, and it is measured
here rather than reported by the producer:

- ``source_msg_count`` is what the producer *said* it read. A producer
  that windowed a long thread reports fewer msgs than the thread holds,
  so subtracting it would read that windowing as staleness.
- ``msg_id`` is allocated **project-wide** (``msg_id_allocator``), so any
  arithmetic over the sequence counts sibling threads' msgs. The count is
  filtered on ``thread_id`` for the same reason ``thread_rollup`` and
  ``api/read_cursor`` are.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.models import Message, ThreadDigest
from spirrow_conclair.models.digest import DEFAULT_DIGEST_STYLE
from spirrow_conclair.schemas.digest import (
    ThreadDigest as ThreadDigestSchema,
)
from spirrow_conclair.schemas.digest import (
    ThreadDigestResponse,
)
from spirrow_conclair.services.msg_id_allocator import parse_msg_id
from spirrow_conclair.services.thread_rollup import (
    ThreadRollup,
    fetch_thread_rollup,
    msg_num_expr,
)


async def _count_behind(
    session: AsyncSession, *, project: str, thread_id: str, source_last_msg_id: str
) -> int:
    """Messages in this thread newer than the digest's coverage point."""
    source_num = parse_msg_id(source_last_msg_id)
    total = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.project == project,
            Message.thread_id == thread_id,
            msg_num_expr() > source_num,
        )
    )
    return int(total or 0)


async def fetch_digest_response(
    session: AsyncSession,
    *,
    project: str,
    thread_id: str,
    scope: str = "thread",
    target_msg_id: str | None = None,
    style: str = DEFAULT_DIGEST_STYLE,
    rollup: ThreadRollup | None = None,
) -> ThreadDigestResponse:
    """A thread's digest and how far behind it is.

    Assumes the thread exists; callers that take a thread_id from the URL
    check that first (``fetch_thread_or_raise``), so this never has to
    decide between "no thread" and "no digest".

    Args:
        session: Open session.
        project: Project scope.
        thread_id: Thread whose digest to read.
        scope: ``thread`` for the whole-thread digest, ``message`` for one msg.
        target_msg_id: The msg, when ``scope`` is ``message``.
        style: Which digest to read; producers may store several.
        rollup: Pass the caller's own rollup when it already has one, so
            ``thread_last_msg_id`` here is the *same* value that caller
            reports. ``get_thread`` does this, which is why embedding the
            digest costs no extra aggregate.

    Returns:
        The response shape, with ``present=False`` and ``digest=None``
        when nothing has been stored yet. Absence is a normal answer.
    """
    resolved_rollup = rollup or await fetch_thread_rollup(
        session, project=project, thread_id=thread_id
    )

    conditions = [
        ThreadDigest.project == project,
        ThreadDigest.thread_id == thread_id,
        ThreadDigest.scope == scope,
        ThreadDigest.style == style,
    ]
    if scope == "message":
        conditions.append(ThreadDigest.target_msg_id == target_msg_id)

    row = await session.scalar(select(ThreadDigest).where(*conditions))

    if row is None:
        return ThreadDigestResponse(
            project=project,
            thread_id=thread_id,
            scope=scope,
            style=style,
            thread_last_msg_id=resolved_rollup.last_msg_id,
            thread_msg_count=resolved_rollup.msg_count,
            present=False,
            digest=None,
        )

    behind_by = await _count_behind(
        session,
        project=project,
        thread_id=thread_id,
        source_last_msg_id=row.source_last_msg_id,
    )

    return ThreadDigestResponse(
        project=project,
        thread_id=thread_id,
        scope=scope,
        style=style,
        thread_last_msg_id=resolved_rollup.last_msg_id,
        thread_msg_count=resolved_rollup.msg_count,
        present=True,
        digest=ThreadDigestSchema(
            scope=row.scope,
            target_msg_id=row.target_msg_id,
            style=row.style,
            digest=row.digest,
            source_last_msg_id=row.source_last_msg_id,
            source_msg_count=row.source_msg_count,
            truncated=row.truncated,
            model=row.model,
            tier=row.tier,
            producer=row.producer,
            generated_at=row.generated_at,
            source_chars=row.source_chars,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            duration_ms=row.duration_ms,
            behind_by=behind_by,
            stale=behind_by > 0,
        ),
    )
