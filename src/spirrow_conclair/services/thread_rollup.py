"""Per-thread activity rollup: last msg / msg count / last activity.

``msg_count`` and ``last_activity_at`` are **derived on read** from
``messages``: nothing sorts on them, so they are only ever needed for
rows a caller is already holding, and a bounded aggregate keeps them
impossible to leave stale.

``last_msg_id`` is different. It is the **sort key** of both triage
surfaces, and a sort key cannot be derived cheaply: ordering by it means
the aggregate must finish before the page's LIMIT can apply. Measured on
the CI runner (``tests/integration/test_thread_listing_scale.py``), with
``GET /threads?limit=100``:

=================================  ==========  =========  =========
scale                              pre-rollup  derived    stored
=================================  ==========  =========  =========
3k msgs / 120 threads                  1.3 ms     2.6 ms     5.5 ms
300k msgs / 5k threads                 2.8 ms    76.4 ms     8.0 ms
300k + a sibling project of 300k       3.3 ms   117.8 ms     7.5 ms
=================================  ==========  =========  =========

(medians of 5, GitHub-hosted runner, postgres:16 in Docker. "derived" is
the abandoned form, still measured on every run so the comparison can be
re-made at a new scale rather than believed from here.)

The third row is the decisive one, and it is about *shape*, not size:
the aggregate's only filter is ``project``, so the plan was a parallel
sequential scan of the **whole** ``messages`` table. One project's
listing therefore got slower as any **other** project grew -- and the
live database holds 15 projects in that table. Storing the key removes
that coupling entirely: 76 -> 118 ms as the table doubles becomes
8.0 -> 7.5 ms, i.e. flat.

**When to revisit**: the page is now an index scan over ``threads``, so
its cost tracks thread count, not msg count, and not the table's other
tenants. If ``GET /threads?limit=100`` is seen above 50 ms again, measure
``fetch_rollups`` -- the per-page aggregate below, whose cost is msgs *in
the 100 threads on the page* -- before touching the ordering.

``msg_id`` is allocated **project-wide** (``msg_id_allocator``), so
every aggregate here must be grouped/filtered on
``messages.thread_id``. Numeric arithmetic over the project-wide
sequence would silently count sibling threads' msgs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger, ColumnElement, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.models import Message
from spirrow_conclair.services.msg_id_allocator import format_msg_id


def msg_num_expr() -> ColumnElement[int]:
    """Numeric ordering key for ``msg-NNN``.

    Lexicographic order on the string is wrong (``msg-9 > msg-100``);
    everything that orders or maxes msgs goes through this cast.
    """
    return cast(func.substring(Message.msg_id, 5), BigInteger)


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


async def fetch_rollups(
    session: AsyncSession, *, project: str, thread_ids: Sequence[str]
) -> dict[str, ThreadRollup]:
    """Rollups for the threads on one page, in one aggregate round-trip.

    Bounded by construction: the caller passes the ids the LIMIT already
    selected (<= 1000, normally 100), so ``idx_messages_thread`` applies and
    the aggregate never touches msgs belonging to threads nobody asked about.
    This is the difference between the page costing what a page costs and the
    page costing what the table costs.

    Threads with no msgs are absent from the result; the caller supplies
    ``EMPTY_ROLLUP`` for them.
    """
    if not thread_ids:
        return {}
    rows = (
        await session.execute(
            select(
                Message.thread_id,
                func.max(msg_num_expr()),
                func.count(),
                func.max(Message.timestamp),
            )
            .where(
                Message.project == project,
                Message.thread_id.in_(list(thread_ids)),
            )
            .group_by(Message.thread_id)
        )
    ).all()
    return {
        row[0]: ThreadRollup.from_parts(row[1], row[2], row[3]) for row in rows
    }


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
