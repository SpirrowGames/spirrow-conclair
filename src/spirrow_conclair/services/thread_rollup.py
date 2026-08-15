"""Per-thread activity rollup: last msg / msg count / last activity.

These three values are **derived on read** from ``messages``. There is
no denormalised column on ``threads`` and no write path that maintains
one, so a stale rollup is not expressible: the value is recomputed by
the same query that returns the row.

The shape is not new. ``GET /unread`` (``api/read_cursor.py``) has
always produced ``latest_msg_id`` from exactly this ``GROUP BY
thread_id`` aggregate; this module is that subquery, lifted so the
thread listing and the single-thread read can use it too. The cost is
therefore a known one rather than an estimate.

``msg_id`` is allocated **project-wide** (``msg_id_allocator``), so
every aggregate here must be grouped/filtered on
``messages.thread_id``. Numeric arithmetic over the project-wide
sequence would silently count sibling threads' msgs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger, ColumnElement, Subquery, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.models import Message
from spirrow_conclair.services.msg_id_allocator import format_msg_id


def msg_num_expr() -> ColumnElement[int]:
    """Numeric ordering key for ``msg-NNN``.

    Lexicographic order on the string is wrong (``msg-9 > msg-100``);
    everything that orders or maxes msgs goes through this cast.
    """
    return cast(func.substring(Message.msg_id, 5), BigInteger)


def thread_meta_subquery(project: str) -> Subquery:
    """``GROUP BY thread_id`` rollup for one project.

    Columns: ``thread_id`` / ``latest_num`` (max numeric msg id) /
    ``total_count`` / ``last_activity_at`` (max msg timestamp).

    ``last_activity_at`` is the max *timestamp*, which is not required
    to agree with ``latest_num``: ``timestamp`` may be supplied by the
    caller (``OpenThreadRequest.timestamp`` / ``PostMessageRequest``),
    while ``latest_num`` is server-allocated and monotonic. Order by
    ``latest_num`` where "which msg is newest" must be exact -- both
    surfaces that rank threads (``GET /threads``, ``GET /unread``) do,
    and ``last_activity_at`` is carried for display only.
    """
    return (
        select(
            Message.thread_id.label("thread_id"),
            func.max(msg_num_expr()).label("latest_num"),
            func.count().label("total_count"),
            func.max(Message.timestamp).label("last_activity_at"),
        )
        .where(Message.project == project)
        .group_by(Message.thread_id)
        .subquery("thread_meta")
    )


@dataclass(frozen=True)
class ThreadRollup:
    """The three activity fields carried on a ``Thread`` response."""

    last_msg_id: str | None
    msg_count: int
    last_activity_at: datetime | None

    @classmethod
    def from_parts(
        cls,
        latest_num: int | None,
        total_count: int | None,
        last_activity_at: datetime | None,
    ) -> ThreadRollup:
        """Build from raw aggregate output (all-NULL when the thread has no msgs)."""
        return cls(
            last_msg_id=format_msg_id(latest_num) if latest_num is not None else None,
            msg_count=int(total_count or 0),
            last_activity_at=last_activity_at,
        )


EMPTY_ROLLUP = ThreadRollup(last_msg_id=None, msg_count=0, last_activity_at=None)


async def fetch_thread_rollup(
    session: AsyncSession, *, project: str, thread_id: str
) -> ThreadRollup:
    """Rollup for a single thread, in one aggregate round-trip.

    Safe to call inside an open transaction after a msg was added: the
    aggregate is a SELECT, so SQLAlchemy's autoflush (and the explicit
    flush in ``post_message_in_session``) makes the pending msg visible.
    """
    row = (
        await session.execute(
            select(
                func.max(msg_num_expr()),
                func.count(),
                func.max(Message.timestamp),
            ).where(Message.project == project, Message.thread_id == thread_id)
        )
    ).one()
    latest_num, total_count, last_activity_at = row
    return ThreadRollup.from_parts(latest_num, total_count, last_activity_at)
