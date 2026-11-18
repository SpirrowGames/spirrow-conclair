"""Migration round-trip for 0011_active_summon.

Same throw-away-database approach as ``test_migration_disposition`` (and the
fixture is duplicated for the reason given there). ``downgrade`` drops a
table, triggers and columns; doing that to the shared database would break
every later test if it failed part-way through.

Sync test on purpose -- alembic's env.py calls ``asyncio.run()`` internally.
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

SCRATCH_DB = "conclair_migtest_summon"
COLUMNS = ("active_summon_thread_id", "active_summon_since", "active_summon_by")
TRIGGERS = ("trg_threads_summon_clear_on_status", "trg_threads_summon_clear_on_delete")
FUNCTIONS = (
    "clear_active_summon",
    "threads_summon_clear_on_status",
    "threads_summon_clear_on_delete",
)


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


def _exists(url: str, statement: str, name: str) -> bool:
    return asyncio.run(_scalar(url, statement, {"n": name})) is not None


def _column(url: str, name: str) -> bool:
    return _exists(
        url,
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'project_control' AND column_name = :n",
        name,
    )


def _table(url: str, name: str) -> bool:
    return _exists(url, "SELECT to_regclass(:n)::text WHERE to_regclass(:n) IS NOT NULL", name)


def _constraint(url: str, name: str) -> bool:
    return _exists(url, "SELECT 1 FROM pg_constraint WHERE conname = :n", name)


def _trigger(url: str, name: str) -> bool:
    return _exists(url, "SELECT 1 FROM pg_trigger WHERE tgname = :n", name)


def _function(url: str, name: str) -> bool:
    return _exists(url, "SELECT 1 FROM pg_proc WHERE proname = :n", name)


def _all_present(url: str) -> bool:
    return (
        all(_column(url, c) for c in COLUMNS)
        and _table(url, "project_summon_history")
        and _constraint(url, "project_control_active_summon_check")
        and all(_trigger(url, t) for t in TRIGGERS)
        and all(_function(url, f) for f in FUNCTIONS)
    )


def _all_absent(url: str) -> bool:
    return (
        not any(_column(url, c) for c in COLUMNS)
        and not _table(url, "project_summon_history")
        and not _constraint(url, "project_control_active_summon_check")
        and not any(_trigger(url, t) for t in TRIGGERS)
        and not any(_function(url, f) for f in FUNCTIONS)
    )


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
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(_exec_autocommit(database_url, f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"'))


def test_0011_upgrade_downgrade_upgrade(scratch_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    command.upgrade(cfg, "head")
    assert _all_present(scratch_url)

    command.downgrade(cfg, "0010")
    assert _all_absent(scratch_url)
    # Neighbouring schema is untouched by this revision.
    assert _table(scratch_url, "project_control")
    assert _table(scratch_url, "project_control_history")
    assert _constraint(scratch_url, "project_control_desired_check")

    command.upgrade(cfg, "head")
    assert _all_present(scratch_url)
