"""Migration round-trip for 0005_project_control.

Runs against a throw-away database on the same container rather than the
shared test database: `downgrade` drops tables, and doing that to the DB
the rest of the suite is using would leave every later test broken if
this one failed part-way through.

Sync test on purpose — alembic's env.py calls `asyncio.run()` internally,
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

SCRATCH_DB = "conclair_migtest"


async def _exec_autocommit(url: str, statement: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _table_exists(url: str, table: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            found = await conn.scalar(
                text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
            )
        return found is not None
    finally:
        await engine.dispose()


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


def test_0005_upgrade_downgrade_upgrade(scratch_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    command.upgrade(cfg, "head")
    assert asyncio.run(_table_exists(scratch_url, "project_control"))
    assert asyncio.run(_table_exists(scratch_url, "project_control_history"))

    command.downgrade(cfg, "0004")
    assert not asyncio.run(_table_exists(scratch_url, "project_control"))
    assert not asyncio.run(_table_exists(scratch_url, "project_control_history"))
    # The rest of the schema is untouched by this revision.
    assert asyncio.run(_table_exists(scratch_url, "threads"))

    command.upgrade(cfg, "head")
    assert asyncio.run(_table_exists(scratch_url, "project_control"))
    assert asyncio.run(_table_exists(scratch_url, "project_control_history"))
