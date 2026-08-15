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

from sqlalchemy import BigInteger, ColumnElement, Select, cast, func, select
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


def _rollup_rows(
    *conditions: ColumnElement[bool],
) -> Select[tuple[str, int, int, datetime]]:
    """One row per thread: newest msg's number, the thread's msg count, and
    **that msg's** timestamp.

    The timestamp must come from the row holding the max sequence number,
    not from an independent ``max(timestamp)``. The two agree only while
    the two orders agree, and they are *allowed* to disagree: ``timestamp``
    is a request field, so an import or a backfill inserts the newest msg
    in the sequence carrying a years-old date. Maxing each column on its
    own then reports one msg's id beside another msg's date -- a row that
    is individually plausible and jointly false, which is worse than either
    error alone because nothing about it looks wrong. ``last_activity_at``
    is contracted (``docs/api-design.md`` §2.1) as the timestamp *of*
    ``last_msg_id``, and that is a single msg.

    A window rather than ``GROUP BY`` + a second lookup, so this stays the
    one round-trip its callers document, over the same bounded input: the
    partition is a thread, and the caller has already chosen which threads.
    """
    msg_num = msg_num_expr()
    ranked = (
        select(
            Message.thread_id.label("thread_id"),
            msg_num.label("latest_num"),
            func.count().over(partition_by=Message.thread_id).label("total_count"),
            Message.timestamp.label("last_activity_at"),
            func.row_number()
            .over(partition_by=Message.thread_id, order_by=msg_num.desc())
            .label("rn"),
        )
        .where(*conditions)
        .subquery()
    )
    return select(
        ranked.c.thread_id,
        ranked.c.latest_num,
        ranked.c.total_count,
        ranked.c.last_activity_at,
    ).where(ranked.c.rn == 1)


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
            _rollup_rows(
                Message.project == project,
                Message.thread_id.in_(list(thread_ids)),
            )
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

    A thread with no msgs produces **no row** here (the bare aggregate it
    replaced produced one all-NULL row), so the empty case is spelled out
    rather than falling out of ``from_parts``.
    """
    row = (
        await session.execute(
            _rollup_rows(Message.project == project, Message.thread_id == thread_id)
        )
    ).first()
    if row is None:
        return EMPTY_ROLLUP
    return ThreadRollup.from_parts(row[1], row[2], row[3])
