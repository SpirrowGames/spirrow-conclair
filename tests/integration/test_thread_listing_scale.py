"""Scale measurement for the activity-ordered thread listing.

Why this file exists
--------------------
This branch replaced ``ORDER BY threads.created_at`` on ``GET /threads``
with an order on a ``GROUP BY thread_id`` rollup over ``messages``. The
independent review of the change called that "a fatal operational
regression"; the change itself claimed "the cost is therefore a known
one rather than an estimate". **Neither statement had a number behind
it**, so neither could settle the design. This module produces the
number, against a threshold fixed *before* the measurement was run:

    GET /threads?limit=100 at 100x today's data
    (~300k msgs / ~5k threads)

        <= 50 ms  ->  keep the rollup derived on read
        >  50 ms  ->  denormalise the sort key onto ``threads`` + index

The measurement said 85 ms (133 ms with a second project of equal size
sharing the table), so the sort key was denormalised. All three forms are
still measured on every run, because a decision that cannot be re-run at
a new scale is a decision nobody can revisit:

* **pre-rollup** -- what ``GET /threads`` ran before any of this,
  verbatim from ``origin/main``: ``SELECT threads.* ... ORDER BY
  created_at DESC LIMIT :n``, plus the count the route also issues.
  ``threads`` carries **no index on ``created_at``** (``models/thread.py``:
  the indexes are the PK, ``(project, status)``, ``(project, owner)``), so
  even this was a scan plus a sort -- the objection's "efficient O(1)
  index scan" was not what it replaced.
* **derived rank** -- the abandoned form, hand-written here because it is
  no longer reachable in ``src/``.
* **stored rank** -- ``api.threads.listing_query`` plus the page-bounded
  ``fetch_rollups``, i.e. what ships. Imported rather than transcribed, so
  this measurement cannot quietly stop describing production.

Row width: seeded ``content`` is ``_CONTENT_BYTES`` and stays *inline*
in the heap. Real chatroom messages are a mix -- short ones inline, long
ones TOASTed out of the heap entirely -- and the aggregate reads only
``thread_id`` / ``msg_id`` / ``timestamp``, never ``content``. So what
matters is heap bytes per row, which scales linearly with this constant:
a reader who thinks the real corpus is twice as wide can double the scan
component of the numbers below.

Marked ``perf`` and excluded from the default suite (``-m "not perf"``);
CI runs it as its own job. The wall-clock assertions here are
deliberately loose -- a shared CI runner cannot honour a 50 ms bound
reproducibly, and a flaky red gate is one everybody learns to ignore.
The tight number is a *decision input*, reported in the captured output;
the assertion is only a catastrophe guard. The plan-shape assertions, by
contrast, are exact: they are the part that cannot drift.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy import ClauseElement, Executable, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles

from spirrow_conclair.api.threads import listing_query
from spirrow_conclair.models import Thread
from spirrow_conclair.services.thread_rollup import fetch_rollups

pytestmark = pytest.mark.perf

PROJECT = "p-under-test"
SIBLING = "p-sibling"

# Fixed in advance (see module docstring). Reported, not asserted.
DECISION_THRESHOLD_MS = 50.0

# Asserted. Two orders of magnitude above the decision threshold: this
# catches "somebody made the listing quadratic", not "the runner was busy".
CATASTROPHE_CEILING_MS = 5_000.0

TIMED_RUNS = 5
_CONTENT_BYTES = 400

_T0 = time.perf_counter()


def _log(message: str) -> None:
    """Progress with elapsed time, flushed.

    A measurement job that takes an unexplained half hour teaches nothing.
    Every phase announces its own cost so the log localises the cost even
    when the run is killed part-way.
    """
    print(f"[{time.perf_counter() - _T0:7.1f}s] {message}", flush=True)


# --- EXPLAIN as a SQLAlchemy construct ------------------------------------
# Lets us EXPLAIN a Select *object* with its real bound parameters, so the
# plan below belongs to the production statement rather than to a copy.


class Explain(Executable, ClauseElement):
    inherit_cache = False

    def __init__(self, statement: ClauseElement, *, analyze: bool = False) -> None:
        self.statement = statement
        self.analyze = analyze


@compiles(Explain, "postgresql")
def _compile_explain(element: Explain, compiler, **kw) -> str:  # type: ignore[no-untyped-def]
    prefix = "EXPLAIN (ANALYZE, BUFFERS) " if element.analyze else "EXPLAIN "
    return prefix + compiler.process(element.statement, **kw)


async def _explain(session: AsyncSession, statement: ClauseElement) -> str:
    rows = (await session.execute(Explain(statement, analyze=True))).all()
    return "\n".join(str(r[0]) for r in rows)


# --- seeding ---------------------------------------------------------------

_SEED_THREADS = text(
    """
    INSERT INTO threads (project, thread_id, title, owner, status, created_at,
                         created_by_msg, resolved_by_msg, affects_threads, tags)
    SELECT :project,
           'T-' || g,
           'seeded thread ' || g,
           'alice',
           'active',
           TIMESTAMPTZ '2026-01-01 00:00:00+00' + (g * interval '1 minute'),
           'msg-001',
           NULL,
           '[]'::jsonb,
           '[]'::jsonb
    FROM generate_series(CAST(:first AS bigint), CAST(:last AS bigint)) AS g
    """
)

# msg n lands in thread ((n-1) % n_threads) + 1, so the project-wide msg
# sequence interleaves across threads exactly as it does in real use --
# max(msg_num) per thread is then genuinely scattered through the table
# rather than being a contiguous tail.
_SEED_MSGS = text(
    """
    INSERT INTO messages (project, msg_id, thread_id, author, timestamp,
                          commit_ref, type, content, reply_to,
                          references_threads, related_tasks, closes_thread,
                          tags, embodiment, role)
    SELECT :project,
           'msg-' || lpad(n::text, greatest(3, length(n::text)), '0'),
           'T-' || (((n - 1) % CAST(:n_threads AS bigint)) + 1),
           'alice',
           TIMESTAMPTZ '2026-01-01 00:00:00+00' + (n * interval '1 second'),
           NULL,
           'report',
           repeat('x', CAST(:content_bytes AS int)),
           NULL,
           '[]'::jsonb,
           '[]'::jsonb,
           NULL,
           '[]'::jsonb,
           NULL,
           NULL
    FROM generate_series(CAST(:first AS bigint), CAST(:last AS bigint)) AS n
    """
)


@dataclass
class _Seeded:
    """How much of a project is already in the database (seeding is monotonic)."""

    threads: int = 0
    msgs: int = 0


async def _grow_project(
    session: AsyncSession, state: _Seeded, *, project: str, n_threads: int, n_msgs: int
) -> None:
    """Top the project up to (n_threads, n_msgs). Each scale extends the last."""
    if n_threads > state.threads:
        started = time.perf_counter()
        await session.execute(
            _SEED_THREADS,
            {"project": project, "first": state.threads + 1, "last": n_threads},
        )
        await session.commit()
        _log(
            f"{project}: +{n_threads - state.threads} threads "
            f"({time.perf_counter() - started:.1f}s)"
        )
        state.threads = n_threads

    if n_msgs > state.msgs:
        started = time.perf_counter()
        # Bulk-load without the per-row FK trigger on `messages -> threads`.
        # 300k row-level `SELECT ... FOR KEY SHARE` probes dominate the seed, and
        # a constraint's *existence* has no bearing on the SELECT plans being
        # measured -- the indexes, which do, are left in place and maintained.
        # Best-effort: if the role may not set it, the seed is merely slower.
        try:
            await session.execute(text("SET session_replication_role = replica"))
        except Exception:  # noqa: BLE001 - diagnostics only, never a test outcome
            _log("could not disable FK triggers for the seed; loading the slow way")
        await session.execute(
            _SEED_MSGS,
            {
                "project": project,
                "n_threads": state.threads,
                "content_bytes": _CONTENT_BYTES,
                "first": state.msgs + 1,
                "last": n_msgs,
            },
        )
        await session.execute(text("SET session_replication_role = DEFAULT"))
        await session.commit()
        _log(
            f"{project}: +{n_msgs - state.msgs} msgs "
            f"({time.perf_counter() - started:.1f}s)"
        )
        started = time.perf_counter()
        # The seed inserts msgs directly, bypassing the write path that keeps
        # `threads.last_msg_num`, so it sets the key the same way the 0006
        # backfill does. Without this the stored-rank listing would be sorting
        # a column of NULLs -- i.e. measuring nothing.
        await session.execute(
            text(
                """
                UPDATE threads t
                   SET last_msg_num = m.latest_num
                  FROM (
                        SELECT thread_id,
                               max(CAST(SUBSTRING(msg_id FROM 5) AS BIGINT))
                                   AS latest_num
                          FROM messages
                         WHERE project = :project
                         GROUP BY thread_id
                       ) m
                 WHERE t.project = :project
                   AND t.thread_id = m.thread_id
                """
            ),
            {"project": project},
        )
        await session.commit()
        _log(f"{project}: activity keys backfilled ({time.perf_counter() - started:.1f}s)")
        state.msgs = n_msgs

    started = time.perf_counter()
    await session.execute(text("ANALYZE threads"))
    await session.execute(text("ANALYZE messages"))
    await session.commit()
    _log(f"ANALYZE ({time.perf_counter() - started:.1f}s)")


# --- timing ----------------------------------------------------------------


@dataclass(frozen=True)
class Timing:
    label: str
    samples_ms: list[float]

    @property
    def best(self) -> float:
        return min(self.samples_ms)

    @property
    def median(self) -> float:
        return statistics.median(self.samples_ms)

    @property
    def worst(self) -> float:
        return max(self.samples_ms)

    def __str__(self) -> str:
        return (
            f"{self.label:<38} best {self.best:9.1f} ms   "
            f"median {self.median:9.1f} ms   worst {self.worst:9.1f} ms"
        )


async def _time(label: str, call: Callable[[], Awaitable[object]]) -> Timing:
    started = time.perf_counter()
    await call()  # warm-up: first call pays connection + statement compilation
    samples: list[float] = []
    for _ in range(TIMED_RUNS):
        run_started = time.perf_counter()
        await call()
        samples.append((time.perf_counter() - run_started) * 1000.0)
    timing = Timing(label, samples)
    _log(f"timed {label!r} in {time.perf_counter() - started:.1f}s -> {timing}")
    return timing


# The pre-branch statement, verbatim from origin/main's list_threads: a
# plain select over `threads` with the count the route also issues.
_BASELINE_COUNT = text("SELECT count(*) FROM threads WHERE project = :p")
_BASELINE_PAGE = text(
    "SELECT threads.* FROM threads WHERE project = :p "
    "ORDER BY created_at DESC LIMIT :n OFFSET 0"
)


async def _baseline_call(session: AsyncSession, limit: int = 100) -> None:
    await session.execute(_BASELINE_COUNT, {"p": PROJECT})
    (await session.execute(_BASELINE_PAGE, {"p": PROJECT, "n": limit})).all()


async def _listing_call(session: AsyncSession, limit: int = 100) -> None:
    """The route's statements: total count, the page, then the page's rollups."""
    await session.scalar(
        select(func.count()).select_from(Thread).where(Thread.project == PROJECT)
    )
    threads = (
        (
            await session.execute(
                listing_query(
                    PROJECT, [Thread.project == PROJECT], limit=limit, offset=0
                )
            )
        )
        .scalars()
        .all()
    )
    await fetch_rollups(
        session, project=PROJECT, thread_ids=[t.thread_id for t in threads]
    )


# The form this branch tried first and abandoned: rank the page on a
# `GROUP BY thread_id` rollup over `messages`. Kept as a hand-written
# statement -- it is no longer reachable in `src/` -- so the comparison that
# decided the design keeps being re-run at whatever scale is current, instead
# of being a number in a commit message that nobody can reproduce.
_DERIVED_PAGE = text(
    """
    SELECT threads.*, meta.latest_num, meta.total_count, meta.last_activity_at
      FROM threads
      LEFT OUTER JOIN (
            SELECT thread_id,
                   max(CAST(SUBSTRING(msg_id FROM 5) AS BIGINT)) AS latest_num,
                   count(*) AS total_count,
                   max(timestamp) AS last_activity_at
              FROM messages
             WHERE project = :p
             GROUP BY thread_id
           ) meta ON meta.thread_id = threads.thread_id
     WHERE threads.project = :p
     ORDER BY meta.latest_num DESC NULLS LAST,
              threads.created_at DESC,
              threads.thread_id ASC
     LIMIT :n OFFSET 0
    """
)


async def _derived_call(session: AsyncSession, limit: int = 100) -> None:
    await session.execute(_BASELINE_COUNT, {"p": PROJECT})
    (await session.execute(_DERIVED_PAGE, {"p": PROJECT, "n": limit})).all()


# --- the measurements ------------------------------------------------------


@dataclass(frozen=True)
class Scale:
    name: str
    n_threads: int
    n_msgs: int
    sibling_threads: int = 0
    sibling_msgs: int = 0


SCALES = [
    # Today's largest real project (spirrow-voxelworld) is ~2.5k msgs over
    # ~100 threads; msg_id is allocated project-wide, so the highest msg_id
    # is a direct proxy for the project's msg count.
    Scale("today (~3k msgs / 120 threads)", n_threads=120, n_msgs=3_000),
    # The 100x point the threshold was fixed against.
    Scale("100x (300k msgs / 5k threads)", n_threads=5_000, n_msgs=300_000),
    # Same project size, but the table also holds another project's rows --
    # the live database has 15 projects in one `messages` table, so the
    # aggregate's WHERE has real work to exclude.
    Scale(
        "100x + a sibling project of equal size",
        n_threads=5_000,
        n_msgs=300_000,
        sibling_threads=5_000,
        sibling_msgs=300_000,
    ),
]


async def test_listing_latency_against_the_pre_branch_baseline(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Wall clock for the page the triage surface actually serves.

    One test rather than one per scale: seeding is monotonic, so each scale
    extends the previous database instead of tearing it down and rebuilding
    it (`_clean_tables` is per-test).
    """

    async def _http() -> object:
        r = await client.get(f"/v1/projects/{PROJECT}/threads", params={"limit": 100})
        assert r.status_code == 200, r.text
        return r

    async def _http_unread() -> object:
        r = await client.get(
            f"/v1/projects/{PROJECT}/unread",
            params={"identity_name": "alice", "limit": 100},
        )
        assert r.status_code == 200, r.text
        return r

    under_test, sibling = _Seeded(), _Seeded()
    summary: list[str] = []

    for scale in SCALES:
        _log(f"=== scale: {scale.name} ===")
        await _grow_project(
            db_session,
            under_test,
            project=PROJECT,
            n_threads=scale.n_threads,
            n_msgs=scale.n_msgs,
        )
        if scale.sibling_msgs:
            await _grow_project(
                db_session,
                sibling,
                project=SIBLING,
                n_threads=scale.sibling_threads,
                n_msgs=scale.sibling_msgs,
            )

        baseline = await _time(
            "pre-rollup (ORDER BY created_at)", lambda: _baseline_call(db_session)
        )
        derived = await _time(
            "derived rank (abandoned form)", lambda: _derived_call(db_session)
        )
        listing = await _time(
            "stored rank (this branch)", lambda: _listing_call(db_session)
        )
        over_http = await _time("branch via GET /threads?limit=100", _http)
        unread = await _time("GET /unread?limit=100 (for scale)", _http_unread)

        verdict = "MET" if over_http.median <= DECISION_THRESHOLD_MS else "EXCEEDED"
        summary.extend(
            [
                f"=== {scale.name} ===",
                f"  {baseline}",
                f"  {derived}",
                f"  {listing}",
                f"  {over_http}",
                f"  {unread}",
                f"  stored vs pre-rollup: {listing.median - baseline.median:+.1f} ms"
                f"   ({listing.median / baseline.median:.1f}x)"
                f"   |  derived vs pre-rollup: "
                f"{derived.median - baseline.median:+.1f} ms"
                f"   ({derived.median / baseline.median:.1f}x)",
                f"  {DECISION_THRESHOLD_MS:.0f} ms threshold on GET /threads: "
                f"{verdict} (median {over_http.median:.1f} ms)",
            ]
        )

        # Catastrophe guard only -- see the module docstring for why this is
        # not the 50 ms number.
        assert over_http.median < CATASTROPHE_CEILING_MS, (
            f"{scale.name}: GET /threads median {over_http.median:.1f} ms exceeds the "
            f"catastrophe ceiling {CATASTROPHE_CEILING_MS:.0f} ms"
        )

    _log("=== EXPLAIN at the largest scale ===")
    print("--- the abandoned derived-rank page ---", flush=True)
    print(
        "\n".join(
            str(r[0])
            for r in (
                await db_session.execute(
                    text("EXPLAIN (ANALYZE, BUFFERS) " + _DERIVED_PAGE.text),
                    {"p": PROJECT, "n": 100},
                )
            ).all()
        ),
        flush=True,
    )
    print("--- the page as shipped ---", flush=True)
    print(
        await _explain(
            db_session,
            listing_query(PROJECT, [Thread.project == PROJECT], limit=100, offset=0),
        ),
        flush=True,
    )

    print("\n" + "\n".join(summary), flush=True)


async def test_the_listing_page_does_not_touch_messages(
    db_session: AsyncSession,
) -> None:
    """Pin the property the whole redesign bought, in plan form.

    The abandoned form ordered on an outer-joined aggregate, so the LIMIT
    could not stop the scan early and the page's cost tracked the size of
    `messages` -- every project's, since the aggregate's only filter is
    `project`. Both plans are printed; the assertions say that the page
    reads `threads` and nothing else.

    This is the guard against someone re-deriving the rank for good-looking
    reasons: it fails at the plan, not at a stopwatch, so it cannot be
    dismissed as a busy runner.
    """
    await _grow_project(
        db_session, _Seeded(), project=PROJECT, n_threads=120, n_msgs=3_000
    )

    derived_plan = "\n".join(
        str(r[0])
        for r in (
            await db_session.execute(
                text("EXPLAIN (ANALYZE, BUFFERS) " + _DERIVED_PAGE.text),
                {"p": PROJECT, "n": 100},
            )
        ).all()
    )
    page_plan = await _explain(
        db_session,
        listing_query(PROJECT, [Thread.project == PROJECT], limit=100, offset=0),
    )

    print("\n=== EXPLAIN: the abandoned derived-rank page ===\n" + derived_plan, flush=True)
    print("\n=== EXPLAIN: the page as shipped ===\n" + page_plan, flush=True)

    # The abandoned form had to aggregate messages before it could sort.
    assert "messages" in derived_plan
    assert "Aggregate" in derived_plan

    # The shipped page reads one table and stops at the LIMIT.
    assert "messages" not in page_plan
    assert "Aggregate" not in page_plan
    assert "threads" in page_plan
