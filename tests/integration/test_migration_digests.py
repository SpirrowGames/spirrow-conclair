"""Migration round-trip for 0008_thread_digests.

Same throw-away-database approach as ``test_migration_control`` /
``test_migration_next_participant``: ``downgrade`` drops a table, and doing
that to the DB the rest of the suite is using would break every later test if
this one failed part-way through.

The scratch fixture is duplicated for the third time rather than lifted into
``conftest.py``, honouring the condition ``test_migration_next_participant``
set for lifting it: "when a third consumer appears **and someone can run the
suite locally**". The third consumer is here; the second half is not — this
repository's integration suite still needs Docker, and this revision was
authored on a host without it. Changing the shared fixture from such a host
would be a change nobody can verify before pushing, which is the more
expensive mistake of the two.

Also note: ``test_migration_control`` downgrades to ``"0004"``, so after this
revision lands that test walks back through 0008 and 0007 on the way. A broken
``0008.downgrade()`` therefore fails a test named ``test_0005_...`` — free
coverage under a misleading name.

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

SCRATCH_DB = "conclair_migtest_digests"

THREAD_INDEX = "uq_thread_digests_thread"
MESSAGE_INDEX = "uq_thread_digests_message"


async def _exec_autocommit(url: str, statement: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _fetch_table_exists(url: str, table: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            found = await conn.scalar(
                text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
            )
        return found is not None
    finally:
        await engine.dispose()


async def _fetch_indexdef(url: str, index: str) -> str | None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.scalar(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :i"),
                {"i": index},
            )
    finally:
        await engine.dispose()


def _table_exists(url: str, table: str) -> bool:
    return asyncio.run(_fetch_table_exists(url, table))


def _indexdef(url: str, index: str) -> str | None:
    return asyncio.run(_fetch_indexdef(url, index))


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


def test_0008_upgrade_downgrade_upgrade(scratch_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    command.upgrade(cfg, "head")
    assert _table_exists(scratch_url, "thread_digests")

    command.downgrade(cfg, "0007")
    assert not _table_exists(scratch_url, "thread_digests")
    # The rest of the schema is untouched by this revision.
    assert _table_exists(scratch_url, "threads")
    assert _table_exists(scratch_url, "messages")
    assert _table_exists(scratch_url, "project_control")

    command.upgrade(cfg, "head")
    assert _table_exists(scratch_url, "thread_digests")


def test_0008_unique_indexes_carry_their_predicates(scratch_url: str) -> None:
    """The ``WHERE scope = ...`` half is load-bearing, so assert it.

    Without the predicates the two indexes collide -- a whole-thread digest
    and a per-message digest for the same thread would fight over one key --
    and ``ON CONFLICT ... index_where`` in ``api/digest.py`` finds no matching
    index, so **every** PUT fails at runtime with an error that points nowhere
    near the migration. This assertion is the only cheap place to catch that.
    """
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    command.upgrade(cfg, "head")

    thread_def = _indexdef(scratch_url, THREAD_INDEX)
    assert thread_def is not None
    assert "UNIQUE" in thread_def
    assert "scope = 'thread'::text" in thread_def
    # A whole-thread digest is keyed without target_msg_id (it is NULL).
    assert "target_msg_id" not in thread_def

    message_def = _indexdef(scratch_url, MESSAGE_INDEX)
    assert message_def is not None
    assert "UNIQUE" in message_def
    assert "scope = 'message'::text" in message_def
    assert "target_msg_id" in message_def

    # `style` is in both keys so a producer trying a new prompt cannot
    # overwrite the digest the UI is currently rendering.
    assert "style" in thread_def
    assert "style" in message_def
