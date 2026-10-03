"""Migration round-trip for 0010_messages_disposition.

Same throw-away-database approach as ``test_migration_control``: ``downgrade``
alters ``messages``, and doing that to the DB the rest of the suite is using
would break every later test if this one failed part-way through.

The scratch fixture is duplicated rather than lifted into ``conftest.py`` on
purpose. This repository's integration suite cannot run on a host without
Docker, so a change to the shared fixture is a change nobody editing from such
a host can verify before pushing; a second copy is the cheaper mistake. Lift it
when a third consumer appears and someone can run the suite locally.

Sync test on purpose — alembic's env.py calls ``asyncio.run()`` internally,
which cannot happen inside pytest-asyncio's running loop.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

SCRATCH_DB = "conclair_migtest_disposition"
SHAPE_CHECK = "messages_disposition_shape_check"
CLOSE_CHECK = "messages_disposition_close_check"


async def _exec_autocommit(url: str, statement: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _scalar(url: str, statement: str, params: dict[str, str]) -> object:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.scalar(text(statement), params)
    finally:
        await engine.dispose()


def _column_exists(url: str, column: str) -> bool:
    found = asyncio.run(
        _scalar(
            url,
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'messages' AND column_name = :c",
            {"c": column},
        )
    )
    return found is not None


def _constraint_exists(url: str, name: str) -> bool:
    found = asyncio.run(
        _scalar(url, "SELECT 1 FROM pg_constraint WHERE conname = :n", {"n": name})
    )
    return found is not None


@pytest.fixture
def scratch_url(database_url: str) -> Iterator[str]:
    target = database_url.rsplit("/", 1)[0] + f"/{SCRATCH_DB}"
    asyncio.run(
        _exec_autocommit(database_url, f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
    )
    asyncio.run(_exec_autocommit(database_url, f'CREATE DATABASE "{SCRATCH_DB}"'))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = target
    try:
        yield target
    finally:
        # env.py reads DATABASE_URL, so restore it before the next test's
        # fixtures run.
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(
            _exec_autocommit(database_url, f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
        )


def test_0010_upgrade_downgrade_upgrade(scratch_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    command.upgrade(cfg, "head")
    assert _column_exists(scratch_url, "disposition")
    assert _constraint_exists(scratch_url, SHAPE_CHECK)
    assert _constraint_exists(scratch_url, CLOSE_CHECK)

    command.downgrade(cfg, "0009")
    assert not _column_exists(scratch_url, "disposition")
    # The constraints go with the column, not linger as orphans that block a
    # re-upgrade with "already exists".
    assert not _constraint_exists(scratch_url, SHAPE_CHECK)
    assert not _constraint_exists(scratch_url, CLOSE_CHECK)
    # Neighbouring schema is untouched by this revision.
    assert _column_exists(scratch_url, "next_participant")
    assert _constraint_exists(scratch_url, "messages_next_participant_close_check")

    command.upgrade(cfg, "head")
    assert _column_exists(scratch_url, "disposition")
    assert _constraint_exists(scratch_url, SHAPE_CHECK)
    assert _constraint_exists(scratch_url, CLOSE_CHECK)


def test_0010_checks_are_vacuously_true_for_pre_existing_rows(scratch_url: str) -> None:
    """Rows written before the column existed survive the upgrade as NULL.

    The row that would expose a wrong close predicate is a **closing** one, so
    that is what this writes; an inverted predicate fails the upgrade itself.
    """
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    command.upgrade(cfg, "0009")
    asyncio.run(
        _exec_autocommit(
            scratch_url,
            "INSERT INTO threads (project, thread_id, title, owner, status, "
            "created_at, created_by_msg, resolved_by_msg, affects_threads, tags) "
            "VALUES ('p', 'T-1', 't', 'alice', 'resolved', now(), 'msg-001', "
            "'msg-002', '[]', '[]')",
        )
    )
    asyncio.run(
        _exec_autocommit(
            scratch_url,
            "INSERT INTO messages (project, msg_id, thread_id, author, timestamp, "
            "type, content, references_threads, related_tasks, tags) VALUES "
            "('p', 'msg-001', 'T-1', 'alice', now(), 'propose', 'legacy', "
            "'[]', '[]', '[]')",
        )
    )
    asyncio.run(
        _exec_autocommit(
            scratch_url,
            "INSERT INTO messages (project, msg_id, thread_id, author, timestamp, "
            "type, content, closes_thread, references_threads, related_tasks, tags) "
            "VALUES ('p', 'msg-002', 'T-1', 'alice', now(), 'decide', 'legacy close', "
            "'T-1', '[]', '[]', '[]')",
        )
    )

    command.upgrade(cfg, "head")

    for msg_id in ("msg-001", "msg-002"):
        stored = asyncio.run(
            _scalar(
                scratch_url,
                "SELECT disposition FROM messages WHERE msg_id = :m",
                {"m": msg_id},
            )
        )
        assert stored is None
