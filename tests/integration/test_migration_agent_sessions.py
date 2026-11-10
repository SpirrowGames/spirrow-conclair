"""Migration round-trip for 0011_agent_sessions (T45 PR1, DoD).

Same throw-away-database approach as ``test_migration_disposition``, and the
scratch fixture is duplicated for the reason that file gives.

Sync test on purpose: alembic's env.py calls ``asyncio.run()`` internally.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

SCRATCH_DB = "conclair_migtest_agent_sessions"
SOURCE_CHECK = "agent_sessions_source_check"
LOOP_SESSION_ID_CHECK = "agent_sessions_loop_session_id_check"


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


def _table_exists(url: str) -> bool:
    found = asyncio.run(
        _scalar(
            url,
            "SELECT 1 FROM information_schema.tables WHERE table_name = :t",
            {"t": "agent_sessions"},
        )
    )
    return found is not None


def _constraint_exists(url: str, name: str) -> bool:
    found = asyncio.run(_scalar(url, "SELECT 1 FROM pg_constraint WHERE conname = :n", {"n": name}))
    return found is not None


def _index_exists(url: str, name: str) -> bool:
    found = asyncio.run(_scalar(url, "SELECT 1 FROM pg_indexes WHERE indexname = :n", {"n": name}))
    return found is not None


@pytest.fixture
def scratch_url(database_url: str) -> Iterator[str]:
    target = database_url.rsplit("/", 1)[0] + f"/{SCRATCH_DB}"
    asyncio.run(_exec_autocommit(database_url, f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"'))
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
        asyncio.run(_exec_autocommit(database_url, f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"'))


def test_0011_upgrade_downgrade_upgrade(scratch_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    command.upgrade(cfg, "head")
    assert _table_exists(scratch_url)
    assert _constraint_exists(scratch_url, SOURCE_CHECK)
    assert _constraint_exists(scratch_url, LOOP_SESSION_ID_CHECK)
    assert _index_exists(scratch_url, "idx_agent_sessions_identity")
    assert _index_exists(scratch_url, "idx_agent_sessions_project")

    command.downgrade(cfg, "-1")
    assert not _table_exists(scratch_url)
    assert not _constraint_exists(scratch_url, LOOP_SESSION_ID_CHECK)
    assert not _index_exists(scratch_url, "idx_agent_sessions_identity")
    # The previous revision is untouched.
    assert _constraint_exists(scratch_url, "messages_disposition_shape_check")

    command.upgrade(cfg, "head")
    assert _table_exists(scratch_url)
    assert _constraint_exists(scratch_url, LOOP_SESSION_ID_CHECK)


def test_0011_refuses_a_loop_row_without_session_id(scratch_url: str) -> None:
    """msg-1322: a loop row stored with NULL would make NULL "match" NULL."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    command.upgrade(cfg, "head")

    insert = (
        "INSERT INTO agent_sessions (cc_session_uuid, project, source, "
        "mindwire_session_id, started_at, last_declared_at) VALUES "
        "('{u}', 'p', '{s}', {m}, now(), now())"
    )
    with pytest.raises(IntegrityError):
        asyncio.run(_exec_autocommit(scratch_url, insert.format(u="u-1", s="loop", m="NULL")))
    with pytest.raises(IntegrityError):
        asyncio.run(_exec_autocommit(scratch_url, insert.format(u="u-2", s="other", m="'x'")))
    # The two legal shapes go in.
    asyncio.run(_exec_autocommit(scratch_url, insert.format(u="u-3", s="loop", m="'x'")))
    asyncio.run(_exec_autocommit(scratch_url, insert.format(u="u-4", s="hook", m="NULL")))
