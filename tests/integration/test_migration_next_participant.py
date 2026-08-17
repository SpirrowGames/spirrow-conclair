"""Migration round-trip for 0007_messages_next_participant.

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

SCRATCH_DB = "conclair_migtest_nextpart"
CHECK_NAME = "messages_next_participant_close_check"


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


def test_0007_upgrade_downgrade_upgrade(scratch_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    command.upgrade(cfg, "head")
    assert _column_exists(scratch_url, "next_participant")
    assert _constraint_exists(scratch_url, CHECK_NAME)

    command.downgrade(cfg, "0006")
    assert not _column_exists(scratch_url, "next_participant")
    # The constraint must go with the column, not linger as an orphan that
    # blocks a re-upgrade with "already exists".
    assert not _constraint_exists(scratch_url, CHECK_NAME)
    # Neighbouring schema is untouched by this revision.
    assert _column_exists(scratch_url, "closes_thread")
    assert _constraint_exists(scratch_url, "messages_type_check")

    command.upgrade(cfg, "head")
    assert _column_exists(scratch_url, "next_participant")
    assert _constraint_exists(scratch_url, CHECK_NAME)


def test_0007_check_is_vacuously_true_for_pre_existing_rows(scratch_url: str) -> None:
    """Rows written before the column existed must survive the upgrade.

    This is the property that let the constraint ship in the same revision as
    the column (``0004`` could not, for ``role``): every legacy row lands on
    NULL, and NULL passes ``IS DISTINCT FROM``. If that were wrong the upgrade
    below would fail while adding the constraint, not later at some write.
    """
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    # Bring the schema to the revision just before this one, then write a row
    # the way callers did back then — no next_participant, no closes_thread.
    command.upgrade(cfg, "0006")
    asyncio.run(
        _exec_autocommit(
            scratch_url,
            "INSERT INTO threads (project, thread_id, title, owner, status, "
            "created_at, created_by_msg, affects_threads, tags) VALUES "
            "('p', 'T-1', 't', 'alice', 'active', now(), 'msg-001', '[]', '[]')",
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

    command.upgrade(cfg, "head")

    stored = asyncio.run(
        _scalar(
            scratch_url,
            "SELECT next_participant FROM messages WHERE msg_id = :m",
            {"m": "msg-001"},
        )
    )
    assert stored is None
