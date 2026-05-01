"""Atomic per-project msg_id allocator.

`msg-NNN` is project-scoped and zero-padded to at least 3 digits. The
underlying `messages` table has `(project, msg_id)` as PK, so collisions
are caught by the database. The allocator's job is to reserve the next
value without races: an advisory lock keyed on `hashtext(project)` keeps
concurrent inserts within the same project serialized for the duration
of the surrounding transaction.

Per System Design v2 §9.1.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from spirrow_conclair.exceptions import ChatroomIntegrityError

_MIN_PAD = 3
_MSG_ID_RE = re.compile(r"^msg-(\d+)$")


def parse_msg_id(msg_id: str) -> int:
    """Return the numeric portion of a msg-NNN id; raises on malformed input."""
    m = _MSG_ID_RE.match(msg_id)
    if not m:
        raise ChatroomIntegrityError(
            f"Malformed msg_id: {msg_id!r} (expected 'msg-NNN')",
            details={"msg_id": msg_id},
        )
    return int(m.group(1))


def format_msg_id(n: int) -> str:
    """Format int as msg-NNN with at least 3-digit zero padding."""
    if n < 1:
        raise ChatroomIntegrityError(
            f"msg_id sequence must be >= 1, got {n}",
            details={"value": n},
        )
    width = max(_MIN_PAD, len(str(n)))
    return f"msg-{n:0{width}d}"


async def allocate_next_msg_id(session: AsyncSession, project: str) -> str:
    """Reserve and return the next msg_id for `project`.

    MUST be called inside an active transaction (e.g. `async with session.begin()`).
    The advisory lock is xact-scoped, so it is released automatically on
    commit/rollback alongside any INSERT made afterwards.
    """
    # pg_advisory_xact_lock(int) — hashtext returns a 32-bit signed int we use
    # as the lock key. Different projects therefore lock independently.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:project))"),
        {"project": project},
    )

    # Order numerically (CAST to int after stripping the 'msg-' prefix) so
    # mixed widths (`msg-009` vs `msg-1000`) compare correctly.
    result = await session.execute(
        text(
            "SELECT msg_id FROM messages "
            "WHERE project = :p "
            "ORDER BY CAST(SUBSTRING(msg_id FROM 5) AS BIGINT) DESC "
            "LIMIT 1"
        ),
        {"p": project},
    )
    last = result.scalar_one_or_none()
    next_n = parse_msg_id(last) + 1 if last else 1
    return format_msg_id(next_n)
